import torch
import torch.nn as nn

from lib.utils import transform_point_cloud, kabsch_transformation_estimation
from utils.chamfer_distance import ChamferDistance

import torch.nn.functional as F
from torch.autograd import Variable


class TrainLoss(nn.Module):
    """
    Training loss consists of a ego-motion loss, background segmentation loss, and a foreground loss. 
    The l1 flow loss is used for the full supervised experiments only. 

    Args:
       args: parameters controling the initialization of the loss functions

    """

    def __init__(self, args):
        nn.Module.__init__(self)


        self.args = args
        self.device = torch.device('cuda' if (torch.cuda.is_available() and args['misc']['use_gpu']) else 'cpu') 

        # Flow loss
        self.flow_criterion = nn.L1Loss(reduction='mean')

        # Ego motion loss
        self.ego_l1_criterion = nn.L1Loss(reduction='mean')
        self.ego_outlier_criterion = OutlierLoss()
        
        # Background segmentation loss, background_loss is True not weighted
        if args['loss']['background_loss'] == 'weighted':

            # Based on the dataset analysis there are 14 times more background labels
            seg_weight = torch.tensor([1.0, 20.0]).to(self.device)
            self.seg_criterion = torch.nn.CrossEntropyLoss(weight=seg_weight, ignore_index=-1)
        
        elif args['loss']['background_loss'] == 'focal':
            self.seg_criterion = FocalLoss(gamma=2.0, alpha=0.25)

        else:
            # ignore_index=-1: https://stackoverflow.com/questions/69346001/pytorch-nllloss-ignore-index-default-value
            self.seg_criterion = torch.nn.CrossEntropyLoss(ignore_index=-1)

        # Foreground loss
        self.chamfer_criterion = ChamferDistance()
        self.rigidity_criterion = nn.L1Loss(reduction='mean')

    def __call__(self, inferred_values, gt_data):
        
        # Initialize the dictionary
        losses = {}
        
        # for weakly training, no flow_loss
        if self.args['method']['flow'] and self.args['loss']['flow_loss']:
            assert (('coarse_flow' in inferred_values) & ('flow' in gt_data)), 'Flow loss selected \
                                                                    but either est or gt flow not provided'

            losses['refined_flow_loss'] = self.flow_criterion(inferred_values['refined_flow'], 
                                                gt_data['flow']) * self.args['loss'].get('flow_loss_w', 1.0)

            losses['coarse_flow_loss'] = self.flow_criterion(inferred_values['coarse_flow'], 
                                                 gt_data['flow']) * self.args['loss'].get('flow_loss_w', 1.0)


        # for weakly training, both True
        if self.args['method']['ego_motion'] and self.args['loss']['ego_loss']:
            assert (('R_est' in inferred_values) & ('R_s_t' in gt_data) is not None), "Ego motion loss selected \
                                            but either est or gt ego motion not provided"
                                                            
            assert 'permutation' in inferred_values is not None, 'Outlier loss selected \
                                                                        but the permutation matrix is not provided'

            # Only evaluate on the background points
            mask = (gt_data['fg_labels_s'] == 0)

            prev_idx = 0
            pc_t_gt, pc_t_est = [], []

            # Iterate over the samples in the batch
            for batch_idx in range(gt_data['R_ego'].shape[0]):
                
                # Convert the voxel indices back to the coordinates
                p_s_temp = gt_data['sinput_s_C'][prev_idx: prev_idx + gt_data['len_batch'][batch_idx][0],:].to(self.device) * self.args['misc']['voxel_size']
                mask_temp = mask[prev_idx: prev_idx + gt_data['len_batch'][batch_idx][0]]

                # Transform the point cloud with gt and estimated ego-motion parameters
                # no correspondence for point from source to target at background, so target is not used
                pc_t_gt_temp = transform_point_cloud(p_s_temp[mask_temp,1:4], gt_data['R_ego'][batch_idx,:,:], gt_data['t_ego'][batch_idx,:,:])
                pc_t_est_temp = transform_point_cloud(p_s_temp[mask_temp,1:4], inferred_values['R_est'][batch_idx,:,:], inferred_values['t_est'][batch_idx,:,:])

                #######################################
                if self.args['method']['background_flow']:
                    background_flow = inferred_values['refined_flow'][prev_idx: prev_idx + gt_data['len_batch'][batch_idx][0],:]

                    pc_t_est_temp=pc_t_est_temp*0.5+0.5*(p_s_temp[mask_temp,1:4]+background_flow[mask_temp,:])
                #########################################
                
                pc_t_gt.append(pc_t_gt_temp.squeeze(0))
                pc_t_est.append(pc_t_est_temp.squeeze(0))
                
                prev_idx += gt_data['len_batch'][batch_idx][0]

            pc_t_est = torch.cat(pc_t_est, 0)
            pc_t_gt = torch.cat(pc_t_gt, 0)

            losses['ego_loss'] = self.ego_l1_criterion(pc_t_est, pc_t_gt) * self.args['loss'].get('ego_loss_w', 1.0)
            losses['outlier_loss'] = self.ego_outlier_criterion(inferred_values['permutation']) * self.args['loss'].get('inlier_loss_w', 1.0)

        ###################################################################################
        if self.args['method']['ego_motion'] and self.args['loss']['ego_loss'] and self.args['method']['loop_ego']:
            assert (('R_est' in inferred_values) & ('R_s_t' in gt_data) is not None), "Ego motion loss selected \
                                            but either est or gt ego motion not provided"
                                                            
            assert 'permutation' in inferred_values is not None, 'Outlier loss selected \
                                                                        but the permutation matrix is not provided'

            mask_t = (gt_data['fg_labels_t'] == 0)

            prev_idx = 0
            pc_s_gt, pc_s_est = [], []

            # Iterate over the samples in the batch
            for batch_idx in range(gt_data['R_ego'].shape[0]):
                
                # Convert the voxel indices back to the coordinates
                p_t_temp = gt_data['sinput_t_C'][prev_idx: prev_idx + gt_data['len_batch'][batch_idx][1],:].to(self.device) * self.args['misc']['voxel_size']
                mask_temp = mask[prev_idx: prev_idx + gt_data['len_batch'][batch_idx][1]]

                # Transform the point cloud with gt and estimated ego-motion parameters
                # no correspondence for point from source to target at background, so target is not used
                pc_s_gt_temp = transform_point_cloud(p_t_temp[mask_temp,1:4], gt_data['R_ego'][batch_idx,:,:].T, -gt_data['t_ego'][batch_idx,:,:])
                pc_s_est_temp = transform_point_cloud(p_t_temp[mask_temp,1:4], inferred_values['R_est_t'][batch_idx,:,:], inferred_values['t_est_t'][batch_idx,:,:])
                
                pc_s_gt.append(pc_s_gt_temp.squeeze(0))
                pc_s_est.append(pc_s_est_temp.squeeze(0))

            pc_s_est = torch.cat(pc_s_est, 0)
            pc_s_gt = torch.cat(pc_s_gt, 0)

            losses['ego_loss']*=0.5
            losses['outlier_loss']*=0.5
            losses['ego_loss'] += 0.5*self.ego_l1_criterion(pc_s_est, pc_s_gt) * self.args['loss'].get('ego_loss_w', 1.0)
            losses['outlier_loss'] += 0.5*self.ego_outlier_criterion(inferred_values['permutation_t']) * self.args['loss'].get('inlier_loss_w', 1.0)
        ###########################################################################################

        # Background segmentation loss
        # for weakly training, both True
        if self.args['method']['semantic'] and self.args['loss']['background_loss']:
            assert (('semantic_logits_s' in inferred_values) & ('fg_labels_s' in gt_data)), "Background loss selected but either est or gt labels not provided"
            
            semantic_loss = torch.tensor(0.0).to(self.device)

            semantic_loss += self.seg_criterion(inferred_values['semantic_logits_s'].F, gt_data['fg_labels_s']) * self.args['loss'].get('bg_loss_w', 1.0)

            # If the background labels for the target point cloud are available also use them for the loss computation
            if 'semantic_logits_t' in inferred_values:
                semantic_loss += self.seg_criterion(inferred_values['semantic_logits_t'].F, gt_data['fg_labels_t']) * self.args['loss'].get('bg_loss_w', 1.0)
                semantic_loss = semantic_loss/2

            losses['semantic_loss'] = semantic_loss

        # Foreground loss
        # for weakly training, both True
        if self.args['method']['clustering'] and self.args['loss']['foreground_loss']:
            assert ('clusters_s' in inferred_values), "Foreground loss selected but inferred cluster labels not provided"
            
            rigidity_loss = torch.tensor(0.0).to(self.device)

            xyz_s = torch.cat(gt_data['pcd_s'], 0).to(self.device)
            xyz_t = torch.cat(gt_data['pcd_t'], 0).to(self.device)

            # # Two-way chamfer distance for the foreground points (only compute if both point clouds have more than 50 foreground points)
            # if torch.where(gt_data['fg_labels_s'] == 1)[0].shape[0] > 50 and torch.where(gt_data['fg_labels_t'] == 1)[0].shape[0] > 50:

            foreground_mask_s = (gt_data['fg_labels_s'] == 1)
            foreground_mask_t = (gt_data['fg_labels_t'] == 1)

            prev_idx_s = 0
            prev_idx_t = 0
            chamfer_loss = []
            # Iterate over the samples in the batch
            for batch_idx in range(gt_data['R_ego'].shape[0]):
    
                # temp_foreground_mask for current batch, 0 or 1
                temp_foreground_mask_s = foreground_mask_s[prev_idx_s : prev_idx_s + gt_data['len_batch'][batch_idx][0]]
                temp_foreground_mask_t = foreground_mask_t[prev_idx_t : prev_idx_t + gt_data['len_batch'][batch_idx][1]]

                if torch.sum(temp_foreground_mask_s) > 50 and torch.sum(temp_foreground_mask_t) > 50:
                    foreground_xyz_s_temp = xyz_s[prev_idx_s: prev_idx_s + gt_data['len_batch'][batch_idx][0],:]
                    foreground_xyz_t_temp = xyz_t[prev_idx_t: prev_idx_t + gt_data['len_batch'][batch_idx][1],:]
                    # self.inferred_values['refined_rigid_flow'] is upsampled 
                    foreground_flow = inferred_values['refined_rigid_flow'][prev_idx_s: prev_idx_s + gt_data['len_batch'][batch_idx][0],:]

                    # obtain foreground points cloud using segmentation label， apply flow to them
                    foreground_xyz_s = foreground_xyz_s_temp[temp_foreground_mask_s,:]
                    foreground_flow = foreground_flow[temp_foreground_mask_s,:]
                    foreground_xyz_t = foreground_xyz_t_temp[temp_foreground_mask_t,:]

                    dist1, dist2 = self.chamfer_criterion(foreground_xyz_t.unsqueeze(0), (foreground_xyz_s + foreground_flow).unsqueeze(0))
                    
                    # Clamp the distance to prevent outliers (objects that appear and disappear from the scene)
                    dist1 = torch.clamp(torch.sqrt(dist1), max=1.0)
                    dist2 = torch.clamp(torch.sqrt(dist2), max=1.0)

                    #########################################################
                    if self.args['method']['loop_flow']:
                        foreground_flow_t = inferred_values['refined_rigid_flow_t'][prev_idx_t: prev_idx_t + gt_data['len_batch'][batch_idx][1],:]
                        foreground_flow_t = foreground_flow_t[temp_foreground_mask_t,:]
                        dist3, dist4 = self.chamfer_criterion(foreground_xyz_s.unsqueeze(0), (foreground_xyz_t + foreground_flow_t).unsqueeze(0))

                        dist3 = torch.clamp(torch.sqrt(dist3), max=1.0)
                        dist4 = torch.clamp(torch.sqrt(dist4), max=1.0)
                        
                        curr_chamfer_loss=(torch.mean(dist1) + torch.mean(dist2) + torch.mean(dist3) + torch.mean(dist4)) / 4.0
                        chamfer_loss.append(curr_chamfer_loss)
                    ########################################################
                    else:
                        chamfer_loss.append((torch.mean(dist1) + torch.mean(dist2)) / 2.0)

                prev_idx_s += gt_data['len_batch'][batch_idx][0]
                prev_idx_t += gt_data['len_batch'][batch_idx][1]

            # Handle the case where there are no foreground points
            if len(chamfer_loss) == 0: chamfer_loss.append(torch.tensor(0.0).to(self.device))

            losses['chamfer_loss'] = torch.mean(torch.stack(chamfer_loss)) * self.args['loss'].get('cd_loss_w', 1.0)

            # Rigidity loss (flow vectors of each cluster should be congruent)
            n_clusters = 0
            # Iterate over the clusters and enforce rigidity within each cluster
            for batch_idx in inferred_values['clusters_s']:
        
                for cluster in inferred_values['clusters_s'][batch_idx]:
                    cluster_xyz_s = xyz_s[cluster,:].unsqueeze(0)
                    cluster_flow = inferred_values['refined_rigid_flow'][cluster,:].unsqueeze(0)
                    reconstructed_xyz = cluster_xyz_s + cluster_flow

                    # Compute the unweighted Kabsch estimation (transformation parameters which best explain the vectors)
                    # obtain the rigid transformation through flow
                    R_cluster, t_cluster, _, _ = kabsch_transformation_estimation(cluster_xyz_s, reconstructed_xyz)

                    # Detach the gradients such that they do not flow through the tansformation parameters but only through flow
                    rigid_xyz = (torch.matmul(R_cluster, cluster_xyz_s.transpose(1, 2)) + t_cluster ).detach().squeeze(0).transpose(0,1)
                    
                    # apply transformation for pc1, l1 loss between transformed pc1 and pc1+flow
                    rigidity_loss += self.rigidity_criterion(reconstructed_xyz.squeeze(0), rigid_xyz)

                    n_clusters += 1

            n_clusters = 1.0 if n_clusters == 0 else n_clusters            
            losses['rigidity_loss'] = (rigidity_loss / n_clusters) * self.args['loss'].get('rigid_loss_w', 1.0)

            #############################################################################
            rigidity_loss = torch.tensor(0.0).to(self.device)
            if self.args['method']['loop_flow']:
                # Rigidity loss (flow vectors of each cluster should be congruent)
                n_clusters = 0
                # Iterate over the clusters and enforce rigidity within each cluster
                for batch_idx in inferred_values['clusters_t']:
            
                    for cluster in inferred_values['clusters_t'][batch_idx]:
                        cluster_xyz_t = xyz_t[cluster,:].unsqueeze(0)
                        cluster_flow = inferred_values['refined_rigid_flow_t'][cluster,:].unsqueeze(0)
                        reconstructed_xyz = cluster_xyz_t + cluster_flow

                        # Compute the unweighted Kabsch estimation (transformation parameters which best explain the vectors)
                        # obtain the rigid transformation through flow
                        R_cluster, t_cluster, _, _ = kabsch_transformation_estimation(cluster_xyz_t, reconstructed_xyz)

                        # Detach the gradients such that they do not flow through the tansformation parameters but only through flow
                        rigid_xyz = (torch.matmul(R_cluster, cluster_xyz_t.transpose(1, 2)) + t_cluster ).detach().squeeze(0).transpose(0,1)
                        
                        # apply transformation for pc1, l1 loss between transformed pc1 and pc1+flow
                        rigidity_loss += self.rigidity_criterion(reconstructed_xyz.squeeze(0), rigid_xyz)

                        n_clusters += 1

                n_clusters = 1.0 if n_clusters == 0 else n_clusters      
                losses['rigidity_loss']*=0.5   
                losses['rigidity_loss'] += 0.5*(rigidity_loss / n_clusters) * self.args['loss'].get('rigid_loss_w', 1.0)
            #####################################################################################

        # Compute the total loss as the sum of individual losses
        total_loss = 0.0
        for key in losses:
            total_loss += losses[key]

        losses['total_loss'] = total_loss
        return losses 






