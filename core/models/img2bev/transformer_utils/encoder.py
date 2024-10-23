# Copyright (c) 2022-2023, NVIDIA Corporation & Affiliates. All rights reserved.
#
# This work is made available under the Nvidia Source Code License-NC.
# To view a copy of this license, visit
# https://github.com/NVlabs/VoxFormer/blob/main/LICENSE

# ---------------------------------------------
# Copyright (c) OpenMMLab. All rights reserved.
# ---------------------------------------------
#  Modified by Zhiqi Li
# ---------------------------------------------

import numpy as np
import torch
import cv2 as cv
import mmcv
import copy
import warnings
from mmcv.cnn.bricks.registry import (ATTENTION, TRANSFORMER_LAYER, TRANSFORMER_LAYER_SEQUENCE)
from mmcv.cnn.bricks.transformer import TransformerLayerSequence
from mmcv.runner import force_fp32, auto_fp16
from mmcv.utils import TORCH_VERSION, digit_version
from mmcv.utils import ext_loader
# from projects.mmdet3d_plugin.models.utils.visual import save_tensor
from .custom_base_transformer_layer import MyCustomBaseTransformerLayer

ext_module = ext_loader.load_ext(
    '_ext', ['ms_deform_attn_backward', 'ms_deform_attn_forward'])


