"""微调辅助：训练方式归一化、checkpoint 结构匹配与骨干冻结。

本模块不 import torch：``engine`` 在外部 Python 里调用它，测试在 QGIS Python
（没有 torch）下也能直接覆盖。权重张量只按 ``.shape`` 比较，因此可以用带
``shape`` 属性的假对象做单测。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from . import catalog

#: 微调时允许的结构化元数据字段（由 ``engine._checkpoint`` 写入）
_META_TASK = "task"
_META_MODEL = "model"
_META_ARCH = "arch"
_META_ENCODER = "encoder"


@dataclass(frozen=True)
class StateMatch:
    """基础模型 state_dict 与当前模型的逐键匹配结果。"""

    #: 名称与形状都一致、可以加载的张量
    loadable: dict[str, Any] = field(default_factory=dict)
    #: 类别相关输出头：按需跳过（类别数 / 类别名 / 任务变化时重建）
    head_skipped: tuple[str, ...] = ()
    #: 非输出头但形状不符：通常意味着模型结构或骨干不一致，应直接报错
    shape_mismatch: tuple[str, ...] = ()
    #: 当前模型里不存在的键（例如旧版本的级联头）
    absent: tuple[str, ...] = ()


def resolve_mode(params: dict) -> str:
    """把 manifest 里的训练方式收敛到 ``catalog.MODE_IDS``。

    旧清单没有 ``mode`` 字段：有 ``init_checkpoint`` 视为继续训练，否则从零训练。
    """
    mode = str(params.get("mode") or "").strip().lower()
    if mode in catalog.MODE_IDS:
        return mode
    return "resume" if str(params.get("init_checkpoint") or "").strip() else "scratch"


def finetune_meta_problem(payload: dict, meta: dict) -> str:
    """校验基础模型元数据；兼容时返回空串，否则返回面向用户的错误。"""
    saved_task = str(payload.get(_META_TASK) or "")
    current_task = str(meta.get("task") or "")
    if saved_task and (saved_task == "instances") != (current_task == "instances"):
        return (
            f"任务不兼容：基础模型为 {saved_task}，当前为 {current_task}；"
            "语义分割与实例分割模型不能互相微调"
        )

    saved_model = str(payload.get(_META_MODEL) or payload.get(_META_ARCH) or "")
    current_model = str(meta.get("model") or "")
    if saved_model and saved_model != current_model:
        return (
            f"模型结构不一致：基础模型为 {saved_model}，当前为 {current_model}；"
            "请选择相同模型"
        )

    saved_encoder = str(payload.get(_META_ENCODER) or "")
    current_encoder = str(meta.get("encoder") or "")
    if saved_encoder and saved_encoder != current_encoder:
        return (
            f"骨干不一致：基础模型为 {saved_encoder}，当前为 {current_encoder}；"
            "请选择相同骨干"
        )
    return ""


def is_class_head_key(key: str, task: str, model_id: str) -> bool:
    """判断某个权重键是否属于随类别数变化的输出头。"""
    key = str(key)
    if task == "instances":
        # Mask / Cascade Mask R-CNN：分类与回归 / 掩膜头随类别数变化
        return ".box_predictor." in key or ".mask_predictor." in key
    if model_id == "internimage":
        return key.startswith("head.")
    # SMP 各模型的最后 1×1 分类卷积
    return key.startswith("segmentation_head.")


def strip_common_prefix(state: dict) -> dict:
    """去掉 DataParallel / 导出工具统一加上的 ``module.`` 前缀。"""
    items = {str(key): value for key, value in state.items()}
    prefix = "module."
    if items and all(key.startswith(prefix) for key in items):
        return {key[len(prefix) :]: value for key, value in items.items()}
    return items


def _shape_of(value) -> tuple[int, ...] | None:
    shape = getattr(value, "shape", None)
    if shape is None:
        return None
    try:
        return tuple(int(item) for item in shape)
    except (TypeError, ValueError):
        return None


def _is_resizable_key(key: str, model_id: str) -> bool:
    """STNet 的相对位置偏置表随窗口尺寸变化，由模型加载时插值。"""
    return model_id == "stnet" and key.endswith("relative_position_bias_table")


def matching_state_entries(
    saved: dict,
    own: dict,
    *,
    task: str,
    model_id: str,
    skip_head: bool,
) -> StateMatch:
    """逐键匹配基础权重与当前模型。

    - 输出头：``skip_head`` 为真或形状不符 → 跳过头（等待重新初始化）；
    - 非输出头形状不符 → 记入 ``shape_mismatch``，由调用方报错；
    - 当前模型没有的键 → ``absent``（不参与加载）。
    """
    own_shapes = {str(key): _shape_of(value) for key, value in own.items()}
    loadable: dict[str, Any] = {}
    head_skipped: list[str] = []
    shape_mismatch: list[str] = []
    absent: list[str] = []

    for raw_key, value in saved.items():
        key = str(raw_key)
        if key not in own_shapes:
            absent.append(key)
            continue
        head = is_class_head_key(key, task, model_id)
        same_shape = own_shapes[key] is not None and own_shapes[key] == _shape_of(value)
        if head and (skip_head or not same_shape):
            head_skipped.append(key)
            continue
        if not same_shape:
            if _is_resizable_key(key, model_id):
                loadable[key] = value
            else:
                shape_mismatch.append(key)
            continue
        loadable[key] = value

    return StateMatch(
        loadable=loadable,
        head_skipped=tuple(head_skipped),
        shape_mismatch=tuple(shape_mismatch),
        absent=tuple(absent),
    )


def backbone_parts(model, task: str) -> list[Any]:
    """返回需要冻结的骨干模块；无法识别时返回空列表。

    - 实例分割：torchvision 的 ``backbone``（ResNet + FPN）；
    - SMP 语义分割：``encoder``；
    - STNet：timm 的 ``backbone``（Swin Transformer）；
    - InternImage 兼容实现：``stem`` + ``stages``（分类头 / 融合层仍训练）。
    """
    if task == "instances":
        backbone = getattr(model, "backbone", None)
        return [backbone] if backbone is not None else []

    backbone = getattr(model, "backbone", None)
    if backbone is not None:
        return [backbone]

    encoder = getattr(model, "encoder", None)
    if encoder is not None:
        return [encoder]

    parts = [getattr(model, "stem", None), getattr(model, "stages", None)]
    return [part for part in parts if part is not None]


def set_backbone_frozen(parts: list[Any], frozen: bool) -> None:
    """冻结 / 解冻骨干参数。

    ``train(not frozen)`` 同步切换 BatchNorm 的 running 统计量：只设
    ``requires_grad=False`` 时 BN 仍会在训练态更新均值 / 方差。
    """
    for part in parts:
        for parameter in part.parameters():
            parameter.requires_grad = not frozen
        part.train(not frozen)


def backbone_should_be_frozen(params: dict, epoch: int) -> bool:
    """当前轮次骨干是否应保持冻结（1 起算）。"""
    if not bool(params.get("freeze_backbone")):
        return False
    try:
        limit = int(params.get("unfreeze_epochs") or 0)
    except (TypeError, ValueError):
        limit = 0
    return limit <= 0 or int(epoch) <= limit


__all__ = [
    "StateMatch",
    "backbone_parts",
    "backbone_should_be_frozen",
    "finetune_meta_problem",
    "is_class_head_key",
    "matching_state_entries",
    "resolve_mode",
    "set_backbone_frozen",
    "strip_common_prefix",
]
