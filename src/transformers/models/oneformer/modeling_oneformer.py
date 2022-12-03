# coding=utf-8
# Copyright 2022 SHI Labs and The HuggingFace Inc. team. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
""" PyTorch OneFormer model."""
import copy
import collections.abc
import math
from dataclasses import dataclass
from collections import OrderedDict
from numbers import Number
from typing import Dict, List, Optional, Tuple
import torch.nn.functional as F
import warnings
import numpy as np
import torch
from torch import Tensor, nn
from torch.autograd import Function
from torch.autograd.function import once_differentiable
import torch.distributed as dist
from torch.cuda.amp import autocast
from transformers.utils import logging
from timm.models.layers import trunc_normal_
from einops import rearrange
from ...activations import ACT2FN
from ...modeling_outputs import  BaseModelOutput
from ...modeling_utils import PreTrainedModel
from ...pytorch_utils import find_pruneable_heads_and_indices, prune_linear_layer
from ...utils import (
    ModelOutput,
    add_start_docstrings,
    add_start_docstrings_to_model_forward,
    is_scipy_available,
    is_torch_cuda_available,
    is_ninja_available,
    replace_return_docstrings,
    requires_backends,
)
from .backbone_swin_oneformer import OneFormerSwinModel, OneFormerSwinEncoder
from .backbone_dinat_oneformer import OneFormerDinatModel
from .configuration_oneformer import OneFormerConfig
from .load_custom import load_cuda_kernels
logger = logging.get_logger(__name__)


_CONFIG_FOR_DOC = "OneFormerConfig"
_CHECKPOINT_FOR_DOC = "shi-labs/oneformer_ade20k_swin_tiny"
_FEAT_EXTRACTOR_FOR_DOC = "OneFormerFeatureExtractor"

ONEFORMER_PRETRAINED_MODEL_ARCHIVE_LIST = [
    "shi-labs/oneformer_ade20k_swin_tiny",
    # See all OneFormer models at https://huggingface.co/models?filter=oneformer
]

################## Utilities #################

# Copied from transformers.models.maskformer.modeling_maskformer
if is_scipy_available():
    from scipy.optimize import linear_sum_assignment


# Copied from transformers.models.deformable_detr.modeling_deformable_detr
if is_torch_cuda_available() and is_ninja_available():
    logger.info("Loading custom CUDA kernels...")
    try:
        MultiScaleDeformableAttention = load_cuda_kernels()
    except Exception as e:
        logger.warning(f"Could not load the custom kernel for multi-scale deformable attention: {e}")
        MultiScaleDeformableAttention = None
else:
    MultiScaleDeformableAttention = None


# Copied from transformers.models.detr.modeling_detr._get_clones
def _get_clones(module, N):
    return nn.ModuleList([copy.deepcopy(module) for i in range(N)])


# Copied from transformers.models.detr.modeling_deformable_detr.ms_deform_attn_core_pytorch
def ms_deform_attn_core_pytorch(value, value_spatial_shapes, sampling_locations, attention_weights):
    # for debug and test only,
    # need to use cuda version instead
    N_, S_, M_, D_ = value.shape
    _, Lq_, M_, L_, P_, _ = sampling_locations.shape
    value_list = value.split([H_ * W_ for H_, W_ in value_spatial_shapes], dim=1)
    sampling_grids = 2 * sampling_locations - 1
    sampling_value_list = []
    for lid_, (H_, W_) in enumerate(value_spatial_shapes):
        # N_, H_*W_, M_, D_ -> N_, H_*W_, M_*D_ -> N_, M_*D_, H_*W_ -> N_*M_, D_, H_, W_
        value_l_ = value_list[lid_].flatten(2).transpose(1, 2).reshape(N_*M_, D_, H_, W_)
        # N_, Lq_, M_, P_, 2 -> N_, M_, Lq_, P_, 2 -> N_*M_, Lq_, P_, 2
        sampling_grid_l_ = sampling_grids[:, :, :, lid_].transpose(1, 2).flatten(0, 1)
        # N_*M_, D_, Lq_, P_
        sampling_value_l_ = F.grid_sample(value_l_, sampling_grid_l_,
                                          mode='bilinear', padding_mode='zeros', align_corners=False)
        sampling_value_list.append(sampling_value_l_)
    # (N_, Lq_, M_, L_, P_) -> (N_, M_, Lq_, L_, P_) -> (N_, M_, 1, Lq_, L_*P_)
    attention_weights = attention_weights.transpose(1, 2).reshape(N_*M_, 1, Lq_, L_*P_)
    output = (torch.stack(sampling_value_list, dim=-2).flatten(-2) * attention_weights).sum(-1).view(N_, M_*D_, Lq_)
    return output.transpose(1, 2).contiguous()


# Copied from transformers.models.maskformer.modeling_maskformer.dice_loss
def dice_loss(inputs: Tensor, labels: Tensor, num_masks: int) -> Tensor:
    r"""
    Compute the DICE loss, similar to generalized IOU for masks as follows:
    
    $$ \mathcal{L}_{\text{dice}(x, y) = 1 - \frac{2 * x \cap y }{x \cup y + 1}} $$

    In practice, since `labels` is a binary mask, (only 0s and 1s), dice can be computed as follow

    $$ \mathcal{L}_{\text{dice}(x, y) = 1 - \frac{2 * x * y }{x + y + 1}} $$

    Args:
        inputs (`torch.Tensor`):
            A tensor representing a mask.
        labels (`torch.Tensor`):
            A tensor with the same shape as inputs. Stores the binary classification labels for each element in inputs
            (0 for the negative class and 1 for the positive class).
        num_masks (`int`):
            The number of masks present in the current batch, used for normalization.

    Returns:
        `torch.Tensor`: The computed loss.
    """
    probs = inputs.sigmoid().flatten(1)
    numerator = 2 * (probs * labels).sum(-1)
    denominator = probs.sum(-1) + labels.sum(-1)
    loss = 1 - (numerator + 1) / (denominator + 1)
    loss = loss.sum() / num_masks
    return loss

# refactored from original implementation
def sigmoid_ce_loss(inputs: Tensor, labels: Tensor, num_masks: int) -> Tensor:
    """
    Compute the sigmoid CE Loss, same as Binary Cross Entropy with Logits loss:

    $$ \mathcal{L}_{\text{ce}(x, y) = - \left[ y \cdot \log \sigma(x) + (1 - y) \cdot \log (1 - \sigma(x)) \right] $$

    Args:
        inputs (`torch.Tensor`):
            A tensor representing a mask.
        labels (`torch.Tensor`):
            A tensor with the same shape as inputs. Stores the binary classification labels for each element in inputs
            (0 for the negative class and 1 for the positive class).
        num_masks (`int`):
            The number of masks present in the current batch, used for normalization.

    Returns:
        `torch.Tensor`: The computed loss.
    """
    loss = F.binary_cross_entropy_with_logits(inputs, labels, reduction="none")
    loss = loss.mean(1)
    loss = loss.sum() / num_masks
    return loss


# Copied from transformers.models.maskformer.modeling_maskformer.pair_wise_dice_los
def pair_wise_dice_loss(inputs: Tensor, labels: Tensor) -> Tensor:
    """
    A pair wise version of the dice loss, see `dice_loss` for usage.

    Args:
        inputs (`torch.Tensor`):
            A tensor representing a mask
        labels (`torch.Tensor`):
            A tensor with the same shape as inputs. Stores the binary classification labels for each element in inputs
            (0 for the negative class and 1 for the positive class).

    Returns:
        `torch.Tensor`: The computed loss between each pairs.
    """
    inputs = inputs.sigmoid().flatten(1)
    numerator = 2 * torch.einsum("nc,mc->nm", inputs, labels)
    # using broadcasting to get a [num_queries, NUM_CLASSES] matrix
    denominator = inputs.sum(-1)[:, None] + labels.sum(-1)[None, :]
    loss = 1 - (numerator + 1) / (denominator + 1)
    return loss


# refactored from original implementation
def pair_wise_sigmoid_ce_loss(inputs: Tensor, labels: Tensor) -> Tensor:
    r"""
    A pair wise version of the ce loss, see `sigmoid_ce_loss` for usage.

    Args:
        inputs (`torch.Tensor`):
            A tensor representing a mask
        labels (`torch.Tensor`):
            A tensor with the same shape as inputs. Stores the binary classification labels for each element in inputs
            (0 for the negative class and 1 for the positive class).

    Returns:
        `torch.Tensor`: The computed loss between each pairs.
    """
    hw = inputs.shape[1]
    pos = F.binary_cross_entropy_with_logits(
        inputs, torch.ones_like(inputs), reduction="none"
    )
    neg = F.binary_cross_entropy_with_logits(
        inputs, torch.zeros_like(inputs), reduction="none"
    )
    loss = torch.einsum("nc,mc->nm", pos, labels) + torch.einsum(
        "nc,mc->nm", neg, (1 - labels)
    )
    return loss / hw


def calculate_uncertainty(logits):
    """
    We estimate uncerainty as L1 distance between 0.0 and the logit prediction in 'logits' for the
        foreground class in `classes`.
    Args:
        logits (Tensor): A tensor of shape (R, 1, ...) for class-specific or
            class-agnostic, where R is the total number of predicted masks in all images and C is
            the number of foreground classes. The values are logits.
    Returns:
        scores (Tensor): A tensor of shape (R, 1, ...) that contains uncertainty scores with
            the most uncertain locations having the highest uncertainty score.
    """
    assert logits.shape[1] == 1
    gt_class_logits = logits.clone()
    return -(torch.abs(gt_class_logits))


def point_sample(input, point_coords, **kwargs):
    """
    A wrapper around :function:`torch.nn.functional.grid_sample` to support 3D point_coords tensors.
    Unlike :function:`torch.nn.functional.grid_sample` it assumes `point_coords` to lie inside
    [0, 1] x [0, 1] square.
    Args:
        input (Tensor): A tensor of shape (N, C, H, W) that contains features map on a H x W grid.
        point_coords (Tensor): A tensor of shape (N, P, 2) or (N, Hgrid, Wgrid, 2) that contains
        [0, 1] x [0, 1] normalized point coordinates.
    Returns:
        output (Tensor): A tensor of shape (N, C, P) or (N, C, Hgrid, Wgrid) that contains
            features for points in `point_coords`. The features are obtained via bilinear
            interplation from `input` the same way as :function:`torch.nn.functional.grid_sample`.
    """
    add_dim = False
    if point_coords.dim() == 3:
        add_dim = True
        point_coords = point_coords.unsqueeze(2)
    output = F.grid_sample(input, 2.0 * point_coords - 1.0, **kwargs)
    if add_dim:
        output = output.squeeze(3)
    return output

def get_uncertain_point_coords_with_randomness(
    coarse_logits, uncertainty_func, num_points, oversample_ratio, importance_sample_ratio
):
    """
    Sample points in [0, 1] x [0, 1] coordinate space based on their uncertainty. The unceratinties
        are calculated for each point using 'uncertainty_func' function that takes point's logit
        prediction as input.
    See PointRend paper for details.
    Args:
        coarse_logits (Tensor): A tensor of shape (N, C, Hmask, Wmask) or (N, 1, Hmask, Wmask) for
            class-specific or class-agnostic prediction.
        uncertainty_func: A function that takes a Tensor of shape (N, C, P) or (N, 1, P) that
            contains logit predictions for P points and returns their uncertainties as a Tensor of
            shape (N, 1, P).
        num_points (int): The number of points P to sample.
        oversample_ratio (int): Oversampling parameter.
        importance_sample_ratio (float): Ratio of points that are sampled via importnace sampling.
    Returns:
        point_coords (Tensor): A tensor of shape (N, P, 2) that contains the coordinates of P
            sampled points.
    """
    assert oversample_ratio >= 1
    assert importance_sample_ratio <= 1 and importance_sample_ratio >= 0
    num_boxes = coarse_logits.shape[0]
    num_sampled = int(num_points * oversample_ratio)
    point_coords = torch.rand(num_boxes, num_sampled, 2, device=coarse_logits.device)
    point_logits = point_sample(coarse_logits, point_coords, align_corners=False)
    # It is crucial to calculate uncertainty based on the sampled prediction value for the points.
    # Calculating uncertainties of the coarse predictions first and sampling them for points leads
    # to incorrect results.
    # To illustrate this: assume uncertainty_func(logits)=-abs(logits), a sampled point between
    # two coarse predictions with -1 and 1 logits has 0 logits, and therefore 0 uncertainty value.
    # However, if we calculate uncertainties for the coarse predictions first,
    # both will have -1 uncertainty, and the sampled point will get -1 uncertainty.
    point_uncertainties = uncertainty_func(point_logits)
    num_uncertain_points = int(importance_sample_ratio * num_points)
    num_random_points = num_points - num_uncertain_points
    idx = torch.topk(point_uncertainties[:, 0, :], k=num_uncertain_points, dim=1)[1]
    shift = num_sampled * torch.arange(num_boxes, dtype=torch.long, device=coarse_logits.device)
    idx += shift[:, None]
    point_coords = point_coords.view(-1, 2)[idx.view(-1), :].view(
        num_boxes, num_uncertain_points, 2
    )
    if num_random_points > 0:
        point_coords = torch.cat(
            [
                point_coords,
                torch.rand(num_boxes, num_random_points, 2, device=coarse_logits.device),
            ],
            dim=1,
        )
    return point_coords

################## Loss and Matcher Classes #################

# refactored from original implementation
class OneFormerHungarianMatcher(nn.Module):
    """This class computes an assignment between the labels and the predictions of the network.

    For efficiency reasons, the labels don't include the no_object. Because of this, in general, there are more
    predictions than labels. In this case, we do a 1-to-1 matching of the best predictions, while the others are
    un-matched (and thus treated as non-objects).
    """

    def __init__(self, cost_class: float = 1.0, cost_mask: float = 1.0, cost_dice: float = 1.0, num_points: int = 12544):
        """Creates the matcher

        Params:
            cost_class (float, *optional*, defaults to 1.0):
                This is the relative weight of the classification error in the matching cost.
            cost_mask (float, *optional*,  defaults to 1.0):
                This is the relative weight of the sigmoid ce loss of the binary mask in the matching cost.
            cost_dice (float, *optional*, defaults to 1.0):
                This is the relative weight of the dice loss of the binary mask in the matching cost
            num_points (int, *optional*, defaults to 12544):
                Number of points to be sampled for dice and mask loss matching cost.
        """
        super().__init__()
        if cost_class == 0 and cost_mask == 0 and cost_dice == 0:
            raise ValueError("All costs cant be 0")
        self.cost_class = cost_class
        self.cost_mask = cost_mask
        self.cost_dice = cost_dice
        self.num_points = num_points

    @torch.no_grad()
    def forward(self, masks_queries_logits, class_queries_logits, mask_labels, class_labels) -> List[Tuple[Tensor]]:
        """Performs the matching

        Params:
            masks_queries_logits (`torch.Tensor`):
                A tensor` of dim `batch_size, num_queries, num_labels` with the
                  classification logits.
            class_queries_logits (`torch.Tensor`):
                A tensor` of dim `batch_size, num_queries, height, width` with the
                  predicted masks.

            class_labels (`torch.Tensor`):
                A tensor` of dim `num_target_boxes` (where num_target_boxes is the number
                  of ground-truth objects in the target) containing the class labels.
            mask_labels (`torch.Tensor`):
                A tensor` of dim `num_target_boxes, height, width` containing the target
                  masks.

        Returns:
            `List[Tuple[Tensor]]`: A list of size batch_size, containing tuples of (index_i, index_j) where:
                - index_i is the indices of the selected predictions (in order)
                - index_j is the indices of the corresponding selected labels (in order)
            For each batch element, it holds:
                len(index_i) = len(index_j) = min(num_queries, num_targets).
        """
        indices: List[Tuple[np.array]] = []

        num_queries = class_queries_logits.shape[1]

        preds_masks = masks_queries_logits
        preds_probs = class_queries_logits
        # iterate through batch size
        for pred_probs, pred_mask, target_mask, labels in zip(preds_probs, preds_masks, mask_labels, class_labels):

            pred_probs = pred_probs.softmax(-1)
            # Compute the classification cost. Contrary to the loss, we don't use the NLL,
            # but approximate it in 1 - proba[target class].
            # The 1 is a constant that doesn't change the matching, it can be ommitted.
            cost_class = -pred_probs[:, labels]
            
            pred_mask = pred_mask[:, None]
            target_mask = target_mask[:, None].to(pred_mask.device)

            # all masks share the same set of points for efficient matching!
            point_coords = torch.rand(1, self.num_points, 2, device=pred_mask.device)

            # get gt labels
            target_mask = point_sample(
                target_mask,
                point_coords.repeat(target_mask.shape[0], 1, 1),
                align_corners=False,
            ).squeeze(1)

            pred_mask = point_sample(
                pred_mask,
                point_coords.repeat(pred_mask.shape[0], 1, 1),
                align_corners=False,
            ).squeeze(1)

            with autocast(enabled=False):
                pred_mask = pred_mask.float()
                target_mask = target_mask.float()

                # compute the sigmoid ce loss
                cost_mask = pair_wise_sigmoid_ce_loss(pred_mask, target_mask)
                # Compute the dice loss
                cost_dice = pair_wise_dice_loss(pred_mask, target_mask)
                # final cost matrix
                cost_matrix = (self.cost_mask * cost_mask + self.cost_class * cost_class + self.cost_dice * cost_dice)
                cost_matrix = cost_matrix.reshape(num_queries, -1).cpu()
                # do the assigmented using the hungarian algorithm in scipy
                assigned_indices: Tuple[np.array] = linear_sum_assignment(cost_matrix.cpu())
                indices.append(assigned_indices)

        # It could be stacked in one tensor
        matched_indices = [
            (torch.as_tensor(i, dtype=torch.int64), torch.as_tensor(j, dtype=torch.int64)) for i, j in indices
        ]
        return matched_indices

    def __repr__(self):
        head = "Matcher " + self.__class__.__name__
        body = [
            f"cost_class: {self.cost_class}",
            f"cost_mask: {self.cost_mask}",
            f"cost_dice: {self.cost_dice}",
        ]
        _repr_indent = 4
        lines = [head] + [" " * _repr_indent + line for line in body]
        return "\n".join(lines)