@TRANSFORMER_LAYER_SEQUENCE.register_module()
class VoxFormerEncoder(TransformerLayerSequence):

    """
    Attention with both self and cross
    Args:
        return_intermediate (bool): Whether to return intermediate outputs.
        coder_norm_cfg (dict): Config of last normalization layer. Default：
            `LN`.
    """

    def __init__(
        self, 
        *args, 
        pc_range=None,
        data_config=None,
        num_points_in_pillar=4, 
        return_intermediate=False, 
        dataset_type='nuscenes',
        **kwargs):

        super(VoxFormerEncoder, self).__init__(*args, **kwargs)
        self.return_intermediate = return_intermediate

        self.num_points_in_pillar = num_points_in_pillar

        self.final_dim = data_config['input_size']
        self.pc_range = pc_range
        self.fp16_enabled = False

    @staticmethod
    def get_reference_points(H, W, Z=8, num_points_in_pillar=4, dim='3d', bs=1, device='cuda', dtype=torch.float):
        """Get the reference points used in DCA and DSA.
        Args:
            H, W: spatial shape of bev.
            Z: hight of pillar.
            D: sample D points uniformly from each pillar.
            device (obj:`device`): The device where
                reference_points should be.
        Returns:
            Tensor: reference points used in decoder, has \
                shape (bs, num_keys, num_levels, 2).
        """

        # reference points in 3D space, used in spatial cross-attention (SCA)
        if dim == '3d':
            zs = torch.linspace(0.5, Z - 0.5, num_points_in_pillar, dtype=dtype,
                                device=device).view(-1, 1, 1).expand(num_points_in_pillar, H, W) / Z
            xs = torch.linspace(0.5, W - 0.5, W, dtype=dtype,
                                device=device).view(1, 1, W).expand(num_points_in_pillar, H, W) / W
            ys = torch.linspace(0.5, H - 0.5, H, dtype=dtype,
                                device=device).view(1, H, 1).expand(num_points_in_pillar, H, W) / H
            ref_3d = torch.stack((xs, ys, zs), -1)
            ref_3d = ref_3d.permute(0, 3, 1, 2).flatten(2).permute(0, 2, 1)
            ref_3d = ref_3d[None].repeat(bs, 1, 1, 1)
            return ref_3d

        # reference points on 2D bev plane, used in temporal self-attention (TSA).
        elif dim == '2d':
            ref_y, ref_x = torch.meshgrid(
                torch.linspace(
                    0.5, H - 0.5, H, dtype=dtype, device=device),
                torch.linspace(
                    0.5, W - 0.5, W, dtype=dtype, device=device), indexing='ij'
            )
            ref_y = ref_y.reshape(-1)[None] / H
            ref_x = ref_x.reshape(-1)[None] / W
            ref_2d = torch.stack((ref_x, ref_y), -1)
            ref_2d = ref_2d.repeat(bs, 1, 1).unsqueeze(2)
            return ref_2d

    # This function must use fp32!!!
    @force_fp32(apply_to=('reference_points', 'img_metas'))
    def point_sampling(self, reference_points, pc_range, cam_params, img_metas=None):

        rots, trans, intrins, post_rots, post_trans, bda = cam_params
        B, num_cam, _ = trans.shape
        eps = 1e-5
        ogfH, ogfW = self.final_dim # [384, 1280]

        # [bs, 1, HWZ, 3] / [bs, D, HWZ, 3]
        reference_points[..., 0:1] = reference_points[..., 0:1] * \
            (pc_range[3] - pc_range[0]) + pc_range[0]
        reference_points[..., 1:2] = reference_points[..., 1:2] * \
            (pc_range[4] - pc_range[1]) + pc_range[1]
        reference_points[..., 2:3] = reference_points[..., 2:3] * \
            (pc_range[5] - pc_range[2]) + pc_range[2]
        
        # [bs, D, HWZ, 3] -> [D, bs, HWZ, 3]
        reference_points = reference_points.permute(1, 0, 2, 3)
        D, B, num_query = reference_points.size()[:3]

        # [D, B, num_cam, num_query, 3]
        reference_points = reference_points.view(
            D, B, 1, num_query, 3).repeat(1, 1, num_cam, 1, 1)

        if bda.shape[-1] == 4:
            # [D, B, num_cam, num_query, 4]
            reference_points = torch.cat((reference_points, torch.ones(*reference_points.shape[:-1], 1).type_as(reference_points)), dim=-1)
            reference_points = torch.inverse(bda).view(1, B, 1, 1, 4, 4).matmul(reference_points.unsqueeze(-1)).squeeze(-1)
            reference_points = reference_points[..., :3]
        else:
            reference_points = torch.inverse(bda).view(1, B, 1, 1, 3, 3).matmul(reference_points.unsqueeze(-1)).squeeze(-1)
        
        reference_points = reference_points - trans.view(1, B, num_cam, 1, 3)
        inv_rots = rots.inverse().view(1, B, num_cam, 1, 3, 3)
        reference_points = (inv_rots @ reference_points.unsqueeze(-1)).squeeze(-1)

        if intrins.shape[3] == 4:            
            reference_points = torch.cat((reference_points, torch.ones(*reference_points.shape[:-1], 1).type_as(reference_points)), dim=-1)
            reference_points_cam = (intrins.view(1, B, num_cam, 1, 4, 4) @ reference_points.unsqueeze(-1)).squeeze(-1)
        else:
            reference_points_cam = (intrins.view(1, B, num_cam, 1, 3, 3) @ reference_points.unsqueeze(-1)).squeeze(-1)
        
        points_d = reference_points_cam[..., 2:3]
        reference_points_cam = reference_points_cam[..., 0:2] / torch.maximum(
            points_d, torch.ones_like(reference_points_cam[..., 2:3]) * eps
        )

        reference_points_cam = post_rots[:, :, :2, :2].view(1, B, num_cam, 1, 2, 2) @ reference_points_cam.unsqueeze(-1)
        reference_points_cam = reference_points_cam.squeeze(-1) + post_trans[:, :, :2].view(1, B, num_cam, 1, 2)

        # [D, B, num_cam, num_query, 2]
        reference_points_cam[..., 0] /= ogfW
        reference_points_cam[..., 1] /= ogfH
        volume_mask = (points_d > eps) #只有深度值大于 1e-5 的位置才会被标记为 True
        # [D, B, num_cam, num_query, 1]
        volume_mask = (volume_mask & (reference_points_cam[..., 0:1] > eps)
                & (reference_points_cam[..., 0:1] < (1.0 - eps))
                & (reference_points_cam[..., 1:2] > eps)
                & (reference_points_cam[..., 1:2] < (1.0 - eps))
                )
        
        reference_points_cam = reference_points_cam.permute(2, 1, 3, 0, 4) # [num_cam, B, num_query, D, 2]
        volume_mask = volume_mask.permute(2, 1, 3, 0, 4).squeeze(-1) # [D, B, num_cam, num_query, 1] -> [num_cam, B, num_query, D]
        return reference_points_cam, volume_mask

    @auto_fp16()
    def forward(self,
                bev_query,
                key,
                value,
                *args,
                ref_3d=None,
                bev_h=None,
                bev_w=None,
                bev_pos=None,
                spatial_shapes=None,
                level_start_index=None,
                cam_params=None,
                valid_ratios=None,
                prev_bev=None,
                shift=0.,
                **kwargs):
        """Forward function for `TransformerEncoder`.
        Args:
            bev_query (Tensor): Input BEV query with shape
                `(num_query, bs, embed_dims)`.
            key & value (Tensor): Input multi-cameta features with shape
                (num_cam, num_value, bs, embed_dims)
            reference_points (Tensor): The reference
                points of offset. has shape
                (bs, num_query, 4) when as_two_stage,
                otherwise has shape ((bs, num_query, 2).
            valid_ratios (Tensor): The radios of valid
                points on the feature map, has shape
                (bs, num_levels, 2)
        Returns:
            Tensor: Results with shape [1, num_query, bs, embed_dims] when
                return_intermediate is `False`, otherwise it has shape
                [num_layers, num_query, bs, embed_dims].
        """

        output = bev_query
        intermediate = []

        ref_2d = self.get_reference_points(
            512, 512, dim='2d', bs=bev_query.size(1), device=bev_query.device, dtype=bev_query.dtype)

        bs, len_bev, num_bev_level, _ = ref_2d.shape

        hybird_ref_2d = torch.stack([ref_2d, ref_2d], 1).reshape(
                bs*2, len_bev, num_bev_level, 2)

        
        reference_points_cam, bev_mask = self.point_sampling(
            ref_3d, self.pc_range, cam_params=cam_params, img_metas=kwargs['img_metas'], )

        # (num_query, bs, embed_dims) -> (bs, num_query, embed_dims)
        bev_query = bev_query.permute(1, 0, 2)
        if bev_pos is not None:
            bev_pos = bev_pos.permute(1, 0, 2)

        for lid, layer in enumerate(self.layers):
            output = layer(
                bev_query,
                key,
                value,
                *args,
                bev_pos=bev_pos,
                ref_2d=hybird_ref_2d,
                ref_3d=ref_3d,
                bev_h=bev_h,
                bev_w=bev_w,
                spatial_shapes=spatial_shapes,
                level_start_index=level_start_index,
                reference_points_cam=reference_points_cam,
                bev_mask=bev_mask,
                prev_bev=prev_bev,
                **kwargs)

            bev_query = output
            if self.return_intermediate:
                intermediate.append(output)

        if self.return_intermediate:
            return torch.stack(intermediate)

        return output
    
