"""模型构建：SMP 语义分割、STNet（Swin）、InternImage 兼容骨干、Mask / Cascade Mask R-CNN。"""

from __future__ import annotations

import copy
import math
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


# ---------------------------------------------------------------------- STNet
_STNET_ENCODERS = {
    "swin_tiny": "swin_tiny_patch4_window7_224",
    "swin_small": "swin_small_patch4_window7_224",
    "swin_base": "swin_base_patch4_window7_224",
}


def _resize_swin_position_bias(state_dict: dict, own_state: dict) -> dict:
    """把不同窗口尺寸的相对位置偏置表插值到当前模型。

    timm 的 Swin 会按各 stage 的输入分辨率收缩窗口（7 → 4/2/...），
    ``relative_position_bias_table`` 的形状因此随训练 / 推理窗口变化。表项是
    连续的位置偏置，按二维网格插值即可复用（timm 加载 ImageNet 权重时也这样
    处理），这样同一份权重可以在不同 tile / 推理窗口下加载。
    """
    try:
        from timm.layers import resize_rel_pos_bias_table
    except ImportError:  # pragma: no cover - timm 版本过旧时保持严格加载
        return state_dict

    resized = dict(state_dict)
    for key, value in state_dict.items():
        key = str(key)
        if not key.endswith("relative_position_bias_table"):
            continue
        target = own_state.get(key)
        shape = getattr(value, "shape", None)
        if target is None or shape is None or tuple(shape) == tuple(target.shape):
            continue
        # 表项数 = (2w-1)^2，由表项数反推窗口边长
        source_side = math.isqrt(int(shape[0]))
        target_side = math.isqrt(int(target.shape[0]))
        if (
            source_side * source_side != int(shape[0])
            or target_side * target_side != int(target.shape[0])
            or source_side % 2 == 0
            or target_side % 2 == 0
        ):
            continue
        window = (target_side + 1) // 2
        resized[key] = resize_rel_pos_bias_table(
            value,
            new_window_size=(window, window),
            new_bias_shape=tuple(target.shape),
        )
    return resized


def _build_stnet(
    encoder: str,
    num_classes: int,
    pretrained: bool,
    tile_size: int,
    log: LogFn,
):
    """Swin Transformer 骨干 + 多尺度 FPN 解码器的语义分割网络。

    SMP 0.5 没有 Swin 编码器，这里直接用 timm（安装 SMP 时会作为依赖装上）
    创建 ``features_only`` 骨干。timm 的 Swin 在创建时固定输入尺寸，因此传入
    实际训练 / 推理窗口尺寸；权重结构与窗口尺寸无关的部分可直接复用，随窗口
    变化的相对位置偏置表在加载时插值。
    """
    try:
        import timm
    except ImportError as exc:
        raise ImportError(
            "缺少 timm，STNet 需要 timm（安装 segmentation-models-pytorch 时会自动安装），"
            "或手动执行：python -m pip install timm"
        ) from exc

    from torch import nn
    from torch.nn import functional

    name = _STNET_ENCODERS.get(encoder)
    if name is None:
        raise ValueError(f"STNet 不支持的骨干：{encoder}")
    size = max(32, int(tile_size))
    # 训练数据固定 tile_size × tile_size，推理按 32 对齐补齐；Swin 要求创建时
    # 固定输入尺寸，这里统一按 32 向上取整，小于模型尺寸的输入在 forward 里补零。
    size = ((size + 31) // 32) * 32

    def _create(use_pretrained: bool):
        return timm.create_model(
            name,
            pretrained=use_pretrained,
            features_only=True,
            img_size=size,
        )

    try:
        backbone = _create(bool(pretrained))
    except Exception as exc:  # 多为离线下载权重失败
        if not pretrained:
            raise
        log(f"预训练权重不可用（{exc}），改用随机初始化骨干继续训练")
        backbone = _create(False)

    channels = tuple(int(item) for item in backbone.feature_info.channels())
    width = 256
    levels = len(channels)

    class STNet(nn.Module):
        """Swin 特征金字塔 + 自顶向下融合 + 逐级上采样解码。"""

        def __init__(self) -> None:
            super().__init__()
            self.backbone = backbone
            self.lateral = nn.ModuleList(
                nn.Conv2d(channel, width, kernel_size=1) for channel in channels
            )
            self.smooth = nn.ModuleList(
                nn.Sequential(
                    nn.Conv2d(width, width, kernel_size=3, padding=1, bias=False),
                    nn.BatchNorm2d(width),
                    nn.ReLU(inplace=True),
                )
                for _ in range(max(0, levels - 1))
            )
            self.fuse = nn.Sequential(
                nn.Conv2d(width, width, kernel_size=3, padding=1, bias=False),
                nn.BatchNorm2d(width),
                nn.ReLU(inplace=True),
            )
            self.segmentation_head = nn.Sequential(
                nn.Conv2d(width, width // 2, kernel_size=3, padding=1, bias=False),
                nn.BatchNorm2d(width // 2),
                nn.ReLU(inplace=True),
                nn.Conv2d(width // 2, int(num_classes), kernel_size=1),
            )

        def load_state_dict(self, state_dict, strict=True):
            # Swin 的相对位置偏置表随窗口尺寸变化；加载时插值到当前尺寸，
            # 使不同训练 tile / 推理窗口的权重可以互相复用。
            return super().load_state_dict(
                _resize_swin_position_bias(state_dict, self.state_dict()),
                strict=strict,
            )

        def forward(self, x):
            input_height, input_width = int(x.shape[-2]), int(x.shape[-1])
            padded = x
            if input_height < size or input_width < size:
                padded = functional.pad(
                    x, (0, size - input_width, 0, size - input_height)
                )
            # timm 的 Swin features_only 输出 NHWC，转成 NCHW 再进卷积解码器
            features = [
                item.permute(0, 3, 1, 2).contiguous() for item in self.backbone(padded)
            ]
            laterals = [
                lateral(feature)
                for lateral, feature in zip(self.lateral, features, strict=False)
            ]
            merged = laterals[-1]
            for level in range(len(laterals) - 2, -1, -1):
                merged = functional.interpolate(
                    merged,
                    size=laterals[level].shape[-2:],
                    mode="bilinear",
                    align_corners=False,
                )
                merged = self.smooth[level](merged + laterals[level])
            logits = self.segmentation_head(self.fuse(merged))
            if padded is not x:
                logits = logits[..., :input_height, :input_width]
            if logits.shape[-2:] != x.shape[-2:]:
                logits = functional.interpolate(
                    logits, size=x.shape[-2:], mode="bilinear", align_corners=False
                )
            return logits

    log(f"STNet：timm {timm.__version__} / {name}，输入固定为 {size}，特征通道 {channels}")
    return STNet()


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
    if model_id == "stnet":
        return _build_stnet(encoder, int(num_classes), bool(pretrained), int(tile_size), log)
    if model_id == "internimage":
        return _build_internimage(encoder, int(num_classes), log)
    return _build_smp(model_id, encoder, int(num_classes), bool(pretrained), log)


def count_parameters(model) -> int:
    return sum(parameter.numel() for parameter in model.parameters() if parameter.requires_grad)


__all__ = ["build_model", "count_parameters"]