class OutlierLoss():
    """
    Outlier loss used regularize the training of the ego-motion. Aims to prevent Sinkhorn algorithm to 
    assign to much mass to the slack row and column.

    """
    def __init__(self):

        self.reduction = 'mean'

    def __call__(self, perm_matrix):

        ref_outliers_strength = []
        src_outliers_strength = []

        for batch_idx in range(len(perm_matrix)):
            ref_outliers_strength.append(1.0 - torch.sum(perm_matrix[batch_idx], dim=1))
            src_outliers_strength.append(1.0 - torch.sum(perm_matrix[batch_idx], dim=2))

        ref_outliers_strength = torch.cat(ref_outliers_strength,1)
        src_outliers_strength = torch.cat(src_outliers_strength,0)

        if self.reduction.lower() == 'mean':
            return torch.mean(ref_outliers_strength) + torch.mean(src_outliers_strength)
        
        elif self.reduction.lower() == 'none':
            return  torch.mean(ref_outliers_strength, dim=1) + \
                                             torch.mean(src_outliers_strength, dim=1)


###############################################################################################
class FocalLoss(nn.Module):
    def __init__(self, gamma=0, alpha=None):
        super(FocalLoss, self).__init__()
        self.gamma = gamma
        self.alpha = alpha

    def forward(self, input, target):
        ce_loss = torch.nn.functional.cross_entropy(input, target, reduction='none',ignore_index=-1) # important to add reduction='none' to keep per-batch-item loss
        pt = torch.exp(-ce_loss)
        focal_loss = (self.alpha * (1-pt)**self.gamma * ce_loss).mean()
        return focal_loss