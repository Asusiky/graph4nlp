import os
import random
import time
import pickle
import argparse
import copy

import dgl
import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.nn.init as init
import torch.optim as optim
from torch.utils.data import DataLoader

from stanfordcorenlp import StanfordCoreNLP

from graph4nlp.pytorch.data.data import GraphData, from_batch

from graph4nlp.pytorch.datasets.jobs import JobsDatasetForTree
from graph4nlp.pytorch.datasets.geo import GeoDatasetForTree

from graph4nlp.pytorch.modules.evaluation.base import EvaluationMetricBase
from graph4nlp.pytorch.modules.graph_construction.dependency_graph_construction import DependencyBasedGraphConstruction
from graph4nlp.pytorch.modules.graph_construction.constituency_graph_construction import ConstituencyBasedGraphConstruction
from graph4nlp.pytorch.modules.graph_construction.node_embedding_based_graph_construction import NodeEmbeddingBasedGraphConstruction
from graph4nlp.pytorch.modules.graph_construction.node_embedding_based_refined_graph_construction import NodeEmbeddingBasedRefinedGraphConstruction

from graph4nlp.pytorch.modules.graph_embedding.gat import GAT
from graph4nlp.pytorch.modules.graph_embedding.ggnn import GGNN
from graph4nlp.pytorch.modules.graph_embedding.graphsage import GraphSAGE
from graph4nlp.pytorch.modules.graph_embedding.gcn import GCN

from graph4nlp.pytorch.modules.prediction.generation.TreeBasedDecoder import \
    StdTreeDecoder

from graph4nlp.pytorch.modules.utils.tree_utils import to_cuda

from graph4nlp.pytorch.modules.prediction.generation.TreeBasedDecoder import StdTreeDecoder, create_mask
from graph4nlp.pytorch.modules.utils.tree_utils import DataLoaderForGraphEncoder, Tree, Vocab, to_cuda

import warnings

warnings.filterwarnings('ignore')


