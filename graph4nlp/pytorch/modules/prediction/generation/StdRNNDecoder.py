import random
from functools import reduce

import torch
import torch.nn as nn

from graph4nlp.pytorch.modules.prediction.generation.attention import Attention
from graph4nlp.pytorch.modules.prediction.generation.base import RNNDecoderBase
from graph4nlp.pytorch.data.data import GraphData, from_batch


class StdRNNDecoder(RNNDecoderBase):
    """
                The standard rnn for sequence decoder.
            Parameters
            ----------
            max_decoder_step: int
                The maximal decoding step.
            decoder_input_size: int
                The dimension for standard rnn decoder's input.
            decoder_hidden_size: int
                The dimension for standard rnn decoder's hidden representation during calculation.
            word_emb: torch.nn.Embedding
                The target's embedding matrix.
            vocab: Any
                The target's vocabulary
            rnn_type: str, option=["LSTM", "GRU"], default="LSTM"
                The rnn's type. We support ``LSTM`` and ``GRU`` here.
            use_attention: bool, default=True
                Whether use attention during decoding.
            attention_type: str, option=["uniform", "sep_diff_encoder_type", sep_diff_node_type], default="uniform"
                The attention strategy choice.
                "``uniform``": uniform attention. We will attend on the nodes uniformly.
                "``sep_diff_encoder_type``": separate attention.
                    We will attend on graph encoder and rnn encoder's results separately.
                "``sep_diff_node_type``": separate attention.
                    We will attend on different node type separately.

            attention_function: str, option=["general", "mlp"], default="mlp"
                Different attention function.
            node_type_num: int, default=None
                When we choose "``sep_diff_node_type``", we must set this parameter.
                This parameter indicate the the amount of node type.
            fuse_strategy: str, option=["average", "concatenate"], default=average
                The strategy to fuse attention results generated by separate attention.
                "``average``": We will take an average on all results.
                "``concatenate``": We will concatenate all results to one.
            use_copy: bool, default=False
                Whether use ``copy`` mechanism. See pointer network. Note that you must use attention first.
            use_coverage: bool, default=False
                Whether use ``coverage`` mechanism. Note that you must use attention first.
            coverage_strategy: str, option=["sum", "max"], default="sum"
                The coverage strategy when calculating the coverage vector.
            tgt_emb_as_output_layer: bool, default=False
                When this option is set ``True``, the output projection layer(It is used to project RNN encoded
                representation to target sequence)'s weight will be shared with the target vocabulary's embedding.
            dropout: float, default=0.3
            """

    def __init__(self, max_decoder_step, decoder_input_size, decoder_hidden_size,  # decoder config
                 word_emb, vocab,  # word embedding & vocabulary TODO: add our vocabulary when building pipeline
                 rnn_type="LSTM", graph_pooling_strategy=None, # RNN config
                 use_attention=True, attention_type="uniform", rnn_emb_input_size=None,  # attention config
                 attention_function="mlp", node_type_num=None, fuse_strategy="average",
                 use_copy=False, use_coverage=False, coverage_strategy="sum",
                 tgt_emb_as_output_layer=False,  # share label projection with word embedding
                 dropout=0.3):
        super(StdRNNDecoder, self).__init__(use_attention=use_attention, use_copy=use_copy, use_coverage=use_coverage,
                                            attention_type=attention_type, fuse_strategy=fuse_strategy)
        self.max_decoder_step = max_decoder_step
        self.word_emb_size = word_emb.embedding_dim
        self.decoder_input_size = decoder_input_size
        self.graph_pooling_strategy = graph_pooling_strategy
        self.dropout = nn.Dropout(p=dropout)
        self.tgt_emb = word_emb
        self.rnn = self._build_rnn(rnn_type=rnn_type, input_size=self.word_emb_size, hidden_size=decoder_hidden_size)
        self.decoder_hidden_size = decoder_hidden_size
        self.rnn_type = rnn_type
        self.num_layers = 1
        self.out_logits_size = self.decoder_hidden_size

        # attention builder
        if self.use_attention:
            if self.rnn_type == "LSTM":
                query_size = 2 * self.decoder_hidden_size
            elif self.rnn_type == "GRU":
                query_size = self.decoder_hidden_size
            else:
                raise NotImplementedError()
            if attention_type == "uniform":
                self.enc_attention = Attention(hidden_size=self.decoder_hidden_size,
                                               query_size=query_size,
                                               memory_size=decoder_input_size, has_bias=True,
                                               attention_funtion=attention_function)
                self.out_logits_size += self.decoder_input_size
            elif attention_type == "sep_diff_encoder_type":
                assert isinstance(rnn_emb_input_size, int)
                self.rnn_emb_input_size = rnn_emb_input_size
                self.enc_attention = Attention(hidden_size=self.decoder_hidden_size,
                                               query_size=query_size,
                                               memory_size=decoder_input_size, has_bias=True,
                                               attention_funtion=attention_function)
                self.out_logits_size += self.decoder_input_size
                self.rnn_attention = Attention(hidden_size=self.decoder_hidden_size, query_size=query_size,
                                               memory_size=rnn_emb_input_size, has_bias=True,
                                               attention_funtion=attention_function)
                if self.fuse_strategy == "concatenate":
                    self.out_logits_size += self.rnn_emb_input_size
                else:
                    if rnn_emb_input_size != decoder_input_size:
                        raise ValueError("input RNN embedding size is not equal to graph embedding size")
            elif attention_type == "sep_diff_node_type":
                assert node_type_num >= 1
                attn_modules = [Attention(hidden_size=self.decoder_hidden_size, query_size=query_size,
                                          memory_size=decoder_input_size, has_bias=True,
                                          attention_funtion=attention_function)
                                for _ in range(node_type_num)]
                self.node_type_num = node_type_num
                self.attn_modules = nn.ModuleList(attn_modules)
                if self.fuse_strategy == "concatenate":
                    self.out_logits_size += self.decoder_input_size * self.node_type_num
                elif self.fuse_strategy == "average":
                    self.out_logits_size += self.decoder_input_size
                else:
                    raise NotImplementedError()
            else:
                raise NotImplementedError()

        self.attention_type = attention_type

        if self.rnn_type == "LSTM":
            self.encoder_decoder_adapter = nn.ModuleList(
                [nn.Linear(self.decoder_input_size, self.decoder_hidden_size) for _ in range(2)])
        elif self.rnn_type == "GRU":
            self.encoder_decoder_adapter = nn.Linear(self.decoder_input_size, self.decoder_hidden_size)
        else:
            raise NotImplementedError()

        # project logits to labels
        self.tgt_emb_as_output_layer = tgt_emb_as_output_layer
        if self.tgt_emb_as_output_layer:  # use pre_out layer
            self.out_embed_size = self.decoder_hidden_size
            self.pre_out = nn.Linear(self.out_logits_size, self.out_embed_size, bias=False)
            size_before_output = self.out_embed_size
        else:  # don't use pre_out layer
            size_before_output = self.out_logits_size
        self.vocab = vocab
        vocab_size = len(vocab)
        self.vocab_size = vocab_size
        self.out_project = nn.Linear(size_before_output, vocab_size, bias=False)
        if self.tgt_emb_as_output_layer:
            self.out_project.weight = self.tgt_emb.weight

        # coverage strategy
        if self.use_coverage:
            if not self.use_attention:
                raise ValueError("You should use attention when you use coverage strategy.")

            self.coverage_strategy = coverage_strategy

            def get_coverage_vector(enc_attn_weights):
                if coverage_strategy == 'max':
                    coverage_vector, _ = torch.max(torch.cat(enc_attn_weights), dim=0)
                elif coverage_strategy == 'sum':
                    coverage_vector = torch.sum(torch.cat(enc_attn_weights), dim=0)
                else:
                    raise ValueError('Unrecognized cover_func: ' + self.cover_func)
                return coverage_vector

            self.coverage_function = get_coverage_vector

            self.coverage_weight = torch.Tensor(1, 1, self.decoder_hidden_size)
            self.coverage_weight = nn.Parameter(nn.init.xavier_uniform_(self.coverage_weight))

        # copy: pointer network
        if self.use_copy:
            ptr_size = self.word_emb_size
            if self.rnn_type == "LSTM":
                ptr_size += self.decoder_hidden_size * 2
            elif self.rnn_type == "GRU":
                ptr_size += self.decoder_hidden_size
            else:
                raise NotImplementedError()
            if self.use_attention:
                if self.attention_type == "uniform":
                    ptr_size += self.decoder_hidden_size
                elif self.attention_type == "sep_diff_encoder_type":
                    ptr_size += self.decoder_hidden_size * 2
                elif self.attention_type == "sep_diff_node_type":
                    ptr_size += self.decoder_hidden_size * node_type_num
            self.ptr = nn.Linear(ptr_size, 1)

    def _build_rnn(self, rnn_type, **kwargs):
        """
            The rnn factory.
        Parameters
        ----------
        rnn_type: str, option=["LSTM", "GRU"], default="LSTM"
            The rnn type.
        """
        if rnn_type == "LSTM" or rnn_type == "GRU":
            return getattr(nn, rnn_type)(**kwargs)
        else:
            # TODO: add more rnn
            raise NotImplementedError()

    def _run_forward_pass(self, graph_node_embedding, graph_node_mask=None, rnn_node_embedding=None,
                          graph_level_embedding=None,
                          graph_edge_embedding=None, graph_edge_mask=None, tgt_seq=None, src_seq=None,
                          teacher_forcing_rate=1.0):
        """
            The forward function for RNN.
        Parameters
        ----------
        graph_node_embedding: torch.Tensor
            shape=[B, N, D]
        graph_node_mask: torch.Tensor
            shape=[B, N]
            -1 indicating dummy node. 0-``node_type_num`` are valid node type.
        rnn_node_embedding: torch.Tensor
            shape=[B, N, D]
        graph_level_embedding: torch.Tensor
            shape=[B, D]
        graph_edge_embedding: torch.Tensor
            shape=[B, E, D]
            Not implemented yet.
        graph_edge_mask: torch.Tensor
            shape=[B, E]
            Not implemented yet.
        tgt_seq: torch.Tensor
            shape=[B, T]
            The target sequence's index.
        src_seq: torch.Tensor
            shape=[B, S]
            The source sequence's index. It is used for ``use_copy``. Note that it can be encoded by target word
            embedding.
        teacher_forcing_rate: float, default=1.0
            The teacher forcing rate.

        Returns
        -------
        logits: torch.Tensor
            shape=[B, tgt_len, vocab_size]
            The probability for predicted target sequence. It is processed by softmax function.
        enc_attn_weights_average: torch.Tensor
            It is used for calculating coverage loss.
            The averaged attention scores.
        coverage_vectors: torch.Tensor
            It is used for calculating coverage loss.
            The coverage vector.
        """
        target_len = self.max_decoder_step
        if tgt_seq is not None:
            target_len = min(tgt_seq.shape[1], target_len)

        batch_size = graph_node_embedding.shape[0]
        decoder_input = torch.tensor([self.vocab.SOS] * batch_size).to(graph_node_embedding.device)
        decoder_state = self._get_decoder_init_state(rnn_type=self.rnn_type, batch_size=batch_size, content=graph_level_embedding)

        outputs = []
        enc_attn_weights_average = []
        coverage_vectors = []
        embed = []
        dec_hidden = []
        attn_results_all = []

        for i in range(target_len):
            dec_emb = self.tgt_emb(decoder_input)
            dec_emb = self.dropout(dec_emb)
            embed.append(dec_emb.unsqueeze(1))
            if self.use_coverage and enc_attn_weights_average:
                coverage_vec = self.coverage_function(enc_attn_weights_average)
            else:
                coverage_vec = None

            decoder_output, decoder_state, dec_attn_results, score_results = \
                self._decode_step(dec_input_emb=dec_emb, rnn_state=decoder_state, dec_input_mask=graph_node_mask,
                                  encoder_out=graph_node_embedding, rnn_emb=rnn_node_embedding,
                                  coverage_vec=coverage_vec)
            if self.rnn_type == "LSTM":
                hidden = torch.cat(decoder_state, -1).squeeze(0)
            elif self.rnn_type == "GRU":
                hidden = decoder_state.squeeze(0)
            else:
                raise NotImplementedError()
            if self.use_attention:
                if self.attention_type == "uniform":
                    assert len(dec_attn_results) == 1
                    assert len(score_results) == 1
                    attn_total = dec_attn_results[0]
                elif self.attention_type == "sep_diff_encoder_type" or self.attention_type == "sep_diff_node_type":
                    if self.fuse_strategy == "average":
                        attn_total = reduce(lambda x, y: x + y, dec_attn_results) / len(dec_attn_results)
                    elif self.fuse_strategy == "concatenate":
                        attn_total = torch.cat(dec_attn_results, dim=-1)
                    else:
                        raise NotImplementedError()
                else:
                    raise NotImplementedError()

                decoder_output = torch.cat((decoder_output, attn_total), dim=-1)
                dec_attn_scores = reduce(lambda x, y: x + y, score_results) / len(score_results)

                enc_attn_weights_average.append(dec_attn_scores.unsqueeze(0))
                coverage_vectors.append(coverage_vec)
                attn_results_all.append(torch.cat(dec_attn_results, dim=-1).unsqueeze(1))
            else:
                decoder_output = decoder_output

            dec_hidden.append(hidden.unsqueeze(1))

            # project
            if self.tgt_emb_as_output_layer:
                out_embed = torch.tanh(self.pre_out(decoder_output))
            else:
                out_embed = decoder_output
            out_embed = self.dropout(out_embed)
            decoder_output = self.out_project(out_embed)

            if self.use_copy:
                assert src_seq is not None
                attn_ptr = torch.cat(dec_attn_results, dim=-1)
                pgen_collect = [dec_emb, hidden, attn_ptr]

                prob_ptr = torch.sigmoid(self.ptr(torch.cat(pgen_collect, -1)))
                prob_gen = 1 - prob_ptr
                gen_output = torch.softmax(decoder_output, dim=-1)

                ret = prob_gen * gen_output

                ptr_output = dec_attn_scores
                ret.scatter_add_(1, src_seq, prob_ptr * ptr_output)
                decoder_output = ret
            else:
                decoder_output = torch.softmax(decoder_output, dim=-1)
            outputs.append(decoder_output.unsqueeze(1))

            # teacher_forcing
            if tgt_seq is not None and random.random() < teacher_forcing_rate:
                decoder_input = tgt_seq[:, i]
            else:
                # sampling
                # TODO: now argmax sampling
                decoder_input = decoder_output.squeeze(1).argmax(dim=-1)
            # decoder_input = self._filter_oov(decoder_input)
        ret = torch.cat(outputs, dim=1)
        return ret, enc_attn_weights_average, coverage_vectors

    def _decode_step(self, dec_input_emb, rnn_emb, dec_input_mask, rnn_state, encoder_out, coverage_vec=None):
        dec_out, rnn_state = self.rnn(dec_input_emb.unsqueeze(0), rnn_state)
        dec_out = dec_out.squeeze(0)

        if self.rnn_type == "LSTM":
            rnn_state = tuple([self.dropout(x) for x in rnn_state])
            hidden = torch.cat(rnn_state, -1).squeeze(0)
        elif self.rnn_type == "GRU":
            rnn_state = self.dropout(rnn_state)
            hidden = rnn_state.squeeze(0)
        else:
            raise NotImplementedError()
        attn_collect = []
        score_collect = []

        if self.use_attention:
            if self.use_coverage and coverage_vec is not None:
                coverage_repr = coverage_vec
            else:
                coverage_repr = None
            if coverage_repr is not None:
                coverage_repr = coverage_repr.unsqueeze(-1) * self.coverage_weight
            if self.attention_type == "uniform" or self.attention_type == "sep_diff_encoder_type":
                # enc_mask = self.extract_mask(dec_input_mask, token=0)
                enc_mask = None
                attn_res, scores = self.enc_attention(query=hidden, memory=encoder_out, memory_mask=enc_mask,
                                                      coverage=coverage_repr)
                attn_collect.append(attn_res)
                score_collect.append(scores)
                if self.attention_type == "sep_diff_encoder_type":
                    rnn_attn_res, rnn_scores = self.rnn_attention(query=hidden, memory=rnn_emb, coverage=coverage_repr)
                    score_collect.append(rnn_scores)
                    attn_collect.append(rnn_attn_res)
            elif self.attention_type == "sep_diff_node_type":
                for i in range(self.node_type_num):
                    node_mask = self.extract_mask(dec_input_mask, token=i)
                    attn, scores = self.attn_modules[i](query=hidden, memory=encoder_out, memory_mask=node_mask,
                                                        coverage=coverage_repr)
                    attn_collect.append(attn)
                    score_collect.append(scores)

        return dec_out, rnn_state, attn_collect, score_collect

    def _get_decoder_init_state(self, rnn_type, batch_size, content=None):
        if rnn_type == "LSTM":
            if content is not None:
                assert len(content.shape) == 2
                assert content.shape[0] == batch_size
                ret = tuple([self.encoder_decoder_adapter[i](content).view(1, batch_size,
                                                                           self.decoder_hidden_size).expand(
                    self.num_layers, -1, -1) for i in range(2)])
            else:
                weight = next(self.parameters()).data
                ret = (weight.new(self.num_layers, batch_size, self.decoder_hidden_size).zero_(),
                       weight.new(self.num_layers, batch_size, self.decoder_hidden_size).zero_())
        elif rnn_type == "GRU":
            if content is not None:
                ret = self.encoder_decoder_adapter(content).view(1, batch_size,
                                                                           self.decoder_hidden_size).expand(
                    self.num_layers, -1, -1)
            else:
                weight = next(self.parameters()).data
                ret = weight.new(self.num_layers, batch_size, self.decoder_hidden_size).zero_()
        else:
            raise NotImplementedError()
        return ret

    def extract_mask(self, mask, token):
        mask_ret = torch.zeros(*(mask.shape)).to(mask.device)
        mask_ret.fill_(0)
        mask_ret[mask == token] = 1
        return mask_ret

    def _filter_oov(self, tokens):
        ret = tokens.clone()
        ret[tokens >= self.vocab_size] = self.vocab.UNK
        return ret

    def forward(self, g, tgt_seq=None, src_seq=None, teacher_forcing_rate=1.0):
        params = self._extract_params(g)
        params['tgt_seq'] = tgt_seq
        params['src_seq'] = src_seq
        params['teacher_forcing_rate'] = teacher_forcing_rate
        return self._run_forward_pass(**params)

    def _extract_params(self, graph_list):
        """

        Parameters
        ----------
        g: GraphData

        Returns
        -------
        params: dict
        """
        graph_node_emb = [s_g.node_features["node_emb"] for s_g in graph_list]
        rnn_node_emb = [s_g.node_features["rnn_emb"] for s_g in graph_list]

        graph_edge_emb = None

        def pad_tensor(x, dim, pad_size):
            if len(x.shape) == 2:
                assert (0 <= dim <= 1)
                assert pad_size >= 0
                dim1, dim2 = x.shape
                pad = torch.zeros(pad_size, dim2) if dim == 0 else torch.zeros(dim1, pad_size)
                pad = pad.to(x.device)
                return torch.cat((x, pad), dim=dim)

        batch_size = len(graph_list)
        max_node_num = max([emb.shape[0] for emb in graph_node_emb])

        graph_node_emb_ret = []
        for emb in graph_node_emb:
            if emb.shape[0] < max_node_num:
                emb = pad_tensor(emb, 0, max_node_num-emb.shape[0])
            graph_node_emb_ret.append(emb.unsqueeze(0))
        graph_node_emb_ret = torch.cat(graph_node_emb_ret, dim=0)

        graph_level_emb = self.graph_pooling(graph_node_emb_ret)

        graph_node_mask = torch.zeros(batch_size, max_node_num).fill_(-1)

        for i, s_g in enumerate(graph_list):
            node_num = s_g.get_node_num()
            for j in range(node_num):
                node_type = s_g.node_attributes[j].get('type')
                if node_type is not None:
                    graph_node_mask[i][j] = node_type
        graph_node_mask_ret = graph_node_mask.to(graph_node_emb_ret.device)

        rnn_node_emb_ret = None
        if self.attention_type == "sep_diff_encoder_type":
            max_rnn_num = max([rnn_emb.shape[0] for rnn_emb in rnn_node_emb])
            rnn_node_emb_ret = []
            assert max_rnn_num == max_node_num
            for rnn_emb in rnn_node_emb:
                if rnn_emb.shape[0] < max_rnn_num:
                    rnn_emb = pad_tensor(rnn_emb, 0, max_rnn_num - rnn_emb.shape[0])
                rnn_node_emb_ret.append(rnn_emb.unsqueeze(0))
            rnn_node_emb_ret = torch.cat(rnn_node_emb_ret, dim=0)

        return {
             "graph_node_embedding": graph_node_emb_ret,
             "graph_node_mask": graph_node_mask_ret,
             "rnn_node_embedding": rnn_node_emb_ret,
             "graph_level_embedding": graph_level_emb,
             "graph_edge_embedding": None,
             "graph_edge_mask": None
        }

    def graph_pooling(self, graph_node):
        if self.graph_pooling_strategy is None:
            pooled_vec = None
        elif self.graph_pooling_strategy == "mean":
            pooled_vec = torch.mean(graph_node, dim=1)
        elif self.graph_pooling_strategy == "max":
            pooled_vec, _ = torch.max(graph_node, dim=1)
        elif self.graph_pooling_strategy == "min":
            pooled_vec, _ = torch.mean(graph_node, dim=1)
        else:
            raise NotImplementedError()
        return pooled_vec
