"""模型构建：SMP 语义分割、InternImage 兼容骨干、Mask / Cascade Mask R-CNN。"""

from __future__ import annotations

import copy
from collections.abc import Callable

LogFn = Callable[[str], None]


# ---------------------------------------------------------------------- SMP
_SMP_MODEL_NAMES = {
    "unet": "Unet",
    "unetpp": "UnetPlusPlus",
    "deeplabv3": "DeepLabV3",
    "deeplabv3plus": "DeepLabV3Plus",
    "segformer": "Segformer",
}


def _build_smp(model_id: str, encoder: str, num_classes: int, pretrained: bool, log: LogFn):
    try:
        import segmentation_models_pytorch as smp
    except ImportError as exc:  # pragma: no cover - 由环境检查先拦截
        raise ImportError(
            "缺少 segmentation-models-pytorch，请在训练工具栏点「安装训练依赖」，"
            "或手动执行：python -m pip install segmentation-models-pytorch"
        ) from exc

    name = _SMP_MODEL_NAMES[model_id]
    factory = getattr(smp, name)
    kwargs = {
        "encoder_name": encoder,
        "encoder_weights": "imagenet" if pretrained else None,
        "in_channels": 3,
        "classes": int(num_classes),
    }
    if not pretrained:
        return factory(**kwargs)
    try:
        return factory(**kwargs)
    except Exception as exc:  # noqa: BLE001 —— 多数是离线导致权重下载失败
        log(f"预训练权重不可用（{exc}），改用随机初始化骨干继续训练")
        kwargs["encoder_weights"] = None
        return factory(**kwargs)


# ---------------------------------------------------------------------- InternImage
def _dcn_block_class():
    import torch
    from torch import nn
    from torchvision.ops import deform_conv2d

    class DCNBlock(nn.Module):
        """DCNv3 兼容块：可变形卷积 + 调制掩码 + 残差。

        官方 InternImage 的 DCNv3 需要 mmcv 编译算子；这里用
        ``torchvision.ops.deform_conv2d`` 实现同构的 3×3 可变形卷积
        （offset / modulation 均由 3×3 卷积预测，零初始化），**不加载**
        官方权重，界面与日志会明确标注。
        """

        def __init__(self, channels: int) -> None:
            super().__init__()
            self.offset = nn.Conv2d(channels, 2 * 9, kernel_size=3, padding=1)
            self.modulation = nn.Conv2d(channels, 9, kernel_size=3, padding=1)
            self.value = nn.Conv2d(channels, channels, kernel_size=3, padding=1, bias=False)
            self.norm = nn.GroupNorm(1, channels)
            self.act = nn.GELU()
            nn.init.zeros_(self.offset.weight)
            nn.init.zeros_(self.offset.bias)
            nn.init.zeros_(self.modulation.weight)
            nn.init.zeros_(self.modulation.bias)

        def forward(self, x):
            offset = self.offset(x)
            mask = torch.sigmoid(self.modulation(x) + 2.0)
            out = deform_conv2d(
                x,
                offset,
                self.value.weight,
                self.value.bias,
                padding=(1, 1),
                mask=mask,
            )
            out = self.act(self.norm(out))
            return x + out

    return DCNBlock