class Graph2Tree(nn.Module):
    def __init__(self, src_vocab,
                 tgt_vocab,
                 use_copy,
                 enc_hidden_size,
                 dec_hidden_size,
                 dropout_for_decoder,
                 dropout_for_word_embedding,
                 enc_dropout_for_feature,
                 enc_dropout_for_attn,
                 direction_option,
                 input_size,
                 output_size,
                 device,
                 teacher_force_ratio,
                 max_dec_seq_length,
                 max_dec_tree_depth,
                 graph_construction_type,
                 gnn_type,
                 rnn_type):
        super(Graph2Tree, self).__init__()

        self.src_vocab = src_vocab
        self.tgt_vocab = tgt_vocab
        self.device = device
        self.use_copy = use_copy
        self.use_edge_weight = False

        embedding_style = {'single_token_item': True,
                           'emb_strategy': "w2v_bilstm",
                           'num_rnn_layers': 1
                           }

        if graph_construction_type == "DependencyGraph":
            self.graph_topology = DependencyBasedGraphConstruction(embedding_style=embedding_style,
                                                                   vocab=self.src_vocab,
                                                                   hidden_size=enc_hidden_size, 
                                                                   word_dropout=dropout_for_word_embedding, 
                                                                   rnn_dropout=dropout_for_word_embedding, 
                                                                   fix_word_emb=False)
        elif graph_construction_type == "ConstituencyGraph":
            self.graph_topology = ConstituencyBasedGraphConstruction(embedding_style=embedding_style,
                                                                     vocab=self.src_vocab,
                                                                     hidden_size=enc_hidden_size,
                                                                     word_dropout=dropout_for_word_embedding,
                                                                     rnn_dropout=dropout_for_word_embedding,
                                                                     fix_word_emb=False)
        elif graph_construction_type == "DynamicGraph_node_emb":
            self.graph_topology = NodeEmbeddingBasedGraphConstruction(self.src_vocab,
                                                                      embedding_style,
                                                                      sim_metric_type='weighted_cosine',
                                                                      num_heads=1,
                                                                      top_k_neigh=None,
                                                                      epsilon_neigh=0.5,
                                                                      smoothness_ratio=0.1,
                                                                      connectivity_ratio=0.05,
                                                                      sparsity_ratio=0.1,
                                                                      input_size=enc_hidden_size,
                                                                      hidden_size=enc_hidden_size,
                                                                      fix_word_emb=False,
                                                                      word_dropout=dropout_for_word_embedding,
                                                                      rnn_dropout=dropout_for_word_embedding
                                                                      )
            self.use_edge_weight = True
        elif graph_construction_type == "DynamicGraph_node_emb_refined":
            self.graph_topology = NodeEmbeddingBasedRefinedGraphConstruction(self.src_vocab,
                                                                             embedding_style,
                                                                             0.2,
                                                                             sim_metric_type="weighted_cosine",
                                                                             num_heads=1,
                                                                             top_k_neigh=None,
                                                                             epsilon_neigh=0.5,
                                                                             smoothness_ratio=0.1,
                                                                             connectivity_ratio=0.05,
                                                                             sparsity_ratio=0.1,
                                                                             input_size=enc_hidden_size,
                                                                             hidden_size=enc_hidden_size,
                                                                             fix_word_emb=False,
                                                                             word_dropout=dropout_for_word_embedding,
                                                                             rnn_dropout=dropout_for_word_embedding,
                                                                             device=device)
            self.use_edge_weight = True
        else:
            raise NotImplementedError()

        self.word_emb = self.graph_topology.embedding_layer.word_emb_layers[
            'w2v'].word_emb_layer

        if gnn_type == "GAT":
            self.encoder = GAT(1, enc_hidden_size, enc_hidden_size, enc_hidden_size, [1],
                               direction_option=direction_option, feat_drop=enc_dropout_for_feature,
                               attn_drop=enc_dropout_for_attn, activation=F.relu, residual=True)
        elif gnn_type == "GGNN":
            self.encoder = GGNN(1, enc_hidden_size, enc_hidden_size, enc_hidden_size,
                                feat_drop=enc_dropout_for_feature, use_edge_weight=self.use_edge_weight,
                                direction_option=direction_option)
        elif gnn_type == "SAGE":
            # aggregate type: 'mean','gcn','pool','lstm'
            self.encoder = GraphSAGE(1, enc_hidden_size, enc_hidden_size, enc_hidden_size,
                                     'lstm', direction_option=direction_option, feat_drop=enc_dropout_for_feature,
                                     activation=F.relu, bias=True, use_edge_weight=self.use_edge_weight)
        elif gnn_type == "GCN":
            self.encoder = GCN(1,
                               enc_hidden_size,
                               enc_hidden_size,
                               enc_hidden_size,
                               direction_option=direction_option,
                               gcn_norm="both",
                               activation=F.relu,
                               use_edge_weight=self.use_edge_weight)
        else:
            print("Wrong gnn type, please use GAT GGNN or SAGE")
            raise NotImplementedError()
        self.criterion = nn.NLLLoss(size_average=False)

        self.decoder = StdTreeDecoder(attn_type="uniform",
                                      embeddings=self.word_emb,
                                      enc_hidden_size=enc_hidden_size,
                                      dec_emb_size=self.tgt_vocab.embedding_dims,
                                      dec_hidden_size=dec_hidden_size,
                                      output_size=output_size,
                                      device=device,
                                      criterion=self.criterion,
                                      teacher_force_ratio=teacher_force_ratio,
                                      use_sibling=False,
                                      use_attention=True,
                                      use_copy=self.use_copy,
                                      use_coverage=True,
                                      fuse_strategy="average",
                                      num_layers=1,
                                      dropout_for_decoder=dropout_for_decoder,
                                      rnn_type=rnn_type,
                                      max_dec_seq_length=max_dec_seq_length,
                                      max_dec_tree_depth=max_dec_tree_depth,
                                      tgt_vocab=self.tgt_vocab)

    def forward(self, batch_graph, tgt_tree_batch, oov_dict=None):
        # batch_graph.batch_node_features["token_id"] = batch_graph.batch_node_features["token_id"].to(self.device)
        batch_graph = self.graph_topology(batch_graph)
        batch_graph = self.encoder(batch_graph)
        batch_graph.node_features["rnn_emb"] = batch_graph.node_features['node_feat']

        loss = self.decoder(g=batch_graph, tgt_tree_batch=tgt_tree_batch, oov_dict=oov_dict)
        return loss

    def init(self, init_weight):
        to_cuda(self.encoder, self.device)
        to_cuda(self.decoder, self.device)

        print('--------------------------------------------------------------')
        for name, param in self.named_parameters():
            # print(name, param.size())
            if param.requires_grad:
                if ("word_embedding" in name) or ("word_emb_layer" in name) or ("bert_embedding" in name):
                    pass
                else:
                    if len(param.size()) >= 2:
                        if "rnn" in name:
                            init.orthogonal_(param)
                        else:
                            init.xavier_uniform_(param, gain=1.0)
                    else:
                        init.uniform_(param, -init_weight, init_weight)
        # print('--------------------------------------------------------------')


