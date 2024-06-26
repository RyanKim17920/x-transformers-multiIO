from x_transformers.x_transformers import *
from x_transformers.x_transformers import AttentionLayers


class MultiIOTransformerWrapper(nn.Module):
    def __init__(
            self,
            *,
            num_tokens: list[int] or int,
            max_seq_len,
            autoregressive=False,
            input_attn_layers: torch.nn.ModuleList = None,
            concat_emb_dim: bool = True,
            attn_layers: AttentionLayers,
            embed_num_tokens: Dict[str, int] = dict(),
            emb_dim: list[int] or int = None,
            max_mem_len=0,
            shift_mem_down=0,
            emb_dropout=0.,
            post_emb_norm=False,
            output_attn_layers: torch.nn.ModuleList = None,
            # num_memory_tokens=None,
            # memory_tokens_interspersed_every=None,
            tie_embedding=False,
            logits_dim: list[int] or int = None,
            use_abs_pos_emb=True,
            scaled_sinu_pos_emb=False,
            l2norm_embed=False,
            emb_frac_gradient=1.,  # GLM-130B and Cogview successfully used this, set at 0.1
            attn_z_loss_weight=1e-4,
    ):
        super().__init__()

        dim = attn_layers.dim
        emb_dim = default(emb_dim, dim)

        if type(emb_dim) == list and type(num_tokens) == list:
            assert len(emb_dim) == len(num_tokens), 'number of embeddings must match number of inputs'

        self.multi_input = ((input_attn_layers is not None) or (type(num_tokens) == list) or (type(emb_dim) == list))
        self.multi_output = (output_attn_layers is not None) or (type(logits_dim) == list) or (
                autoregressive and type(num_tokens) == list)
        self.autoregressive = autoregressive
        self.max_seq_len = max_seq_len
        if not self.multi_input:
            self.model = TransformerWrapper(
                num_tokens=num_tokens,
                logits_dim=logits_dim if not self.multi_output else dim,
                max_seq_len=max_seq_len,
                attn_layers=attn_layers,
                embed_num_tokens=embed_num_tokens,
                emb_dim=emb_dim,
                max_mem_len=max_mem_len,
                shift_mem_down=shift_mem_down,
                emb_dropout=emb_dropout,
                post_emb_norm=post_emb_norm,
                # num_memory_tokens=num_memory_tokens,
                # =memory_tokens_interspersed_every,
                tie_embedding=tie_embedding,
                use_abs_pos_emb=use_abs_pos_emb,
                scaled_sinu_pos_emb=scaled_sinu_pos_emb,
                l2norm_embed=l2norm_embed,
                emb_frac_gradient=emb_frac_gradient,
                attn_z_loss_weight=attn_z_loss_weight,
            )
            self.pre_attn_layers = None
            self.post_attn_layers = None

        else:
            self.emb_dim = emb_dim if (input_attn_layers is None) else [layer.dim for layer in input_attn_layers]
            self.num_tokens = num_tokens

            self.max_mem_len = max_mem_len
            self.shift_mem_down = shift_mem_down

            self.pre_attn_layers = input_attn_layers
            if input_attn_layers is not None:
                assert type(num_tokens) == list, 'num_tokens must be a list of number of tokens for each input'
                assert len(input_attn_layers) == len(
                    num_tokens), 'number of input_attn_layers must match number of inputs'
                if concat_emb_dim:
                    assert sum(self.emb_dim) == dim, 'sum of embedding dimensions must be equal to model dimension'
                    self.pre_attn_layers_map = nn.Linear(sum(self.emb_dim), dim) if sum(self.emb_dim) != dim else \
                        nn.Identity()
                    if sum(self.emb_dim) != dim:
                        print('Note: Since the embedding dimensions of the pre_attention layers are concatenated, '
                              'the dimensions are added. As your model dimension is not equal to the sum of the '
                              'embedding dimensions, a linear layer is added to project the concatenated embedding. '
                              'If this is not desired, please change the model dimensions.')
                else:
                    # assert that all embedding dimensions are equal to the model dimension
                    assert all(dim == d for d in self.emb_dim), 'all embedding dimensions must be equal to the model ' \
                                                                'dimension since having concat_emb_dim means that ' \
                                                                'the model dimension is added to the embedding '
                    self.pre_attn_layers_map = nn.Identity()
            else:
                self.pre_attn_layers_map = nn.Identity()
            self.concat_emb_dim = concat_emb_dim

            self.l2norm_embed = l2norm_embed

            self.token_emb = torch.nn.ModuleList(
                [TokenEmbedding(self.emb_dim[i], num_tokens[i], l2norm_embed=l2norm_embed) for i in
                 range(len(num_tokens))])

            no_abs_pos_emb = max_seq_len == 0 or not (use_abs_pos_emb and not attn_layers.disable_abs_pos_emb)

            if no_abs_pos_emb:
                self.pos_emb = [always(0) for _ in self.emb_dim]
            elif scaled_sinu_pos_emb:
                self.pos_emb = torch.nn.ModuleList([ScaledSinusoidalEmbedding(emb_dim) for emb_dim in self.emb_dim])
            else:
                self.pos_emb = torch.nn.ModuleList(
                    [AbsolutePositionalEmbedding(emb_dim, max_seq_len, l2norm_embed=l2norm_embed) for emb_dim
                     in self.emb_dim])
            # additional embeddings - say type embedding from BERT

            self.embeds = None

            if len(embed_num_tokens) > 0:
                if self.pre_attn_layers is not None:
                    self.embeds = torch.nn.ModuleList(
                        [nn.ModuleDict({f'{name}_embed': nn.Embedding(num_tokens, self.emb_dim[i])
                                        for name, num_tokens in embed_num_tokens.items()}) for i in
                         range(len(self.emb_dim))])
                else:
                    self.embeds = nn.ModuleDict(
                        {f'{name}_embed': nn.Embedding(num_tokens, self.emb_dim) for name, num_tokens in
                         embed_num_tokens.items()})

            # fraction of the gradient that should go to the embedding, https://arxiv.org/abs/2105.13290

            self.emb_frac_gradient = emb_frac_gradient
            if self.multi_input is not None:
                self.post_emb_norm = torch.nn.ModuleList(
                    [LayerNorm(self.emb_dim[i]) if post_emb_norm else nn.Identity() for i in
                     range(len(self.emb_dim))])
                self.emb_dropout = torch.nn.ModuleList([nn.Dropout(emb_dropout) for _ in range(len(self.emb_dim))])
                if self.pre_attn_layers:
                    self.project_emb = torch.nn.ModuleList([
                        nn.Linear(self.emb_dim[i], dim) if self.emb_dim[i] != self.pre_attn_layers[i].dim else nn.Identity()
                        for i in
                        range(len(self.emb_dim))])
                else:
                    self.project_emb = torch.nn.ModuleList([
                        nn.Identity()
                        for i in
                        range(len(self.emb_dim))])
            else:
                self.post_emb_norm = LayerNorm(self.emb_dim) if post_emb_norm else nn.Identity()
                self.emb_dropout = nn.Dropout(emb_dropout)
                self.project_emb = nn.Linear(self.emb_dim, dim) if self.emb_dim != dim else nn.Identity()

            self.attn_layers = attn_layers

            self.init_()

            # memory tokens (like [cls]) from Memory Transformers paper

            # self.can_cache_kv = self.num_memory_tokens == 0
            # self.can_cache_kv_outside_max_seq_len = no_abs_pos_emb
        if self.autoregressive:
            if logits_dim is not None:
                assert logits_dim == num_tokens, 'if autoregressive, logits_dim must be equal to num_tokens'
            if logits_dim is None:
                logits_dim = num_tokens
        if self.multi_output:
            self.post_attn_layers = output_attn_layers
            if self.post_attn_layers is not None:
                self.post_mapping = torch.nn.ModuleList([nn.Linear(dim, self.post_attn_layers[i].dim)
                                                         if dim != self.post_attn_layers[i].dim else nn.Identity() for i
                                                         in
                                                         range(len(self.post_attn_layers))])
                if any(dim != self.post_attn_layers[i].dim for i in range(len(self.post_attn_layers))):
                    print('Note: Since the model dimension is not equal to the output_attn_layers dimension, '
                          'a linear layer is added to project the model dimension to the output_attn_layers dimension. '
                          'If this is not desired, please change the model dimensions.')
                if logits_dim is not None:
                    assert len(output_attn_layers) == len(
                        logits_dim), 'number of output_attn_layers must match number of outputs'
                    if tie_embedding:
                        assert all(self.post_attn_layers[i].dim == self.post_attn_layers[i].dim for i in range(
                            len(self.post_attn_layers))), 'if tie_embedding is True, the dimensions of the input and output attn layers must be equal'
                    self.to_logits = torch.nn.ModuleList(
                        [nn.Linear(dim, d, bias=False) for d in logits_dim]) if not tie_embedding else \
                        lambda t: ([t @ self.token_emb[i].emb.weight.t() for i in range(len(logits_dim))])
                else:
                    self.to_logits = torch.nn.ModuleList(
                        [nn.Linear(self.post_attn_layers[i].dim, self.post_attn_layers[i].dim, bias=False)
                         for i in
                         range(len(self.post_attn_layers))])
            else:
                if logits_dim is not None:
                    if tie_embedding:
                        self.logits = [lambda t: t @ self.token_emb[i].emb.weight.t() if self.multi_input else lambda
                            t: t @ self.model.token_emb.emb.weight.t() for i in range(len(logits_dim))]
                    else:
                        self.to_logits = torch.nn.ModuleList([nn.Linear(dim, d, bias=False) for d in logits_dim])
                else:
                    self.to_logits = nn.Linear(dim, num_tokens, bias=False)
        self.logits_dim = logits_dim

    def init_(self):
        if self.multi_input:
            if self.l2norm_embed:
                for i in range(len(self.token_emb)):
                    nn.init.normal_(self.token_emb[i].emb.weight, std=1e-5)
                    if isinstance(self.pos_emb[i], AbsolutePositionalEmbedding) or isinstance(self.pos_emb[i],
                                                                                              ScaledSinusoidalEmbedding):
                        nn.init.normal_(self.pos_emb[i].emb.weight, std=1e-5)

            for i in range(len(self.token_emb)):
                nn.init.kaiming_normal_(self.token_emb[i].emb.weight)

    def forward(
            self,
            x,
            return_embeddings=False,
            return_logits_and_embeddings=False,
            return_intermediates=False,
            mask=None,
            return_mems=False,
            return_attn=False,
            mems=None,
            mem_masks=None,
            pos=None,
            prepend_embeds=None,
            prepend_mask=None,
            embed_ids: list[Dict[str, Tensor]] or Dict[str, Tensor] = dict(),
            sum_embeds=None,
            return_attn_z_loss=False,
            attn_z_loss_weight=1e-4,
            seq_start_pos=None,
            cache=None,
            **kwargs
    ):

        return_hiddens = return_mems | return_attn | return_intermediates | return_attn_z_loss

        if not self.multi_input and not self.multi_output:
            return self.model(x, return_embeddings, return_logits_and_embeddings, return_intermediates, mask,
                              return_mems, return_attn, mems, mem_masks, pos, prepend_embeds, prepend_mask, embed_ids,
                              sum_embeds, return_attn_z_loss, attn_z_loss_weight, seq_start_pos, cache)
        if cache is not None:
            if self.pre_attn_layers is not None and self.post_attn_layers is not None:
                cache_pre_attn_layers, cache_model, cache_post_attn_layers = cache
            elif self.pre_attn_layers is not None:
                cache_pre_attn_layers, cache_model = cache
                cache_post_attn_layers = None
            elif self.post_attn_layers is not None:
                cache_model, cache_post_attn_layers = cache
                cache_pre_attn_layers = None
            else:
                cache_model = cache
                cache_pre_attn_layers = None
                cache_post_attn_layers = None
        else:
            cache_pre_attn_layers = None
            cache_model = None
            cache_post_attn_layers = None
        mems_pre, mems_model, mems_post = None, None, None
        if mems is not None:
            if self.pre_attn_layers is not None and self.post_attn_layers is not None:
                mems_pre, mems_model, mems_post = mems
            elif self.pre_attn_layers is not None:
                mems_pre, mems_model = mems
                mems_post = None
            elif self.post_attn_layers is not None:
                mems_model, mems_post = mems
                mems_pre = None
            else:
                mems_model = mems
                mems_pre = None
                mems_post = None
        attn_maps_pre = None
        attn_maps = None

        if self.multi_input:
            b, n, device, emb_frac_gradient = x.shape[0], x.shape[1], x.device, self.emb_frac_gradient
            # if self.num_memory_tokens is not None:
            #    num_mems, has_memory_tokens = self.num_memory_tokens, True
            # else:
            #    num_mems, has_memory_tokens = 0, False
            external_pos_emb = exists(pos) and pos.dtype != torch.long
            intermediates_pre = []
            out_x = None
            for i in range(len(self.token_emb)):
                x_i = x[:, :, i]
                pos_emb = self.pos_emb[i](x_i, pos=pos, seq_start_pos=seq_start_pos) if not external_pos_emb else pos
                x_i = self.token_emb[i](x_i) + pos_emb
                if exists(self.embeds):
                    assert len(embed_ids[i]) == len(self.embeds)

                    for name, embed_id in embed_ids[i].items():
                        embed_key = f'{name}_embed'

                        assert embed_key in self.embeds
                        embed = self.embeds[embed_key](embed_id)

                        x_i = x_i + embed
                x_i = self.post_emb_norm[i](x_i)
                if exists(prepend_embeds):
                    prepend_seq, prepend_dim = prepend_embeds[i].shape[1:]
                    assert prepend_dim == x_i.shape[
                        -1], 'prepended embeddings need to have same dimensions as text model dimensions'
                    x_i = torch.cat((prepend_embeds[i], x_i), dim=-2)
                    if exists(prepend_mask) or exists(mask):
                        mask = default(mask, lambda: torch.ones((b, n), device=device, dtype=torch.bool))
                        prepend_mask = default(prepend_mask[i],
                                               lambda: torch.ones((b, prepend_seq), device=device, dtype=torch.bool))
                        mask = torch.cat((prepend_mask, mask), dim=-1)

                if emb_frac_gradient < 1:
                    assert emb_frac_gradient > 0
                    x_i = x_i * emb_frac_gradient + x_i.detach() * (1 - emb_frac_gradient)
                x_i = self.emb_dropout[i](x_i)
                x_i = self.project_emb[i](x_i)
                if self.pre_attn_layers is not None:
                    cur_mem = mems_pre[i] if exists(mems_pre) else None
                    if self.shift_mem_down and exists(cur_mem):
                        mems_l, mems_r = cur_mem[:self.shift_mem_down], cur_mem[self.shift_mem_down:]
                        cur_mem = [*mems_r, *mems_l]
                    x_i, intermediates_pre_attn_layer = self.pre_attn_layers[i](x_i, mask=mask,
                                                                                mems=cur_mem,
                                                                                cache=cache_pre_attn_layers[
                                                                                    i] if cache_pre_attn_layers is not None else None,
                                                                                return_hiddens=True,
                                                                                seq_start_pos=seq_start_pos,
                                                                                **kwargs)
                    intermediates_pre.append(intermediates_pre_attn_layer)
                if out_x is None:
                    out_x = x_i
                else:
                    if self.concat_emb_dim:
                        out_x = torch.cat((out_x, x_i), dim=-1)
                    else:
                        out_x = out_x + x_i
            x = out_x
            x = self.pre_attn_layers_map(x)

            """
            Process pre-attention layer outputs
            """
            if return_attn:
                attn_maps_pre = list(
                    map(lambda t: t.post_softmax_attn, intermediates_pre.attn_intermediates))

            if return_attn_z_loss:
                pre_softmax_attns = list(list(map(lambda t: t.pre_softmax_attn, intermediate.attn_intermediates))
                                         for intermediate in intermediates_pre)
                for i in range(len(intermediates_pre)):
                    intermediates_pre[i].attn_z_loss = calc_z_loss(pre_softmax_attns[i],
                                                                   weight=attn_z_loss_weight)
                return_intermediates = True

            if return_mems:
                mems_pre_out = []
                for i in range(len(intermediates_pre)):
                    hiddens = intermediates_pre[i].hiddens
                    new_mems = list(map(lambda pair: torch.cat(pair, dim=-2), zip(mems_pre, hiddens))) if exists(
                        mems_pre) else hiddens
                    new_mems = list(map(lambda t: t[..., -self.max_mem_len:, :].detach(), new_mems))

                    if not return_intermediates:
                        mems_pre_out.append(new_mems)

                    intermediates_pre[i].mems = new_mems

            """
            Running the main attention layers of the model
            """
            if self.shift_mem_down and exists(mems_model):
                mems_l, mems_r = mems_model[:self.shift_mem_down], mems_model[self.shift_mem_down:]
                mems_model = [*mems_r, *mems_l]
            x, intermediates_model = self.attn_layers(x, mask=mask, mems=mems_model, mem_masks=mem_masks,
                                                      cache=cache_model,
                                                      return_hiddens=True, seq_start_pos=seq_start_pos, **kwargs)
        else:
            if return_hiddens:
                x, intermediates_model = self.model(x, return_embeddings, return_logits_and_embeddings,
                                                    return_intermediates, mask,
                                                    return_mems, return_attn, mems_model, mem_masks, pos,
                                                    prepend_embeds,
                                                    prepend_mask, embed_ids,
                                                    sum_embeds, return_attn_z_loss, attn_z_loss_weight, seq_start_pos,
                                                    cache)
            else:
                x = self.model(x, False, False, False, mask,
                               False, False, mems_model, mem_masks, pos, prepend_embeds, prepend_mask, embed_ids,
                               sum_embeds, False, attn_z_loss_weight, seq_start_pos, cache)

        """
        Output processing for middle (model) layers
        """
        if return_attn:
            attn_maps = list(
                map(lambda t: t.post_softmax_attn, intermediates_model.attn_intermediates))

        if return_attn_z_loss:
            pre_softmax_attns = list(list(map(lambda t: t.pre_softmax_attn, intermediate.attn_intermediates))
                                     for intermediate in intermediates_model)

            intermediates_model.attn_z_loss = calc_z_loss(pre_softmax_attns[i],
                                                          weight=attn_z_loss_weight)
            return_intermediates = True

        if return_mems:
            hiddens = intermediates_model.hiddens
            new_mems = list(map(lambda pair: torch.cat(pair, dim=-2), zip(mems_model, hiddens))) if exists(
                mems_model) else hiddens
            new_mems = list(map(lambda t: t[..., -self.max_mem_len:, :].detach(), new_mems))

            if not return_intermediates:
                mems_model = new_mems
            intermediates_model.mems = new_mems

        """
        Output processing
        """

        if self.multi_output:
            if self.post_attn_layers is not None:
                outputs = []
                intermediates_post = []
                x_values = []
                for i, layer in enumerate(self.post_attn_layers):
                    post_x = self.post_mapping[i](x)
                    mems_cur = mems_post[i] if exists(mems_post) else None
                    if self.shift_mem_down and exists(mems_cur):
                        mems_l, mems_r = mems_cur[:self.shift_mem_down], mems_cur[self.shift_mem_down:]
                        mems_cur = [*mems_r, *mems_l]
                    if return_hiddens:
                        post_x, inter = layer(post_x, mask=mask,
                                              mems=mems_cur,
                                              mem_masks=mem_masks,
                                              cache=cache_post_attn_layers[
                                                  i] if cache_post_attn_layers is not None else None,
                                              return_hiddens=True, seq_start_pos=seq_start_pos, **kwargs)
                        intermediates_post.append(inter)
                        x_values.append(post_x)
                    else:
                        x_values.append(
                            layer(post_x, mask=mask, mems=mems_cur,
                                  cache=cache_post_attn_layers[i] if cache_post_attn_layers is not None else None,
                                  return_hiddens=False, seq_start_pos=seq_start_pos, **kwargs))
                    outputs.append(self.to_logits[i](x_values[i]))
                if return_logits_and_embeddings:
                    out = (outputs, x_values)
                elif return_embeddings:
                    out = x_values
                else:
                    out = outputs
                """
                Outputs Processing for multi-output attention layers
                """
                if return_attn_z_loss:
                    pre_softmax_attns = list(list(map(lambda t: t.pre_softmax_attn, intermediate.attn_intermediates))
                                             for intermediate in intermediates_post)
                    for i in range(len(intermediates_post)):
                        intermediates_post[i].attn_z_loss = calc_z_loss(pre_softmax_attns[i], weight=attn_z_loss_weight)
                    return_intermediates = True

                if return_mems:
                    for i in range(len(intermediates_post)):
                        hiddens = intermediates_post[i].hiddens
                        new_mems = list(map(lambda pair: torch.cat(pair, dim=-2), zip(mems_post, hiddens))) if exists(
                            mems_post) else hiddens
                        new_mems = list(map(lambda t: t[..., -self.max_mem_len:, :].detach(), new_mems))

                        if not return_intermediates:
                            if self.pre_attn_layers is not None:
                                return out, (mems_pre_out, mems_model, new_mems)
                            else:
                                return out, (mems_model, new_mems)

                        intermediates_post[i].mems = new_mems

                if return_intermediates:
                    if self.pre_attn_layers is not None:
                        return out, (intermediates_pre, intermediates_model, intermediates_post)
                    else:
                        return out, (intermediates_model, intermediates_post)

                if return_attn:
                    attn_maps_post = list(list(map(lambda t: t.post_softmax_attn, intermediate.attn_intermediates))
                                          for intermediate in intermediates_post)
                    if attn_maps_pre is not None:
                        return out, (attn_maps_pre, attn_maps, attn_maps_post)
                    else:
                        return out, (attn_maps, attn_maps_post)

                return out
            else:
                """
                Output processing for multi-output no attention layers
                """
                x_values = []
                for i in self.to_logits:
                    x_values.append(i(x))

                if return_mems:
                    if self.pre_attn_layers is not None:
                        return x_values, (mems_pre, mems_model)
                    else:
                        return x_values, mems_model

                if return_logits_and_embeddings:
                    out = (x_values, x)
                elif return_embeddings:
                    out = x
                else:
                    out = x_values
                if return_intermediates:
                    if self.pre_attn_layers is not None:
                        return out, (intermediates_pre, intermediates_model)
                    else:
                        return out, intermediates_model
                return out
        else:
            if return_logits_and_embeddings:
                if type(self.to_logits) == list:
                    out = (list(self.to_logits[i](x) for i in range(len(self.to_logits))), x)
                else:
                    out = (self.to_logits(x), x)
            elif return_embeddings:
                out = x
            else:
                out = list(self.to_logits[i](x) for i in range(len(self.to_logits)))

            if return_attn_z_loss:
                pre_softmax_attns = list(map(lambda t: t.pre_softmax_attn, intermediates_model.attn_intermediates))
                intermediates_model.attn_z_loss = calc_z_loss(pre_softmax_attns, weight=attn_z_loss_weight)
                return_intermediates = True

            if return_mems:
                hiddens = intermediates_model.hiddens
                new_mems = list(map(lambda pair: torch.cat(pair, dim=-2), zip(mems, hiddens))) if exists(
                    mems) else hiddens
                new_mems = list(map(lambda t: t[..., -self.max_mem_len:, :].detach(), new_mems))

                if not return_intermediates:
                    return out, new_mems

                intermediates_model.mems = new_mems

            if return_intermediates:
                if self.pre_attn_layers is not None:
                    return out, (intermediates_pre, intermediates_model)
                else:
                    return out, intermediates_model

            if return_attn:
                return out, attn_maps

            return out