def _build_internimage(encoder: str, num_classes: int, log: LogFn):
    import torch
    from torch import nn

    widths = {
        "dcn_tiny": (32, 64, 128, 256),
        "dcn_small": (48, 96, 192, 384),
        "dcn_base": (64, 128, 256, 512),
    }.get(encoder, (32, 64, 128, 256))
    depths = {"dcn_tiny": (1, 1, 1, 1), "dcn_small": (1, 1, 1, 2), "dcn_base": (2, 2, 2, 2)}
    blocks = depths.get(encoder, (1, 1, 1, 1))
    dcn_block = _dcn_block_class()

    class InternImageUNet(nn.Module):
        def __init__(self) -> None:
            super().__init__()
            self.stem = nn.Sequential(
                nn.Conv2d(3, widths[0], kernel_size=3, stride=2, padding=1, bias=False),
                nn.GroupNorm(1, widths[0]),
                nn.GELU(),
            )
            stages = []
            previous = widths[0]
            for level, (width, depth) in enumerate(zip(widths, blocks, strict=False)):
                layers = []
                if level > 0:
                    layers.extend(
                        [
                            nn.Conv2d(previous, width, kernel_size=3, stride=2, padding=1, bias=False),
                            nn.GroupNorm(1, width),
                            nn.GELU(),
                        ]
                    )
                layers.extend(dcn_block(width) for _ in range(depth))
                stages.append(nn.Sequential(*layers))
                previous = width
            self.stages = nn.ModuleList(stages)
            self.lateral = nn.ModuleList(
                nn.Conv2d(width, widths[0], kernel_size=1) for width in widths
            )
            self.fuse = nn.Sequential(
                nn.Conv2d(widths[0] * 4, widths[0], kernel_size=3, padding=1, bias=False),
                nn.GroupNorm(1, widths[0]),
                nn.GELU(),
            )
            self.head = nn.Conv2d(widths[0], int(num_classes), kernel_size=1)

        def forward(self, x):
            size = x.shape[-2:]
            features = []
            current = self.stem(x)
            for stage, lateral in zip(self.stages, self.lateral, strict=False):
                current = stage(current)
                features.append(lateral(current))
            target = features[0].shape[-2:]
            resized = [
                item
                if item.shape[-2:] == target
                else nn.functional.interpolate(
                    item, size=target, mode="bilinear", align_corners=False
                )
                for item in features
            ]
            logits = self.head(self.fuse(torch.cat(resized, dim=1)))
            if logits.shape[-2:] != size:
                logits = nn.functional.interpolate(
                    logits, size=size, mode="bilinear", align_corners=False
                )
            return logits

    log("InternImage：使用 torchvision deform_conv2d 的 DCNv3 兼容实现（不加载官方权重）")
    return InternImageUNet()


# ---------------------------------------------------------------------- 实例
def _build_torchvision_instance(
    model_id: str,
    num_classes_including_background: int,
    pretrained: bool,
    tile_size: int,
    log: LogFn,
):
    import torchvision
    from torchvision.models.detection import maskrcnn_resnet50_fpn_v2
    from torchvision.models.detection.faster_rcnn import FastRCNNPredictor
    from torchvision.models.detection.mask_rcnn import MaskRCNNPredictor

    backbone_weights = None
    if pretrained:
        try:
            from torchvision.models import ResNet50_Weights

            backbone_weights = ResNet50_Weights.DEFAULT
        except Exception:  # noqa: BLE001 —— 老版本 torchvision
            backbone_weights = "DEFAULT"
    try:
        model = maskrcnn_resnet50_fpn_v2(
            weights=None,
            weights_backbone=backbone_weights,
            num_classes=int(num_classes_including_background),
        )
    except Exception as exc:
        if backbone_weights is None:
            raise
        log(f"预训练主干不可用（{exc}），改用随机初始化")
        model = maskrcnn_resnet50_fpn_v2(
            weights=None,
            weights_backbone=None,
            num_classes=int(num_classes_including_background),
        )

    # 小图训练：不做 800px 上采样，直接按标注块尺寸送网络
    model.transform.min_size = (int(tile_size),)
    model.transform.max_size = int(tile_size)

    # 显式重建检测 / 掩膜头，保证类别数与数据集一致（预训练权重只复用主干）
    in_features = model.roi_heads.box_predictor.cls_score.in_features
    model.roi_heads.box_predictor = FastRCNNPredictor(
        in_features, int(num_classes_including_background)
    )
    mask_features = model.roi_heads.mask_predictor.conv5_mask.in_channels
    model.roi_heads.mask_predictor = MaskRCNNPredictor(
        mask_features, 256, int(num_classes_including_background)
    )

    if model_id == "cascade_maskrcnn":
        _install_cascade_heads(model, log)
    log(f"torchvision {torchvision.__version__} / Mask R-CNN ResNet50-FPN v2")
    return model