class Jobs:
    def __init__(self, opt=None):
        super(Jobs, self).__init__()
        self.opt = opt

        seed = opt.seed
        random.seed(seed)
        np.random.seed(seed)
        torch.manual_seed(seed)
        torch.cuda.manual_seed(seed)
        torch.backends.cudnn.deterministic = True

        if opt.gpuid == -1:
            self.device = torch.device("cpu")
        else:
            self.device = torch.device("cuda:{}".format(opt.gpuid))
        self.use_copy = True if opt.use_copy == 1 else False
        self.revectorization = True
        self.data_dir = opt.data_dir
        self.checkpoint_dir = opt.checkpoint_dir

        self._build_dataloader()
        self._build_model()
        self._build_optimizer()

    def _build_dataloader(self):
        use_copy = self.use_copy
        use_share_vocab = True

        if self.opt.graph_construction_type == "DependencyGraph":
            dataset = JobsDatasetForTree(root_dir=self.data_dir,
                                         topology_builder=DependencyBasedGraphConstruction,
                                         topology_subdir='DependencyGraph', edge_strategy='as_node',
                                         share_vocab=use_share_vocab, enc_emb_size=self.opt.enc_emb_size,
                                         dec_emb_size=self.opt.tgt_emb_size,
                                         min_word_vocab_freq=self.opt.min_freq)

        elif self.opt.graph_construction_type == "ConstituencyGraph":
            dataset = JobsDatasetForTree(root_dir=self.data_dir,
                                         topology_builder=ConstituencyBasedGraphConstruction,
                                         topology_subdir='ConstituencyGraph', 
                                         share_vocab=use_share_vocab,
                                         enc_emb_size=self.opt.enc_emb_size, 
                                         dec_emb_size=self.opt.tgt_emb_size,
                                         min_word_vocab_freq=self.opt.min_freq)

        elif self.opt.graph_construction_type == "DynamicGraph_node_emb":
            dataset = JobsDatasetForTree(root_dir=self.data_dir, 
                                         seed=self.opt.seed, 
                                         word_emb_size=self.opt.enc_emb_size,
                                         topology_builder=NodeEmbeddingBasedGraphConstruction,
                                         topology_subdir='DynamicGraph_node_emb', 
                                         graph_type='dynamic',
                                         dynamic_graph_type='node_emb', 
                                         share_vocab=use_share_vocab,
                                         enc_emb_size=self.opt.enc_emb_size, 
                                         dec_emb_size=self.opt.tgt_emb_size,
                                         min_word_vocab_freq=self.opt.min_freq)

        elif self.opt.graph_construction_type == "DynamicGraph_node_emb_refined":
            if self.opt.dynamic_init_graph_type is None or self.opt.dynamic_init_graph_type == 'line':
                dynamic_init_topology_builder = None
            elif self.opt.dynamic_init_graph_type == 'dependency':
                dynamic_init_topology_builder = DependencyBasedGraphConstruction
            elif self.opt.dynamic_init_graph_type == 'constituency':
                dynamic_init_topology_builder = ConstituencyBasedGraphConstruction
            else:
                # dynamic_init_topology_builder
                raise RuntimeError(
                    'Define your own dynamic_init_topology_builder')
            dataset = JobsDatasetForTree(root_dir=self.data_dir, seed=self.opt.seed, word_emb_size=self.opt.enc_emb_size,
                                         topology_builder=NodeEmbeddingBasedRefinedGraphConstruction,
                                         topology_subdir='DynamicGraph_node_emb_refined', graph_type='dynamic',
                                         dynamic_graph_type='node_emb_refined', share_vocab=use_share_vocab,
                                         enc_emb_size=self.opt.enc_emb_size, dec_emb_size=self.opt.tgt_emb_size,
                                         dynamic_init_topology_builder=dynamic_init_topology_builder,
                                         min_word_vocab_freq=self.opt.min_freq)
        else:
            raise NotImplementedError

        # self.train_data_loader = DataLoaderForGraphEncoder(
        #     use_copy=use_copy, use_share_vocab=use_share_vocab, data=dataset.train, dataset=dataset, mode="train", batch_size=self.opt.batch_size, device=self.device)
        # print("train sample size:", len(self.train_data_loader.data))
        # self.test_data_loader = DataLoaderForGraphEncoder(
        #     use_copy=use_copy, use_share_vocab=use_share_vocab, data=dataset.test, dataset=dataset, mode="test", batch_size=1, device=self.device)

        self.train_data_loader = DataLoader(dataset.train, batch_size=self.opt.batch_size, shuffle=True, num_workers=1,
                                           collate_fn=dataset.collate_fn)
        self.test_data_loader = DataLoader(dataset.test, batch_size=1, shuffle=False, num_workers=1,
                                          collate_fn=dataset.collate_fn)
        print("test sample size:", len(dataset.test))

        self.src_vocab = dataset.src_vocab_model
        self.tgt_vocab = dataset.tgt_vocab_model
        # print(self.src_vocab.symbol2idx)
        # print(self.tgt_vocab.symbol2idx)
        if use_share_vocab:
            self.share_vocab = dataset.share_vocab_model
        print(self.share_vocab.symbol2idx)
        print("---Loading data done---\n")

    def _build_model(self):
        '''For encoder-decoder'''
        self.model = Graph2Tree(src_vocab=self.src_vocab,
                                tgt_vocab=self.tgt_vocab,
                                use_copy=self.use_copy,
                                enc_hidden_size=self.opt.enc_hidden_size,
                                dec_hidden_size=self.opt.dec_hidden_size,
                                dropout_for_decoder=self.opt.dropout_for_decoder,
                                dropout_for_word_embedding=self.opt.dropout_for_word_embedding,
                                enc_dropout_for_feature=self.opt.dropout_for_encoder,
                                enc_dropout_for_attn=self.opt.dropout_for_encoder,
                                direction_option=self.opt.direction_option,
                                input_size=self.src_vocab.vocab_size,
                                output_size=self.tgt_vocab.vocab_size,
                                device=self.device,
                                teacher_force_ratio=self.opt.teacher_force_ratio,
                                max_dec_seq_length=self.opt.max_dec_seq_length,
                                max_dec_tree_depth=self.opt.max_dec_tree_depth_for_train,
                                graph_construction_type=self.opt.graph_construction_type,
                                gnn_type=self.opt.gnn_type,
                                rnn_type=self.opt.rnn_type)
        self.model.init(self.opt.init_weight)
        self.model = to_cuda(self.model, self.device)
        print(self.model)

    def _build_optimizer(self):
        optim_state = {"learningRate": self.opt.learning_rate,
                       "weight_decay": self.opt.weight_decay}
        parameters = [p for p in self.model.parameters() if p.requires_grad]
        self.optimizer = optim.Adam(
            parameters, lr=optim_state['learningRate'], weight_decay=optim_state['weight_decay'])
        # self.scheduler = torch.optim.lr_scheduler.StepLR(self.optimizer, step_size = self.opt.max_epochs//3, gamma=0.5)

    def prepare_ext_vocab(self, batch_graph, src_vocab):
        oov_dict = copy.deepcopy(src_vocab)
        token_matrix = []
        for n in batch_graph.node_attributes:
            node_token = n['token']
            if (n.get('type') == None or n.get('type') == 0) and oov_dict.get_symbol_idx(node_token) == oov_dict.get_symbol_idx(oov_dict.unk_token):
                oov_dict.add_symbol(node_token)
                # print("add", node_token)
            token_matrix.append(oov_dict.get_symbol_idx(node_token))
        batch_graph.node_features['token_id_oov'] = torch.tensor(token_matrix, dtype=torch.long).to(self.device)
        return oov_dict

    def train_epoch(self, epoch):
        from graph4nlp.pytorch.modules.utils.copy_utils import prepare_ext_vocab
        loss_to_print = 0
        # for i in range(self.train_data_loader.num_batch):
        num_batch = len(self.train_data_loader)
        for step, data in enumerate(self.train_data_loader):
            batch_graph_list, batch_tree_list, batch_original_tree_list = data['graph_data'], data['dec_tree_batch'], data['original_dec_tree_batch']
            batch_graph_list = batch_graph_list.to(self.device)
            self.optimizer.zero_grad()
            # print(batch_graph_list.node_attributes)
            # print(batch_tree_list[0])
            # print(batch_original_tree_list[0])
            # batch_graph_list, _, batch_tree_list, batch_original_tree_list = self.train_data_loader.random_batch()
            # print(self.train_data_loader.num_batch) 25
            # print(batch_graph_list.device)
            oov_dict = self.prepare_ext_vocab(
                batch_graph_list, self.src_vocab) if self.use_copy else None

            if self.use_copy and self.revectorization:
                batch_tree_list_refined = []
                for item in batch_original_tree_list:
                    tgt_list = oov_dict.get_symbol_idx_for_list(item.strip().split())
                    tgt_tree = Tree.convert_to_tree(tgt_list, 0, len(tgt_list), oov_dict)
                    batch_tree_list_refined.append(tgt_tree)
            # for index in range(len(batch_tree_list_refined)):
            #     print("---------------------------------------")
            #     print(batch_tree_list[index])
            #     print(batch_tree_list_refined[index])
            # loss = self.model(batch_graph_list, batch_tree_list, oov_dict=oov_dict)
            loss = self.model(batch_graph_list, batch_tree_list_refined if self.use_copy else batch_tree_list, oov_dict=oov_dict)
            loss.backward()
            torch.nn.utils.clip_grad_value_(
                self.model.parameters(), self.opt.grad_clip)
            self.optimizer.step()
            loss_to_print += loss
        return loss_to_print/num_batch

    def train(self):
        best_acc = -1

        print("-------------\nStarting training.")
        for epoch in range(1, self.opt.max_epochs+1):
            self.model.train()
            loss_to_print = self.train_epoch(epoch)
            # self.scheduler.step()
            print("epochs = {}, train_loss = {:.3f}".format(epoch, loss_to_print))
            # print(self.scheduler.get_lr())
            if epoch > 2 and epoch % 5 == 0:
                # torch.save(checkpoint, "{}/g2t".format(self.checkpoint_dir) + str(i))
                # pickle.dump(checkpoint, open("{}/g2t".format(self.checkpoint_dir) + str(i), "wb"))
                test_acc = self.eval((self.model))
                if test_acc > best_acc:
                    best_acc = test_acc
        print("Best Acc: {:.3f}\n".format(best_acc))
        return best_acc

    def eval(self, model):
        from graph4nlp.pytorch.data.data import to_batch
        device = model.device

        max_dec_seq_length = self.opt.max_dec_seq_length
        max_dec_tree_depth = self.opt.max_dec_tree_depth_for_test
        
        use_copy = self.use_copy
        enc_emb_size = model.src_vocab.embedding_dims
        tgt_emb_size = model.tgt_vocab.embedding_dims

        enc_hidden_size = model.decoder.enc_hidden_size
        dec_hidden_size = model.decoder.hidden_size

        model.eval()

        reference_list = []
        candidate_list = []

        data_loader = self.test_data_loader
        for data in data_loader:
            input_graph_list, batch_tree_list, batch_original_tree_list = data['graph_data'], data['dec_tree_batch'], data['original_dec_tree_batch']
            input_graph_list = input_graph_list.to(device)
        # for i in range(len(data)):
        #     x = data[i]

        #     # get input graph list
        #     input_graph_list = to_batch([x[0]])
            # if use_copy:
            oov_dict = self.prepare_ext_vocab(
                input_graph_list, self.src_vocab)

            # get indexed tgt sequence
            if self.use_copy and self.revectorization:
                assert len(batch_original_tree_list) == 1
                reference = oov_dict.get_symbol_idx_for_list(batch_original_tree_list[0].split())
                # reference = Tree.convert_to_tree(tmp_list, 0, len(tmp_list), oov_dict)
                eval_vocab = oov_dict
            else:
                assert len(batch_original_tree_list) == 1
                reference = model.tgt_vocab.get_symbol_idx_for_list(batch_original_tree_list[0].split())
                eval_vocab = self.tgt_vocab

            candidate = model.decoder.translate(use_copy,
                                                enc_hidden_size,
                                                dec_hidden_size,
                                                model,
                                                input_graph_list,
                                                self.src_vocab,
                                                self.tgt_vocab,
                                                device,
                                                max_dec_seq_length,
                                                max_dec_tree_depth,
                                                oov_dict=oov_dict,
                                                use_beam_search=True,
                                                beam_size=self.opt.beam_size)
                                                # beam_search_version=self.opt.beam_search_version)
            
            candidate = [int(c) for c in candidate]
            num_left_paren = sum(
                1 for c in candidate if eval_vocab.idx2symbol[int(c)] == "(")
            num_right_paren = sum(
                1 for c in candidate if eval_vocab.idx2symbol[int(c)] == ")")
            diff = num_left_paren - num_right_paren
            if diff > 0:
                for i in range(diff):
                    candidate.append(
                        self.test_data_loader.tgt_vocab.symbol2idx[")"])
            elif diff < 0:
                candidate = candidate[:diff]
            ref_str = convert_to_string(
                reference, eval_vocab)
            cand_str = convert_to_string(
                candidate, eval_vocab)

            for c in candidate:
                if c >= self.tgt_vocab.vocab_size:
                    print("====================")
                    print(oov_dict.symbol2idx)
                    print(cand_str)
                    print(ref_str)
                    print("====================")
            # print(cand_str)
            # print(ref_str)

            reference_list.append(reference)
            candidate_list.append(candidate)
            # print(cand_str)

        test_acc = compute_tree_accuracy(
            candidate_list, reference_list, eval_vocab)
        print("TEST ACCURACY = {:.3f}\n".format(test_acc))
        return test_acc