@TRANSFORMER_LAYER_SEQUENCE.register_module()
class VoxFormerEncoder_DFA3D(VoxFormerEncoder):
    def __init__(
        self,
        *args, 
        pc_range=None,
        data_config=None,
        num_points_in_pillar=4, 
        return_intermediate=False, 
        dataset_type='nuscenes',
        d_bound=[2.0, 58.0, 0.5],
        **kwargs):
        super(VoxFormerEncoder_DFA3D, self).__init__(*args, pc_range=pc_range, data_config=data_config,
                         return_intermediate=return_intermediate, dataset_type=dataset_type, **kwargs)
        self.d_bound = d_bound
    
    def point_sampling(self, reference_points, pc_range, cam_params, img_metas=None):

        rots, trans, intrins, post_rots, post_trans, bda = cam_params
        B, num_cam, _ = trans.shape
        eps = 1e-5
        ogfH, ogfW = self.final_dim # [384, 1280]

        # [bs, 1, HWZ, 3] / [bs, D, HWZ, 3]
        reference_points[..., 0:1] = reference_points[..., 0:1] * \
            (pc_range[3] - pc_range[0]) + pc_range[0]
        reference_points[..., 1:2] = reference_points[..., 1:2] * \
            (pc_range[4] - pc_range[1]) + pc_range[1]
        reference_points[..., 2:3] = reference_points[..., 2:3] * \
            (pc_range[5] - pc_range[2]) + pc_range[2]
        
        # [bs, D, HWZ, 3] -> [D, bs, HWZ, 3]
        reference_points = reference_points.permute(1, 0, 2, 3)
        D, B, num_query = reference_points.size()[:3]

        # [D, B, num_cam, num_query, 3]
        reference_points = reference_points.view(
            D, B, 1, num_query, 3).repeat(1, 1, num_cam, 1, 1)

        if bda.shape[-1] == 4:
            # [D, B, num_cam, num_query, 4]
            reference_points = torch.cat((reference_points, torch.ones(*reference_points.shape[:-1], 1).type_as(reference_points)), dim=-1)
            reference_points = torch.inverse(bda).view(1, B, 1, 1, 4, 4).matmul(reference_points.unsqueeze(-1)).squeeze(-1)
            reference_points = reference_points[..., :3]
        else:
            reference_points = torch.inverse(bda).view(1, B, 1, 1, 3, 3).matmul(reference_points.unsqueeze(-1)).squeeze(-1)
        
        reference_points = reference_points - trans.view(1, B, num_cam, 1, 3)
        inv_rots = rots.inverse().view(1, B, num_cam, 1, 3, 3)
        reference_points = (inv_rots @ reference_points.unsqueeze(-1)).squeeze(-1)

        if intrins.shape[3] == 4:            
            reference_points = torch.cat((reference_points, torch.ones(*reference_points.shape[:-1], 1).type_as(reference_points)), dim=-1)
            reference_points_cam = (intrins.view(1, B, num_cam, 1, 4, 4) @ reference_points.unsqueeze(-1)).squeeze(-1)
        else:
            reference_points_cam = (intrins.view(1, B, num_cam, 1, 3, 3) @ reference_points.unsqueeze(-1)).squeeze(-1)
        
        points_d = reference_points_cam[..., 2:3]
        reference_points_cam[..., 0:2] = reference_points_cam[..., 0:2] / torch.maximum(
            points_d, torch.ones_like(reference_points_cam[..., 2:3]) * eps
        )

        reference_points_cam[..., 0:2] = (post_rots[:, :, :2, :2].view(1, B, num_cam, 1, 2, 2) @ reference_points_cam[..., 0:2].unsqueeze(-1)).squeeze(-1)
        reference_points_cam[..., 0:2] = reference_points_cam[..., 0:2] + post_trans[:, :, :2].view(1, B, num_cam, 1, 2)

        # [D, B, num_cam, num_query, 2]
        reference_points_cam[..., 0] /= ogfW
        reference_points_cam[..., 1] /= ogfH
        reference_points_cam[..., 2] = (reference_points_cam[..., 2] - self.d_bound[0]) / (self.d_bound[1]-self.d_bound[0])
        reference_points_cam = reference_points_cam[..., :3]
        
        volume_mask = (points_d > eps)
        # [D, B, num_cam, num_query, 1]
        volume_mask = (volume_mask & (reference_points_cam[..., 0:1] > eps)
                & (reference_points_cam[..., 0:1] < (1.0 - eps))
                & (reference_points_cam[..., 1:2] > eps)
                & (reference_points_cam[..., 1:2] < (1.0 - eps))
                )
        # print(torch.sum(volume_mask))
        reference_points_cam = reference_points_cam.permute(2, 1, 3, 0, 4) # [num_cam, B, num_query, D, 2]
        volume_mask = volume_mask.permute(2, 1, 3, 0, 4).squeeze(-1) # [D, B, num_cam, num_query, 1] -> [num_cam, B, num_query, D]
        return reference_points_cam, volume_mask

