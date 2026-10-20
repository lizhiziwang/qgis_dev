"""训练任务 / 模型 / 骨干目录（纯 stdlib，界面与训练子进程共用）。

模型分三族：

- ``smp``：``segmentation-models-pytorch`` 提供的语义分割网络；
- ``internimage``：用 ``torchvision.ops.deform_conv2d`` 实现的 DCNv3 兼容骨干
  （官方 DCNv3 需要编译 mmcv CUDA 算子，Windows + 当前 torch 无可用轮子）；
- ``torchvision``：Mask R-CNN / Cascade Mask R-CNN 实例分割。

界面只读这里的 id / 显示名 / 可选骨干，不 import 任何深度学习框架。
"""

from __future__ import annotations

from dataclasses import dataclass

#: 训练任务：对象提取（语义二值）/ 地物分类（多类语义）/ 对象提取（实例）
TASKS: tuple[tuple[str, str], ...] = (
    ("objects", "对象提取（语义分割）"),
    ("landcover", "地物分类（语义分割）"),
    ("instances", "对象提取（实例分割）"),
)

TASK_IDS: tuple[str, ...] = tuple(task for task, _label in TASKS)
SEMANTIC_TASKS: tuple[str, ...] = ("objects", "landcover")
INSTANCE_TASKS: tuple[str, ...] = ("instances",)

#: 语义分割损失
SEMANTIC_LOSSES: tuple[tuple[str, str], ...] = (
    ("dice_ce", "Dice + 交叉熵"),
    ("ce", "交叉熵"),
)
LOSS_IDS: tuple[str, ...] = tuple(loss for loss, _label in SEMANTIC_LOSSES)


@dataclass(frozen=True)
class EncoderOption:
    """模型可选骨干。``id`` 传给 SMP / 自建骨干，``name`` 只用于界面显示。"""

    id: str
    name: str


@dataclass(frozen=True)
class ModelOption:
    """一个可选训练模型。"""

    id: str
    name: str
    family: str
    tasks: tuple[str, ...]
    encoders: tuple[EncoderOption, ...]
    default_encoder: str
    #: False 表示没有可信的官方预训练权重（界面会禁用“预训练骨干”）
    pretrained_supported: bool = True
    note: str = ""

    def supports_task(self, task: str) -> bool:
        return task in self.tasks

    def encoder_ids(self) -> tuple[str, ...]:
        return tuple(encoder.id for encoder in self.encoders)

    def encoder_label(self, encoder_id: str) -> str:
        for encoder in self.encoders:
            if encoder.id == encoder_id:
                return encoder.name
        return encoder_id


def _encoders(*items: str) -> tuple[EncoderOption, ...]:
    return tuple(EncoderOption(item, item) for item in items)