def convert_to_string(idx_list, form_manager):
    w_list = []
    for i in range(len(idx_list)):
        w_list.append(form_manager.get_idx_symbol(int(idx_list[i])))
    return " ".join(w_list)


def get_split_comma(input_str):
    input_str = input_str.replace(",", " , ")
    input_list = [item.strip() for item in input_str.split()]
    ref_char = "$"
    for index in range(len(input_list)):
        if input_list[index] == ',':
            if input_list[:index].count('(') == input_list[:index].count(')'):
                if input_list[index+1:].count('(') == input_list[index+1:].count(')'):
                    if input_list[index] == ref_char:
                        raise RuntimeError
                    else:
                        input_list[index] = ref_char
    new_str = " ".join(input_list).split('$')
    result_set = set()
    for str_ in new_str:
        result_set.add(str_.strip())
    return result_set


def is_all_same(c1, c2, form_manager):
    all_same = True
    if len(c1) == len(c2):
        all_same = True
        for j in range(len(c1)):
            if c1[j] != c2[j]:
                all_same = False
                break
        if all_same:
            return True
    if len(c1) != len(c2) or all_same == False:
        d1 = " ".join([form_manager.get_idx_symbol(x) for x in c1])
        d2 = " ".join([form_manager.get_idx_symbol(x) for x in c2])
        if get_split_comma(d1) == get_split_comma(d2):
            # print(d1)
            # print(d2)
            # print("============")
            return True
        return False
    raise NotImplementedError("you should not arrive here!")