@TRANSFORMER_LAYER.register_module()
class VoxFormerLayer(MyCustomBaseTransformerLayer):
    """Implements encoder layer in DETR transformer.
    Args:
        attn_cfgs (list[`mmcv.ConfigDict`] | list[dict] | dict )):
            Configs for self_attention or cross_attention, the order
            should be consistent with it in `operation_order`. If it is
            a dict, it would be expand to the number of attention in
            `operation_order`.
        feedforward_channels (int): The hidden dimension for FFNs.
        ffn_dropout (float): Probability of an element to be zeroed
            in ffn. Default 0.0.
        operation_order (tuple[str]): The execution order of operation
            in transformer. Such as ('self_attn', 'norm', 'ffn', 'norm').
            Default：None
        act_cfg (dict): The activation config for FFNs. Default: `LN`
        norm_cfg (dict): Config dict for normalization layer.
            Default: `LN`.
        ffn_num_fcs (int): The number of fully-connected layers in FFNs.
            Default：2.
    """

    def __init__(self,
                 attn_cfgs,
                 feedforward_channels,
                 ffn_dropout=0.0,
                 operation_order=None,
                 act_cfg=dict(type='ReLU', inplace=True),
                 norm_cfg=dict(type='LN'),
                 ffn_num_fcs=2,
                 **kwargs):
        super(VoxFormerLayer, self).__init__(
            attn_cfgs=attn_cfgs,
            feedforward_channels=feedforward_channels,
            ffn_dropout=ffn_dropout,
            operation_order=operation_order,
            act_cfg=act_cfg,
            norm_cfg=norm_cfg,
            ffn_num_fcs=ffn_num_fcs,
            **kwargs)
        self.fp16_enabled = False
        # assert len(operation_order) == 6
        # assert set(operation_order) == set(
        #     ['self_attn', 'norm', 'cross_attn', 'ffn'])

    def forward(self,
                query,
                key=None,
                value=None,
                bev_pos=None,
                query_pos=None,
                key_pos=None,
                attn_masks=None,
                query_key_padding_mask=None,
                key_padding_mask=None,
                ref_2d=None,
                ref_3d=None,
                bev_h=None,
                bev_w=None,
                reference_points_cam=None,
                mask=None,
                spatial_shapes=None,
                level_start_index=None,
                prev_bev=None,
                **kwargs):
        """Forward function for `TransformerEncoderLayer`.

        **kwargs contains some specific arguments of attentions.

        Args:
            query (Tensor): The input query with shape
                [num_queries, bs, embed_dims] if
                self.batch_first is False, else
                [bs, num_queries embed_dims].
            key (Tensor): The key tensor with shape [num_keys, bs,
                embed_dims] if self.batch_first is False, else
                [bs, num_keys, embed_dims] .
            value (Tensor): The value tensor with same shape as `key`.
            query_pos (Tensor): The positional encoding for `query`.
                Default: None.
            key_pos (Tensor): The positional encoding for `key`.
                Default: None.
            attn_masks (List[Tensor] | None): 2D Tensor used in
                calculation of corresponding attention. The length of
                it should equal to the number of `attention` in
                `operation_order`. Default: None.
            query_key_padding_mask (Tensor): ByteTensor for `query`, with
                shape [bs, num_queries]. Only used in `self_attn` layer.
                Defaults to None.
            key_padding_mask (Tensor): ByteTensor for `query`, with
                shape [bs, num_keys]. Default: None.

        Returns:
            Tensor: forwarded results with shape [num_queries, bs, embed_dims].
        """

        norm_index = 0
        attn_index = 0
        ffn_index = 0
        identity = query
        if attn_masks is None:
            attn_masks = [None for _ in range(self.num_attn)]
        elif isinstance(attn_masks, torch.Tensor):
            attn_masks = [
                copy.deepcopy(attn_masks) for _ in range(self.num_attn)
            ]
            warnings.warn(f'Use same attn_mask in all attentions in '
                          f'{self.__class__.__name__} ')
        else:
            assert len(attn_masks) == self.num_attn, f'The length of ' \
                                                     f'attn_masks {len(attn_masks)} must be equal ' \
                                                     f'to the number of attention in ' \
                f'operation_order {self.num_attn}'

        for layer in self.operation_order:
            # temporal self attention
            if layer == 'self_attn':

                query = self.attentions[attn_index](
                    query,
                    prev_bev,
                    prev_bev,
                    identity if self.pre_norm else None,
                    query_pos=bev_pos,
                    key_pos=bev_pos,
                    attn_mask=attn_masks[attn_index],
                    key_padding_mask=query_key_padding_mask,
                    reference_points=ref_2d,
                    spatial_shapes=torch.tensor(
                        [[bev_h, bev_w]], device=query.device),
                    level_start_index=torch.tensor([0], device=query.device),
                    **kwargs)
                attn_index += 1
                identity = query

            elif layer == 'norm':
                query = self.norms[norm_index](query)
                norm_index += 1

            # spaital cross attention
            elif layer == 'cross_attn':
                query = self.attentions[attn_index](
                    query,
                    key,
                    value,
                    identity if self.pre_norm else None,
                    query_pos=query_pos,
                    key_pos=key_pos,
                    reference_points=ref_3d,
                    reference_points_cam=reference_points_cam,
                    mask=mask,
                    attn_mask=attn_masks[attn_index],
                    key_padding_mask=key_padding_mask,
                    spatial_shapes=spatial_shapes,
                    level_start_index=level_start_index,
                    **kwargs)
                attn_index += 1
                identity = query

            elif layer == 'ffn':
                query = self.ffns[ffn_index](
                    query, identity if self.pre_norm else None)
                ffn_index += 1

        return query
