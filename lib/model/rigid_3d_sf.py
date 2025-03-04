import torch
import torch.nn as nn

import numpy as np
from collections import defaultdict
from sklearn.cluster import DBSCAN
import MinkowskiEngine as ME

from lib.utils import pairwise_distance, transform_point_cloud, kabsch_transformation_estimation, refine_ego_motion, refine_cluster_motion
from lib.utils import upsample_flow, upsample_bckg_labels, upsample_cluster_labels
from lib.model.minkowski.MinkowskiFlow import SparseEnoder, SparseDecoder, SparseFlowRefiner, EgoMotionHead, SparseSegHead, EgoMotionHeadLoop



class MinkowskiFlow(nn.Module):
    def __init__(self, args):
        super(MinkowskiFlow, self).__init__()
        
        self.args = args
        self.voxel_size = args['misc']['voxel_size']
        self.device = torch.device('cuda' if (torch.cuda.is_available() and args['misc']['use_gpu']) else 'cpu') 
        self.normalize_feature  = args['network']['normalize_features']
        self.test_flag = True if args['misc']['run_mode'] == 'test' else False

        ########## new attributes for
        self.loop_ego = args['method']['loop_ego']
        self.loop_flow = args['method']['loop_flow']

        if self.test_flag:
            self.postprocess_ego = args['test']['postprocess_ego']
            self.postprocess_clusters = args['test']['postprocess_clusters']
        
        self.estimate_ego, self.estimate_flow, self.estimate_semantic, self.estimate_cluster = False, False, False, False

        self.upsampling_k = 36 if args['data']['dataset'] in ['StereoKITTI_ME', 'FlyingThings3D_ME'] else 3
        self.tau_offset = 0.025 if args['data']['dataset'] in ['StereoKITTI_ME', 'FlyingThings3D_ME'] else 0.03

        if args['data']['input_features'] == 'occupancy':
            self.input_feature_dim = 1
        else:
            self.input_feature_dim = 3

        # Initialize the backbone network
        self.encoder = SparseEnoder(in_channels=self.input_feature_dim,
                                            conv1_kernel_size=args['network']['in_kernel_size'],
                                            norm_type=args['network']['norm_type'])

        self.decoder = SparseDecoder(out_channels=args['network']['feature_dim'],
                                norm_type=args['network']['norm_type'])

        # Initialize the scene flow head
        if args['method']['flow']:
            self.estimate_flow = True
            self.epsilon = torch.nn.Parameter(torch.tensor(-5.0))

            self.flow_refiner = SparseFlowRefiner(flow_dim=3)

        # Initialize the background segmentation head
        if args['method']['semantic']:
            self.estimate_semantic = True

            self.seg_decoder = SparseSegHead(in_channels=args['network']['feature_dim'],
                                             out_channels=args['data']['n_classes'],
                                             norm_type=args['network']['norm_type'])


        # Initialize the ego motion head
        if args['method']['ego_motion']:
            self.estimate_ego = True
            self.ego_n_points = args['network']['ego_motion_points']
            self.add_slack = args['network']['add_slack']
            self.sinkhorn_iter = args['network']['sinkhorn_iter']

            if args['method']['loop_ego']:
                self.ego_motion_decoder = EgoMotionHeadLoop(add_slack=self.add_slack,
                                                   sinkhorn_iter=self.sinkhorn_iter)
            else:
                self.ego_motion_decoder = EgoMotionHead(add_slack=self.add_slack,
                                                   sinkhorn_iter=self.sinkhorn_iter,umeyama=args['method']['umeyama'])
        
        # Initialize the foreground clustering head
        if args['method']['clustering']:
                self.estimate_cluster = True
                self.min_p_cluster = args['network']['min_p_cluster']
                
                self.cluster_estimator = DBSCAN(min_samples=args['network']['min_samples_dbscan'], 
                                                metric=args['network']['cluster_metric'], eps=args['network']['eps_dbscan'])        


    # TODO infer another flow
    def _infer_flow(self, flow_f_1, flow_f_2):
        
        # Normalize the features
        if self.normalize_feature:
            flow_f_1= ME.SparseTensor(
                        flow_f_1.F / torch.norm(flow_f_1.F, p=2, dim=1, keepdim=True),
                        coordinate_map_key=flow_f_1.coordinate_map_key,
                        coordinate_manager=flow_f_1.coordinate_manager)

            flow_f_2= ME.SparseTensor(
                        flow_f_2.F / torch.norm(flow_f_2.F, p=2, dim=1, keepdim=True),
                        coordinate_map_key=flow_f_2.coordinate_map_key,
                        coordinate_manager=flow_f_2.coordinate_manager)

        # Extract the coarse flow based on the feature correspondences
        coarse_flow = []

        # Iterate over the examples in the batch
        # https://github.com/NVIDIA/MinkowskiEngine/blob/8a9dae528f47e33d6f48820cbdfe6f8e7fab12ef/MinkowskiEngine/MinkowskiTensor.py#L309
        for b_idx in range(len(flow_f_1.decomposed_coordinates)):
            # flow_f_1 is ME.SparseTensor, flow_f_1.F:features, flow_f_1.C:coordinates
            feat_s = flow_f_1.F[flow_f_1.C[:,0] == b_idx]
            feat_t = flow_f_2.F[flow_f_2.C[:,0] == b_idx]

            coor_s = flow_f_1.C[flow_f_1.C[:,0] == b_idx,1:].to(self.device) * self.voxel_size
            coor_t = flow_f_2.C[flow_f_2.C[:,0] == b_idx,1:].to(self.device) * self.voxel_size


            # Squared l2 distance between points points of both point clouds
            coor_s, coor_t = coor_s.unsqueeze(0), coor_t.unsqueeze(0)
            feat_s, feat_t = feat_s.unsqueeze(0), feat_t.unsqueeze(0)
            
            # Force transport to be zero for points further than 10 m apart
            support = (pairwise_distance(coor_s, coor_t, normalized=False ) < 10**2).float()

            # Transport cost matrix, size [1, n, m], since the in the for loop with b_idx(batch)
            # features dimension 128, the number of points is below 8192
            C = pairwise_distance(feat_s, feat_t)

            # size [1, n, m]
            K = torch.exp(-C / (torch.exp(self.epsilon) + self.tau_offset)) * support
       
            # size [1, n, 1]
            row_sum  = K.sum(-1, keepdim=True)

            # Estimate flow  [1, n, m]@[1, m, co] -> [1, n, co], co: dimension of coordinate
            corr_flow = (K  @ coor_t) / (row_sum + 1e-8) - coor_s
            
            coarse_flow.append(corr_flow.squeeze(0))
        

        # coarse_flow stack as coor_s like [batchsize*n,co] due to the ME sparse tensor
        coarse_flow = torch.cat(coarse_flow,dim=0)

        st_cf = ME.SparseTensor(features=coarse_flow, 
                                coordinate_manager=flow_f_1.coordinate_manager, 
                                coordinate_map_key=flow_f_1.coordinate_map_key)
        
        self.inferred_values['coarse_flow'] = st_cf.F


        # Refine the flow with the second network
        refined_flow  = self.flow_refiner(st_cf)


        self.inferred_values['refined_flow'] = refined_flow.F

    ##################################################################
    def _infer_flow_loop(self, flow_f_1, flow_f_2):
        
        # Normalize the features
        if self.normalize_feature:
            flow_f_1= ME.SparseTensor(
                        flow_f_1.F / torch.norm(flow_f_1.F, p=2, dim=1, keepdim=True),
                        coordinate_map_key=flow_f_1.coordinate_map_key,
                        coordinate_manager=flow_f_1.coordinate_manager)

            flow_f_2= ME.SparseTensor(
                        flow_f_2.F / torch.norm(flow_f_2.F, p=2, dim=1, keepdim=True),
                        coordinate_map_key=flow_f_2.coordinate_map_key,
                        coordinate_manager=flow_f_2.coordinate_manager)

        # Extract the coarse flow based on the feature correspondences
        coarse_flow = []
        coarse_flow_t = []

        # Iterate over the examples in the batch
        # https://github.com/NVIDIA/MinkowskiEngine/blob/8a9dae528f47e33d6f48820cbdfe6f8e7fab12ef/MinkowskiEngine/MinkowskiTensor.py#L309
        for b_idx in range(len(flow_f_1.decomposed_coordinates)):
            # flow_f_1 is ME.SparseTensor, flow_f_1.F:features, flow_f_1.C:coordinates
            feat_s = flow_f_1.F[flow_f_1.C[:,0] == b_idx]
            feat_t = flow_f_2.F[flow_f_2.C[:,0] == b_idx]

            coor_s = flow_f_1.C[flow_f_1.C[:,0] == b_idx,1:].to(self.device) * self.voxel_size
            coor_t = flow_f_2.C[flow_f_2.C[:,0] == b_idx,1:].to(self.device) * self.voxel_size


            # Squared l2 distance between points points of both point clouds
            coor_s, coor_t = coor_s.unsqueeze(0), coor_t.unsqueeze(0)
            feat_s, feat_t = feat_s.unsqueeze(0), feat_t.unsqueeze(0)
            
            # Force transport to be zero for points further than 10 m apart
            support = (pairwise_distance(coor_s, coor_t, normalized=False ) < 10**2).float()

            # Transport cost matrix, size [1, n, m], since the in the for loop with b_idx(batch)
            # features dimension 128, the number of points is below 8192
            C = pairwise_distance(feat_s, feat_t)
            # size [1, n, m]
            K = torch.exp(-C / (torch.exp(self.epsilon) + self.tau_offset)) * support      
            # size [1, n, 1]
            row_sum  = K.sum(-1, keepdim=True)
            # Estimate flow  [1, n, m]@[1, m, co] -> [1, n, co], co: dimension of coordinate
            corr_flow = (K  @ coor_t) / (row_sum + 1e-8) - coor_s
            
            coarse_flow.append(corr_flow.squeeze(0))

            # back flow
            C_t = C.transpose(1, 2)
            K_t = K.transpose(1, 2)
            row_sum_t  = K_t.sum(-1, keepdim=True)
            corr_flow_t = (K_t  @ coor_s) / (row_sum_t + 1e-8) - coor_t

            coarse_flow_t.append(corr_flow_t.squeeze(0))
        

        # coarse_flow stack as coor_s like [batchsize*n,co] due to the ME sparse tensor
        coarse_flow = torch.cat(coarse_flow,dim=0)
        st_cf = ME.SparseTensor(features=coarse_flow, 
                                coordinate_manager=flow_f_1.coordinate_manager, 
                                coordinate_map_key=flow_f_1.coordinate_map_key)   
        self.inferred_values['coarse_flow'] = st_cf.F
        # Refine the flow with the second network
        refined_flow  = self.flow_refiner(st_cf)
        self.inferred_values['refined_flow'] = refined_flow.F


        coarse_flow_t = torch.cat(coarse_flow_t,dim=0)
        st_cf_t = ME.SparseTensor(features=coarse_flow_t, 
                        coordinate_manager=flow_f_2.coordinate_manager, 
                        coordinate_map_key=flow_f_2.coordinate_map_key)    
        self.inferred_values['coarse_flow_t'] = st_cf_t.F
        # Refine the flow with the second network
        refined_flow_t  = self.flow_refiner(st_cf_t)
        self.inferred_values['refined_flow_t'] = refined_flow_t.F
    ###################################################################


    # sem_label_s,  sem_label_t GT for training, prediction for test
    def _infer_ego_motion(self, flow_f_1, flow_f_2, sem_label_s,  sem_label_t):

        ego_motion_R = []
        ego_motion_t = []
        ego_motion_perm = []

        run_b_len_s = 0
        run_b_len_t = 0

        # Normalize the features
        if self.normalize_feature:
            flow_f_1= ME.SparseTensor(
                        flow_f_1.F / torch.norm(flow_f_1.F, p=2, dim=1, keepdim=True),
                        coordinate_map_key=flow_f_1.coordinate_map_key,
                        coordinate_manager=flow_f_1.coordinate_manager)

            flow_f_2= ME.SparseTensor(
                        flow_f_2.F / torch.norm(flow_f_2.F, p=2, dim=1, keepdim=True),
                        coordinate_map_key=flow_f_2.coordinate_map_key,
                        coordinate_manager=flow_f_2.coordinate_manager)

        for b_idx in range(len(flow_f_1.decomposed_coordinates)):
            feat_s = flow_f_1.F[flow_f_1.C[:,0] == b_idx]
            feat_t = flow_f_2.F[flow_f_2.C[:,0] == b_idx]

            coor_s = flow_f_1.C[flow_f_1.C[:,0] == b_idx,1:].to(self.device) * self.voxel_size
            coor_t = flow_f_2.C[flow_f_2.C[:,0] == b_idx,1:].to(self.device) * self.voxel_size

            # Get the number of points in the current b_idx
            b_len_s = feat_s.shape[0]
            b_len_t = feat_t.shape[0]

            # Extract the semantic labels for the current b_idx (0 are the background points)
            mask_s = (sem_label_s[run_b_len_s: (run_b_len_s + b_len_s)] == 0)
            mask_t = (sem_label_t[run_b_len_t: (run_b_len_t + b_len_t)] == 0)
            
            # Update the running number of points 
            run_b_len_s += b_len_s
            run_b_len_t += b_len_t

            # Squared l2 distance between points points of both point clouds
            coor_s, coor_t = coor_s[mask_s, :].unsqueeze(0), coor_t[mask_t, :].unsqueeze(0)
            feat_s, feat_t = feat_s[mask_s, :].unsqueeze(0), feat_t[mask_t, :].unsqueeze(0)
            
            # Sample the points randomly (to keep the computation memory tracktable)
            # we only use self.ego_n_points(1024) from background
            idx_ego_s = torch.randperm(coor_s.shape[1])[:self.ego_n_points]
            idx_ego_t = torch.randperm(coor_t.shape[1])[:self.ego_n_points]

            coor_s_ego = coor_s[:,idx_ego_s,:]
            coor_t_ego = coor_t[:,idx_ego_t,:]
            feat_s_ego = feat_s[:,idx_ego_s,:]
            feat_t_ego = feat_t[:,idx_ego_t,:]

            # Force transport to be zero for points further than 5 m apart (10 m to 5 m from the change of orignial)
            # calculate the distance of every point from frame1 to frame2 in coordinate space, and make a mask
            support_ego = (pairwise_distance(coor_s_ego, coor_t_ego, normalized=False ) < 5 ** 2).float()

            # Cost matrix in the feature space
            # calculate the distance of every point from frame1 to frame2 in the feature space
            feat_dist = pairwise_distance(feat_s_ego, feat_t_ego)

            # sinkhorn algorithm with slack column and row to get the similarity of points from background
            # get the corresponding points from pc1 to pc2 using similarity matrix
            # use kabsch_transformation_estimation to get the rigid transformation for ego motion
            R_est, t_est, perm_matrix = self.ego_motion_decoder(feat_dist, support_ego, coor_s_ego, coor_t_ego)

            ego_motion_R.append(R_est)
            ego_motion_t.append(t_est)
            ego_motion_perm.append(perm_matrix)


        # Save ego motion results
        self.inferred_values['R_est'] = torch.cat(ego_motion_R, dim=0)
        self.inferred_values['t_est'] = torch.cat(ego_motion_t, dim=0)
        self.inferred_values['permutation'] = ego_motion_perm

