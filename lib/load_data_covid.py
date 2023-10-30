import numpy as np
import torch
from torch_geometric.data import DataLoader,Data
from torch.utils.data import DataLoader as Loader
from tqdm import tqdm
import math
from scipy.linalg import block_diag
import lib.utils as utils
import pandas as pd


weights = [10, 1, 10, 1, 1000, 1000000, 100000]

class ParseData(object):

    def __init__(self,args):
        self.args = args
        self.datapath = args.datapath
        self.dataset = args.dataset
        self.random_seed = args.random_seed
        self.pred_length = args.pred_length
        self.condition_length = args.condition_length
        self.batch_size = args.batch_size

        torch.manual_seed(self.random_seed)
        np.random.seed(self.random_seed)


    def load_train_data(self, is_train = True):

        # Loading Data. N is state number, T is number of days. D is feature number.

        features = np.load(self.args.datapath + self.args.dataset + '/train.npy')  # [N,T,D]
        graphs = np.load(self.args.datapath + self.args.dataset + '/graph_train.npy')  # [T,N,N]
        self.num_states = features.shape[0]


        # Feature Preprocessing: Selection + Null Value + Normalization (take log and use cummulative number)
        features = self.feature_preprocessing(features, graphs, method = 'norm_const', is_inc = True) #[N,T,D]
        self.num_features = features.shape[2]


        # Graph Preprocessing: remain self-loop and take log
        graphs = self.graph_preprocessing(graphs, method = 'norm_const', is_self_loop = True)  #[T,N,N]

        # Split Training Samples
        features, graphs = self.generateTrainSamples(features, graphs) #[K,N,T,D], [K,T,N,N]

        if is_train:
            features = features[:-5, :, :, :]
            graphs = graphs[:-5, :, :, :]
        else:
            features = features[-5:, :, :, :]
            graphs = graphs[-5:, :, :, :]



        encoder_data_loader, decoder_data_loader, decoder_graph_loader, num_batch, self.num_states = self.generate_train_val_dataloader(features, graphs, is_train)


        return encoder_data_loader, decoder_data_loader, decoder_graph_loader, num_batch, self.num_states


    def generate_train_val_dataloader(self, features, graphs, is_train = True):
        # Split data for encoder and decoder dataloader
        feature_observed, times_observed, series_decoder, times_extrap = self.split_data(features)  # series_decoder[K*N,T2,D]
        self.times_extrap = times_extrap

        # Generate Encoder data
        encoder_data_loader = self.transfer_data(feature_observed, graphs, times_observed, self.batch_size)

        # Generate Decoder Data and Graph
        if is_train:
            series_decoder_gt = self.decoder_gt_train()  # [K*N,T2,D]
        else:
            series_decoder_gt = self.decoder_gt_train()  # [K*N,T2,D]
            series_decoder_gt = series_decoder_gt[-5*self.num_states:,:,:]

        series_decoder_all = [(series_decoder[i, :, :], series_decoder_gt[i, :, :]) for i in
                              range(series_decoder.shape[0])]

        decoder_data_loader = Loader(series_decoder_all, batch_size=self.batch_size * self.num_states, shuffle=False,
                                     collate_fn=lambda batch: self.variable_time_collate_fn_activity(
                                         batch))  # num_graph*num_ball [tt,vals,masks]

        graph_decoder = graphs[:, self.args.condition_length:, :, :]  # [K,T2,N,N]
        decoder_graph_loader = Loader(graph_decoder, batch_size=self.batch_size, shuffle=False)

        num_batch = len(decoder_data_loader)
        assert len(decoder_data_loader) == len(decoder_graph_loader)

        # Inf-Generator
        encoder_data_loader = utils.inf_generator(encoder_data_loader)
        decoder_graph_loader = utils.inf_generator(decoder_graph_loader)
        decoder_data_loader = utils.inf_generator(decoder_data_loader)

        return encoder_data_loader, decoder_data_loader, decoder_graph_loader, num_batch, self.num_states



    def load_test_data(self, pred_length, condition_length):

        # Loading Data. N is state number, T is number of days. D is feature number.
        print("predicting data at: %s" % self.args.dataset)
        features_1 = np.load(self.args.datapath + self.args.dataset + '/train.npy')  # [N,T,D]
        graphs_1 = np.load(self.args.datapath + self.args.dataset + '/graph_train.npy')  # [T,N,N]
        features_2 = np.load(self.args.datapath + self.args.dataset + '/test.npy')  # [N,T,D]
        graphs_2 = np.load(self.args.datapath + self.args.dataset + '/graph_test.npy')  # [T,N,N]

        features = np.concatenate([features_1, features_2], axis=1)
        graphs = np.concatenate([graphs_1, graphs_2], axis=0)
        self.num_states = features.shape[0]

        # Feature Preprocessing: Selection + Null Value + Normalization (take log and use cummulative number)
        features = self.feature_preprocessing(features, graphs, method='norm_const',is_inc = True)  # [N,T,D]
        self.num_features = features.shape[2]

        # Graph Preprocessing: remain self-loop and take log
        graphs = self.graph_preprocessing(graphs, method='norm_const', is_self_loop=True)  # [T,N,N]

        # Generate Encoding data (which is aligned)
        df = self.loading_test_points(pred_length,condition_length)

        start_indexes = df['start_index'].tolist()
        end_indexes = df['end_index'].tolist()
        features_list = []
        graphs_list = []

        for i, start_index in enumerate(start_indexes):
            # encoder data are aligned:
            encoder_data = np.expand_dims(features[:, start_index:start_index + condition_length, :], axis=0)  # [1,N,T1,D]
            features_list.append(encoder_data)
            graph_data = np.expand_dims(graphs[start_index:start_index + condition_length, :, :], axis=0)  # [1,T1,N,N]
            graphs_list.append(graph_data)

        features_enc = np.concatenate(features_list, axis=0) #[K,N,T,D]
        graphs_enc = np.concatenate(graphs_list, axis=0) #[K,T,N,N]


        times_pred_max = max(df['pred_length'].tolist())
        times = np.asarray([i / (times_pred_max + condition_length) for i in
                            range(times_pred_max + condition_length)])  # normalized in [0,1] T
        times_observed = times[:condition_length]  # [T1]
        self.times_extrap = times[condition_length:] - times[condition_length]  # [T2] making starting time of T2 be 0.

        encoder_data_loader = self.transfer_data(features_enc, graphs_enc, times_observed,1)

        # Decoder data
        features_masks_dec = []  # K*[1,T,D]
        graphs_dec = []  # k*[1,T,N,N]

        # Reloading data for gt
        features_1 = np.load(self.args.datapath + self.args.dataset + '/train.npy')  # [N,T,D]
        graphs_1 = np.load(self.args.datapath + self.args.dataset + '/graph_train.npy')  # [T,N,N]
        features_2 = np.load(self.args.datapath + self.args.dataset + '/test.npy')  # [N,T,D]
        graphs_2 = np.load(self.args.datapath + self.args.dataset + '/graph_test.npy')  # [T,N,N]

        features_origin = np.concatenate([features_1, features_2], axis=1)
        graphs_origin = np.concatenate([graphs_1, graphs_2], axis=0)
        features_origin = self.feature_preprocessing(features_origin, graphs_origin, method='norm_const', is_inc = False)  # [N,T,D]

        for i, start_index in enumerate(start_indexes):
            # decoder data
            test_start_index = start_index + condition_length
            end_index = end_indexes[i]
            features_each = features[:, test_start_index:end_index+1, self.args.feature_out_index]  # [N,T2,D]
            features_each_origin = features_origin[:, test_start_index:end_index+1, self.args.feature_out_index]  # [N,T2,D]
            graph_each = graphs[test_start_index:end_index+1, :, :] # [T2,N,N]
            graphs_dec.append(torch.FloatTensor(graph_each))  # K*[T=1,N,N]
            masks_each = np.asarray([i for i in range(end_index - test_start_index+1)])
            features_masks_dec.append((features_each,features_each_origin, masks_each))

        decoder_graph_loader = Loader(graphs_dec, batch_size=1, shuffle=False)
        decoder_data_loader = Loader(features_masks_dec, batch_size=1, shuffle=False,
                                     collate_fn=lambda batch: self.variable_test(batch))  #



        # Inf-Generator
        # Inf-Generator
        encoder_data_loader = utils.inf_generator(encoder_data_loader)
        decoder_graph_loader = utils.inf_generator(decoder_graph_loader)
        decoder_data_loader = utils.inf_generator(decoder_data_loader)

        num_batch = len(start_indexes)

        return encoder_data_loader, decoder_data_loader, decoder_graph_loader,num_batch

    def feature_preprocessing(self, feature_input, graph_input, method = 'norm_const', is_inc = True):
        '''
        Step1: Feature adding and selection.
        Step2: Null value preprocess
        Step3: Feature Normalization
        '''
        #Step1: Feature adding and selection.
        feature_names = self.args.features.split(",")
        self.feature_names = feature_names
        assert len(weights) == len(feature_names)
        #print("Selected features are: " + self.args.features)
        if "Population" in feature_names:
            feature_input = self.add_population(feature_input)
        if "Mobility" in feature_names:
            feature_input = self.add_mobility(graph_input, self.num_states, feature_input)

        # load feature dict to do feature selection
        f = open(self.args.datapath + "feature_dict.txt", 'r')
        tmp = f.read()
        feature_dict = eval(tmp)
        f.close()

        feature_indices = []
        for each_feature in feature_names:
            feature_indices.append(feature_dict[each_feature])

        feature_input = feature_input[:, :, feature_indices]

        #Step2: Null value preprocess
        feature_input = np.where(feature_input <= -1, 0, feature_input)

        #Step3: Feature Normalization
        feature_input = np.where(feature_input <= -1, 0, feature_input)

        for i,each_feature in enumerate(feature_names):
            if each_feature in ["Confirmed", "Deaths", "Recovered", "Active"]:
                feature_input[:, :, i] = self.feature_normalization(feature_input, i, method = method, is_inc = is_inc)
            elif each_feature != "Mortality_Rate": #Mortality keep original
                feature_input[:, :, i] = self.feature_normalization(feature_input, i, method = method, is_inc = False)



        return feature_input

    def graph_preprocessing(self, graph_input, method = 'norm_const', is_self_loop = True):
        '''
                :param graph_input: [T,N,N]
                :param method: norm--norm by rows: G[i,j] is the outflow from i to j.
                :param is_self_loop: True to remain self-loop, otherwise no self-loop
                :return: [T,N,N]
                '''
        if not is_self_loop:
            num_days = graph_input.shape[0]
            num_states = graph_input.shape[1]
            graph_output = np.ones_like(graph_input)
            for i in range(num_days):
                graph_output[i] = graph_input[i] * (1 - np.identity(num_states))
        else:
            graph_output = graph_input

        if method == "log":
            graph_output = np.log(graph_output + 1)  # 0 remains 0
        elif method == "norm_const":
            graph_output = graph_output / weights[-1]

        return graph_output

    def generateTrainSamples(self, features, graphs):
        '''
        Split training data into several overlapping series.
        :param features: [N,T,D]
        :param graphs: [T,N,N]
        :param interval: 3
        :return: transform feature into [K,N,T,D], transform graph into [K,T,N,N]
        '''
        interval = self.args.split_interval
        each_length = self.args.pred_length + self.args.condition_length
        num_batch = math.floor((features.shape[1] - each_length) / interval) + 1
        num_states = features.shape[0]
        num_features = features.shape[2]
        features_split = np.zeros((num_batch, num_states, each_length, num_features))
        graphs_split = np.zeros((num_batch, each_length, num_states, num_states))
        batch_num = 0

        for i in range(0, features.shape[1] - each_length + 1, interval):
            assert i + each_length <= features.shape[1]
            features_split[batch_num] = features[:, i:i + each_length, :]
            graphs_split[batch_num] = graphs[i:i + each_length, :, :]
            batch_num += 1
        return features_split, graphs_split  # [K,N,T,D], [K,T,N,N]

    def split_data(self, feature):
        '''
               Generate encoder data (need further preprocess) and decoder data
               :param feature: [K,N,T,D], T=T1+T2
               :param data_type:
               :return:
               '''

        feature_observed = feature[:, :, :self.args.condition_length, :]
        # select corresponding features
        feature_out_index = self.args.feature_out_index
        feature_extrap = feature[:, :, self.args.condition_length:, feature_out_index]
        assert feature_extrap.shape[-1] == len(feature_out_index)
        times = np.asarray([i / feature.shape[2] for i in range(feature.shape[2])])  # normalized in [0,1] T
        times_observed = times[:self.args.condition_length]  # [T1]
        times_extrap = times[self.args.condition_length:] - times[self.args.condition_length]  # [T2] making starting time of T2 be 0.
        assert times_extrap[0] == 0
        series_decoder = np.reshape(feature_extrap, (-1, len(times_extrap), len(feature_out_index)))  # [K*N,T2,D]

        return feature_observed, times_observed, series_decoder, times_extrap

    def transfer_data(self, feature, edges, times, batch_size):
        '''

        :param feature: #[K,N,T1,D]
        :param edges: #[K,T,N,N], with self-loop
        :param times: #[T1]
        :param time_begin: 1
        :return:
        '''
        data_list = []
        edge_size_list = []

        num_samples = feature.shape[0]

        for i in tqdm(range(num_samples)):
            data_per_graph, edge_size = self.transfer_one_graph(feature[i], edges[i], times)
            data_list.append(data_per_graph)
            edge_size_list.append(edge_size)

        print("average number of edges per graph is %.4f" % np.mean(np.asarray(edge_size_list)))
        data_loader = DataLoader(data_list, batch_size = batch_size, shuffle = False)

        return data_loader

    def add_mobility(self,graph_input, num_states,feature_input):
        '''
        Adding self-loop mobility data from graph to features [N,T,D]
        :param graph_input: [T,N,N]
        :param num_states: [N,T,D]
        :param feature_input: [N,T,D]
        :return: feature_output: [N,T,D+1]
        '''
        num_days = graph_input.shape[0]
        mobility_matrix = np.zeros((num_states, num_days, 1))
        for i in range(num_days):
            for j in range(num_states):
                mobility_matrix[j, i, 0] = graph_input[i, j, j]
        feature_output = np.concatenate([feature_input, mobility_matrix], axis=2)
        return feature_output

    def add_population(self, feature_input):
        '''
        Adding population data to features [N,T,D]
        :param feature_input: [N,T,D]
        :return: feature_output: [N,T,D+1]
        '''
        population = np.reshape(np.load(self.args.datapath + "state_info.npy").astype("int"),(-1,1))  # [N,1]
        population = np.expand_dims(population, axis=2)
        population_tensor = np.zeros((feature_input.shape[0], feature_input.shape[1], 1))
        population_tensor += population  # [N,T,1]
        feature_output = np.concatenate([feature_input, population_tensor], axis=2)

        return feature_output

    def feature_normalization(self, features, feature_ID, method = 'None', is_inc = False):
        '''
        normalize one single feature.
        :param features: [N,T,D]
        :param feature_ID:
        :param method:
        :param is_inc:
        :return: [N,T]
        '''
        # method: log, norm, log_norm, None, norm is to normalize within [1,2]
        if is_inc:
            one_feature = np.ones_like(features[:, :, feature_ID])  # [N,T]
            one_feature[:, 1:] = features[:, 1:, feature_ID] - features[:, :-1, feature_ID]
        else:
            one_feature = features[:, :, feature_ID]

        if method == "log":
            one_feature = np.log(one_feature + 1)
            return one_feature
        elif method == 'norm_const':
            one_feature = one_feature / weights[feature_ID]
            return one_feature
        elif method == "None":
            return one_feature

    def transfer_one_graph(self, feature, edge, time):
        '''f

        :param feature: [N,T1,D]
        :param edge: [T,N,N]  (needs to transfer into [T1,N,N] first, already with self-loop)
        :param time: [T1]
        :return:
            1. x : [N*T1,D]: feature for each node.
            2. edge_index [2,num_edge]: edges including cross-time
            3. edge_weight [num_edge]: edge weights
            4. y: [N], value= num_steps: number of timestamps for each state node.
            5. x_pos 【N*T1】: timestamp for each node
            6. edge_time [num_edge]: edge relative time.
        '''

        ########## Getting and setting hyperparameters:
        num_states = feature.shape[0]
        T1 = self.args.condition_length
        each_gap = 1 / edge.shape[0]
        edge = edge[:T1, :, :]
        time = np.reshape(time, (-1, 1))

        ########## Compute Node related data:  x, y, x_pos
        # [Num_states], value is the number of timestamp for each state in the encoder, == args.condition_length
        y = self.args.condition_length * np.ones(num_states)
        # [Num_states*T1,D]
        x = np.reshape(feature, (-1, feature.shape[2]))
        # [Num_states*T1,1] node timestamp
        x_pos = np.concatenate([time for i in range(num_states)], axis = 0)
        assert len(x_pos) == feature.shape[0] * feature.shape[1]

        ########## Compute edge related data
        edge_time_matrix = np.concatenate([np.asarray(x_pos).reshape(-1, 1) for _ in range(len(x_pos))], axis = 1) -\
             np.concatenate([np.asarray(x_pos).reshape(1, -1) for _ in range(len(x_pos))], axis = 0)  # [N*T1,N*T1], SAME TIME = 0

        edge_exist_matrix = np.ones((len(x_pos), len(x_pos)))  # [N*T1,N*T1] NO-EDGE = 0, depends on both edge weight and time matrix

        # Step1: Construct edge_weight_matrix [N*T1,N*T1]
        edge_repeat = np.repeat(edge, self.args.condition_length, axis = 2)  # [T1,N,NT1]
        edge_repeat = np.transpose(edge_repeat, (1, 0, 2))  # [N,T1,NT1]
        edge_weight_matrix = np.reshape(edge_repeat, (-1, edge_repeat.shape[2]))  # [N*T1,N*T1]

        # mask out cross_time edges of different state nodes.
        a = np.identity(T1)  # [T1,T1]
        b = np.concatenate([a for i in range(num_states)], axis = 0)  # [N*T1,T1]
        c = np.concatenate([b for i in range(num_states)], axis = 1)  # [N*T1,N*T1]

        a = np.ones((T1, T1))
        d = block_diag(*([a] * num_states))
        edge_weight_mask = (1 - d) * c + d
        edge_weight_matrix = edge_weight_matrix * edge_weight_mask  # [N*T1,N*T1]

        max_gap = each_gap


        # Step2: Construct edge_exist_matrix [N*T1,N*T1]: depending on both time and weight.
        edge_exist_matrix = np.where(
            (edge_time_matrix <= 0) & (abs(edge_time_matrix) <= max_gap) & (edge_weight_matrix != 0),
            edge_exist_matrix, 0)


        edge_weight_matrix = edge_weight_matrix * edge_exist_matrix
        edge_index, edge_weight_attr = utils.convert_sparse(edge_weight_matrix)
        assert np.sum(edge_weight_matrix != 0) != 0  #at least one edge weight (one edge) exists.

        edge_time_matrix = (edge_time_matrix + 3) * edge_exist_matrix # padding 2 to avoid equal time been seen as not exists.
        _, edge_time_attr = utils.convert_sparse(edge_time_matrix)
        edge_time_attr -= 3

        # converting to tensor
        x = torch.FloatTensor(x)
        edge_index = torch.LongTensor(edge_index)
        edge_weight_attr = torch.FloatTensor(edge_weight_attr)
        edge_time_attr = torch.FloatTensor(edge_time_attr)
        y = torch.LongTensor(y)
        x_pos = torch.FloatTensor(x_pos)


        graph_data = Data(x = x, edge_index = edge_index, edge_weight = edge_weight_attr, y = y, pos = x_pos, edge_time = edge_time_attr)
        edge_num = edge_index.shape[1]

        return graph_data, edge_num

    def variable_time_collate_fn_activity(self,batch):
        """
        Expects a batch of
            - (feature0,feaure_gt) [K*N, T2, D]
        """
        # Extract corrsponding deaths or cases
        combined_vals = np.concatenate([np.expand_dims(ex[0],axis=0) for ex in batch],axis=0)
        combined_vals_true = np.concatenate([np.expand_dims(ex[1],axis=0) for ex in batch], axis = 0)



        combined_vals = torch.FloatTensor(combined_vals) #[M,T2,D]
        combined_vals_true = torch.FloatTensor(combined_vals_true)  # [M,T2,D]

        combined_tt = torch.FloatTensor(self.times_extrap)

        data_dict = {
            "data": combined_vals,
            "time_steps": combined_tt,
            "data_gt" : combined_vals_true
            }
        return data_dict

    def variable_test(self,batch):
        """
        Expects a batch of
            - (feature,feature_gt,mask)
            - feature: [N,T,D]
            - mask: T
        Returns:
            combined_tt: The union of all time observations. [T2], varies from different testing sample
            combined_vals: (M, T2, D) tensor containing the gt values.
            combined_masks: index for output timestamps. Only for masking out prediction.
        """
        # Extract corrsponding deaths or cases

        combined_vals = torch.FloatTensor(batch[0][0]) #[M,T2,D]
        combined_vals_gt = torch.FloatTensor(batch[0][1]) #[M,T2,D]
        combined_masks = torch.LongTensor(batch[0][2]) #[1]

        combined_tt = torch.FloatTensor(self.times_extrap)

        data_dict = {
            "data": combined_vals,
            "time_steps": combined_tt,
            "masks":combined_masks,
            "data_gt": combined_vals_gt,
            }
        return data_dict

    def loading_test_points(self,pred_length,condition_length):
        df = pd.read_csv(self.args.datapath + self.args.dataset + "/test_point.csv", header=0, sep="\t")
        df_type = df['Pred_Length'].map(lambda x: int(x) == pred_length)
        df = df[df_type]
        df['start_index'] = df["Start_date"].map(lambda x: utils.transfer_index(x) - condition_length)
        df['end_index'] = df["End_date"].map(lambda x: utils.transfer_index(x))
        df['pred_length'] = df['end_index'] - df['start_index'] - condition_length + 1

        return df

    def decoder_gt_train(self):

        features = np.load(self.args.datapath + self.args.dataset + '/train.npy')  # [N,T,D]
        graphs = np.load(self.args.datapath + self.args.dataset + '/graph_train.npy')  # [T,N,N]
        # Feature Preprocessing: Selection + Null Value + Normalization (take log and use cummulative number)
        features = self.feature_preprocessing(features, graphs, method='norm_const', is_inc = False)  # [N,T,D]
        # Split Training Samples
        features, _ = self.generateTrainSamples(features, graphs)  # [K,N,T,D], [K,T,N,N]
        # Split data for encoder and decoder dataloader
        _, _, series_decoder, _ = self.split_data(features)  # series_decoder[K*N,T2,D]
        return series_decoder