# copied and adapted from original implementation
class OneFormerLoss(nn.Module):
    def __init__(
        self,
        num_classes: int,
        matcher: OneFormerHungarianMatcher,
        weight_dict: Dict[str, float],
        eos_coef: float,
        num_points: int, 
        oversample_ratio: float, 
        importance_sample_ratio: float, 
        contrastive_temperature: float = None
    ):
        """
        The OneFormer Loss.

        Args:
            num_labels (`int`):
                The number of classes.
            matcher (`OneFormerHungarianMatcher`):
                A torch module that computes the assigments between the predictions and labels.
            weight_dict (`Dict[str, float]`):
                A dictionary of weights to be applied to the different losses.
            eos_coef (`float`):
                Weight to apply to the null class.
            num_points (`int`):
                Number of points to be sampled for dice and mask loss calculations.
            oversample_ratio (`float`):
                Required for pointwise loss calculation.
            importance_sample_ratio (`float`):
                Required for pointwise loss calculation.
            contrastive_temperature (`float`):
                Temperature for scaling the contrastive logits.
        """

        super().__init__()
        requires_backends(self, ["scipy"])
        self.num_classes = num_classes
        self.matcher = matcher
        self.weight_dict = weight_dict
        self.eos_coef = eos_coef
        empty_weight = torch.ones(self.num_classes + 1)
        empty_weight[-1] = self.eos_coef
        self.register_buffer("empty_weight", empty_weight)

        # pointwise mask loss parameters
        self.num_points = num_points
        self.oversample_ratio = oversample_ratio
        self.importance_sample_ratio = importance_sample_ratio
        self.contrastive_temperature = contrastive_temperature
        if self.contrastive_temperature is not None:
            self.logit_scale = nn.Parameter(torch.ones([]) * np.log(1 / contrastive_temperature))

    def _max_by_axis(self, the_list: List[List[int]]) -> List[int]:
        maxes = the_list[0]
        for sublist in the_list[1:]:
            for index, item in enumerate(sublist):
                maxes[index] = max(maxes[index], item)
        return maxes

    def _pad_images_to_max_in_batch(self, tensors: List[Tensor]) -> Tuple[Tensor, Tensor]:
        # get the maximum size in the batch
        max_size = self._max_by_axis([list(tensor.shape) for tensor in tensors])
        batch_size = len(tensors)
        # compute finel size
        batch_shape = [batch_size] + max_size
        b, _, h, w = batch_shape
        # get metadata
        dtype = tensors[0].dtype
        device = tensors[0].device
        padded_tensors = torch.zeros(batch_shape, dtype=dtype, device=device)
        padding_masks = torch.ones((b, h, w), dtype=torch.bool, device=device)
        # pad the tensors to the size of the biggest one
        for tensor, padded_tensor, padding_mask in zip(tensors, padded_tensors, padding_masks):
            padded_tensor[: tensor.shape[0], : tensor.shape[1], : tensor.shape[2]].copy_(tensor)
            padding_mask[: tensor.shape[1], : tensor.shape[2]] = False

        return padded_tensors, padding_masks

    def loss_contrastive(self, contrastive_queries_logits: Tensor, text_queries: Tensor):
        """Compute the query-text contrastive loss.

        Args:
            contrastive_queries_logits (`torch.Tensor`):
                A tensor of shape `batch_size, num_queries, hidden_dim`
            text_queries (`torch.Tensor`):
                A tensor of shape `batch_size, num_queries, hidden_dim`
        Returns:
            `Dict[str, Tensor]`: A dict of `torch.Tensor` containing the following key:
            - **loss_contrastive** -- The query-text contrastive loss computed using task-guided queries
                                    and text queries derived from input text list.
        """

        image_queries = contrastive_queries_logits.float()
        
        batch_size = image_queries.shape[0]

        # [B, C]
        image_queries = F.normalize(image_queries.flatten(1), dim=-1)
        text_queries = F.normalize(text_queries.flatten(1), dim=-1)

        logit_scale = torch.clamp(self.logit_scale.exp(), max=100)

        logits_per_text = torch.matmul(text_queries, image_queries.t()) * logit_scale
        logits_per_img = logits_per_text.t()

        loss_img = F.cross_entropy(logits_per_img, torch.arange(len(logits_per_img), device=logits_per_text.device))
        loss_text = F.cross_entropy(logits_per_text, torch.arange(len(logits_per_text), device=logits_per_text.device))

        loss_contrastive = loss_img + loss_text

        losses = {"loss_contrastive": loss_contrastive}
        return losses

    def loss_labels(
        self, class_queries_logits: Tensor, class_labels: List[Tensor], indices: Tuple[np.array]
    ) -> Dict[str, Tensor]:
        """Compute the losses related to the labels using cross entropy.

        Args:
            class_queries_logits (`torch.Tensor`):
                A tensor of shape `batch_size, num_queries, num_labels`
            class_labels (`List[torch.Tensor]`):
                List of class labels of shape `(labels)`.
            indices (`Tuple[np.array])`:
                The indices computed by the Hungarian matcher.

        Returns:
            `Dict[str, Tensor]`: A dict of `torch.Tensor` containing the following key:
            - **loss_cross_entropy** -- The loss computed using cross entropy on the predicted and ground truth labels.
        """

        pred_logits = class_queries_logits
        batch_size, num_queries, _ = pred_logits.shape
        criterion = nn.CrossEntropyLoss(weight=self.empty_weight)
        idx = self._get_predictions_permutation_indices(indices)
        # shape = (batch_size, num_queries)
        target_classes_o = torch.cat([target[j] for target, (_, j) in zip(class_labels, indices)])
        # shape = (batch_size, num_queries)
        target_classes = torch.full(
            (batch_size, num_queries), fill_value=self.num_classes, dtype=torch.int64, device=pred_logits.device
        )
        target_classes[idx] = target_classes_o
        # target_classes is a (batch_size, num_labels, num_queries), we need to permute pred_logits "b q c -> b c q"
        pred_logits_transposed = pred_logits.transpose(1, 2)
        loss_ce = criterion(pred_logits_transposed, target_classes)
        losses = {"loss_cross_entropy": loss_ce}
        return losses

    def loss_masks(
        self, masks_queries_logits: Tensor, mask_labels: List[Tensor], indices: Tuple[np.array], num_masks: int
    ) -> Dict[str, Tensor]:
        """Compute the losses related to the masks using focal and dice loss.

        Args:
            masks_queries_logits (`torch.Tensor`):
                A tensor of shape `batch_size, num_queries, height, width`
            mask_labels (`torch.Tensor`):
                List of mask labels of shape `(labels, height, width)`.
            indices (`Tuple[np.array])`:
                The indices computed by the Hungarian matcher.
            num_masks (`int)`:
                The number of masks, used for normalization.

        Returns:
            `Dict[str, Tensor]`: A dict of `torch.Tensor` containing two keys:
            - **loss_mask** -- The loss computed using sigmoid ce loss on the predicted and ground truth masks.
            - **loss_dice** -- The loss computed using dice loss on the predicted on the predicted and ground truth
              masks.
        """
        src_idx = self._get_predictions_permutation_indices(indices)
        tgt_idx = self._get_targets_permutation_indices(indices)
        # shape (batch_size * num_queries, height, width)
        pred_masks = masks_queries_logits[src_idx]
        # shape (batch_size, num_queries, height, width)
        # pad all and stack the targets to the num_labels dimension
        target_masks, _ = self._pad_images_to_max_in_batch(mask_labels)
        target_masks = target_masks[tgt_idx]
        
        pred_masks = pred_masks[:, None]
        target_masks = target_masks[:, None]

        with torch.no_grad():
            # sample point_coords
            point_coords = get_uncertain_point_coords_with_randomness(
                pred_masks,
                lambda logits: calculate_uncertainty(logits),
                self.num_points,
                self.oversample_ratio,
                self.importance_sample_ratio,
            )
            # get gt labels
            point_labels = point_sample(
                target_masks,
                point_coords,
                align_corners=False,
            ).squeeze(1)

        point_logits = point_sample(
            pred_masks,
            point_coords,
            align_corners=False,
        ).squeeze(1)

        losses = {
            "loss_mask": sigmoid_ce_loss(point_logits, point_labels, num_masks),
            "loss_dice": dice_loss(point_logits, point_labels, num_masks),
        }

        del pred_masks
        del target_masks
        return losses

    def _get_predictions_permutation_indices(self, indices):
        # permute predictions following indices
        batch_indices = torch.cat([torch.full_like(src, i) for i, (src, _) in enumerate(indices)])
        predictions_indices = torch.cat([src for (src, _) in indices])
        return batch_indices, predictions_indices

    def _get_targets_permutation_indices(self, indices):
        # permute labels following indices
        batch_indices = torch.cat([torch.full_like(tgt, i) for i, (_, tgt) in enumerate(indices)])
        target_indices = torch.cat([tgt for (_, tgt) in indices])
        return batch_indices, target_indices

    def forward(
        self,
        masks_queries_logits: Tensor,
        class_queries_logits: Tensor,
        contrastive_queries_logits: Tensor,
        mask_labels: List[Tensor],
        class_labels: List[Tensor],
        text_queries: Tensor,
        auxiliary_predictions: Optional[Dict[str, Tensor]] = None,
        calculate_contrastive_loss: bool = True,
    ) -> Dict[str, Tensor]:
        """
        This performs the loss computation.

        Args:
            masks_queries_logits (`torch.Tensor`):
                A tensor of shape `batch_size, num_queries, height, width`
            class_queries_logits (`torch.Tensor`):
                A tensor of shape `batch_size, num_queries, num_labels`
            contrastive_queries_logits (`torch.Tensor`):
                A tensor of shape `batch_size, num_queries, hidden_dim`
            mask_labels (`torch.Tensor`):
                List of mask labels of shape `(labels, height, width)`.
            class_labels (`List[torch.Tensor]`):
                List of class labels of shape `(labels)`.
            text_queries (`torch.Tensor`):
                A tensor of shape `batch_size, num_queries, hidden_dim`
            auxiliary_predictions (`Dict[str, torch.Tensor]`, *optional*):
                if `use_auxiliary_loss` was set to `true` in [`MaskFormerConfig`], then it contains the logits from the
                inner layers of the Detr's Decoder.
            calculate_contrastive_loss (`bool`, *optional*, defaults to `True`):
                Whether or not to calculate the contrastive loss.

        Returns:
            `Dict[str, Tensor]`: A dict of `torch.Tensor` containing two keys:
            - **loss_cross_entropy** -- The loss computed using cross entropy on the predicted and ground truth labels.
            - **loss_mask** -- The loss computed using sigmoid ce loss on the predicted and ground truth masks.
            - **loss_dice** -- The loss computed using dice loss on the predicted on the predicted and ground truth
              masks.
            - **loss_contrastive** -- The query-text contrstive loss computed using object and text queries.
            if `use_auxiliary_loss` was set to `true` in [`OneFormerConfig`], the dictionary contains addional losses
            for each auxiliary predictions.
        """
        
        # retrieve the matching between the outputs of the last layer and the labels
        indices = self.matcher(masks_queries_logits, class_queries_logits, mask_labels, class_labels)
        # compute the average number of target masks for normalization purposes
        num_masks: Number = self.get_num_masks(class_labels, device=class_labels[0].device)
        # get all the losses
        losses: Dict[str, Tensor] = {
            **self.loss_masks(masks_queries_logits, mask_labels, indices, num_masks),
            **self.loss_labels(class_queries_logits, class_labels, indices),
        }
        if calculate_contrastive_loss:
            losses = {**losses, **self.loss_contrastive(contrastive_queries_logits, text_queries)}

        # in case of auxiliary losses, we repeat this process with the output of each intermediate layer.
        if auxiliary_predictions is not None:
            for idx, aux_outputs in enumerate(auxiliary_predictions):
                masks_queries_logits = aux_outputs["masks_queries_logits"]
                class_queries_logits = aux_outputs["class_queries_logits"]
                loss_dict = self.forward(masks_queries_logits, class_queries_logits, None, mask_labels, class_labels, None, calculate_contrastive_loss=False)
                loss_dict = {f"{key}_{idx}": value for key, value in loss_dict.items()}
                losses.update(loss_dict)

        return losses

    def get_num_masks(self, class_labels: torch.Tensor, device: torch.device) -> torch.Tensor:
        """
        Computes the average number of target masks across the batch, for normalization purposes.
        """
        num_masks = sum([len(classes) for classes in class_labels])
        num_masks_pt = torch.as_tensor([num_masks], dtype=torch.float, device=device)
        return num_masks_pt
    

################## Data Classes #################

@dataclass    
class OneFormerTransformerDecoderOutput(BaseModelOutput):
    """
    Base class for outputs of the Transformer decoder. This class adds attributes for class predictions, 
    mask predictions and contrastive logits to BaseModelOutputWithCrossAttentions.
    Args:
        object_logits (`torch.FloatTensor` of shape `(batch_size, num_queries, hidden_dim)`):
            Queries representation for the region proposals.
        contrastive_logits (`torch.FloatTensor` of shape `(batch_size, num_queries, hidden_dim)`):
            Queries representation for the contrastive loss.
        prediction_masks (`torch.FloatTensor` of shape `(batch_size, num_queries, height, width)`):
            Mask predictions from last layer of the transformer decoder.
        prediction_class (`torch.FloatTensor` of shape `(batch_size, num_queries, num_classes+1)`):
            Class predictions from last layer of the transformer decoder.
        auxiliary_predictions (Tuple of Dict of `str, torch.FloatTensor`, *optional*):
            Tuple of class and mask predictions from each layer of the transformer decoder.
    """

    object_queries: torch.FloatTensor = None
    contrastive_logits: Optional[torch.FloatTensor] = None
    prediction_masks: torch.FloatTensor = None
    prediction_class: torch.FloatTensor = None
    auxiliary_predictions: Optional[Tuple[Dict[str, torch.FloatTensor]]] = None


@dataclass
class OneFormerPixelDecoderOutput(ModelOutput):
    """
    OneFormer's pixel decoder module output, practically a MSDeformAttn based decoder. It returns the mask features
    and the multiscale features.

    Args:
        multi_scale_features (List of `torch.FloatTensor` of shape `(batch_size, num_channels, height, width)`):
            Multiscale features of scales [1/8, 1/16, 1/32] from the MSDeformAttn based Pixel Decoder.
        mask_features (`torch.FloatTensor`, of shape `(batch_size, num_channels, height, width)`):
            1/4 scale features from the last Pixel Decoder Layer.
        attentions (`tuple(torch.FloatTensor)`, *optional*, returned when `output_attentions=True` is passed or when `config.output_attentions=True`):
            Tuple of `torch.FloatTensor` (one for each layer) of shape `(batch_size, num_heads, sequence_length,
            sequence_length)`. Attentions weights from pixel decoder.
    """
    multi_scale_features: List[torch.FloatTensor] = None
    mask_features: torch.FloatTensor = None
    attentions: Optional[Tuple[torch.FloatTensor]] = None


# Copied from transformers.models.maskformer.modeling_maskformer.MaskFormerPixelLevelModuleOutput with Mask->One
@dataclass
class OneFormerPixelLevelModuleOutput(ModelOutput):
    """
    OneFormer's pixel level module output. It returns both the last and (optionally) the hidden states from the
    `encoder` and `decoder`. By default, the `encoder` is a Swin/Dinat Backbone and the `decoder` is a MSDeformAttn based decoder.

    Args:
        encoder_features (List of `(torch.FloatTensor)`):
            List of `torch.FloatTensor` of shape `(batch_size, num_channels, height, width)`. 
            Hidden-states (also called feature maps) of the model at the output of each stage.
        decoder_features (List of `(torch.FloatTensor)`):
            List of `torch.FloatTensor` of shape `(batch_size, num_channels, height, width)`. 
            Hidden-states (also called feature maps) of the model at the output of each stage.
        decoder_last_feature (`torch.FloatTensor` of shape `(batch_size, num_channels, height, width)):
            1/4 scale features from the last Pixel Decoder Layer.
    """

    encoder_features: List[torch.FloatTensor] = None
    decoder_features: List[torch.FloatTensor] = None
    decoder_last_feature: torch.FloatTensor = None

    