#######################################################################################
    def _infer_ego_motion_loop(self, flow_f_1, flow_f_2, sem_label_s,  sem_label_t):
    
        ego_motion_R_s = []
        ego_motion_t_s = []
        ego_motion_perm_s = []

        ego_motion_R_t = []
        ego_motion_t_t = []
        ego_motion_perm_t = []

        run_b_len_s = 0
        run_b_len_t = 0

        # Normalize the features
        if self.normalize_feature:
            flow_f_1= ME.SparseTensor(
                        flow_f_1.F / torch.norm(flow_f_1.F, p=2, dim=1, keepdim=True),
                        coordinate_map_key=flow_f_1.coordinate_map_key,
                        coordinate_manager=flow_f_1.coordinate_manager)

            flow_f_2= ME.SparseTensor(
                        flow_f_2.F / torch.norm(flow_f_2.F, p=2, dim=1, keepdim=True),
                        coordinate_map_key=flow_f_2.coordinate_map_key,
                        coordinate_manager=flow_f_2.coordinate_manager)

        for b_idx in range(len(flow_f_1.decomposed_coordinates)):
            feat_s = flow_f_1.F[flow_f_1.C[:,0] == b_idx]
            feat_t = flow_f_2.F[flow_f_2.C[:,0] == b_idx]

            coor_s = flow_f_1.C[flow_f_1.C[:,0] == b_idx,1:].to(self.device) * self.voxel_size
            coor_t = flow_f_2.C[flow_f_2.C[:,0] == b_idx,1:].to(self.device) * self.voxel_size

            # Get the number of points in the current b_idx
            b_len_s = feat_s.shape[0]
            b_len_t = feat_t.shape[0]

            # Extract the semantic labels for the current b_idx (0 are the background points)
            mask_s = (sem_label_s[run_b_len_s: (run_b_len_s + b_len_s)] == 0)
            mask_t = (sem_label_t[run_b_len_t: (run_b_len_t + b_len_t)] == 0)
            
            # Update the running number of points 
            run_b_len_s += b_len_s
            run_b_len_t += b_len_t

            # Squared l2 distance between points points of both point clouds
            coor_s, coor_t = coor_s[mask_s, :].unsqueeze(0), coor_t[mask_t, :].unsqueeze(0)
            feat_s, feat_t = feat_s[mask_s, :].unsqueeze(0), feat_t[mask_t, :].unsqueeze(0)
            
            # Sample the points randomly (to keep the computation memory tracktable)
            # we only use self.ego_n_points(1024) from background
            idx_ego_s = torch.randperm(coor_s.shape[1])[:self.ego_n_points]
            idx_ego_t = torch.randperm(coor_t.shape[1])[:self.ego_n_points]

            coor_s_ego = coor_s[:,idx_ego_s,:]
            coor_t_ego = coor_t[:,idx_ego_t,:]
            feat_s_ego = feat_s[:,idx_ego_s,:]
            feat_t_ego = feat_t[:,idx_ego_t,:]

            # Force transport to be zero for points further than 5 m apart (10 m to 5 m from the change of orignial)
            # calculate the distance of every point from frame1 to frame2 in coordinate space, and make a mask
            support_ego = (pairwise_distance(coor_s_ego, coor_t_ego, normalized=False ) < 5 ** 2).float()

            # Cost matrix in the feature space
            # calculate the distance of every point from frame1 to frame2 in the feature space
            feat_dist = pairwise_distance(feat_s_ego, feat_t_ego)

            # sinkhorn algorithm with slack column and row to get the similarity of points from background
            # get the corresponding points from pc1 to pc2 using similarity matrix
            # use kabsch_transformation_estimation to get the rigid transformation for ego motion
            R_est, t_est, perm_matrix = self.ego_motion_decoder(feat_dist, support_ego, coor_s_ego, coor_t_ego)

            ego_motion_R_s.append(R_est[0])
            ego_motion_t_s.append(t_est[0])
            ego_motion_perm_s.append(perm_matrix[0])

            ego_motion_R_t.append(R_est[1])
            ego_motion_t_t.append(t_est[1])
            ego_motion_perm_t.append(perm_matrix[1])


        # Save ego motion results
        self.inferred_values['R_est'] = torch.cat(ego_motion_R_s, dim=0)
        self.inferred_values['t_est'] = torch.cat(ego_motion_t_s, dim=0)
        self.inferred_values['permutation'] = ego_motion_perm_s

        self.inferred_values['R_est_t'] = torch.cat(ego_motion_R_t, dim=0)
        self.inferred_values['t_est_t'] = torch.cat(ego_motion_t_t, dim=0)
        self.inferred_values['permutation_t'] = ego_motion_perm_t