def compute_accuracy(candidate_list, reference_list, form_manager):
    if len(candidate_list) != len(reference_list):
        print("candidate list has length {}, reference list has length {}\n".format(
            len(candidate_list), len(reference_list)))

    len_min = min(len(candidate_list), len(reference_list))
    c = 0
    for i in range(len_min):
        if is_all_same(candidate_list[i], reference_list[i], form_manager):
            c = c+1
        else:
            pass

    return c/float(len_min)


def compute_tree_accuracy(candidate_list_, reference_list_, form_manager):
    candidate_list = []
    for i in range(len(candidate_list_)):
        candidate_list.append(candidate_list_[i])
    reference_list = []
    for i in range(len(reference_list_)):
        reference_list.append(reference_list_[i])
    return compute_accuracy(candidate_list, reference_list, form_manager)


if __name__ == "__main__":
    start = time.time()
    main_arg_parser = argparse.ArgumentParser(description="parser")

    main_arg_parser.add_argument(
        '-gpuid', type=int, default=1, help='which gpu to use. -1 = use CPU')
    main_arg_parser.add_argument(
        '-seed', type=int, default=1234, help='torch manual random number generator seed')
    main_arg_parser.add_argument(
        '-use_copy', type=int, default=1, help='whether use copy mechanism')

    main_arg_parser.add_argument('-data_dir', type=str,
                                 default='/home/lishucheng/Graph4AI/graph4nlp/examples/pytorch/semantic_parsing/graph2tree/data/jobs', help='data path')
    main_arg_parser.add_argument('-checkpoint_dir', type=str,
                                 default='/home/lishucheng/Graph4AI/graph4nlp/examples/pytorch/semantic_parsing/graph2tree/checkpoint_dir_jobs', help='output directory where checkpoints get written')

    main_arg_parser.add_argument('-gnn_type', type=str, default="SAGE")
    main_arg_parser.add_argument('-rnn_type', type=str, default="lstm")

    main_arg_parser.add_argument('-enc_emb_size', type=int, default=300)
    main_arg_parser.add_argument('-tgt_emb_size', type=int, default=300)

    main_arg_parser.add_argument('-enc_hidden_size', type=int, default=300)
    main_arg_parser.add_argument('-dec_hidden_size', type=int, default=300)

    # DynamicGraph_node_emb_refined, DynamicGraph_node_emb, ConstituencyGraph
    main_arg_parser.add_argument(
        '-graph_construction_type', type=str, default="DynamicGraph_node_emb")

    # "None, line, dependency, constituency"
    main_arg_parser.add_argument(
        '-dynamic_init_graph_type', type=str, default="constituency")

    main_arg_parser.add_argument('-batch_size', type=int, default=20)

    main_arg_parser.add_argument(
        '-dropout_for_word_embedding', type=float, default=0.1)

    main_arg_parser.add_argument(
        '-dropout_for_encoder', type=float, default=0)

    main_arg_parser.add_argument(
        '-dropout_for_decoder', type=float, default=0.1)

    main_arg_parser.add_argument(
        '-direction_option', type=str, default="undirected")

    main_arg_parser.add_argument(
        '-beam_size', type=int, default=2)

    main_arg_parser.add_argument('-max_dec_seq_length', type=int, default=50)
    main_arg_parser.add_argument(
        '-max_dec_tree_depth_for_train', type=int, default=50)
    main_arg_parser.add_argument(
        '-max_dec_tree_depth_for_test', type=int, default=20)

    main_arg_parser.add_argument(
        '-teacher_force_ratio', type=float, default=1.0)

    main_arg_parser.add_argument(
        '-init_weight', type=float, default=0.08, help='initailization weight')
    main_arg_parser.add_argument(
        '-learning_rate', type=float, default=1e-3, help='learning rate')
    main_arg_parser.add_argument('-weight_decay', type=float, default=0)

    main_arg_parser.add_argument('-max_epochs', type=int, default=200,
                                 help='number of full passes through the training data')
    main_arg_parser.add_argument('-min_freq', type=int, default=1,
                                 help='minimum frequency for vocabulary')
    main_arg_parser.add_argument(
        '-grad_clip', type=int, default=5, help='clip gradients at this value')

    main_arg_parser.add_argument(
        '-pretrain_fn', type=str, default="/home/lishucheng/projects/Tools-and-Resources/glove/glove.6B.300d.zip")

    args = main_arg_parser.parse_args()

    runner = Jobs(opt=args)
    best_acc = runner.train()

    end = time.time()
    print("total time: {} minutes\n".format((end - start)/60))