@dataclass
class OneFormerModelOutput(ModelOutput):
    """
    Class for outputs of [`OneFormerModel`]. This class returns all the needed hidden states to compute the logits.

    Args:
        encoder_hidden_states (`tuple(torch.FloatTensor)`, *optional*, returned when `output_hidden_states=True` is passed or when `config.output_hidden_states=True`):
            Tuple of `torch.FloatTensor` (one for the output of the embeddings + one for the output of each stage) of
            shape `(batch_size, num_channels, height, width)`. Hidden-states (also called feature maps) of the encoder
            model at the output of each stage.
        pixel_decoder_hidden_states (`tuple(torch.FloatTensor)`, *optional*, returned when `output_hidden_states=True` is passed or when `config.output_hidden_states=True`):
            Tuple of `torch.FloatTensor` (one for the output of the embeddings + one for the output of each stage) of
            shape `(batch_size, num_channels, height, width)`. Hidden-states (also called feature maps) of the pixel
            decoder model at the output of each stage.
        transformer_decoder_hidden_states (`tuple(torch.FloatTensor)`, *optional*, returned when `output_hidden_states=True` is passed or when `config.output_hidden_states=True`):
            Tuple of `torch.FloatTensor` (one for the output of the embeddings + one for the output of each stage) of
            shape `(batch_size, sequence_length, hidden_size)`. Hidden-states (also called feature maps) of the
            transformer decoder at the output of each stage.
        transformer_decoder_object_queries (`torch.FloatTensor` of shape `(batch_size, num_queries, hidden_dim)`)
            Output object queries from the last layer in the transformer decoder.
        transformer_decoder_contrastive_queries (`torch.FloatTensor` of shape `(batch_size, num_queries, hidden_dim)`)
            Contrastive queries from the transformer decoder.
        transformer_decoder_mask_predictions (`torch.FloatTensor` of shape `(batch_size, num_queries, height, width)`)
            Mask Predictions from the last layer in the transformer decoder.
        transformer_decoder_class_predictions (`torch.FloatTensor` of shape `(batch_size, num_queries, num_classes+1)`)
            Class Predictions from the last layer in the transformer decoder.
        transformer_decoder_auxiliary_predictions (Tuple of Dict of `str, torch.FloatTensor`, *optional*):
            Tuple of class and mask predictions from each layer of the transformer decoder.
        text_queries (`torch.FloatTensor`, *optional* of shape `(batch_size, num_queries, hidden_dim)`)
            Text queries derived from the input text list used for calculating contrastive loss during training.
        task_token (`torch.FloatTensor` of shape `(batch_size, hidden_dim)`)
            1D task token to condition the queries.
        attentions (`tuple(tuple(torch.FloatTensor))`, *optional*, returned when `output_attentions=True` is passed or when `config.output_attentions=True`):
            Tuple of `tuple(torch.FloatTensor)` (one for each layer) of shape `(batch_size, num_heads, sequence_length,
            sequence_length)`. Self and Cross Attentions weights from transformer decoder.
    """

    encoder_hidden_states: Optional[Tuple[torch.FloatTensor]] = None
    pixel_decoder_hidden_states: Optional[Tuple[torch.FloatTensor]] = None
    transformer_decoder_hidden_states: Optional[torch.FloatTensor] = None
    transformer_decoder_object_queries: torch.FloatTensor = None
    transformer_decoder_contrastive_queries: Optional[torch.FloatTensor] = None
    transformer_decoder_mask_predictions: torch.FloatTensor = None
    transformer_decoder_class_predictions: torch.FloatTensor = None
    transformer_decoder_auxiliary_predictions: Optional[Tuple[Dict[str, torch.FloatTensor]]] = None
    text_queries: Optional[torch.FloatTensor] = None
    task_token: torch.FloatTensor = None
    attentions: Optional[Tuple[torch.FloatTensor]] = None

dataclass
class OneFormerForUniversalSegmentationOutput(ModelOutput):
    """
    Class for outputs of [`OneFormerForUniversalSegmentationOutput`].

    This output can be directly passed to [`~OneFormerFeatureExtractor.post_process_semantic_segmentation`] or
    [`~OneFormerFeatureExtractor.post_process_instance_segmentation`] or
    [`~OneFormerFeatureExtractor.post_process_panoptic_segmentation`] depending on the task. Please, see
    [`~OneFormerFeatureExtractor] for details regarding usage.

    Args:
        loss (`torch.Tensor`, *optional*):
            The computed loss, returned when labels are present.
        class_queries_logits (`torch.FloatTensor`):
            A tensor of shape `(batch_size, num_queries, num_labels + 1)` representing the proposed classes for each
            query. Note the `+ 1` is needed because we incorporate the null class.
        masks_queries_logits (`torch.FloatTensor`):
            A tensor of shape `(batch_size, num_queries, height, width)` representing the proposed masks for each
            query.
        auxiliary_predictions (List of Dict of `str, torch.FloatTensor`, *optional*):
            List of class and mask predictions from each layer of the transformer decoder.
        encoder_hidden_states (`tuple(torch.FloatTensor)`, *optional*, returned when `output_hidden_states=True` is passed or when `config.output_hidden_states=True`):
            Tuple of `torch.FloatTensor` (one for the output of the embeddings + one for the output of each stage) of
            shape `(batch_size, num_channels, height, width)`. Hidden-states (also called feature maps) of the encoder
            model at the output of each stage.
        pixel_decoder_hidden_states (`tuple(torch.FloatTensor)`, *optional*, returned when `output_hidden_states=True` is passed or when `config.output_hidden_states=True`):
            Tuple of `torch.FloatTensor` (one for the output of the embeddings + one for the output of each stage) of
            shape `(batch_size, num_channels, height, width)`. Hidden-states (also called feature maps) of the pixel
            decoder model at the output of each stage.
        transformer_decoder_hidden_states (`tuple(torch.FloatTensor)`, *optional*, returned when `output_hidden_states=True` is passed or when `config.output_hidden_states=True`):
            Tuple of `torch.FloatTensor` (one for the output of the embeddings + one for the output of each stage) of
            shape `(batch_size, sequence_length, hidden_size)`. Hidden-states (also called feature maps) of the
            transformer decoder at the output of each stage.
        transformer_decoder_object_queries (`torch.FloatTensor` of shape `(batch_size, num_queries, hidden_dim)`)
            Output object queries from the last layer in the transformer decoder.
        transformer_decoder_contrastive_queries (`torch.FloatTensor` of shape `(batch_size, num_queries, hidden_dim)`)
            Contrastive queries from the transformer decoder.
        transformer_decoder_mask_predictions (`torch.FloatTensor` of shape `(batch_size, num_queries, height, width)`)
            Mask Predictions from the last layer in the transformer decoder.
        transformer_decoder_class_predictions (`torch.FloatTensor` of shape `(batch_size, num_queries, num_classes+1)`)
            Class Predictions from the last layer in the transformer decoder.
        transformer_decoder_auxiliary_predictions (List of Dict of `str, torch.FloatTensor`, *optional*):
            List of class and mask predictions from each layer of the transformer decoder.
        text_queries (`torch.FloatTensor`, *optional* of shape `(batch_size, num_queries, hidden_dim)`)
            Text queries derived from the input text list used for calculating contrastive loss during training.
        task_token (`torch.FloatTensor` of shape `(batch_size, hidden_dim)`)
            1D task token to condition the queries.
        attentions (`tuple(tuple(torch.FloatTensor))`, *optional*, returned when `output_attentions=True` is passed or when `config.output_attentions=True`):
            Tuple of `tuple(torch.FloatTensor)` (one for each layer) of shape `(batch_size, num_heads, sequence_length,
            sequence_length)`. Self and Cross Attentions weights from transformer decoder.
    """

    loss: Optional[torch.FloatTensor] = None
    class_queries_logits: torch.FloatTensor = None
    masks_queries_logits: torch.FloatTensor = None
    auxiliary_predictions: List[Dict[str, torch.FloatTensor]] = None
    encoder_hidden_states: Optional[Tuple[torch.FloatTensor]] = None
    pixel_decoder_hidden_states: Optional[List[torch.FloatTensor]] = None
    transformer_decoder_hidden_states: Optional[torch.FloatTensor] = None
    transformer_decoder_object_queries: torch.FloatTensor = None
    transformer_decoder_contrastive_queries: Optional[torch.FloatTensor] = None
    transformer_decoder_mask_predictions: torch.FloatTensor = None
    transformer_decoder_class_predictions: torch.FloatTensor = None
    transformer_decoder_auxiliary_predictions: Optional[List[Dict[str, torch.FloatTensor]]] = None
    text_queries: Optional[torch.FloatTensor] = None
    task_token: torch.FloatTensor = None
    attentions: Optional[Tuple[Tuple[torch.FloatTensor]]] = None


################## Dinat Backbone Classes #################

# Copied from transformers.models.maskformer.modeling_maskformer_swin.MaskFormerSwinTransformerBackbone with Mask->One    
class OneFormerDinatBackbone(nn.Module):
    """
    This class uses [`OneFormerDinatModel`] to reshape its `hidden_states` from (`batch_size, sequence_length,
    hidden_size)` output four level of features of varying resolutions: (`batch_size, num_channels, height, width)`).

    Args:
        config (`OneFormerConfig`):
            The configuration used by [`OneFormerDinatModel`].
    """

    def __init__(self, config: OneFormerConfig):
        super().__init__()
        self.model = OneFormerDinatModel(config)
        self.outputs_shapes = config.backbone_config["feature_channels"]
        self.hidden_states_norms = nn.ModuleList([nn.LayerNorm(out_shape) for out_shape in self.outputs_shapes])

    def forward(self, *args, **kwargs) -> List[Tensor]:
        output = self.model(*args, **kwargs, output_hidden_states=True)
        hidden_states_permuted: List[Tensor] = []
        hidden_states: Tuple[Tuple[Tensor]] = output.hidden_states[1:]
        # spatial dimensions contains all the heights and widths of each stage, including after the embeddings
        for i, hidden_state in enumerate(hidden_states):
            norm = self.hidden_states_norms[i]
            # the last element corespond to the layer's last block output but before patch merging
            batch_size, hidden_size, height, width = hidden_state.shape
            hidden_state_unpolled = hidden_state.permute(0, 2, 3, 1).view((batch_size, -1, hidden_size))
            hidden_state_norm = norm(hidden_state_unpolled)
            # reshape our tensor "b (h w) d -> b d h w"
            hidden_state_permuted = (
                hidden_state_norm.permute(0, 2, 1).view((batch_size, hidden_size, height, width)).contiguous()
            )
            hidden_states_permuted.append(hidden_state_permuted)
        return hidden_states_permuted

################## Swin Backbone Classes #################

# Copied from transformers.models.maskformer.modeling_maskformer_swin.MaskFormerSwinTransformerBackbone with Mask->One    
class OneFormerSwinTransformerBackbone(nn.Module):
    """
    This class uses [`OneFormerSwinModel`] to reshape its `hidden_states` from (`batch_size, sequence_length,
    hidden_size)` output four level of features of varying resolutions: (`batch_size, num_channels, height, width)`).

    Args:
        config (`OneFormerConfig`):
            The configuration used by [`OneFormerSwinModel`].
    """

    def __init__(self, config: OneFormerConfig):
        super().__init__()
        self.model = OneFormerSwinModel(config)
        self.hidden_states_norms = nn.ModuleList([nn.LayerNorm(out_shape) for out_shape in self.outputs_shapes])

    def forward(self, *args, **kwargs) -> List[Tensor]:
        output = self.model(*args, **kwargs, output_hidden_states=True)
        hidden_states_permuted: List[Tensor] = []
        # we need to reshape the hidden state to their original spatial dimensions
        # skipping the embeddings
        hidden_states: Tuple[Tuple[Tensor]] = output.hidden_states[1:]
        # spatial dimensions contains all the heights and widths of each stage, including after the embeddings
        spatial_dimensions: Tuple[Tuple[int, int]] = output.hidden_states_spatial_dimensions
        for i, (hidden_state, (height, width)) in enumerate(zip(hidden_states, spatial_dimensions)):
            norm = self.hidden_states_norms[i]
            # the last element corespond to the layer's last block output but before patch merging
            hidden_state_unpolled = hidden_state[-1]
            hidden_state_norm = norm(hidden_state_unpolled)
            # our pixel decoder (FPN) expect 3D tensors (features)
            batch_size, _, hidden_size = hidden_state_norm.shape
            # reshape our tensor "b (h w) d -> b d h w"
            hidden_state_permuted = (
                hidden_state_norm.permute(0, 2, 1).view((batch_size, hidden_size, height, width)).contiguous()
            )
            hidden_states_permuted.append(hidden_state_permuted)
        return hidden_states_permuted

    @property
    def input_resolutions(self) -> List[int]:
        return [layer.input_resolution for layer in self.model.encoder.layers]

    @property
    def outputs_shapes(self) -> List[int]:
        return [layer.dim for layer in self.model.encoder.layers]

################## Pixel Decoder Classes #################

# Copied from transformers.models.maskformer.modeling_deformable_detr.MultiScaleDeformableAttentionFunction
class MultiScaleDeformableAttentionFunction(Function):
    @staticmethod
    def forward(
        context,
        value,
        value_spatial_shapes,
        value_level_start_index,
        sampling_locations,
        attention_weights,
        im2col_step,
    ):
        context.im2col_step = im2col_step
        output = MultiScaleDeformableAttention.ms_deform_attn_forward(
            value,
            value_spatial_shapes,
            value_level_start_index,
            sampling_locations,
            attention_weights,
            context.im2col_step,
        )
        context.save_for_backward(
            value, value_spatial_shapes, value_level_start_index, sampling_locations, attention_weights
        )
        return output

    @staticmethod
    @once_differentiable
    def backward(context, grad_output):
        (
            value,
            value_spatial_shapes,
            value_level_start_index,
            sampling_locations,
            attention_weights,
        ) = context.saved_tensors
        grad_value, grad_sampling_loc, grad_attn_weight = MultiScaleDeformableAttention.ms_deform_attn_backward(
            value,
            value_spatial_shapes,
            value_level_start_index,
            sampling_locations,
            attention_weights,
            grad_output,
            context.im2col_step,
        )

        return grad_value, None, None, grad_sampling_loc, grad_attn_weight, None


# Copied from transformers.models.detr.modeling_deformable_detr.DeformableDetrFrozenBatchNorm2d with DeformableDetr->OneFormerPixelDecoder
class OneFormerPixelDecoderFrozenBatchNorm2d(nn.Module):
    """
    BatchNorm2d where the batch statistics and the affine parameters are fixed.

    Copy-paste from torchvision.misc.ops with added eps before rqsrt, without which any other models than
    torchvision.models.resnet[18,34,50,101] produce nans.
    """

    def __init__(self, n):
        super().__init__()
        self.register_buffer("weight", torch.ones(n))
        self.register_buffer("bias", torch.zeros(n))
        self.register_buffer("running_mean", torch.zeros(n))
        self.register_buffer("running_var", torch.ones(n))

    def _load_from_state_dict(
        self, state_dict, prefix, local_metadata, strict, missing_keys, unexpected_keys, error_msgs
    ):
        num_batches_tracked_key = prefix + "num_batches_tracked"
        if num_batches_tracked_key in state_dict:
            del state_dict[num_batches_tracked_key]

        super()._load_from_state_dict(
            state_dict, prefix, local_metadata, strict, missing_keys, unexpected_keys, error_msgs
        )

    def forward(self, x):
        # move reshapes to the beginning
        # to make it user-friendly
        weight = self.weight.reshape(1, -1, 1, 1)
        bias = self.bias.reshape(1, -1, 1, 1)
        running_var = self.running_var.reshape(1, -1, 1, 1)
        running_mean = self.running_mean.reshape(1, -1, 1, 1)
        epsilon = 1e-5
        scale = weight * (running_var + epsilon).rsqrt()
        bias = bias - running_mean * scale
        return x * scale + bias