#: 语义分割 / 实例分割模型目录
MODELS: tuple[ModelOption, ...] = (
    ModelOption(
        id="unet",
        name="U-Net",
        family="smp",
        tasks=SEMANTIC_TASKS,
        encoders=_encoders("resnet34", "resnet50", "efficientnet-b0", "mobilenet_v2"),
        default_encoder="resnet34",
        note="遥感分割最常用基线，收敛稳定。",
    ),
    ModelOption(
        id="unetpp",
        name="U-Net++（改进 U-Net）",
        family="smp",
        tasks=SEMANTIC_TASKS,
        encoders=_encoders("resnet34", "resnet50", "efficientnet-b0", "mobilenet_v2"),
        default_encoder="resnet34",
        note="嵌套跳跃连接，小目标更敏感，显存占用略高。",
    ),
    ModelOption(
        id="deeplabv3",
        name="DeepLabV3",
        family="smp",
        tasks=SEMANTIC_TASKS,
        encoders=_encoders("resnet50", "resnet101", "mobilenet_v2"),
        default_encoder="resnet50",
        note="空洞空间金字塔池化，适合大范围地物。",
    ),
    ModelOption(
        id="deeplabv3plus",
        name="DeepLabV3+",
        family="smp",
        tasks=SEMANTIC_TASKS,
        encoders=_encoders("resnet50", "resnet101", "mobilenet_v2"),
        default_encoder="resnet50",
        note="DeepLabV3 + 编码器-解码器，边界更精细。",
    ),
    ModelOption(
        id="segformer",
        name="SegFormer",
        family="smp",
        tasks=SEMANTIC_TASKS,
        encoders=_encoders("mit_b0", "mit_b1", "mit_b2", "mit_b3"),
        default_encoder="mit_b0",
        note="Transformer 结构，轻量且全局感受野大。",
    ),
    ModelOption(
        id="internimage",
        name="InternImage（DCNv3 兼容实现）",
        family="internimage",
        tasks=SEMANTIC_TASKS,
        encoders=(
            EncoderOption("dcn_tiny", "DCN-Tiny"),
            EncoderOption("dcn_small", "DCN-Small"),
            EncoderOption("dcn_base", "DCN-Base"),
        ),
        default_encoder="dcn_tiny",
        pretrained_supported=False,
        note="可变形卷积骨干；当前实现不加载官方 InternImage 权重。",
    ),
    ModelOption(
        id="maskrcnn",
        name="Mask R-CNN",
        family="torchvision",
        tasks=INSTANCE_TASKS,
        encoders=_encoders("resnet50"),
        default_encoder="resnet50",
        note="实例分割基线；预训练仅加载 ResNet50 主干。",
    ),
    ModelOption(
        id="cascade_maskrcnn",
        name="Cascade Mask R-CNN",
        family="torchvision",
        tasks=INSTANCE_TASKS,
        encoders=_encoders("resnet50"),
        default_encoder="resnet50",
        note="三级级联 ROI（IoU 0.5 / 0.6 / 0.7）；预训练仅加载 ResNet50 主干。",
    ),
)

_MODELS_BY_ID = {model.id: model for model in MODELS}


def task_label(task: str) -> str:
    for key, label in TASKS:
        if key == task:
            return label
    return task


def is_semantic_task(task: str) -> bool:
    return task in SEMANTIC_TASKS


def is_instance_task(task: str) -> bool:
    return task in INSTANCE_TASKS


def get_model(model_id: str) -> ModelOption | None:
    return _MODELS_BY_ID.get(str(model_id or ""))


def models_for_task(task: str) -> tuple[ModelOption, ...]:
    return tuple(model for model in MODELS if model.supports_task(task))


def default_model_id(task: str) -> str:
    models = models_for_task(task)
    if models:
        return models[0].id
    return ""


def default_encoder(model_id: str) -> str:
    model = get_model(model_id)
    return model.default_encoder if model else ""


def default_loss(task: str) -> str:
    return "dice_ce" if is_semantic_task(task) else ""


def normalize_for_task(task: str, model_id: str, encoder_id: str = "") -> tuple[str, str]:
    """把任意（可能过期的）模型 / 骨干选择收敛到当前任务可用的组合。"""
    model = get_model(model_id)
    if model is None or not model.supports_task(task):
        model = models_for_task(task)[0] if models_for_task(task) else None
    if model is None:
        return "", ""
    encoder = encoder_id if encoder_id in model.encoder_ids() else model.default_encoder
    return model.id, encoder


def model_uses_smp(model_id: str) -> bool:
    model = get_model(model_id)
    return model is not None and model.family == "smp"


__all__ = [
    "INSTANCE_TASKS",
    "LOSS_IDS",
    "MODELS",
    "SEMANTIC_LOSSES",
    "SEMANTIC_TASKS",
    "TASKS",
    "TASK_IDS",
    "EncoderOption",
    "ModelOption",
    "default_encoder",
    "default_loss",
    "default_model_id",
    "get_model",
    "is_instance_task",
    "is_semantic_task",
    "model_uses_smp",
    "models_for_task",
    "normalize_for_task",
    "task_label",
]