##################################################################################
        

    def _infer_semantics(self, dec_f_1, dec_f_2):

        # Extract the logits
        logits_s = self.seg_decoder(dec_f_1)
        logits_t = self.seg_decoder(dec_f_2)

        # output has 2 channels, for background and foreground 
        self.inferred_values['semantic_logits_s'] = logits_s
        self.inferred_values['semantic_logits_t'] = logits_t


    def _infer_clusters(self, st_s, st_t, sem_label_s, sem_label_t):

        # Cluster the source and target point cloud (only source clusters will be used)
        running_idx_s = 0
        running_idx_t = 0
        
        clusters_s = defaultdict(list)
        clusters_t = defaultdict(list)
        
        clusters_s_rot = defaultdict(list)
        clusters_s_trans = defaultdict(list)

        batch_size = torch.max(st_s.coordinates[:,0]) + 1

        for b_idx in range(batch_size):
            # only foreground points
            b_fgrnd_idx_s = torch.where(sem_label_s[running_idx_s:(running_idx_s + st_s.C[st_s.C[:,0] == b_idx,1:].shape[0])] == 1)[0]
            b_fgrnd_idx_t = torch.where(sem_label_t[running_idx_t:(running_idx_t + st_t.C[st_t.C[:,0] == b_idx,1:].shape[0])] == 1)[0]

            coor_s = st_s.C[st_s.C[:,0] == b_idx,1:].to(self.device) * self.voxel_size
            coor_t = st_t.C[st_t.C[:,0] == b_idx,1:].to(self.device) * self.voxel_size

            # Only perform if foreground points are in both source and target
            if b_fgrnd_idx_s.shape[0] and b_fgrnd_idx_t.shape[0]:
                xyz_fgrnd_s = coor_s[b_fgrnd_idx_s, :].cpu().numpy()
                xyz_fgrnd_t = coor_t[b_fgrnd_idx_t, :].cpu().numpy()

                # Perform clustering
                labels_s = self.cluster_estimator.fit_predict(xyz_fgrnd_s)
                labels_t = self.cluster_estimator.fit_predict(xyz_fgrnd_t)
                
                # Map cluster labels to indices (consider only clusters that have at least n points)
                for class_label in np.unique(labels_s):
                    if class_label != -1 and np.where(labels_s == class_label)[0].shape[0] >= self.min_p_cluster:
                        clusters_s[str(b_idx)].append(b_fgrnd_idx_s[np.where(labels_s == class_label)[0]] + running_idx_s)

                for class_label in np.unique(labels_t):
                    if class_label != -1 and np.where(labels_t == class_label)[0].shape[0] >= self.min_p_cluster:
                        clusters_t[str(b_idx)].append(b_fgrnd_idx_t[np.where(labels_t == class_label)[0]] + running_idx_t)
            
                # Estimate the relative transformation parameteres of each cluster
                if self.test_flag:
                    for c_idx in clusters_s[str(b_idx)]:
                        # in one sample: every cluster in pc1 + its inferred flow -> the reconstructed foreground cluster in pc2
                        # so we can calculate its rigid transform
                        cluster_xyz_s = (st_s.C[c_idx,1:] * self.voxel_size).unsqueeze(0).to(self.device)
                        cluster_flow = self.inferred_values['refined_flow'][c_idx,:].unsqueeze(0)
                        reconstructed_xyz = cluster_xyz_s + cluster_flow

                        R_cluster, t_cluster, _, _ = kabsch_transformation_estimation(cluster_xyz_s, reconstructed_xyz)

                        clusters_s_rot[str(b_idx)].append(R_cluster.squeeze(0))
                        clusters_s_trans[str(b_idx)].append(t_cluster.squeeze(0))

            running_idx_s += coor_s.shape[0]
            running_idx_t += coor_t.shape[0]

        self.inferred_values['clusters_s'] = clusters_s
        self.inferred_values['clusters_t'] = clusters_t
        # only for test part available 
        self.inferred_values['clusters_s_R'] = clusters_s_rot
        self.inferred_values['clusters_s_t'] = clusters_s_trans


    # inferred_values = self.model(sinput1, sinput2, input_dict['pcd_s'], input_dict['pcd_t'], input_dict['fg_labels_s'], input_dict['fg_labels_t']) for train
    # inferred_values = self.model(sinput1, sinput2, input_dict['pcd_eval_s'], input_dict['pcd_eval_t'], input_dict['fg_labels_s'], input_dict['fg_labels_t']) for eval
    # sinput1 = ME.SparseTensor(features=input_dict['sinput_s_F'].to(self.device), coordinates=input_dict['sinput_s_C'].to(self.device))
    # xyz_2 not in use for all case, xyz_1 is used only for upsampling
    def forward(self, st_1, st_2, xyz_1, xyz_2, sem_label_s, sem_label_t):
        
        self.inferred_values = {}

        # Run both point clouds through the backbone network
        enc_feat_1, skip_features_1 = self.encoder(st_1)
        enc_feat_2, skip_features_2 = self.encoder(st_2)

        dec_feat_1 = self.decoder(enc_feat_1, skip_features_1)
        dec_feat_2 = self.decoder(enc_feat_2, skip_features_2)
        # all _infer methods use the output of decoder as dec_feat_1 and dec_feat_2

        # Rune the background segmentation head
        if self.estimate_semantic:
            self._infer_semantics(dec_feat_1, dec_feat_2)
            # https://github.com/NVIDIA/MinkowskiEngine/blob/56e026605bdf516f075056103f3a0aed4327e823/MinkowskiEngine/MinkowskiSparseTensor.py#L284 for the .F
            #  .F.max(1)[1] along the batch, get the indices of the max, 0 or 1 since the out channel from seghead is 2
            est_sem_label_s = self.inferred_values['semantic_logits_s'].F.max(1)[1]
            est_sem_label_t = self.inferred_values['semantic_logits_t'].F.max(1)[1]

        # Rune the scene flow head
        # get the flow: self.inferred_values['refined_flow'] = refined_flow.F
        if self.estimate_flow:    
            if self.loop_flow:
                self._infer_flow_loop(dec_feat_1, dec_feat_2)
            else:
                self._infer_flow(dec_feat_1, dec_feat_2)

        # Rune the ego-motion head
        if self.estimate_ego:
            # During training use the given semantic labels to sample the points
            if self.test_flag:
                if self.estimate_semantic:
                    if self.loop_ego:
                        self._infer_ego_motion_loop(dec_feat_1, dec_feat_2, est_sem_label_s, est_sem_label_t)
                    else:
                        self._infer_ego_motion(dec_feat_1, dec_feat_2, est_sem_label_s, est_sem_label_t)
                else:
                    raise ValueError("Ego motion estimation selected in test phase but background segmentation head was not used")                
            else:
                if self.loop_ego:
                    self._infer_ego_motion_loop(dec_feat_1, dec_feat_2, sem_label_s, sem_label_t)
                else:
                    self._infer_ego_motion(dec_feat_1, dec_feat_2, sem_label_s, sem_label_t)

        # Rune the foreground clustering
        if self.estimate_cluster:
            # During training use the given semantic labels
            if self.test_flag:
                if self.estimate_semantic:
                    self._infer_clusters(st_1,st_2, est_sem_label_s, est_sem_label_t)
                else:
                    raise ValueError("Foreground clustering selected in test phase but background segmentation head was not used")
            else:

                self._infer_clusters(st_1,st_2, sem_label_s, sem_label_t)



        # From rigid transformations to pointwise scene flow
        if self.test_flag and self.estimate_ego:

            coor_s = st_1.C[st_1.C[:,0] == 0,1:].to(self.device) * self.voxel_size
            coor_t = st_2.C[st_2.C[:,0] == 0,1:].to(self.device) * self.voxel_size

            # Ego-motion test-time optimization
            if self.test_flag and self.postprocess_ego:
                bckg_mask_s = (est_sem_label_s == 0).unsqueeze(0)
                bckg_mask_t = (est_sem_label_t == 0).unsqueeze(0)

                R_e, t_e = refine_ego_motion(coor_s.unsqueeze(0), coor_t.unsqueeze(0), bckg_mask_s, bckg_mask_t, self.inferred_values['R_est'], self.inferred_values['t_est'] )

                self.inferred_values['R_est'] = torch.from_numpy(R_e).to(self.device)
                self.inferred_values['t_est'] = torch.from_numpy(t_e).to(self.device)


            # Update the flow vectors of the background based on the ego motion for all points       
            xyz_1_transformed = transform_point_cloud(coor_s.to(self.device), self.inferred_values['R_est'], self.inferred_values['t_est'])
            bckg_idx = torch.where(est_sem_label_s == 0)[0]
            # Using ego motion to estimate the background flow
            self.inferred_values['refined_flow'][bckg_idx,:] = xyz_1_transformed[0,bckg_idx,:].to(self.device) - coor_s[bckg_idx,:].to(self.device)

        if self.test_flag and self.estimate_cluster:

            # Foreground test time optimization
            if self.test_flag and self.postprocess_clusters:
                fgnd_mask_t = (est_sem_label_t == 1).unsqueeze(0)

                # self.inferred_values['clusters_s']['0'], ['0'] means the first sample of the batch, and only 1 sample in a batch during test duo to config
                for idx, c_idx in enumerate(self.inferred_values['clusters_s']['0']):
                    pc_s_cluster = coor_s[c_idx,:]
                    pc_t_fgnd = coor_t[fgnd_mask_t[0],:]
                    
                    R_coarse = self.inferred_values['clusters_s_R']['0'][idx]
                    t_coarse = self.inferred_values['clusters_s_t']['0'][idx]

                    R_c, t_c = refine_cluster_motion(pc_s_cluster, pc_t_fgnd, R_coarse, t_coarse)

                    R_c = torch.from_numpy(R_c).to(self.device)
                    t_c = torch.from_numpy(t_c).to(self.device)

                    self.inferred_values['clusters_s_R']['0'][idx] = R_c
                    self.inferred_values['clusters_s_t']['0'][idx] = t_c


            # Update the flow vectors of the foreground based on the object wise rigid motion
            for idx, c_idx in enumerate(self.inferred_values['clusters_s']['0']):
                pc_s_cluster = coor_s[c_idx,:]
        
                cluster_transformed = transform_point_cloud(pc_s_cluster.to(self.device), self.inferred_values['clusters_s_R']['0'][idx], 
                                                            self.inferred_values['clusters_s_t']['0'][idx])

                # foreground flow obtained by the cluster motion
                self.inferred_values['refined_flow'][c_idx,:] = cluster_transformed.squeeze(0).to(self.device) - pc_s_cluster.to(self.device)

        # remaining points which are foreground without cluster can be estimated just in the flow part
        # so foreground cluster and background ego motion have the rigid transformation constraint

        #################################################################################
        # Upsample the flow from the voxel centers to the original points
        # training: xyz_1 in original coordinate after voxelization, other inferred value in voxel coordinate, corresponding points scale by voxel_size 
        # test: xyz_1 in original coordinate before voxelization, other inferred value in voxel coordinate, so the interpolation for getting the value for xyz_1
        
        if self.estimate_flow:
            # Finally we upsample the voxel flow to the actuall raw points 
            refined_voxel_flow = ME.SparseTensor(features=self.inferred_values['refined_flow'], 
                            coordinate_manager=dec_feat_1.coordinate_manager, 
                            coordinate_map_key=dec_feat_1.coordinate_map_key)

            # Interpolate the flow from the voxels to the continuos coordinates on the coarse level and upsample the labels
            upsampled_voxel_flow =  upsample_flow(xyz_1, refined_voxel_flow, k_value=self.upsampling_k, voxel_size=self.voxel_size)
            # self.inferred_values['refined_rigid_flow'] is upsampled
            self.inferred_values['refined_rigid_flow'] = torch.cat(upsampled_voxel_flow, dim=0)

        #####################################################################
        if self.loop_flow:
            # Finally we upsample the voxel flow to the actuall raw points 
            refined_voxel_flow_t = ME.SparseTensor(features=self.inferred_values['refined_flow_t'], 
                            coordinate_manager=dec_feat_2.coordinate_manager, 
                            coordinate_map_key=dec_feat_2.coordinate_map_key)

            # Interpolate the flow from the voxels to the continuos coordinates on the coarse level and upsample the labels
            upsampled_voxel_flow_t =  upsample_flow(xyz_2, refined_voxel_flow_t, k_value=self.upsampling_k, voxel_size=self.voxel_size)
            # self.inferred_values['refined_rigid_flow'] is upsampled
            self.inferred_values['refined_rigid_flow_t'] = torch.cat(upsampled_voxel_flow_t, dim=0)
        ###########################################################

        if self.estimate_semantic:
            upsampled_seg_labels =  upsample_bckg_labels(xyz_1, self.inferred_values['semantic_logits_s'],  voxel_size=self.voxel_size)

            self.inferred_values['semantic_logits_s_all'] = upsampled_seg_labels

        if self.estimate_cluster:

            upsampled_cluster_labels =  upsample_cluster_labels(xyz_1, self.inferred_values['semantic_logits_s'], self.inferred_values['clusters_s'],  voxel_size=self.voxel_size)

            self.inferred_values['clusters_s_all'] = upsampled_cluster_labels

        return self.inferred_values