# Modified from transformers.models.detr.modeling_deformable_detr.DeformableDetrMultiscaleDeformableAttention with DeformableDetr->OneFormerPixelDecoderEncoder
class OneFormerPixelDecoderEncoderMultiscaleDeformableAttention(nn.Module):
    """
    Multiscale deformable attention as proposed in Deformable DETR.
    """

    def __init__(self, embed_dim: int, num_heads: int, n_levels: int, n_points: int):
        super().__init__()
        if embed_dim % num_heads != 0:
            raise ValueError(
                f"embed_dim (d_model) must be divisible by num_heads, but got {embed_dim} and {num_heads}"
            )
        dim_per_head = embed_dim // num_heads
        # check if dim_per_head is power of 2
        if not ((dim_per_head & (dim_per_head - 1) == 0) and dim_per_head != 0):
            warnings.warn(
                "You'd better set embed_dim (d_model) in DeformableDetrMultiscaleDeformableAttention to make the"
                " dimension of each attention head a power of 2 which is more efficient in the authors' CUDA"
                " implementation."
            )

        self.im2col_step = 128

        self.d_model = embed_dim
        self.n_levels = n_levels
        self.n_heads = num_heads
        self.n_points = n_points

        self.sampling_offsets = nn.Linear(embed_dim, num_heads * n_levels * n_points * 2)
        self.attention_weights = nn.Linear(embed_dim, num_heads * n_levels * n_points)
        self.value_proj = nn.Linear(embed_dim, embed_dim)
        self.output_proj = nn.Linear(embed_dim, embed_dim)

        self._reset_parameters()

    def _reset_parameters(self):
        nn.init.constant_(self.sampling_offsets.weight.data, 0.0)
        thetas = torch.arange(self.n_heads, dtype=torch.float32) * (2.0 * math.pi / self.n_heads)
        grid_init = torch.stack([thetas.cos(), thetas.sin()], -1)
        grid_init = (
            (grid_init / grid_init.abs().max(-1, keepdim=True)[0])
            .view(self.n_heads, 1, 1, 2)
            .repeat(1, self.n_levels, self.n_points, 1)
        )
        for i in range(self.n_points):
            grid_init[:, :, i, :] *= i + 1
        with torch.no_grad():
            self.sampling_offsets.bias = nn.Parameter(grid_init.view(-1))
        nn.init.constant_(self.attention_weights.weight.data, 0.0)
        nn.init.constant_(self.attention_weights.bias.data, 0.0)
        nn.init.xavier_uniform_(self.value_proj.weight.data)
        nn.init.constant_(self.value_proj.bias.data, 0.0)
        nn.init.xavier_uniform_(self.output_proj.weight.data)
        nn.init.constant_(self.output_proj.bias.data, 0.0)

    def with_pos_embed(self, tensor: torch.Tensor, position_embeddings: Optional[Tensor]):
        return tensor if position_embeddings is None else tensor + position_embeddings

    def forward(
        self,
        hidden_states: torch.Tensor,
        attention_mask: Optional[torch.Tensor] = None,
        encoder_hidden_states=None,
        encoder_attention_mask=None,
        position_embeddings: Optional[torch.Tensor] = None,
        reference_points=None,
        spatial_shapes=None,
        level_start_index=None,
        output_attentions: bool = False,
    ):
        # add position embeddings to the hidden states before projecting to queries and keys
        if position_embeddings is not None:
            hidden_states = self.with_pos_embed(hidden_states, position_embeddings)

        batch_size, num_queries, _ = hidden_states.shape
        batch_size, sequence_length, _ = encoder_hidden_states.shape
        if (spatial_shapes[:, 0] * spatial_shapes[:, 1]).sum() != sequence_length:
            raise ValueError(
                "Make sure to align the spatial shapes with the sequence length of the encoder hidden states"
            )

        value = self.value_proj(encoder_hidden_states)
        if attention_mask is not None:
            # we invert the attention_mask
            value = value.masked_fill(attention_mask[..., None], float(0))
        value = value.view(batch_size, sequence_length, self.n_heads, self.d_model // self.n_heads)
        sampling_offsets = self.sampling_offsets(hidden_states).view(
            batch_size, num_queries, self.n_heads, self.n_levels, self.n_points, 2
        )
        attention_weights = self.attention_weights(hidden_states).view(
            batch_size, num_queries, self.n_heads, self.n_levels * self.n_points
        )
        attention_weights = F.softmax(attention_weights, -1).view(
            batch_size, num_queries, self.n_heads, self.n_levels, self.n_points
        )
        # batch_size, num_queries, n_heads, n_levels, n_points, 2
        if reference_points.shape[-1] == 2:
            offset_normalizer = torch.stack([spatial_shapes[..., 1], spatial_shapes[..., 0]], -1)
            sampling_locations = (
                reference_points[:, :, None, :, None, :]
                + sampling_offsets / offset_normalizer[None, None, None, :, None, :]
            )
        elif reference_points.shape[-1] == 4:
            sampling_locations = (
                reference_points[:, :, None, :, None, :2]
                + sampling_offsets / self.n_points * reference_points[:, :, None, :, None, 2:] * 0.5
            )
        else:
            raise ValueError(f"Last dim of reference_points must be 2 or 4, but got {reference_points.shape[-1]}")
        try:
            # GPU
            output = MultiScaleDeformableAttentionFunction.apply(
                value,
                spatial_shapes,
                level_start_index,
                sampling_locations,
                attention_weights,
                self.im2col_step,
            )
        except Exception:
            # CPU
            output = ms_deform_attn_core_pytorch(value, spatial_shapes, sampling_locations, attention_weights)
        output = self.output_proj(output)

        return output, attention_weights


# Copied from transformers.models.detr.modeling_deformable_detr.DeformableDetrEncoderLayer with DeformableDetrEncoder->OneFormerPixelDecoder
class OneFormerPixelDecoderEncoderLayer(nn.Module):
    def __init__(self, config: OneFormerConfig):
        super().__init__()
        self.embed_dim = config.decoder_config["conv_dim"]
        self.self_attn = OneFormerPixelDecoderEncoderMultiscaleDeformableAttention(
            embed_dim=self.embed_dim,
            num_heads=config.decoder_config["num_heads"],
            n_levels=3,
            n_points=4,
        )

        self.self_attn_layer_norm = nn.LayerNorm(self.embed_dim)
        self.dropout = config.decoder_config["dropout"]
        self.activation_fn = F.relu
        self.activation_dropout = config.decoder_config["dropout"]
        self.fc1 = nn.Linear(self.embed_dim, config.decoder_config["encoder_feedforward_dim"])
        self.fc2 = nn.Linear(config.decoder_config["encoder_feedforward_dim"], self.embed_dim)
        self.final_layer_norm = nn.LayerNorm(self.embed_dim)

        self.training = config.general_config["is_train"]

    def forward(
        self,
        hidden_states: torch.Tensor,
        attention_mask: torch.Tensor,
        position_embeddings: torch.Tensor = None,
        reference_points=None,
        spatial_shapes=None,
        level_start_index=None,
        output_attentions: bool = False,
    ):
        """
        Args:
            hidden_states (`torch.FloatTensor` of shape `(batch_size, sequence_length, hidden_size)`):
                Input to the layer.
            attention_mask (`torch.FloatTensor` of shape `(batch_size, sequence_length)`):
                Attention mask.
            position_embeddings (`torch.FloatTensor`, *optional*):
                Position embeddings, to be added to `hidden_states`.
            reference_points (`torch.FloatTensor`, *optional*):
                Reference points.
            spatial_shapes (`torch.LongTensor`, *optional*):
                Spatial shapes of the backbone feature maps.
            level_start_index (`torch.LongTensor`, *optional*):
                Level start index.
            output_attentions (`bool`, *optional*):
                Whether or not to return the attentions tensors of all attention layers. See `attentions` under
                returned tensors for more detail.
        """
        residual = hidden_states

        # Apply Multi-scale Deformable Attention Module on the multi-scale feature maps.
        hidden_states, attn_weights = self.self_attn(
            hidden_states=hidden_states,
            attention_mask=attention_mask,
            encoder_hidden_states=hidden_states,
            encoder_attention_mask=attention_mask,
            position_embeddings=position_embeddings,
            reference_points=reference_points,
            spatial_shapes=spatial_shapes,
            level_start_index=level_start_index,
            output_attentions=output_attentions,
        )

        hidden_states = F.dropout(hidden_states, p=self.dropout, training=self.training)
        hidden_states = residual + hidden_states
        hidden_states = self.self_attn_layer_norm(hidden_states)

        residual = hidden_states
        hidden_states = self.activation_fn(self.fc1(hidden_states))
        hidden_states = F.dropout(hidden_states, p=self.activation_dropout, training=self.training)

        hidden_states = self.fc2(hidden_states)
        hidden_states = F.dropout(hidden_states, p=self.dropout, training=self.training)

        hidden_states = residual + hidden_states
        hidden_states = self.final_layer_norm(hidden_states)

        if self.training:
            if torch.isinf(hidden_states).any() or torch.isnan(hidden_states).any():
                clamp_value = torch.finfo(hidden_states.dtype).max - 1000
                hidden_states = torch.clamp(hidden_states, min=-clamp_value, max=clamp_value)

        outputs = (hidden_states,)

        if output_attentions:
            outputs += (attn_weights,)

        return outputs


# Modified from from transformers.models.detr.modeling_deformable_detr.DeformableDetrEncoder with DeformableDetrEncoder->OneFormerPixelDecoderEncoderOnly
class OneFormerPixelDecoderEncoderOnly(nn.Module):
    """
    Transformer encoder consisting of *config.decoder_config["encoder_layers"]* deformable attention layers. Each layer is a
    [`OneFormerPixelDecoderEncoderLayer`].

    The encoder updates the flattened multi-scale feature maps through multiple deformable attention layers.

    Args:
        config: OneFormerConfig
    """

    def __init__(self, config: OneFormerConfig):
        super().__init__()

        self.config = config
        self.dropout = config.decoder_config["dropout"]
        self.layers = nn.ModuleList([OneFormerPixelDecoderEncoderLayer(config) for _ in range(config.decoder_config["encoder_layers"])])

        self._reset_parameters()

    def _reset_parameters(self):
        for p in self.parameters():
            if p.dim() > 1:
                nn.init.xavier_uniform_(p)
        for module in self.modules():
            if isinstance(module, MultiScaleDeformableAttentionFunction):
                module._reset_parameters()

    @staticmethod
    def get_reference_points(spatial_shapes, valid_ratios, device):
        """
        Get reference points for each feature map. Used in decoder.

        Args:
            spatial_shapes (`torch.LongTensor` of shape `(num_feature_levels, 2)`):
                Spatial shapes of each feature map.
            valid_ratios (`torch.FloatTensor` of shape `(batch_size, num_feature_levels, 2)`):
                Valid ratios of each feature map.
            device (`torch.device`):
                Device on which to create the tensors.
        Returns:
            `torch.FloatTensor` of shape `(batch_size, num_queries, num_feature_levels, 2)`
        """
        reference_points_list = []
        for lvl, (H_, W_) in enumerate(spatial_shapes):

            ref_y, ref_x = torch.meshgrid(torch.linspace(0.5, H_ - 0.5, H_, dtype=torch.float32, device=device),
                                          torch.linspace(0.5, W_ - 0.5, W_, dtype=torch.float32, device=device))
            ref_y = ref_y.reshape(-1)[None] / (valid_ratios[:, None, lvl, 1] * H_)
            ref_x = ref_x.reshape(-1)[None] / (valid_ratios[:, None, lvl, 0] * W_)
            ref = torch.stack((ref_x, ref_y), -1)
            reference_points_list.append(ref)
        reference_points = torch.cat(reference_points_list, 1)
        reference_points = reference_points[:, :, None] * valid_ratios[:, None]
        return reference_points
        

    def forward(
        self,
        inputs_embeds=None,
        attention_mask=None,
        position_embeddings=None,
        spatial_shapes=None,
        level_start_index=None,
        valid_ratios=None,
        output_attentions=None,
        output_hidden_states=None,
        return_dict=None,
    ):
        r"""
        Args:
            inputs_embeds (`torch.FloatTensor` of shape `(batch_size, sequence_length, hidden_size)`):
                Flattened feature map (output of the backbone + projection layer) that is passed to the encoder.
            attention_mask (`torch.Tensor` of shape `(batch_size, sequence_length)`, *optional*):
                Mask to avoid performing attention on padding pixel features. Mask values selected in `[0, 1]`:
                - 1 for pixel features that are real (i.e. **not masked**),
                - 0 for pixel features that are padding (i.e. **masked**).
                [What are attention masks?](../glossary#attention-mask)
            position_embeddings (`torch.FloatTensor` of shape `(batch_size, sequence_length, hidden_size)`):
                Position embeddings that are added to the queries and keys in each self-attention layer.
            spatial_shapes (`torch.LongTensor` of shape `(num_feature_levels, 2)`):
                Spatial shapes of each feature map.
            level_start_index (`torch.LongTensor` of shape `(num_feature_levels)`):
                Starting index of each feature map.
            valid_ratios (`torch.FloatTensor` of shape `(batch_size, num_feature_levels, 2)`):
                Ratio of valid area in each feature level.
            output_attentions (`bool`, *optional*):
                Whether or not to return the attentions tensors of all attention layers. See `attentions` under
                returned tensors for more detail.
            output_hidden_states (`bool`, *optional*):
                Whether or not to return the hidden states of all layers. See `hidden_states` under returned tensors
                for more detail.
            return_dict (`bool`, *optional*):
                Whether or not to return a [`~file_utils.ModelOutput`] instead of a plain tuple.
        """
        output_attentions = output_attentions if output_attentions is not None else self.config.output_attentions
        output_hidden_states = (
            output_hidden_states if output_hidden_states is not None else self.config.output_hidden_states
        )
        return_dict = return_dict if return_dict is not None else self.config.use_return_dict

        hidden_states = inputs_embeds
        reference_points = self.get_reference_points(spatial_shapes, valid_ratios, device=inputs_embeds.device)

        encoder_states = () if output_hidden_states else None
        all_attentions = () if output_attentions else None
        for i, encoder_layer in enumerate(self.layers):
            if output_hidden_states:
                encoder_states = encoder_states + (hidden_states,)
            layer_outputs = encoder_layer(
                hidden_states,
                attention_mask,
                position_embeddings=position_embeddings,
                reference_points=reference_points,
                spatial_shapes=spatial_shapes,
                level_start_index=level_start_index,
                output_attentions=output_attentions,
            )

            hidden_states = layer_outputs[0]

            if output_attentions:
                all_attentions = all_attentions + (layer_outputs[1],)

        if output_hidden_states:
            encoder_states = encoder_states + (hidden_states,)
        
        return BaseModelOutput(
            last_hidden_state=hidden_states, hidden_states=encoder_states, attentions=all_attentions
        )



# Modified from from transformers.models.detr.modeling_deformable_detr.DeformableDetrModel with DeformableDetrModel->OneFormerPixelDecoder    
class OneFormerPixelDecoder(nn.Module):
    def __init__(self, config: OneFormerConfig):
        super().__init__()

        self.config = config

        #  positional encoding
        self.position_embedding = OneFormerSinePositionEmbedding(num_pos_feats=config.decoder_config["conv_dim"] // 2, normalize=True)
        self.num_feature_levels = 3
        transformer_in_channels = config.backbone_config["feature_channels"][-self.num_feature_levels:]
        self.transformer_feature_strides = config.backbone_config["strides"][-self.num_feature_levels:]
        self.feature_channels = config.backbone_config["feature_channels"]
        self.level_embed = nn.Parameter(torch.Tensor(self.num_feature_levels, config.decoder_config["conv_dim"]))

        # Create input projection layers
        if self.num_feature_levels > 1:
            input_projections_list = []
            for in_channels in transformer_in_channels[::-1]:
                input_projections_list.append(
                    nn.Sequential(
                        nn.Conv2d(in_channels, config.decoder_config["conv_dim"], kernel_size=1),
                        nn.GroupNorm(32, config.decoder_config["conv_dim"]),
                    )
                )
            self.input_projections = nn.ModuleList(input_projections_list)
        else:
            self.input_projections = nn.ModuleList(
                [
                    nn.Sequential(
                        nn.Conv2d(transformer_in_channels[-1], config.decoder_config["conv_dim"], kernel_size=1),
                        nn.GroupNorm(32, config.decoder_config["conv_dim"]),
                    )
                ]
            )

        self.encoder = OneFormerPixelDecoderEncoderOnly(config)

        self.mask_projection = nn.Conv2d(
            config.decoder_config["conv_dim"],
            config.decoder_config["mask_dim"],
            kernel_size=1,
            stride=1,
            padding=0,
        )
        
        self.common_stride = config.decoder_config["common_stride"]

        # extra fpn levels
        stride = min(self.transformer_feature_strides)
        self.num_fpn_levels = int(np.log2(stride) - np.log2(self.common_stride))

        lateral_convs = []
        output_convs = []

        for idx, in_channels in enumerate(self.feature_channels[:self.num_fpn_levels]):

            lateral_conv = nn.Sequential(
                    nn.Conv2d(
                    in_channels, 
                    config.decoder_config["conv_dim"], 
                    kernel_size=1, 
                    bias=False,
                ),
                    nn.GroupNorm(32, config.decoder_config["conv_dim"]),
            )
            output_conv = nn.Sequential(
                    nn.Conv2d(
                    config.decoder_config["conv_dim"],
                    config.decoder_config["conv_dim"],
                    kernel_size=3,
                    stride=1,
                    padding=1,
                    bias=False,
                ),
                    nn.GroupNorm(32, config.decoder_config["conv_dim"]),
                    nn.ReLU(),
            )
            self.add_module("adapter_{}".format(idx + 1), lateral_conv)
            self.add_module("layer_{}".format(idx + 1), output_conv)

            lateral_convs.append(lateral_conv)
            output_convs.append(output_conv)
        # Place convs into top-down order (from low to high resolution)
        # to make the top-down computation in forward clearer.
        self.lateral_convs = lateral_convs[::-1]
        self.output_convs = output_convs[::-1]
        
        self._reset_parameters()

    def _reset_parameters(self):
        for p in self.parameters():
            if p.dim() > 1:
                nn.init.xavier_uniform_(p)
        for m in self.modules():
            if isinstance(m, MultiScaleDeformableAttentionFunction):
                m._reset_parameters()
        nn.init.normal_(self.level_embed, std=0)

    def get_valid_ratio(self, mask):
        """Get the valid ratio of all feature maps."""

        _, height, width = mask.shape
        valid_height = torch.sum(~mask[:, :, 0], 1)
        valid_width = torch.sum(~mask[:, 0, :], 1)
        valid_ratio_heigth = valid_height.float() / height
        valid_ratio_width = valid_width.float() / width
        valid_ratio = torch.stack([valid_ratio_width, valid_ratio_heigth], -1)
        return valid_ratio

    def forward(
        self,
        features,
        encoder_outputs=None,
        output_attentions=None,
        output_hidden_states=None,
        return_dict=None,
    ):
        output_attentions = output_attentions if output_attentions is not None else self.config.output_attentions
        output_hidden_states = (
            output_hidden_states if output_hidden_states is not None else self.config.output_hidden_states
        )

        # Then, apply 1x1 convolution to reduce the channel dimension to d_model (256 by default)
        sources = []
        position_embeddings_list = []
        for level, source in enumerate(features[::-1][:self.num_feature_levels]):
            x = source.float()
            sources.append(self.input_projections[level](x))
            position_embeddings_list.append(self.position_embedding(x))

        masks = [torch.zeros((x.size(0), x.size(2), x.size(3)), device=x.device, dtype=torch.bool) for x in sources]
        
        # Prepare encoder inputs (by flattening)
        source_flatten = []
        mask_flatten = []
        lvl_pos_embed_flatten = []
        spatial_shapes = []
        for level, (source, mask, pos_embed) in enumerate(zip(sources, masks, position_embeddings_list)):
            batch_size, num_channels, height, width = source.shape
            spatial_shape = (height, width)
            spatial_shapes.append(spatial_shape)
            source = source.flatten(2).transpose(1, 2)
            mask = mask.flatten(1)
            pos_embed = pos_embed.flatten(2).transpose(1, 2)
            lvl_pos_embed = pos_embed + self.level_embed[level].view(1, 1, -1)
            lvl_pos_embed_flatten.append(lvl_pos_embed)
            source_flatten.append(source)
            mask_flatten.append(mask)
        source_flatten = torch.cat(source_flatten, 1)
        mask_flatten = torch.cat(mask_flatten, 1)
        lvl_pos_embed_flatten = torch.cat(lvl_pos_embed_flatten, 1)
        spatial_shapes = torch.as_tensor(spatial_shapes, dtype=torch.long, device=source_flatten.device)
        level_start_index = torch.cat((spatial_shapes.new_zeros((1,)), spatial_shapes.prod(1).cumsum(0)[:-1]))
        valid_ratios = torch.stack([self.get_valid_ratio(m) for m in masks], 1)
        valid_ratios = valid_ratios.float()

        # Fourth, sent source_flatten + mask_flatten + lvl_pos_embed_flatten (backbone + proj layer output) through encoder
        # Also provide spatial_shapes, level_start_index and valid_ratios
        if encoder_outputs is None:
            encoder_outputs = self.encoder(
                inputs_embeds=source_flatten,
                attention_mask=mask_flatten,
                position_embeddings=lvl_pos_embed_flatten,
                spatial_shapes=spatial_shapes,
                level_start_index=level_start_index,
                valid_ratios=valid_ratios,
                output_attentions=output_attentions,
                output_hidden_states=output_hidden_states,
                return_dict=return_dict,
            )
        
        y = encoder_outputs.last_hidden_state
        bs = y.shape[0]

        split_size_or_sections = [None] * self.num_feature_levels
        for i in range(self.num_feature_levels):
            if i < self.num_feature_levels - 1:
                split_size_or_sections[i] = level_start_index[i + 1] - level_start_index[i]
            else:
                split_size_or_sections[i] = y.shape[1] - level_start_index[i]
        y = torch.split(y, split_size_or_sections, dim=1)

        out = []
        multi_scale_features = []
        num_cur_levels = 0
        for i, z in enumerate(y):
            out.append(z.transpose(1, 2).view(bs, -1, spatial_shapes[i][0], spatial_shapes[i][1]))

        # append `out` with extra FPN levels
        # Reverse feature maps into top-down order (from low to high resolution)
        for idx, x in enumerate(features[:self.num_fpn_levels][::-1]):
            x = x.float()
            lateral_conv = self.lateral_convs[idx]
            output_conv = self.output_convs[idx]
            cur_fpn = lateral_conv(x)
            # Following FPN implementation, we use nearest upsampling here
            y = cur_fpn + F.interpolate(out[-1], size=cur_fpn.shape[-2:], mode="bilinear", align_corners=False)
            y = output_conv(y)
            out.append(y)

        for o in out:
            if num_cur_levels < self.num_feature_levels:
                multi_scale_features.append(o)
                num_cur_levels += 1

        return OneFormerPixelDecoderOutput(
            mask_features=self.mask_projection(out[-1]), multi_scale_features=multi_scale_features, attentions=encoder_outputs.attentions
        )


# Copied from transformers.models.maskformer.modeling_maskformer.MaskFormerPixelLevelModule with Mask->One    
class OneFormerPixelLevelModule(nn.Module):
    def __init__(self, config: OneFormerConfig):
        """
        It runs the input image through a backbone and a pixel decoder, returning mask_features and multi_scale_features 
        to be fed into the transformer decoder.
        
        Args:
            config ([`OneFormerConfig`]):
                The configuration used to instantiate this model.
        """
        super().__init__()
        if config.general_config["backbone_type"] == "swin":
            self.encoder = OneFormerSwinTransformerBackbone(config)
        else:
            self.encoder = OneFormerDinatBackbone(config)
        self.decoder = OneFormerPixelDecoder(config)

    def forward(self, pixel_values: Tensor, output_hidden_states: bool = False) -> OneFormerPixelLevelModuleOutput:
        features: List[Tensor] = self.encoder(pixel_values)
        decoder_output: OneFormerPixelDecoderOutput = self.decoder(features, output_hidden_states=output_hidden_states)
        return OneFormerPixelLevelModuleOutput(
            encoder_features=tuple(features),
            decoder_features=decoder_output.multi_scale_features,
            decoder_last_feature=decoder_output.mask_features,
        )

################## Transformer Decoder Classes #################


# Modified from transformers.models.detr.modeling_detr.DetrAttention with Detr->OneFormer
class OneFormerAttention(nn.Module):
    """
    Multi-headed attention from 'Attention Is All You Need' paper.
    Here, we add position embeddings to the queries and keys (as explained in the DETR paper).
    """

    def __init__(
        self,
        embed_dim: int,
        num_heads: int,
        dropout: float = 0.0,
        is_decoder: bool = False,
        bias: bool = True,
    ):
        super().__init__()
        self.embed_dim = embed_dim
        self.num_heads = num_heads
        self.dropout = dropout
        self.head_dim = embed_dim // num_heads
        if self.head_dim * num_heads != self.embed_dim:
            raise ValueError(
                f"embed_dim must be divisible by num_heads (got `embed_dim`: {self.embed_dim} and `num_heads`:"
                f" {num_heads})."
            )
        self.scaling = self.head_dim**-0.5

        self.k_proj = nn.Linear(embed_dim, embed_dim, bias=bias)
        self.v_proj = nn.Linear(embed_dim, embed_dim, bias=bias)
        self.q_proj = nn.Linear(embed_dim, embed_dim, bias=bias)
        self.out_proj = nn.Linear(embed_dim, embed_dim, bias=bias)

    def _shape(self, tensor: torch.Tensor, seq_len: int, batch_size: int):
        return tensor.view(batch_size, seq_len, self.num_heads, self.head_dim).transpose(1, 2).contiguous()

    def with_pos_embed(self, tensor: torch.Tensor, position_embeddings: Optional[Tensor]):
        return tensor if position_embeddings is None else tensor + position_embeddings

    def forward(
        self,
        hidden_states: torch.Tensor,
        attention_mask: Optional[torch.Tensor] = None,
        position_embeddings: Optional[torch.Tensor] = None,
        key_value_states: Optional[torch.Tensor] = None,
        key_value_position_embeddings: Optional[torch.Tensor] = None,
        output_attentions: bool = False,
    ) -> Tuple[torch.Tensor, Optional[torch.Tensor], Optional[Tuple[torch.Tensor]]]:
        """Input shape: Batch x Time x Channel"""

        
        hidden_states = hidden_states.permute(1, 0, 2) if hidden_states is not None else None
        position_embeddings = position_embeddings.permute(1, 0, 2) if position_embeddings is not None else None
        key_value_states = key_value_states.permute(1, 0, 2) if key_value_states is not None else None
        key_value_position_embeddings = key_value_position_embeddings.permute(1, 0, 2) if key_value_position_embeddings is not None else None
        
        # if key_value_states are provided this layer is used as a cross-attention layer
        # for the decoder
        is_cross_attention = key_value_states is not None
        batch_size, target_len, embed_dim = hidden_states.size()

        # add position embeddings to the hidden states before projecting to queries and keys
        if position_embeddings is not None:
            hidden_states_original = hidden_states
            hidden_states = self.with_pos_embed(hidden_states, position_embeddings)

        # add key-value position embeddings to the key value states
        if key_value_position_embeddings is not None:
            key_value_states_original = key_value_states
            key_value_states = self.with_pos_embed(key_value_states, key_value_position_embeddings)

        # get query proj
        query_states = self.q_proj(hidden_states) * self.scaling
        # get key, value proj
        if is_cross_attention:
            # cross_attentions
            key_states = self._shape(self.k_proj(key_value_states), -1, batch_size)
            value_states = self._shape(self.v_proj(key_value_states_original), -1, batch_size)
        else:
            # self_attention
            key_states = self._shape(self.k_proj(hidden_states), -1, batch_size)
            value_states = self._shape(self.v_proj(hidden_states_original), -1, batch_size)

        proj_shape = (batch_size * self.num_heads, -1, self.head_dim)
        query_states = self._shape(query_states, target_len, batch_size).view(*proj_shape)
        key_states = key_states.view(*proj_shape)
        value_states = value_states.view(*proj_shape)

        source_len = key_states.size(1)

        attn_weights = torch.bmm(query_states, key_states.transpose(1, 2))

        if attn_weights.size() != (batch_size * self.num_heads, target_len, source_len):
            raise ValueError(
                f"Attention weights should be of size {(batch_size * self.num_heads, target_len, source_len)}, but is"
                f" {attn_weights.size()}"
            )
        

        if attention_mask is not None:
            if attention_mask.size() != (batch_size * self.num_heads, target_len, source_len):
                raise ValueError(
                    f"Attention mask should be of size {(target_len, batch_size * self.num_heads, source_len)}, but is"
                    f" {attention_mask.size()}"
                )
            attn_weights += attention_mask

        attn_weights = nn.functional.softmax(attn_weights, dim=-1)

        if output_attentions:
            # this operation is a bit awkward, but it's required to
            # make sure that attn_weights keeps its gradient.
            # In order to do so, attn_weights have to reshaped
            # twice and have to be reused in the following
            attn_weights_reshaped = attn_weights.view(batch_size, self.num_heads, target_len, source_len)
            attn_weights = attn_weights_reshaped.view(batch_size * self.num_heads, target_len, source_len)
        else:
            attn_weights_reshaped = None

        attn_probs = nn.functional.dropout(attn_weights, p=self.dropout, training=self.training)

        attn_output = torch.bmm(attn_probs, value_states)

        if attn_output.size() != (batch_size * self.num_heads, target_len, self.head_dim):
            raise ValueError(
                f"`attn_output` should be of size {(batch_size, self.num_heads, target_len, self.head_dim)}, but is"
                f" {attn_output.size()}"
            )

        attn_output = attn_output.view(batch_size, self.num_heads, target_len, self.head_dim)
        attn_output = attn_output.transpose(1, 2)
        attn_output = attn_output.reshape(batch_size, target_len, embed_dim)

        attn_output = self.out_proj(attn_output).permute(1, 0, 2)

        return attn_output, attn_weights_reshaped

class OneFormerTransformerDecoderSelfAttentionLayer(nn.Module):

    def __init__(self, embed_dim, num_heads, dropout=0.0,
                 activation="relu", normalize_before=False):
        super().__init__()
        self.self_attn = OneFormerAttention(embed_dim=embed_dim, 
                                num_heads=num_heads, 
                                dropout=dropout, 
                                is_decoder=True)

        self.norm = nn.LayerNorm(embed_dim)
        self.dropout = nn.Dropout(dropout)

        self.activation = ACT2FN[activation]
        self.normalize_before = normalize_before

        self._reset_parameters()
    
    def _reset_parameters(self):
        for p in self.parameters():
            if p.dim() > 1:
                nn.init.xavier_uniform_(p)

    def with_pos_embed(self, tensor, pos: Optional[Tensor]):
        return tensor if pos is None else tensor + pos

    def forward_post(self, output,
                     output_mask: Optional[Tensor] = None,
                     output_key_padding_mask: Optional[Tensor] = None,
                     query_pos: Optional[Tensor] = None):
        output2, attention_weights = self.self_attn(
                                            hidden_states=output,
                                            position_embeddings=query_pos,
                                            attention_mask=output_mask,
                                            output_attentions=True
                                        )
        output = output + self.dropout(output2)
        output = self.norm(output)

        return output, attention_weights

    def forward_pre(self, output,
                    output_mask: Optional[Tensor] = None,
                    output_key_padding_mask: Optional[Tensor] = None,
                    query_pos: Optional[Tensor] = None):
        output2 = self.norm(output)
        output2, attention_weights = self.self_attn(
                                        hidden_states=output2,
                                        position_embeddings=query_pos,
                                        attention_mask=output_mask,
                                        output_attentions=True
                                    )
        output = output + self.dropout(output2)
        
        return output, attention_weights

    def forward(self, output,
                output_mask: Optional[Tensor] = None,
                output_key_padding_mask: Optional[Tensor] = None,
                query_pos: Optional[Tensor] = None):
        if self.normalize_before:
            return self.forward_pre(output, output_mask,
                                    output_key_padding_mask, query_pos)
        return self.forward_post(output, output_mask,
                                 output_key_padding_mask, query_pos)


class OneFormerTransformerDecoderCrossAttentionLayer(nn.Module):

    def __init__(self, embed_dim, num_heads, dropout=0.0,
                 activation="relu", normalize_before=False):
        super().__init__()
        self.multihead_attn = nn.MultiheadAttention(embed_dim, num_heads, dropout=dropout)

        self.norm = nn.LayerNorm(embed_dim)
        self.dropout = nn.Dropout(dropout)

        self.activation = ACT2FN[activation]
        self.normalize_before = normalize_before

        self._reset_parameters()
    
    def _reset_parameters(self):
        for p in self.parameters():
            if p.dim() > 1:
                nn.init.xavier_uniform_(p)

    def with_pos_embed(self, tensor, pos: Optional[Tensor]):
        return tensor if pos is None else tensor + pos

    def forward_post(self, output, memory,
                     memory_mask: Optional[Tensor] = None,
                     memory_key_padding_mask: Optional[Tensor] = None,
                     pos: Optional[Tensor] = None,
                     query_pos: Optional[Tensor] = None):
        output2, attention_weights = self.multihead_attn(query=self.with_pos_embed(output, query_pos),
                                   key=self.with_pos_embed(memory, pos),
                                   value=memory, attn_mask=memory_mask,
                                   key_padding_mask=memory_key_padding_mask)
        output = output + self.dropout(output2)
        output = self.norm(output)
        
        return output, attention_weights

    def forward_pre(self, output, memory,
                    memory_mask: Optional[Tensor] = None,
                    memory_key_padding_mask: Optional[Tensor] = None,
                    pos: Optional[Tensor] = None,
                    query_pos: Optional[Tensor] = None):
        output2 = self.norm(output)
        output2, attention_weights = self.multihead_attn(query=self.with_pos_embed(output2, query_pos),
                                   key=self.with_pos_embed(memory, pos),
                                   value=memory, attn_mask=memory_mask,
                                   key_padding_mask=memory_key_padding_mask)
        output = output + self.dropout(output2)

        return output, attention_weights

    def forward(self, output, memory,
                memory_mask: Optional[Tensor] = None,
                memory_key_padding_mask: Optional[Tensor] = None,
                pos: Optional[Tensor] = None,
                query_pos: Optional[Tensor] = None):
        if self.normalize_before:
            return self.forward_pre(output, memory, memory_mask,
                                    memory_key_padding_mask, pos, query_pos)
        return self.forward_post(output, memory, memory_mask,
                                 memory_key_padding_mask, pos, query_pos)


class OneFormerTransformerDecoderFFNLayer(nn.Module):

    def __init__(self, d_model, dim_feedforward=2048, dropout=0.0,
                 activation="relu", normalize_before=False):
        super().__init__()
        # Implementation of Feedforward model
        self.linear1 = nn.Linear(d_model, dim_feedforward)
        self.dropout = nn.Dropout(dropout)
        self.linear2 = nn.Linear(dim_feedforward, d_model)

        self.norm = nn.LayerNorm(d_model)

        self.activation = ACT2FN[activation]
        self.normalize_before = normalize_before

        self._reset_parameters()
    
    def _reset_parameters(self):
        for p in self.parameters():
            if p.dim() > 1:
                nn.init.xavier_uniform_(p)

    def with_pos_embed(self, tensor, pos: Optional[Tensor]):
        return tensor if pos is None else tensor + pos

    def forward_post(self, output):
        output2 = self.linear2(self.dropout(self.activation(self.linear1(output))))
        output = output + self.dropout(output2)
        output = self.norm(output)
        return output

    def forward_pre(self, output):
        output2 = self.norm(output)
        output2 = self.linear2(self.dropout(self.activation(self.linear1(output2))))
        output = output + self.dropout(output2)
        return output

    def forward(self, output):
        if self.normalize_before:
            return self.forward_pre(output)
        return self.forward_post(output)


# Copied from transformers.models.maskformer.modeling_maskformer.MaskFormerMLPPredictionHead with Mask->One
class OneFormerMLPPredictionHead(nn.Module):
    def __init__(self, input_dim: int, hidden_dim: int, output_dim: int, num_layers: int = 3):
        """
        A classic Multi Layer Perceptron (MLP).

        Args:
            input_dim (`int`):
                The input dimensions.
            hidden_dim (`int`):
                The hidden dimensions.
            output_dim (`int`):
                The output dimensions.
            num_layers (int, *optional*, defaults to 3):
                The number of layers.
        """
        super().__init__()
        in_dims = [input_dim] + [hidden_dim] * (num_layers - 1)
        out_dims = [hidden_dim] * (num_layers - 1) + [output_dim]

        self.layers = []
        for i, (in_dim, out_dim) in enumerate(zip(in_dims, out_dims)):
            activation = nn.ReLU() if i < num_layers - 1 else nn.Identity()
            layer = PredictionBlock(in_dim, out_dim, activation=activation)
            self.layers.append(layer)
            # Provide backwards compatibility from when the class inherited from nn.Sequential
            # In nn.Sequential subclasses, the name given to the layer is its index in the sequence.
            # In nn.Module subclasses they derived from the instance attribute they are assigned to e.g.
            # self.my_layer_name = Layer()
            # We can't give instance attributes integer names i.e. self.0 is not permitted and so need to register
            # explicitly
            self.add_module(str(i), layer)

    def forward(self, input: Tensor) -> Tensor:
        hidden_state = input
        for layer in self.layers:
            hidden_state = layer(hidden_state)
        return hidden_state


# refactored from original implementation    
class OneFormerTransformerDecoderLayer(nn.Module):
    def __init__(self, config: OneFormerConfig):
        super().__init__()
        self.embed_dim = config.decoder_config["hidden_dim"]
        self.num_feature_levels = 3

        self.cross_attn = OneFormerTransformerDecoderCrossAttentionLayer(
            embed_dim=self.embed_dim,
            num_heads=config.decoder_config["num_heads"],
            dropout=0.0,
            normalize_before=config.decoder_config["pre_norm"],
        )

        self.self_attn = OneFormerTransformerDecoderSelfAttentionLayer(
            embed_dim=self.embed_dim,
            num_heads=config.decoder_config["num_heads"],
            dropout=0.0,
            normalize_before=config.decoder_config["pre_norm"],
        )

        self.ffn = OneFormerTransformerDecoderFFNLayer(
                    d_model=self.embed_dim,
                    dim_feedforward=config.decoder_config["dim_feedforward"],
                    dropout=0.0,
                    normalize_before=config.decoder_config["pre_norm"],
                )

    def forward(
        self,
        index: int,
        output: torch.Tensor,
        multi_stage_features: List[torch.Tensor],
        multi_stage_positional_embeddings: List[torch.Tensor],
        attention_mask: Optional[torch.Tensor] = None,
        query_embeddings: Optional[torch.Tensor] = None,
        output_attentions: Optional[bool] = False,
    ):
        """
        Args:
            index (`int`): index of the layer in the Transformer decoder.
            output (`torch.FloatTensor`): the object queries of shape `(N, batch, hidden_dim)`
            multi_stage_features (`List[torch.Tensor]`): the multi-scale features from the pixel decoder.
            multi_stage_positional_embeddings (`List[torch.Tensor]`): positional embeddings for the multi_stage_features
            attention_mask (`torch.FloatTensor`): attention mask for the masked cross attention layer
            query_embeddings (`torch.FloatTensor`, *optional*):
                position embeddings that are added to the queries and keys in the self-attention layer.
            output_attentions (`bool`, *optional*):
                Whether or not to return the attentions tensors of all attention layers. See `attentions` under
                returned tensors for more detail.
        """
        
        level_index = index % self.num_feature_levels
        attention_mask[torch.where(attention_mask.sum(-1) == attention_mask.shape[-1])] = False

         # Masked Cross Attention
        output, cross_attn_weights = self.cross_attn(
            output, multi_stage_features[level_index],
                memory_mask=attention_mask,
                memory_key_padding_mask=None,  # here we do not apply masking on padded region
                pos=multi_stage_positional_embeddings[level_index], query_pos=query_embeddings
        )

        # Self Attention
        output, self_attn_weights = self.self_attn(
            output, output_mask=None,
                output_key_padding_mask=None,
                query_pos=query_embeddings,
        )
        
        # Fully Connected
        output = self.ffn(output)

        outputs = (output,)

        if output_attentions:
            outputs += (self_attn_weights, cross_attn_weights)
        
        return outputs


class OneFormerTransformerDecoderQueryTransformerDecoder(nn.Module):
    def __init__(self, decoder_layer, num_layers, norm=None, return_intermediate=False):
        super().__init__()
        self.layers = _get_clones(decoder_layer, num_layers)
        self.num_layers = num_layers
        self.norm = norm
        self.return_intermediate = return_intermediate

    def forward(
        self,
        output,
        memory,
        output_mask: Optional[Tensor] = None,
        memory_mask: Optional[Tensor] = None,
        output_key_padding_mask: Optional[Tensor] = None,
        memory_key_padding_mask: Optional[Tensor] = None,
        pos: Optional[Tensor] = None,
        query_pos: Optional[Tensor] = None,
    ):

        intermediate = []

        for layer in self.layers:
            output = layer(
                output,
                memory,
                output_mask=output_mask,
                memory_mask=memory_mask,
                output_key_padding_mask=output_key_padding_mask,
                memory_key_padding_mask=memory_key_padding_mask,
                pos=pos,
                query_pos=query_pos,
            )
            if self.return_intermediate:
                intermediate.append(self.norm(output))

        if self.norm is not None:
            output = self.norm(output)
            if self.return_intermediate:
                intermediate.pop()
                intermediate.append(output)

        if self.return_intermediate:
            return torch.stack(intermediate)

        return output.unsqueeze(0)


class OneFormerTransformerDecoderQueryTransformerDecoderLayer(nn.Module):
    def __init__(
        self,
        d_model,
        nhead,
        dim_feedforward=2048,
        dropout=0.1,
        activation="relu",
        normalize_before=False,
    ):
        super().__init__()
        self.self_attn = nn.MultiheadAttention(d_model, nhead, dropout=dropout)
        self.multihead_attn = nn.MultiheadAttention(d_model, nhead, dropout=dropout)
        # Implementation of Feedforward model
        self.linear1 = nn.Linear(d_model, dim_feedforward)
        self.dropout = nn.Dropout(dropout)
        self.linear2 = nn.Linear(dim_feedforward, d_model)

        self.norm1 = nn.LayerNorm(d_model)
        self.norm2 = nn.LayerNorm(d_model)
        self.norm3 = nn.LayerNorm(d_model)
        self.dropout1 = nn.Dropout(dropout)
        self.dropout2 = nn.Dropout(dropout)
        self.dropout3 = nn.Dropout(dropout)

        self.activation = ACT2FN[activation]
        self.normalize_before = normalize_before

    def with_pos_embed(self, tensor, pos: Optional[Tensor]):
        return tensor if pos is None else tensor + pos

    def forward_post(
        self,
        output,
        memory,
        output_mask: Optional[Tensor] = None,
        memory_mask: Optional[Tensor] = None,
        output_key_padding_mask: Optional[Tensor] = None,
        memory_key_padding_mask: Optional[Tensor] = None,
        pos: Optional[Tensor] = None,
        query_pos: Optional[Tensor] = None,
    ):
        q = k = self.with_pos_embed(output, query_pos)
        output2 = self.self_attn(
            q, k, value=output, attn_mask=output_mask, key_padding_mask=output_key_padding_mask
        )[0]
        output = output + self.dropout1(output2)
        output = self.norm1(output)
        output2 = self.multihead_attn(
            query=self.with_pos_embed(output, query_pos),
            key=self.with_pos_embed(memory, pos),
            value=memory,
            attn_mask=memory_mask,
            key_padding_mask=memory_key_padding_mask,
        )[0]
        output = output + self.dropout2(output2)
        output = self.norm2(output)
        output2 = self.linear2(self.dropout(self.activation(self.linear1(output))))
        output = output + self.dropout3(output2)
        output = self.norm3(output)
        return output

    def forward_pre(
        self,
        output,
        memory,
        output_mask: Optional[Tensor] = None,
        memory_mask: Optional[Tensor] = None,
        output_key_padding_mask: Optional[Tensor] = None,
        memory_key_padding_mask: Optional[Tensor] = None,
        pos: Optional[Tensor] = None,
        query_pos: Optional[Tensor] = None,
    ):
        output2 = self.norm1(output)
        q = k = self.with_pos_embed(output2, query_pos)
        output2 = self.self_attn(
            q, k, value=output2, attn_mask=output_mask, key_padding_mask=output_key_padding_mask
        )[0]
        output = output + self.dropout1(output2)
        output2 = self.norm2(output)
        output2 = self.multihead_attn(
            query=self.with_pos_embed(output2, query_pos),
            key=self.with_pos_embed(memory, pos),
            value=memory,
            attn_mask=memory_mask,
            key_padding_mask=memory_key_padding_mask,
        )[0]
        output = output + self.dropout2(output2)
        output2 = self.norm3(output)
        output2 = self.linear2(self.dropout(self.activation(self.linear1(output2))))
        output = output + self.dropout3(output2)
        return output

    def forward(
        self,
        output,
        memory,
        output_mask: Optional[Tensor] = None,
        memory_mask: Optional[Tensor] = None,
        output_key_padding_mask: Optional[Tensor] = None,
        memory_key_padding_mask: Optional[Tensor] = None,
        pos: Optional[Tensor] = None,
        query_pos: Optional[Tensor] = None,
    ):
        if self.normalize_before:
            return self.forward_pre(
                output,
                memory,
                output_mask,
                memory_mask,
                output_key_padding_mask,
                memory_key_padding_mask,
                pos,
                query_pos,
            )
        return self.forward_post(
            output,
            memory,
            output_mask,
            memory_mask,
            output_key_padding_mask,
            memory_key_padding_mask,
            pos,
            query_pos,
        )

    
class OneFormerTransformerDecoderQueryTransformer(nn.Module):
    def __init__(
        self,
        d_model=512,
        nhead=8,
        num_decoder_layers=6,
        dim_feedforward=2048,
        dropout=0.1,
        activation="relu",
        normalize_before=False,
        return_intermediate_dec=False,
    ):
        super().__init__()

        decoder_layer = OneFormerTransformerDecoderQueryTransformerDecoderLayer(
            d_model, nhead, dim_feedforward, dropout, activation, normalize_before
        )
        decoder_norm = nn.LayerNorm(d_model)
        self.decoder = OneFormerTransformerDecoderQueryTransformerDecoder(
            decoder_layer,
            num_decoder_layers,
            decoder_norm,
            return_intermediate=return_intermediate_dec,
        )

        self._reset_parameters()

        self.d_model = d_model
        self.nhead = nhead

    def _reset_parameters(self):
        for p in self.parameters():
            if p.dim() > 1:
                nn.init.xavier_uniform_(p)

    def forward(self, src, mask, query_embed, pos_embed, task_token=None):
        # flatten NxCxHxW to HWxNxC
        bs, c, h, w = src.shape
        src = src.flatten(2).permute(2, 0, 1)
        pos_embed = pos_embed.flatten(2).permute(2, 0, 1)
        query_embed = query_embed.unsqueeze(1).repeat(1, bs, 1)
        if mask is not None:
            mask = mask.flatten(1)
            
        if task_token is None:
            queries = torch.zeros_like(query_embed)
        else:
            queries = task_token.repeat(query_embed.shape[0], 1, 1)
   
        queries = self.decoder(
            queries, src, memory_key_padding_mask=mask, pos=pos_embed, query_pos=query_embed
        )
        return queries.transpose(1, 2)
    

class OneFormerTransformerDecoder(nn.Module):
    """
    Transformer decoder 
    """

    def __init__(self, in_channels: int, config: OneFormerConfig):
        super().__init__()
        self.config = config

        self.dropout = config.decoder_config["dropout"]
        self.num_heads = config.decoder_config["num_heads"]
        self.is_train = config.general_config["is_train"]
        self.use_task_norm = config.decoder_config["use_task_norm"]
        self.use_auxiliary_loss = config.general_config["use_auxiliary_loss"]

        self.query_transformer = OneFormerTransformerDecoderQueryTransformer(
            d_model=config.decoder_config["hidden_dim"],
            dropout=config.decoder_config["dropout"],
            nhead=config.decoder_config["num_heads"],
            dim_feedforward=config.decoder_config["dim_feedforward"],
            num_decoder_layers=config.decoder_config["query_dec_layers"],
            normalize_before=config.decoder_config["pre_norm"],
            return_intermediate_dec=False,
        )

        self.decoder_norm = nn.LayerNorm(config.decoder_config["hidden_dim"])

        self.num_feature_levels = 3

        self.layers = nn.ModuleList([OneFormerTransformerDecoderLayer(config) for _ in range(config.decoder_config["decoder_layers"]-1)])

        self.query_input_projection = nn.Conv2d(in_channels, config.decoder_config["hidden_dim"], kernel_size=1)

        self.class_embed = nn.Linear(config.decoder_config["hidden_dim"], config.general_config["num_classes"] + 1)
        self.mask_embed = OneFormerMLPPredictionHead(config.decoder_config["hidden_dim"], config.decoder_config["hidden_dim"], config.decoder_config["mask_dim"], 3)

    def forward(
        self,
        task_token=None,
        multi_stage_features=None,
        multi_stage_positional_embeddings=None,
        mask_features=None,
        query_features=None,
        query_embeddings=None,
        query_embedder=None,
        size_list=None,
        output_attentions=None,
    ):

        if self.use_task_norm:
            task_token = self.decoder_norm(task_token)

        object_queries = self.query_transformer(query_features, None, 
                                    query_embedder.weight[:-1], 
                                    self.query_input_projection(mask_features),
                                    task_token if self.use_task_norm else None)

        object_queries = object_queries[0].permute(1, 0, 2)
        
        queries = torch.cat([object_queries, task_token], dim=0)

        output = queries.clone()

        intermediate_class_predictions = []
        intermediate_mask_predictions = []

        # prediction heads on learnable query features
        outputs_class, outputs_mask, attention_mask = self.forward_prediction_heads(output, mask_features, 
                                                        attention_mask_target_size=size_list[0])
        intermediate_class_predictions.append(outputs_class)
        intermediate_mask_predictions.append(outputs_mask)

        attentions = ()

        for index, layer in enumerate(self.layers):
            layer_outputs = layer(
                index=index,
                output=output,
                multi_stage_features=multi_stage_features,
                multi_stage_positional_embeddings=multi_stage_positional_embeddings,
                attention_mask=attention_mask,
                query_embeddings=query_embeddings,
                output_attentions=output_attentions
            )
            
            output = layer_outputs[0]
            attentions += (layer_outputs[1:],)

            outputs_class, outputs_mask, attention_mask = self.forward_prediction_heads(output, 
                            mask_features, attention_mask_target_size=size_list[(index + 1) % self.num_feature_levels])
            intermediate_class_predictions.append(outputs_class)
            intermediate_mask_predictions.append(outputs_mask)
            
        assert len(intermediate_mask_predictions) == len(self.layers) + 1
        
        object_queries = layer_outputs[0].permute(1, 0, 2)

        contrastive_logits = queries.permute(1, 0, 2)

        return OneFormerTransformerDecoderOutput(
            object_queries=object_queries,
            contrastive_logits=contrastive_logits,
            prediction_masks=intermediate_mask_predictions[-1],
            prediction_class=intermediate_class_predictions[-1],
            auxiliary_predictions=self._get_aux_predictions(intermediate_class_predictions, 
                                                        intermediate_mask_predictions) if self.use_auxiliary_loss else None,
            attentions=attentions,


        )

    def forward_prediction_heads(self, output, mask_features, attention_mask_target_size):
        decoder_output = self.decoder_norm(output)
        decoder_output = decoder_output.transpose(0, 1)
        outputs_class = self.class_embed(decoder_output)
        mask_embed = self.mask_embed(decoder_output)
        outputs_mask = torch.einsum("bqc,bchw->bqhw", mask_embed, mask_features)

        # NOTE: prediction is of higher-resolution
        # [B, Q, H, W] -> [B, Q, H*W] -> [B, h, Q, H*W] -> [B*h, Q, HW]
        attention_mask = F.interpolate(outputs_mask, size=attention_mask_target_size, mode="bilinear", align_corners=False)
        
        # must use bool type
        # If a BoolTensor is provided, positions with ``True`` are not allowed to attend while ``False`` values will be unchanged.
        attention_mask = (attention_mask.sigmoid().flatten(2).unsqueeze(1).repeat(1, self.num_heads, 1, 1).flatten(0, 1) < 0.5).bool()
        attention_mask = attention_mask.detach()

        return outputs_class, outputs_mask, attention_mask

    @torch.jit.unused
    def _get_aux_predictions(self, outputs_class, outputs_seg_masks):
        # this is a workaround to make torchscript happy, as torchscript
        # doesn't support dictionary with non-homogeneous values, such
        # as a dict having both a Tensor and a list.
        aux_list = [
            {"class_queries_logits": a, "masks_queries_logits": b}
            for a, b in zip(outputs_class[:-1], outputs_seg_masks[:-1])
        ]
        return tuple(aux_list)
    

# Copied from transformers.models.maskformer.modeling_maskformer.MaskFormerTransformerModule with Mask->One
class OneFormerTransformerModule(nn.Module):
    """
    The OneFormer's transformer module.
    """

    def __init__(self, in_features: int, config: OneFormerConfig):
        super().__init__()
        hidden_dim = config.decoder_config["hidden_dim"]
        self.num_feature_levels = 3
        self.position_embedder = OneFormerSinePositionEmbedding(num_pos_feats=hidden_dim // 2, normalize=True)
        self.queries_embedder = nn.Embedding(config.general_config["num_queries"], hidden_dim)
        self.input_projections = []

        for _ in range(self.num_feature_levels):
            if in_features != hidden_dim or config.decoder_config["enforce_input_proj"]:
                self.input_projections.append(nn.Conv2d(in_features, hidden_dim, kernel_size=1))
            else:
                self.input_projections.append(nn.Sequential())
        
        self.decoder = OneFormerTransformerDecoder(in_channels=in_features, config=config)
        self.level_embed = nn.Embedding(self.num_feature_levels, hidden_dim)

    def forward(
        self, multi_scale_features: List[Tensor], mask_features: Tensor, task_token: Tensor, output_attentions: bool = False
    ) -> OneFormerTransformerDecoderOutput:
        
        assert len(multi_scale_features) == self.num_feature_levels
        multi_stage_features = []
        multi_stage_positional_embeddings = []
        size_list = []

        for i in range(self.num_feature_levels):
            size_list.append(multi_scale_features[i].shape[-2:])
            multi_stage_positional_embeddings.append(self.position_embedder(multi_scale_features[i], None).flatten(2))
            multi_stage_features.append(self.input_projections[i](multi_scale_features[i]).flatten(2) + self.level_embed.weight[i][None, :, None])

            # flatten NxCxHxW to HWxNxC
            multi_stage_positional_embeddings[-1] = multi_stage_positional_embeddings[-1].permute(2, 0, 1)
            multi_stage_features[-1] = multi_stage_features[-1].permute(2, 0, 1)

        _, batch_size, _ = multi_stage_features[0].shape

        # QxNxC
        query_embeddings = self.queries_embedder.weight.unsqueeze(1).repeat(1, batch_size, 1)
        task_token = task_token.unsqueeze(0)
        
        query_features = self.position_embedder(mask_features, None)
        
        decoder_output: OneFormerTransformerDecoderOutput = self.decoder(
            task_token=task_token,
            multi_stage_features=multi_stage_features,
            multi_stage_positional_embeddings=multi_stage_positional_embeddings,
            mask_features=mask_features,
            query_features=query_features,
            query_embeddings=query_embeddings,
            query_embedder=self.queries_embedder,
            size_list=size_list,
            output_attentions=output_attentions,
        )
        return decoder_output
    

# Copied from transformers.models.maskformer.modeling_maskformer.MaskFormerSinePositionEmbedding with Mask->One
class OneFormerSinePositionEmbedding(nn.Module):
    """
    This is a more standard version of the position embedding, very similar to the one
    used by the Attention is all you need paper, generalized to work on images.
    """
    
    def __init__(self, num_pos_feats=64, temperature=10000, normalize=False, scale=None):
        super().__init__()
        self.num_pos_feats = num_pos_feats
        self.temperature = temperature
        self.normalize = normalize
        if scale is not None and normalize is False:
            raise ValueError("normalize should be True if scale is passed")
        if scale is None:
            scale = 2 * math.pi
        self.scale = scale

    def forward(self, x, mask=None):
        if mask is None:
            mask = torch.zeros((x.size(0), x.size(2), x.size(3)), device=x.device, dtype=torch.bool)
        not_mask = ~mask
        y_embed = not_mask.cumsum(1, dtype=torch.float32)
        x_embed = not_mask.cumsum(2, dtype=torch.float32)
        if self.normalize:
            eps = 1e-6
            y_embed = y_embed / (y_embed[:, -1:, :] + eps) * self.scale
            x_embed = x_embed / (x_embed[:, :, -1:] + eps) * self.scale

        dim_t = torch.arange(self.num_pos_feats, dtype=torch.float32, device=x.device)
        dim_t = self.temperature ** (2 * (dim_t // 2) / self.num_pos_feats)

        pos_x = x_embed[:, :, :, None] / dim_t
        pos_y = y_embed[:, :, :, None] / dim_t
        pos_x = torch.stack(
            (pos_x[:, :, :, 0::2].sin(), pos_x[:, :, :, 1::2].cos()), dim=4
        ).flatten(3)
        pos_y = torch.stack(
            (pos_y[:, :, :, 0::2].sin(), pos_y[:, :, :, 1::2].cos()), dim=4
        ).flatten(3)
        pos = torch.cat((pos_y, pos_x), dim=3).permute(0, 3, 1, 2)
        return pos

# Copied from transformers.models.maskformer.modeling_maskformer.PredictionBlock
class PredictionBlock(nn.Module):
    def __init__(self, in_dim: int, out_dim: int, activation: nn.Module) -> None:
        super().__init__()
        self.layers = [nn.Linear(in_dim, out_dim), activation]
        # Maintain submodule indexing as if part of a Sequential block
        for i, layer in enumerate(self.layers):
            self.add_module(str(i), layer)

    def forward(self, input: Tensor) -> Tensor:
        hidden_state = input
        for layer in self.layers:
            hidden_state = layer(hidden_state)
        return hidden_state

################## Text Mapper Classes #################

class OneFormerTextMapperAttention(nn.Module):
    def __init__(self, dim, num_heads=8, qkv_bias=False, qk_scale=None, attn_drop=0., proj_drop=0.):
        super().__init__()
        self.num_heads = num_heads
        head_dim = dim // num_heads
        # NOTE scale factor was wrong in my original version, can set manually to be compat with prev weights
        self.scale = qk_scale or head_dim ** -0.5

        self.q_proj = nn.Linear(dim, dim, bias=qkv_bias)
        self.k_proj = nn.Linear(dim, dim, bias=qkv_bias)
        self.v_proj = nn.Linear(dim, dim, bias=qkv_bias)

        self.attn_drop = nn.Dropout(attn_drop)
        self.proj = nn.Linear(dim, dim)
        self.proj_drop = nn.Dropout(proj_drop)

    def forward(self, q, k, v):
        B, N, C = q.shape
        assert k.shape == v.shape
        B, M, C = k.shape
        q = self.q_proj(q).reshape(B, N, self.num_heads, C // self.num_heads)
        k = self.k_proj(k).reshape(B, M, self.num_heads, C // self.num_heads)
        v = self.v_proj(v).reshape(B, M, self.num_heads, C // self.num_heads)

        attn = torch.einsum('bnkc,bmkc->bknm', q, k) * self.scale

        attn = attn.softmax(dim=-1)

        x = torch.einsum('bknm,bmkc->bnkc', attn, v).reshape(B, N, C)

        x = self.proj(x)
        x = self.proj_drop(x)
        return x


class OneFormerTextTransformerDecoderLayer(nn.Module):
    def __init__(
        self,
        d_model,
        nhead,
        dropout=0.1,
    ):
        super().__init__()
        self.self_attn = OneFormerTextMapperAttention(d_model, nhead, proj_drop=dropout)
        self.cross_attn = OneFormerTextMapperAttention(d_model, nhead, proj_drop=dropout)

        self.norm1 = nn.LayerNorm(d_model)
        self.norm2 = nn.LayerNorm(d_model)
        self.norm3 = nn.LayerNorm(d_model)
        self.dropout = nn.Dropout(dropout)

        self.mlp = nn.Sequential(
            nn.Linear(d_model, d_model * 4),
            nn.GELU(),
            nn.Dropout(dropout),
            nn.Linear(d_model * 4, d_model)
        )

    def forward(self, x, mem):
        q = k = v = self.norm1(x)
        x = x + self.self_attn(q, k, v)
        q = self.norm2(x)
        x = x + self.cross_attn(q, mem, mem)
        x = x + self.dropout(self.mlp(self.norm3(x)))
        return x


class OneFormerTextContextDecoder(nn.Module):
    def __init__(self,
                 transformer_width=256,
                 transformer_heads=4,
                 transformer_layers=6,
                 visual_dim=1024,
                 dropout=0.1,
                 **kwargs):
        super().__init__()

        self.memory_proj = nn.Sequential(
            nn.LayerNorm(visual_dim),
            nn.Linear(visual_dim, transformer_width),
            nn.LayerNorm(transformer_width),
        )

        self.text_proj = nn.Sequential(
            nn.LayerNorm(visual_dim),
            nn.Linear(visual_dim, transformer_width),
        )

        self.decoder = nn.ModuleList([
                    OneFormerTextTransformerDecoderLayer(transformer_width, transformer_heads, dropout) for _ in range(transformer_layers)
                ])
        
        self.out_proj = nn.Sequential(
            nn.LayerNorm(transformer_width),
            nn.Linear(transformer_width, visual_dim)
        )

        self.apply(self._init_weights)

    def _init_weights(self, m):
        if isinstance(m, nn.Linear):
            trunc_normal_(m.weight, std=.02)
            if isinstance(m, nn.Linear) and m.bias is not None:
                nn.init.constant_(m.bias, 0)
        elif isinstance(m, nn.LayerNorm):
            nn.init.constant_(m.bias, 0)
            nn.init.constant_(m.weight, 1.0)

    
    def forward(self, text, visual):
        B, N, C = visual.shape
        visual = self.memory_proj(visual)
        x = self.text_proj(text)

        for layer in self.decoder:
            x = layer(x, visual)
        
        return self.out_proj(x)


# Copied from transformers.models.groupvit..modeling_groupvit.GroupViTMLP with GroupViT->OneFormerText
class OneFormerTextMLP(nn.Module):
    def __init__(
        self,
        hidden_size: Optional[int] = None,
        intermediate_size: Optional[int] = None,
        output_size: Optional[int] = None,
    ):
        super().__init__()
        self.activation_fn = ACT2FN["quick_gelu"]
        hidden_size = hidden_size
        intermediate_size = intermediate_size
        output_size = output_size
        self.fc1 = nn.Linear(hidden_size, intermediate_size)
        self.fc2 = nn.Linear(intermediate_size, output_size)

    def forward(self, hidden_states: torch.Tensor) -> torch.Tensor:
        hidden_states = self.fc1(hidden_states)
        hidden_states = self.activation_fn(hidden_states)
        hidden_states = self.fc2(hidden_states)
        return hidden_states


# Copied from transformers.models.groupvit..modeling_groupvit.GroupViTEncoderLayer with GroupViTEncoderLaye->OneFormerTextTransformer
class OneFormerTextTransformerLayer(nn.Module):
    def __init__(self, width: int, heads: int, attn_mask: torch.Tensor):
        super().__init__()
        self.self_attn = nn.MultiheadAttention(width, heads)
        self.layer_norm1 = nn.LayerNorm(width)
        self.mlp = OneFormerTextMLP(width, width * 4, width)
        self.layer_norm2 = nn.LayerNorm(width)
        self.attn_mask = attn_mask

    def forward(
        self,
        hidden_states: torch.Tensor,
        key_padding_mask: Optional[torch.Tensor] = None,
    ) -> torch.FloatTensor:
    
        residual = hidden_states

        hidden_states = self.layer_norm1(hidden_states)
        hidden_states = self.self_attn(
            hidden_states,
            hidden_states,
            hidden_states,
            need_weights=False,
            key_padding_mask=key_padding_mask,
        )[0]
        hidden_states = residual + hidden_states

        residual = hidden_states
        hidden_states = self.layer_norm2(hidden_states)
        hidden_states = self.mlp(hidden_states)
        hidden_states = residual + hidden_states

        return hidden_states


class OneFormerTextTransformer(nn.Module):

    def __init__(self, width: int, layers: int, heads: int, attn_mask: torch.Tensor = None, use_checkpoint=False):
        super().__init__()
        self.width = width
        self.num_layers = layers
        self.layers = nn.Sequential(*[OneFormerTextTransformerLayer(width, heads, attn_mask) for _ in range(layers)])
        proj_std = (self.width**-0.5) * ((2 * self.num_layers)**-0.5)
        attn_std = self.width**-0.5
        fc_std = (2 * self.width)**-0.5
        for layer in self.layers:
            nn.init.normal_(layer.self_attn.in_proj_weight, std=attn_std)
            nn.init.normal_(layer.self_attn.out_proj.weight, std=proj_std)
            nn.init.normal_(layer.mlp.fc1.weight, std=fc_std)
            nn.init.normal_(layer.mlp.fc2.weight, std=proj_std)

        self.use_checkpoint = use_checkpoint

    def forward(self, x: torch.Tensor):
        for layer in self.layers:
            if self.use_checkpoint:
                x = torch.utils.checkpoint.checkpoint(layer, x)
            else:
                x = layer(x)
        return x


class OneFormerTextEncoder(nn.Module):

    def __init__(
        self,
        context_length: int,
        width: int,
        layers: int,
        vocab_size,
        use_checkpoint=False,
    ):

        super().__init__()
        heads = width // 64
        self.context_length = context_length
        self.width = width
        self.transformer = OneFormerTextTransformer(
            width=width,
            layers=layers,
            heads=heads,
            attn_mask=self.build_attention_mask(),
            use_checkpoint=use_checkpoint)

        self.positional_embedding = nn.Parameter(torch.empty(self.context_length, width))
        self.ln_final = nn.LayerNorm(width)
        self.token_embedding = nn.Embedding(vocab_size, width)
        nn.init.normal_(self.token_embedding.weight, std=0.02)

        # initialization
        nn.init.normal_(self.positional_embedding, std=0.01)

    def build_attention_mask(self):
        # lazily create causal attention mask, with full attention between the vision tokens
        # pytorch uses additive attention mask; fill with -inf
        mask = torch.empty(self.context_length, self.context_length)
        mask.fill_(float('-inf'))
        mask.triu_(1)  # zero out the lower diagonal
        return mask

    def forward(self, text):
        x = self.token_embedding(text)
        x = x + self.positional_embedding
        x = x.permute(1, 0, 2)  # NLD -> LND
        x = self.transformer(x)
        x = x.permute(1, 0, 2)  # LND -> NLD
        x = self.ln_final(x)

        # x.shape = [batch_size, n_ctx, transformer.width]
        # take features from the eot embedding (eot_token is the highest number in each sequence)
        x = x[torch.arange(x.shape[0]), text.argmax(dim=-1)]

        return x

    
class OneFormerTextMapper(nn.Module):
    def __init__(self, config: OneFormerConfig):
        super().__init__()
        self.text_encoder = OneFormerTextEncoder(context_length=config.text_encoder_config["text_encoder_context_length"],
                                    width=config.text_encoder_config["text_encoder_width"],
                                    layers=config.text_encoder_config["text_encoder_num_layers"],
                                    vocab_size=config.text_encoder_config["text_encoder_vocab_size"],)
        
        self.text_projector = OneFormerMLPPredictionHead(config.text_encoder_config["text_encoder_width"], 
                                    config.decoder_config["hidden_dim"],
                                    config.decoder_config["hidden_dim"],
                                    config.text_encoder_config["text_encoder_proj_layers"],)
        if config.text_encoder_config["text_encoder_n_ctx"] > 0:
            self.prompt_ctx = nn.Embedding(config.text_encoder_config["text_encoder_n_ctx"], config.text_encoder_config["text_encoder_width"],)
        else:
            self.prompt_ctx = None

    def forward(
        self,
        inputs: Tensor,
    ) -> Tensor:
        text_queries = self.encode_text(inputs)

        return text_queries
    
    def encode_text(self, text):
        assert text.ndim in [2, 3], text.ndim
        b = text.shape[0]
        squeeze_dim = False
        num_text = 1
        if text.ndim == 3:
            num_text = text.shape[1]
            text = rearrange(text, 'b n l -> (b n) l', n=num_text)
            squeeze_dim = True

        # [B, C]
        x = self.text_encoder(text)

        text_x = self.text_projector(x)

        if squeeze_dim:
            text_x = rearrange(text_x, '(b n) c -> b n c', n=num_text)
            if self.prompt_ctx is not None:
                text_ctx = self.prompt_ctx.weight.unsqueeze(0).repeat(text_x.shape[0], 1, 1)
                text_x = torch.cat([text_x, text_ctx], dim=1)
        
        return text_x
    

class OneFormerTaskModel(nn.Module):

    def __init__(self, config: OneFormerConfig):
        super().__init__()
        self.task_mlp = OneFormerMLPPredictionHead(config.text_encoder_config["task_seq_len"], config.decoder_config["hidden_dim"], 
                            config.decoder_config["hidden_dim"], 2)

    def forward(
        self,
        inputs: Tensor,
    ) -> Tensor:
        task_tokens = self.task_mlp(inputs.float())
        return task_tokens
    

ONEFORMER_START_DOCSTRING = r"""
    This model is a PyTorch [torch.nn.Module](https://pytorch.org/docs/stable/nn.html#torch.nn.Module) sub-class. Use
    it as a regular PyTorch Module and refer to the PyTorch documentation for all matter related to general usage and
    behavior.

    Parameters:
        config ([`OneFormerConfig`]): Model configuration class with all the parameters of the model.
            Initializing with a config file does not load the weights associated with the model, only the
            configuration. Check out the [`~PreTrainedModel.from_pretrained`] method to load the model weights.
"""

ONEFORMER_INPUTS_DOCSTRING = r"""
    Args:
        pixel_values (`torch.FloatTensor` of shape `(batch_size, num_channels, height, width)`):
            Pixel values. Pixel values can be obtained using [`OneFormerFeatureExtractor`]. See
            [`OneFormerFeatureExtractor.__call__`] for details.
        task_inputs (`torch.FloatTensor` of shape `(batch_size, sequence_length)`):
            Task inputs. Task inputs can be obtained using [`OneFormerFeatureExtractor`]. See
            [`OneFormerFeatureExtractor.__call__`] for details.
        pixel_mask (`torch.LongTensor` of shape `(batch_size, height, width)`, *optional*):
            Mask to avoid performing attention on padding pixel values. Mask values selected in `[0, 1]`:

            - 1 for pixels that are real (i.e. **not masked**),
            - 0 for pixels that are padding (i.e. **masked**).

            [What are attention masks?](../glossary#attention-mask)
        output_hidden_states (`bool`, *optional*):
            Whether or not to return the hidden states of all layers. See `hidden_states` under returned tensors for
            more detail.
        output_attentions (`bool`, *optional*):
            Whether or not to return the attentions tensors of Detr's decoder attention layers.
        return_dict (`bool`, *optional*):
            Whether or not to return a [`~OneFormerModelOutput`] instead of a plain tuple.
"""

class OneFormerPreTrainedModel(PreTrainedModel):
    config_class = OneFormerConfig
    base_model_prefix = "model"
    main_input_name = "pixel_values"

    def _init_weights(self, module: nn.Module):
        xavier_std = self.config.init_xavier_std
        std = self.config.init_std
        if isinstance(module, OneFormerTransformerModule):
            if module.input_projections is not None:
                for input_projection in module.input_projections:
                    if not isinstance(input_projection, nn.Sequential):
                        nn.init.xavier_uniform_(input_projection.weight, gain=xavier_std)
                        nn.init.constant_(input_projection.bias, 0)
        elif isinstance(module, OneFormerTransformerDecoder):
            nn.init.xavier_uniform_(module.query_input_projection.weight, gain=xavier_std)
            nn.init.constant_(module.query_input_projection.bias, 0)
        elif isinstance(module, OneFormerPixelDecoderEncoderMultiscaleDeformableAttention):
            module._reset_parameters()
        elif isinstance(module, OneFormerPixelLevelModule):
            for submodule in module.modules():
                if isinstance(submodule, (nn.Conv2d, nn.Linear)):
                    submodule.weight.data.normal_(mean=0.0, std=std)
                    if submodule.bias is not None:
                        submodule.bias.data.zero_()
        elif isinstance(module, OneFormerSwinModel):
            for submodule in module.modules():
                if isinstance(submodule, (nn.Conv2d, nn.Linear)):
                    submodule.weight.data.normal_(mean=0.0, std=std)
                    if submodule.bias is not None:
                        submodule.bias.data.zero_()
        elif isinstance(module, (nn.Linear, nn.Conv2d, nn.BatchNorm2d)):
            module.weight.data.normal_(mean=0.0, std=std)
            if module.bias is not None:
                module.bias.data.zero_()
        elif isinstance(module, nn.Embedding):
            module.weight.data.normal_(mean=0.0, std=std)
            if module.padding_idx is not None:
                module.weight.data[module.padding_idx].zero_()
        if hasattr(module, "reference_points"):
            nn.init.xavier_uniform_(module.reference_points.weight.data, gain=1.0)
            nn.init.constant_(module.reference_points.bias.data, 0.0)
        elif isinstance(module, OneFormerTaskModel):
            for submodule in module.modules():
                if isinstance(module, OneFormerMLPPredictionHead):
                    for submodule in module.modules():
                        if isinstance(submodule, nn.Linear):
                            nn.init.xavier_uniform_(submodule.weight, gain=xavier_std)
                            nn.init.constant_(submodule.bias, 0)
                        elif isinstance(module, nn.LayerNorm):
                            module.bias.data.zero_()
                            module.weight.data.fill_(1.0)
        elif isinstance(module, OneFormerSwinModel):
            for submodule in module.modules():
                if isinstance(submodule, (nn.Conv2d, nn.Linear)):
                    submodule.weight.data.normal_(mean=0.0, std=std)
                    if submodule.bias is not None:
                        submodule.bias.data.zero_()
                elif isinstance(module, nn.LayerNorm):
                    module.bias.data.zero_()
                    module.weight.data.fill_(1.0)
        elif isinstance(module, OneFormerDinatModel):
            for submodule in module.modules():
                if isinstance(submodule, (nn.Conv2d, nn.Linear)):
                    submodule.weight.data.normal_(mean=0.0, std=std)
                    if submodule.bias is not None:
                        submodule.bias.data.zero_()
                elif isinstance(module, nn.LayerNorm):
                    module.bias.data.zero_()
                    module.weight.data.fill_(1.0)
        elif isinstance(module, nn.Embedding):
            module.weight.data.normal_(mean=0.0, std=std)
            if module.padding_idx is not None:
                module.weight.data[module.padding_idx].zero_()
        elif isinstance(module, nn.MultiheadAttention):
            module.in_proj_weight.data.normal_(mean=0.0, std=std)
            module.in_proj_bias.data.zero_()

                

    def _set_gradient_checkpointing(self, module, value=False):
        if isinstance(module, OneFormerSwinEncoder):
            module.gradient_checkpointing = value


@add_start_docstrings(
    "The bare OneFormer Model outputting raw hidden-states without any specific head on top.",
    ONEFORMER_START_DOCSTRING,
)


class OneFormerModel(OneFormerPreTrainedModel):
    main_input_name = ["pixel_values", "task_inputs"]
    def __init__(self, config: OneFormerConfig):
        super().__init__(config)
        self.pixel_level_module = OneFormerPixelLevelModule(config)
        self.transformer_module = OneFormerTransformerModule(
            in_features=config.decoder_config["conv_dim"], config=config
        )
        self.task_encoder = OneFormerTaskModel(config)
        self.is_train = config.general_config["is_train"]
        
        if self.is_train:
            self.text_mapper = OneFormerTextMapper(config)
        else:
            self.text_mapper = None

        self.post_init()

    @add_start_docstrings_to_model_forward(ONEFORMER_INPUTS_DOCSTRING)
    @replace_return_docstrings(output_type=OneFormerModelOutput, config_class=_CONFIG_FOR_DOC)
    def forward(
        self,
        pixel_values: Tensor,
        task_inputs: Tensor,
        text_inputs: Optional[Tensor] = None,
        pixel_mask: Optional[Tensor] = None,
        output_hidden_states: Optional[bool] = None,
        output_attentions: Optional[bool] = None,
        return_dict: Optional[bool] = None,
    ) -> OneFormerModelOutput:
        r"""
        Returns:
        Examples:
        ```python
        >>> import torch
        >>> from PIL import Image
        >>> import requests
        >>> from transformers import CLIPTokenizer, OneFormerFeatureExtractor, OneFormerModel

        >>> # download texting image
        >>> url = "http://images.cocodataset.org/val2017/000000039769.jpg"
        >>> image = Image.open(requests.get(url, stream=True).raw)

        >>> # load feature extractor for preprocessing the inputs
        >>> feature_extractor = OneFormerFeatureExtractor.from_pretrained("shi-labs/oneformer_ade20k_swin_tiny")
        >>> model = OneFormerModel.from_pretrained("shi-labs/oneformer_ade20k_swin_tiny")
        >>> inputs = feature_extractor(image, ["semantic"], return_tensors="pt")

        >>> with torch.no_grad():
        ...    outputs = model(**inputs)

        >>> mask_predictions = outputs.transformer_decoder_mask_predictions
        >>> class_predictions = outputs.transformer_decoder_class_predictions

        >>> f'\n👉 Mask Predictions Shape: {list(mask_predictions.shape)}, Class Predictions Shape: {list(class_predictions.shape)}'
        👉 Mask Predictions Shape: [1, 150, 128, 176], Class Predictions Shape: [1, 150, 151]

        🎉 Congratulations on successfully running OneFormer
        📖 For more information, checkout the official repo: https://github.com/SHI-Labs/OneFormer
        ```"""

        if pixel_values is None:
            raise ValueError("You have to specify pixel_values")

        output_attentions = output_attentions if output_attentions is not None else self.config.output_attentions
        output_hidden_states = (
            output_hidden_states if output_hidden_states is not None else self.config.output_hidden_states
        )
        return_dict = return_dict if return_dict is not None else self.config.use_return_dict

        batch_size, _, height, width = pixel_values.shape

        if pixel_mask is None:
            pixel_mask = torch.ones((batch_size, height, width), device=pixel_values.device)

        pixel_level_module_output: OneFormerPixelLevelModuleOutput = self.pixel_level_module(
            pixel_values, output_hidden_states
        )
        
        multi_scale_features = pixel_level_module_output.decoder_features
        mask_features = pixel_level_module_output.decoder_last_feature

        task_token = self.task_encoder(task_inputs)

        if self.is_train:
            text_queries = self.text_mapper(text_inputs)
        else:
            text_queries = None

        transformer_module_output: OneFormerTransformerDecoderOutput = self.transformer_module(
            multi_scale_features=multi_scale_features, mask_features=mask_features, task_token=task_token, output_attentions=output_attentions,
        )

        queries = transformer_module_output.object_queries

        encoder_hidden_states = None
        pixel_decoder_hidden_states = None
        transformer_decoder_hidden_states = None

        if output_hidden_states:
            encoder_hidden_states = pixel_level_module_output.encoder_features
            pixel_decoder_hidden_states = (pixel_level_module_output.decoder_last_feature, )
            for f in pixel_level_module_output.decoder_features:
                pixel_decoder_hidden_states += (f,)
            transformer_decoder_hidden_states = transformer_module_output.auxiliary_predictions

        output = OneFormerModelOutput(
            encoder_hidden_states=encoder_hidden_states,
            pixel_decoder_hidden_states=pixel_decoder_hidden_states,
            transformer_decoder_hidden_states=transformer_decoder_hidden_states,
            transformer_decoder_object_queries=queries,
            transformer_decoder_contrastive_queries=transformer_module_output.contrastive_logits,
            transformer_decoder_mask_predictions=transformer_module_output.prediction_masks,
            transformer_decoder_class_predictions=transformer_module_output.prediction_class,
            transformer_decoder_auxiliary_predictions=transformer_module_output.auxiliary_predictions,
            text_queries=text_queries,
            task_token=task_token,
            attentions=transformer_module_output.attentions,
        )

        if not return_dict:
            output = tuple(v for v in output.values())

        return output
    

class OneFormerForUniversalSegmentation(OneFormerPreTrainedModel):
    main_input_name = ["pixel_values", "task_inputs"]
    def __init__(self, config: OneFormerConfig):
        super().__init__(config)
        self.model = OneFormerModel(config)

        self.matcher = OneFormerHungarianMatcher(
            cost_class=config.general_config["class_weight"], cost_dice=config.general_config["dice_weight"], 
            cost_mask=config.general_config["mask_weight"], num_points=config.general_config["train_num_points"]
        )

        self.weight_dict: Dict[str, float] = {
            "loss_cross_entropy": config.general_config["class_weight"],
            "loss_mask": config.general_config["mask_weight"],
            "loss_dice": config.general_config["dice_weight"],
            "loss_contrastive": config.general_config["contrastive_weight"],
        }

        self.criterion = OneFormerLoss(
            num_classes=config.general_config["num_classes"],
            matcher=self.matcher,
            weight_dict=self.weight_dict,
            eos_coef=config.general_config["no_object_weight"],
            num_points=config.general_config["train_num_points"], 
            oversample_ratio=config.general_config["oversample_ratio"], 
            importance_sample_ratio=config.general_config["importance_sample_ratio"], 
            contrastive_temperature=config.general_config["contrastive_temperature"],
        )

        self.post_init()

    def get_loss_dict(
        self,
        masks_queries_logits: Tensor,
        class_queries_logits: Tensor,
        contrastive_queries_logits: Tensor,
        mask_labels: Tensor,
        class_labels: Tensor,
        text_queries: Tensor,
        auxiliary_predictions: Dict[str, Tensor],
        calculate_contrastive_loss: bool,
    ) -> Dict[str, Tensor]:
        loss_dict: Dict[str, Tensor] = self.criterion(
                    masks_queries_logits=masks_queries_logits, 
                    class_queries_logits=class_queries_logits, 
                    contrastive_queries_logits=contrastive_queries_logits, 
                    mask_labels=mask_labels, 
                    class_labels=class_labels, 
                    text_queries=text_queries,
                    auxiliary_predictions=auxiliary_predictions,
                    calculate_contrastive_loss=calculate_contrastive_loss,
        )

        # weight each loss by `self.weight_dict[<LOSS_NAME>]` including auxiliary losses
        for key, weight in self.weight_dict.items():
            for loss_key, loss in loss_dict.items():
                if key in loss_key:
                    loss *= weight

        return loss_dict

    def get_loss(self, loss_dict: Dict[str, Tensor]) -> Tensor:
        return sum(loss_dict.values())

    @add_start_docstrings_to_model_forward(ONEFORMER_INPUTS_DOCSTRING)
    @replace_return_docstrings(output_type=OneFormerForUniversalSegmentationOutput, config_class=_CONFIG_FOR_DOC)
    def forward(
        self,
        pixel_values: Tensor,
        task_inputs: Tensor,
        text_inputs: Optional[Tensor] = None,
        mask_labels: Optional[List[Tensor]] = None,
        class_labels: Optional[List[Tensor]] = None,
        pixel_mask: Optional[Tensor] = None,
        output_auxiliary_logits: Optional[bool] = None,
        output_hidden_states: Optional[bool] = None,
        output_attentions: Optional[bool] = None,
        return_dict: Optional[bool] = None,
    ) -> OneFormerForUniversalSegmentationOutput:
        r"""
        text_inputs (`List[torch.Tensor]`, *optional*):
            Tensor fof shape `(num_queries, sequence_length)` to be fed to a model
        mask_labels (`List[torch.Tensor]`, *optional*):
            List of mask labels of shape `(num_labels, height, width)` to be fed to a model
        class_labels (`List[torch.LongTensor]`, *optional*):
            list of target class labels of shape `(num_labels, height, width)` to be fed to a model. They identify the
            labels of `mask_labels`, e.g. the label of `mask_labels[i][j]` if `class_labels[i][j]`.

        Returns:
            `OneFormerUniversalSegmentationOutput`
        Example:

        Universal segmentation example:

        ```python
        >>> from transformers import OneFormerFeatureExtractor, OneFormerForUniversalSegmentation
        >>> from PIL import Image
        >>> import requests
        >>> import torch

        >>> # load OneFormer fine-tuned on ADE20k for universal segmentation
        >>> feature_extractor = OneFormerFeatureExtractor.from_pretrained("shi-labs/oneformer_ade20k_swin_tiny")
        >>> model = OneFormerForUniversalSegmentation.from_pretrained("shi-labs/oneformer_ade20k_swin_tiny")

        >>> url = (
        ...    "https://huggingface.co/datasets/hf-internal-testing/fixtures_ade20k/resolve/main/ADE_val_00000001.jpg"
        ... )
        >>> image = Image.open(requests.get(url, stream=True).raw)

        >>> ######## Semantic Segmentation ########
        >>> inputs = feature_extractor(image, ["semantic"], return_tensors="pt")

        >>> with torch.no_grad():
        ...    outputs = model(**inputs)
        >>> # model predicts class_queries_logits of shape `(batch_size, num_queries)`
        >>> # and masks_queries_logits of shape `(batch_size, num_queries, height, width)`
        >>> class_queries_logits = outputs.class_queries_logits
        >>> masks_queries_logits = outputs.masks_queries_logits

        >>> # you can pass them to feature_extractor for semantic postprocessing
        >>> predicted_semantic_map = feature_extractor.post_process_semantic_segmentation(
        ...    outputs, target_sizes=[image.size[::-1]]
        ... )[0]
        >>> f'\n👉 Semantic Predictions Shape: {list(predicted_semantic_map.shape)}'
        Semantic Predictions Shape: [512, 683]


        >>> ######## Instance Segmentation ########
        >>> inputs = feature_extractor(image, ["instance"], return_tensors="pt")

        >>> with torch.no_grad():
        ...    outputs = model(**inputs)
        >>>  # model predicts class_queries_logits of shape `(batch_size, num_queries)`
        >>>  # and masks_queries_logits of shape `(batch_size, num_queries, height, width)`
        >>>  class_queries_logits = outputs.class_queries_logits
        >>>  masks_queries_logits = outputs.masks_queries_logits

        >>> # you can pass them to feature_extractor for instance postprocessing
        >>> predicted_instance_map = feature_extractor.post_process_instance_segmentation(
        ...    outputs, "instance", target_sizes=[image.size[::-1]]
        ... )[0]["segmentation"]
        >>> print(f'\n👉 Instance Predictions Shape: {list(predicted_instance_map.shape)}')
        Instance Predictions Shape: [512, 683]

        >>> ######## Panoptic Segmentation ########
        >>> inputs = feature_extractor(image, ["panoptic"], return_tensors="pt")

        >>> with torch.no_grad():
        ...    outputs = model(**inputs)
        >>> # model predicts class_queries_logits of shape `(batch_size, num_queries)`
        >>> # and masks_queries_logits of shape `(batch_size, num_queries, height, width)`
        >>> class_queries_logits = outputs.class_queries_logits
        >>> masks_queries_logits = outputs.masks_queries_logits

        >>> # you can pass them to feature_extractor for panoptic postprocessing
        >>> predicted_panoptic_map = feature_extractor.post_process_panoptic_segmentation(
        ...    outputs, target_sizes=[image.size[::-1]]
        ... )[0]["segmentation"]
        >>> f'👉 Panoptic Predictions Shape: {list(predicted_panoptic_map.shape)}'
        👉 Panoptic Predictions Shape: [512, 683]

        🎉 Congratulations on successfully running OneFormer
        📖 For more information, checkout the official repo: https://github.com/SHI-Labs/OneFormer
        ```
        """

        output_attentions = output_attentions if output_attentions is not None else self.config.output_attentions
        output_hidden_states = (
            output_hidden_states if output_hidden_states is not None else self.config.output_hidden_states
        )
        return_dict = return_dict if return_dict is not None else self.config.use_return_dict

        outputs: OneFormerModelOutput = self.model(
            pixel_values=pixel_values,
            task_inputs=task_inputs,
            text_inputs=text_inputs,
            pixel_mask=pixel_mask,
            output_hidden_states=output_hidden_states or self.config.general_config["use_auxiliary_loss"],
            output_attentions=output_attentions,
            return_dict=True,
        )

        loss, loss_dict, auxiliary_predictions = None, None, None

        class_queries_logits = outputs.transformer_decoder_class_predictions 
        masks_queries_logits = outputs.transformer_decoder_mask_predictions
        contrastive_queries_logits = outputs.transformer_decoder_contrastive_queries
        auxiliary_predictions = outputs.transformer_decoder_auxiliary_predictions
        text_queries = outputs.text_queries

        if mask_labels is not None and class_labels is not None:
            loss_dict: Dict[str, Tensor] = self.get_loss_dict(
                    masks_queries_logits=masks_queries_logits,
                    class_queries_logits=class_queries_logits,
                    contrastive_queries_logits=contrastive_queries_logits,
                    mask_labels=mask_labels,
                    class_labels=class_labels,
                    text_queries=text_queries,
                    auxiliary_predictions=auxiliary_predictions,
                    calculate_contrastive_loss=self.config.general_config["contrastive_temperature"] is not None,
            )
            loss = self.get_loss(loss_dict)

        output_auxiliary_logits = (
            self.config.general_config["output_auxiliary_logits"] if output_auxiliary_logits is None else output_auxiliary_logits
        )
        if not output_auxiliary_logits:
            auxiliary_predictions = None

        output = OneFormerForUniversalSegmentationOutput(
            class_queries_logits=class_queries_logits,
            masks_queries_logits=masks_queries_logits,
            auxiliary_predictions=auxiliary_predictions,
            loss=loss,
            **outputs,
        )

        if not return_dict:
            output = tuple(v for v in output.values())
            if loss is not None:
                output = ((loss)) + output
        return output