def _cascade_class():
    import torch
    from torch import nn
    from torchvision.models.detection.roi_heads import fastrcnn_loss, maskrcnn_loss

    class CascadeRoIHeads(nn.Module):
        """三级级联 ROI 头，接口与 torchvision ``RoIHeads`` 一致。"""

        def __init__(self, heads) -> None:
            super().__init__()
            self.heads = nn.ModuleList(heads)

        def forward(self, features, proposals, image_shapes, targets=None):
            if self.training:
                return self._forward_train(features, proposals, image_shapes, targets)
            return self._forward_eval(features, proposals, image_shapes)

        # ---------------------------------------------------------- 训练
        def _forward_train(self, features, proposals, image_shapes, targets):
            losses: dict = {}
            current = proposals
            state = None
            for index, head in enumerate(self.heads):
                selected, matched_idxs, labels, regression_targets = head.select_training_samples(
                    current, targets
                )
                box_features = head.box_head(head.box_roi_pool(features, selected, image_shapes))
                class_logits, box_regression = head.box_predictor(box_features)
                loss_classifier, loss_box_reg = fastrcnn_loss(
                    class_logits, box_regression, labels, regression_targets
                )
                losses[f"stage{index + 1}_loss_classifier"] = loss_classifier
                losses[f"stage{index + 1}_loss_box_reg"] = loss_box_reg
                boxes, _scores, _labels = head.postprocess_detections(
                    class_logits, box_regression, selected, image_shapes
                )
                next_proposals = []
                for detected, previous in zip(boxes, selected, strict=False):
                    candidate = detected.detach()
                    # 该级没有输出时退回本级输入框，避免下一级拿到空 batch
                    next_proposals.append(candidate if candidate.numel() else previous)
                current = next_proposals
                state = (head, selected, matched_idxs, labels)

            head, selected, matched_idxs, labels = state
            if head.has_mask():
                losses.update(
                    self._mask_loss(
                        head, features, selected, image_shapes, targets, matched_idxs, labels
                    )
                )
            return [], losses

        @staticmethod
        def _mask_loss(head, features, proposals, image_shapes, targets, matched_idxs, labels):
            mask_proposals = []
            positive_matches = []
            for index in range(len(proposals)):
                positive = torch.where(labels[index] > 0)[0]
                mask_proposals.append(proposals[index][positive])
                positive_matches.append(matched_idxs[index][positive])
            if not any(item.numel() for item in mask_proposals):
                return {}
            mask_features = head.mask_roi_pool(features, mask_proposals, image_shapes)
            mask_logits = head.mask_predictor(head.mask_head(mask_features))
            gt_masks = [target["masks"] for target in targets]
            gt_labels = [target["labels"] for target in targets]
            return {
                "stage3_loss_mask": maskrcnn_loss(
                    mask_logits, mask_proposals, gt_masks, gt_labels, positive_matches
                )
            }

        # ---------------------------------------------------------- 推理
        def _forward_eval(self, features, proposals, image_shapes):
            current = proposals
            detections = None
            for head in self.heads:
                result, _losses = head(features, current, image_shapes, None)
                current = [item["boxes"] for item in result]
                detections = result
            return detections, {}

    return CascadeRoIHeads


def _install_cascade_heads(model, log: LogFn) -> None:
    """把单个 ROI 头替换成三级级联（IoU 0.5 / 0.6 / 0.7）。

    每级复用 torchvision 的 ``RoIHeads``；前两级去掉 mask 分支，最后一级保留。
    训练时逐级用上一级的检测框作为下一级提议。
    """
    from torchvision.models.detection._utils import Matcher

    cascade_class = _cascade_class()
    thresholds = (0.5, 0.6, 0.7)
    heads = []
    for index, threshold in enumerate(thresholds):
        head = copy.deepcopy(model.roi_heads)
        head.proposal_matcher = Matcher(threshold, 0.5, allow_low_quality_matches=False)
        if index < len(thresholds) - 1:
            head.mask_roi_pool = None
            head.mask_head = None
            head.mask_predictor = None
        heads.append(head)
    model.roi_heads = cascade_class(heads)
    log("Cascade Mask R-CNN：三级 ROI（IoU 0.5 / 0.6 / 0.7），预训练仅加载 ResNet50 主干")


# ---------------------------------------------------------------------- 入口
def build_model(
    task: str,
    model_id: str,
    encoder: str,
    num_classes: int,
    *,
    pretrained: bool = True,
    tile_size: int = 512,
    log: LogFn = print,
):
    """按任务构建模型。``num_classes`` 对语义任务是输出通道数，对实例含背景。"""
    if task == "instances":
        return _build_torchvision_instance(
            model_id,
            int(num_classes),
            bool(pretrained),
            int(tile_size),
            log,
        )
    if model_id == "internimage":
        return _build_internimage(encoder, int(num_classes), log)
    return _build_smp(model_id, encoder, int(num_classes), bool(pretrained), log)


def count_parameters(model) -> int:
    return sum(parameter.numel() for parameter in model.parameters() if parameter.requires_grad)


__all__ = ["build_model", "count_parameters"]
