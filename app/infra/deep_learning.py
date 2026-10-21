"""深度学习标注配置（``config/deep_learning.yaml``）。

SAM 推理跑在外部 Python 子进程里，本模块只负责把 YAML 读成强类型配置：

- 支持 ``${VAR}`` / ``${VAR:-默认值}`` 环境变量展开（与 ``app.yaml`` 一致）；
- 外部 Python / 源码目录 / 权重 / 模型 / 缓存设置允许被 QSettings（设置页）覆盖；
- 文件缺失 / 损坏 / 字段类型不对时回落到内置默认值并记日志，绝不阻止标注窗口打开。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from app.core.training.trainlib import catalog as training_catalog
from app.infra.fs.paths import Paths
from app.infra.logging import get_logger

logger = get_logger(__name__)

_CONFIG_NAME = "deep_learning.yaml"
_SETTINGS_ORG = "qgis_dev"
_SETTINGS_APP = "qgis_dev"

#: QSettings 键（设置页 / 标注窗口共用）
SETTING_PYTHON = "deep_learning/python"
SETTING_SAM_SRC = "deep_learning/sam_src"
SETTING_CHECKPOINT = "deep_learning/checkpoint"
SETTING_MODEL_ID = "deep_learning/model_id"
SETTING_MODEL_TYPE = "deep_learning/model_type"
SETTING_DEVICE = "deep_learning/device"
SETTING_SAM2_CONFIG = "deep_learning/sam2_config"
SETTING_MODELS_DIR = "deep_learning/models_dir"
SETTING_CACHE_ENABLED = "deep_learning/cache_enabled"
SETTING_CACHE_DIR = "deep_learning/cache_dir"
SETTING_CACHE_MAX_MB = "deep_learning/cache_max_mb"
SETTING_CACHE_CLEAR_ON_CLOSE = "deep_learning/cache_clear_on_close"

#: 训练工作区设置的 QSettings 键前缀（``deep_learning/training/<字段>``）
TRAINING_SETTING_PREFIX = "deep_learning/training"
TRAINING_FIELDS: tuple[str, ...] = (
    "python",
    "dataset_dir",
    "output_dir",
    "task",
    "model",
    "encoder",
    "loss",
    "epochs",
    "batch_size",
    "lr",
    "tile_size",
    "val_percent",
    "early_stop",
    "seed",
    "device",
    "pretrained",
    "augment",
    "min_instance_area",
    "init_checkpoint",
)


def training_setting_key(field: str) -> str:
    return f"{TRAINING_SETTING_PREFIX}/{field}"


#: 推理工作区设置的 QSettings 键前缀（``deep_learning/inference/<字段>``）
INFERENCE_SETTING_PREFIX = "deep_learning/inference"
INFERENCE_FIELDS: tuple[str, ...] = (
    "python",
    "model_dir",
    "image",
    "output_dir",
    "window_size",
    "overlap_percent",
    "zero_as_nodata",
    "batch_size",
    "device",
    "bands",
    "score_threshold",
    "min_instance_pixels",
    "preview_max_side",
)


def inference_setting_key(field: str) -> str:
    return f"{INFERENCE_SETTING_PREFIX}/{field}"

#: 兼容旧代码：权重路径键名
CHECKPOINT_SETTINGS_KEY = SETTING_CHECKPOINT

_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")

#: 标签调色板默认值（与 YAML 保持同步；YAML 缺字段时用它）
DEFAULT_PALETTE: tuple[str, ...] = (
    "#e6194b",
    "#3cb44b",
    "#ffe119",
    "#4363d8",
    "#f58231",
    "#911eb4",
    "#46f0f0",
    "#f032e6",
    "#bcf60c",
    "#fabebe",
    "#008080",
    "#e6beff",
    "#9a6324",
    "#fffac8",
    "#800000",
    "#aaffc3",
)


def expand_env(value: str) -> str:
    """展开 ``${VAR}`` / ``${VAR:-默认值}``；未定义且无默认时替换为空串。"""

    def _replace(match: re.Match[str]) -> str:
        name, default = match.group(1), match.group(2)
        return os.environ.get(name, "" if default is None else default)

    return _ENV_PATTERN.sub(_replace, value)


def _as_int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_str(value, default: str = "") -> str:
    return default if value is None else str(value)


def _as_bool(value, default: bool) -> bool:
    """QSettings / YAML 的布尔值容错（``"true"`` / ``"1"`` / ``"on"`` 均算真）。"""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in ("1", "true", "yes", "on"):
        return True
    if text in ("0", "false", "no", "off"):
        return False
    return default


def _as_path(value) -> str:
    text = expand_env(_as_str(value)).strip()
    return str(Path(text)) if text else ""


def _as_bands(value, default: tuple[int, int, int] = (0, 0, 0)) -> tuple[int, int, int]:
    """解析 R/G/B 波段：``"1,2,3"`` / ``[1, 2, 3]``；非法值回落默认。"""
    if isinstance(value, str):
        parts = [part for part in value.replace(";", ",").split(",") if part.strip()]
        value = parts
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        return default
    try:
        bands = tuple(int(item) for item in value)
    except (TypeError, ValueError):
        return default
    if any(item < 0 or item > 65535 for item in bands):
        return default
    if any(item == 0 for item in bands):
        return (0, 0, 0)
    return bands


@dataclass(frozen=True)
class SamConfig:
    """SAM 子进程 / 模型 / 缓存配置。"""

    python: str = ""
    sam_src: str = ""
    checkpoint: str = ""
    model_type: str = "auto"
    device: str = "auto"
    #: 注册表模型 id（如 ``sam2.1_hiera_large``）；空表示按文件名推断
    model_id: str = ""
    #: SAM2 hydra 配置（注册表模型自动带出）；空表示按文件名推断
    sam2_config: str = ""
    #: 模型下载/管理目录；空 = 用户配置目录/models/sam
    models_dir: str = ""
    #: 图像特征缓存
    cache_enabled: bool = True
    cache_dir: str = ""
    cache_max_mb: int = 2048
    cache_clear_on_close: bool = False

    load_timeout_s: int = 180
    set_image_timeout_s: int = 60
    predict_timeout_s: int = 120
    auto_timeout_s: int = 900
    max_line_bytes: int = 8 * 1024 * 1024


@dataclass(frozen=True)
class AnnotationConfig:
    """画布/标注行为配置。"""

    max_side: int = 4096
    undo_limit: int = 40
    palette: tuple[str, ...] = DEFAULT_PALETTE
    #: 宫格切片：默认块大小（像素）、重叠度（百分比）、自动勾选阈值（长边像素）
    grid_tile_size: int = 512
    grid_overlap_percent: int = 10
    grid_auto_threshold: int = 2000


@dataclass(frozen=True)
class AutoSegConfig:
    """SAM 自动分割参数。"""

    points_per_side: int = 24
    pred_iou_thresh: float = 0.86
    stability_score_thresh: float = 0.92
    min_mask_region_area: int = 0
    max_candidates: int = 200
    max_side: int = 2048


@dataclass(frozen=True)
class TrainingConfig:
    """训练工作区默认值（YAML ``training:`` 段 + QSettings 覆盖）。"""

    #: 外部 Python；空表示回退 ``sam.python``
    python: str = ""
    dataset_dir: str = ""
    output_dir: str = ""
    task: str = "objects"
    model: str = "unet"
    encoder: str = "resnet34"
    loss: str = "dice_ce"
    epochs: int = 30
    batch_size: int = 4
    lr: float = 0.0001
    tile_size: int = 512
    val_percent: int = 20
    early_stop: int = 8
    seed: int = 42
    device: str = "auto"
    pretrained: bool = True
    augment: bool = True
    min_instance_area: int = 16
    init_checkpoint: str = ""


@dataclass(frozen=True)
class InferenceConfig:
    """推理工作区默认值（YAML ``inference:`` 段 + QSettings 覆盖）。"""

    #: 外部 Python；空表示回退 ``training.python`` → ``sam.python``
    python: str = ""
    #: 训练 run 根目录；空表示回退 ``training.output_dir``
    model_dir: str = ""
    image: str = ""
    #: 留空 = ``<checkpoint 所在 run>/predictions``
    output_dir: str = ""
    #: 0 = 跟随 checkpoint 的 tile_size；实例分割强制跟随
    window_size: int = 0
    overlap_percent: int = 25
    #: 选中波段同时为 0 的像元视为 nodata（常见黑色填充边界），不参与推理
    zero_as_nodata: bool = True
    batch_size: int = 4
    device: str = "auto"
    #: R/G/B 波段；(0, 0, 0) = 按影像自动
    bands: tuple[int, int, int] = (0, 0, 0)
    score_threshold: float = 0.5
    min_instance_pixels: int = 16
    preview_max_side: int = 4096


@dataclass(frozen=True)
class DeepLearningConfig:
    """标注功能总配置。"""

    sam: SamConfig = field(default_factory=SamConfig)
    annotation: AnnotationConfig = field(default_factory=AnnotationConfig)
    auto: AutoSegConfig = field(default_factory=AutoSegConfig)
    training: TrainingConfig = field(default_factory=TrainingConfig)
    inference: InferenceConfig = field(default_factory=InferenceConfig)
    source: str = ""
    load_error: str = ""


def _config_path(path: Path | None) -> Path:
    if path is not None:
        return Path(path)
    return Paths.default().config_file(_CONFIG_NAME)


def _qsettings_value(key: str, default=None):
    """读取 QSettings 值；没有 Qt 的纯配置单测里返回 ``default``。

    ``default=None`` 也是读取哨兵：返回值是 ``None`` 表示键不存在，
    返回空串表示用户显式把它清空了（例如删除当前模型后权重置空）。
    """
    try:
        from qgis.PyQt.QtCore import QSettings
    except Exception:  # noqa: BLE001 —— 纯配置单测可能没有 Qt
        return default
    try:
        value = QSettings(_SETTINGS_ORG, _SETTINGS_APP).value(key, default)
    except Exception:  # noqa: BLE001
        return default
    return default if value is None else value


def _qsettings_checkpoint() -> str:
    """兼容旧接口：返回 QSettings 里的权重路径（没有则为空串）。"""
    return _as_str(_qsettings_value(SETTING_CHECKPOINT, ""))


def save_sam_setting(key: str, value) -> None:
    """保存深度学习设置页/标注窗口的单个选项（QSettings，下一步启动生效）。"""
    from qgis.PyQt.QtCore import QSettings

    settings = QSettings(_SETTINGS_ORG, _SETTINGS_APP)
    settings.setValue(str(key), "" if value is None else value)
    settings.sync()


def save_training_setting(field: str, value) -> None:
    """保存训练工作区的单个表单值（字段名见 :data:`TRAINING_FIELDS`）。"""
    save_sam_setting(training_setting_key(field), value)


def save_training_settings(values: dict) -> None:
    """批量保存训练表单；未知字段忽略。"""
    for name, value in values.items():
        if name in TRAINING_FIELDS:
            save_training_setting(name, value)


def save_inference_setting(field: str, value) -> None:
    """保存推理工作区的单个表单值（字段名见 :data:`INFERENCE_FIELDS`）。"""
    save_sam_setting(inference_setting_key(field), value)


def save_inference_settings(values: dict) -> None:
    """批量保存推理表单；未知字段忽略。"""
    for name, value in values.items():
        if name in INFERENCE_FIELDS:
            save_inference_setting(name, value)


def remove_sam_setting(key: str) -> None:
    """删除单个设置项（回到 YAML / 内置默认值）。"""
    from qgis.PyQt.QtCore import QSettings

    settings = QSettings(_SETTINGS_ORG, _SETTINGS_APP)
    settings.remove(str(key))
    settings.sync()


def save_checkpoint_override(path: str) -> None:
    """记住用户在界面里选择的权重路径（QSettings，覆盖 YAML）。"""
    save_sam_setting(SETTING_CHECKPOINT, str(path or ""))


def load_deep_learning_config(path: Path | None = None) -> DeepLearningConfig:
    """读取配置；任何异常都回落到默认值，并通过 ``load_error`` 说明原因。"""
    config_path = _config_path(path)
    raw: dict = {}
    load_error = ""
    try:
        import yaml

        data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        if isinstance(data, dict):
            raw = data
        else:
            load_error = f"配置格式不是映射：{config_path}"
    except FileNotFoundError:
        load_error = f"配置文件不存在：{config_path}"
    except Exception as exc:  # noqa: BLE001 —— 配置损坏不能阻止窗口打开
        load_error = f"读取配置失败：{exc}"
    if load_error:
        logger.warning("%s，使用内置默认值", load_error)

    sam_raw = raw.get("sam") or {}
    ann_raw = raw.get("annotation") or {}
    auto_raw = raw.get("auto") or {}
    grid_raw = ann_raw.get("grid") or {}
    if not isinstance(grid_raw, dict):
        grid_raw = {}
    cache_raw = raw.get("cache") or {}
    if not isinstance(cache_raw, dict):
        cache_raw = {}
    training_raw = raw.get("training") or {}
    if not isinstance(training_raw, dict):
        training_raw = {}
    inference_raw = raw.get("inference") or {}
    if not isinstance(inference_raw, dict):
        inference_raw = {}

    def setting(key: str):
        return _qsettings_value(key, None)

    def text_override(key: str, fallback) -> str:
        value = setting(key)
        return _as_str(fallback) if value is None else _as_str(value)

    def path_override(key: str, fallback) -> str:
        value = setting(key)
        return _as_path(fallback) if value is None else _as_path(value)

    def int_override(key: str, fallback) -> int:
        value = setting(key)
        default = _as_int(fallback, 0)
        return default if value is None else _as_int(value, default)

    def bool_override(key: str, fallback) -> bool:
        value = setting(key)
        default = _as_bool(fallback, False)
        return default if value is None else _as_bool(value, default)

    def float_override(key: str, fallback) -> float:
        value = setting(key)
        default = _as_float(fallback, 0.0)
        return default if value is None else _as_float(value, default)

    palette_raw = ann_raw.get("palette") or DEFAULT_PALETTE
    try:
        palette = tuple(str(item) for item in palette_raw if str(item).strip())
    except TypeError:
        palette = DEFAULT_PALETTE
    if not palette:
        palette = DEFAULT_PALETTE

    model_type = text_override(SETTING_MODEL_TYPE, raw.get("model_type") or "auto").lower()
    if model_type not in ("auto", "vit_h", "vit_l", "vit_b"):
        model_type = "auto"
    device = text_override(SETTING_DEVICE, raw.get("device") or "auto").lower()
    if device not in ("auto", "cuda", "cpu"):
        device = "auto"
    cache_max_mb = min(102400, max(0, int_override(SETTING_CACHE_MAX_MB, cache_raw.get("max_mb", 2048))))

    sam = SamConfig(
        python=path_override(SETTING_PYTHON, raw.get("python")),
        sam_src=path_override(SETTING_SAM_SRC, raw.get("sam_src")),
        checkpoint=path_override(SETTING_CHECKPOINT, raw.get("checkpoint")),
        model_type=model_type,
        device=device,
        model_id=text_override(SETTING_MODEL_ID, raw.get("model_id")),
        sam2_config=text_override(SETTING_SAM2_CONFIG, raw.get("sam2_config")),
        models_dir=path_override(SETTING_MODELS_DIR, raw.get("models_dir")),
        cache_enabled=bool_override(SETTING_CACHE_ENABLED, cache_raw.get("enabled", True)),
        cache_dir=path_override(SETTING_CACHE_DIR, cache_raw.get("dir")),
        cache_max_mb=cache_max_mb,
        cache_clear_on_close=bool_override(
            SETTING_CACHE_CLEAR_ON_CLOSE, cache_raw.get("clear_on_close", False)
        ),
        load_timeout_s=_as_int(sam_raw.get("load_timeout_s"), 180),
        set_image_timeout_s=_as_int(sam_raw.get("set_image_timeout_s"), 60),
        predict_timeout_s=_as_int(sam_raw.get("predict_timeout_s"), 120),
        auto_timeout_s=_as_int(sam_raw.get("auto_timeout_s"), 900),
        max_line_bytes=_as_int(sam_raw.get("max_line_bytes"), 8 * 1024 * 1024),
    )
    annotation = AnnotationConfig(
        max_side=max(256, _as_int(ann_raw.get("max_side"), 4096)),
        undo_limit=max(1, _as_int(ann_raw.get("undo_limit"), 40)),
        palette=palette,
        grid_tile_size=min(8192, max(64, _as_int(grid_raw.get("tile_size"), 512))),
        grid_overlap_percent=min(50, max(0, _as_int(grid_raw.get("overlap_percent"), 10))),
        grid_auto_threshold=max(256, _as_int(grid_raw.get("auto_threshold"), 2000)),
    )
    auto = AutoSegConfig(
        points_per_side=max(1, _as_int(auto_raw.get("points_per_side"), 24)),
        pred_iou_thresh=_as_float(auto_raw.get("pred_iou_thresh"), 0.86),
        stability_score_thresh=_as_float(auto_raw.get("stability_score_thresh"), 0.92),
        min_mask_region_area=max(0, _as_int(auto_raw.get("min_mask_region_area"), 0)),
        max_candidates=max(1, _as_int(auto_raw.get("max_candidates"), 200)),
        max_side=max(256, _as_int(auto_raw.get("max_side"), 2048)),
    )

    training = _load_training_config(
        training_raw,
        text_override=text_override,
        path_override=path_override,
        int_override=int_override,
        float_override=float_override,
        bool_override=bool_override,
        fallback_python=sam.python,
    )
    inference = _load_inference_config(
        inference_raw,
        text_override=text_override,
        path_override=path_override,
        int_override=int_override,
        float_override=float_override,
        fallback_python=training.python or sam.python,
    )
    return DeepLearningConfig(
        sam=sam,
        annotation=annotation,
        auto=auto,
        training=training,
        inference=inference,
        source=str(config_path),
        load_error=load_error,
    )


def _load_training_config(
    raw: dict,
    *,
    text_override,
    path_override,
    int_override,
    float_override,
    bool_override,
    fallback_python: str,
) -> TrainingConfig:
    """解析 ``training:`` 段；模型 / 骨干按任务收敛到可用组合。"""

    def key(field: str) -> str:
        return training_setting_key(field)

    task = text_override(key("task"), raw.get("task") or "objects").strip().lower()
    if task not in training_catalog.TASK_IDS:
        task = "objects"
    model_raw = text_override(
        key("model"), raw.get("model") or training_catalog.default_model_id(task)
    ).strip()
    encoder_raw = text_override(
        key("encoder"),
        raw.get("encoder") or training_catalog.default_encoder(model_raw),
    ).strip()
    model_id, encoder_id = training_catalog.normalize_for_task(task, model_raw, encoder_raw)

    loss = text_override(key("loss"), raw.get("loss") or training_catalog.default_loss(task))
    loss = loss.strip().lower()
    if training_catalog.is_semantic_task(task):
        if loss not in training_catalog.LOSS_IDS:
            loss = "dice_ce"
    else:
        loss = ""

    device = text_override(key("device"), raw.get("device") or "auto").strip().lower()
    if device not in ("auto", "cuda", "cpu"):
        device = "auto"

    python = path_override(key("python"), raw.get("python")) or fallback_python
    return TrainingConfig(
        python=python,
        dataset_dir=path_override(key("dataset_dir"), raw.get("dataset_dir")),
        output_dir=path_override(key("output_dir"), raw.get("output_dir")),
        task=task,
        model=model_id,
        encoder=encoder_id,
        loss=loss,
        epochs=min(10000, max(1, int_override(key("epochs"), raw.get("epochs", 30)))),
        batch_size=min(1024, max(1, int_override(key("batch_size"), raw.get("batch_size", 4)))),
        lr=min(1.0, max(1e-7, float_override(key("lr"), raw.get("lr", 0.0001)))),
        tile_size=min(4096, max(64, int_override(key("tile_size"), raw.get("tile_size", 512)))),
        val_percent=min(90, max(0, int_override(key("val_percent"), raw.get("val_percent", 20)))),
        early_stop=min(1000, max(0, int_override(key("early_stop"), raw.get("early_stop", 8)))),
        seed=min(2**31 - 1, max(0, int_override(key("seed"), raw.get("seed", 42)))),
        device=device,
        pretrained=bool_override(key("pretrained"), raw.get("pretrained", True)),
        augment=bool_override(key("augment"), raw.get("augment", True)),
        min_instance_area=min(
            1_000_000,
            max(0, int_override(key("min_instance_area"), raw.get("min_instance_area", 16))),
        ),
        init_checkpoint=path_override(key("init_checkpoint"), raw.get("init_checkpoint")),
    )


def _load_inference_config(
    raw: dict,
    *,
    text_override,
    path_override,
    int_override,
    float_override,
    fallback_python: str,
) -> InferenceConfig:
    """解析 ``inference:`` 段；坏值回落默认，不阻止页面打开。"""

    def key(field: str) -> str:
        return inference_setting_key(field)

    device = text_override(key("device"), raw.get("device") or "auto").strip().lower()
    if device not in ("auto", "cuda", "cpu"):
        device = "auto"

    bands_value = _qsettings_value(key("bands"), None)
    if bands_value is None:
        bands_value = raw.get("bands")
    bands = _as_bands(bands_value)

    zero_value = _qsettings_value(key("zero_as_nodata"), None)
    if zero_value is None:
        zero_value = raw.get("zero_as_nodata", True)
    zero_as_nodata = _as_bool(zero_value, True)

    return InferenceConfig(
        python=path_override(key("python"), raw.get("python")) or fallback_python,
        model_dir=path_override(key("model_dir"), raw.get("model_dir")),
        image=path_override(key("image"), raw.get("image")),
        output_dir=path_override(key("output_dir"), raw.get("output_dir")),
        window_size=min(8192, max(0, int_override(key("window_size"), raw.get("window_size", 0)))),
        overlap_percent=min(
            50, max(0, int_override(key("overlap_percent"), raw.get("overlap_percent", 25)))
        ),
        zero_as_nodata=zero_as_nodata,
        batch_size=min(1024, max(1, int_override(key("batch_size"), raw.get("batch_size", 4)))),
        device=device,
        bands=bands,
        score_threshold=min(
            1.0, max(0.0, float_override(key("score_threshold"), raw.get("score_threshold", 0.5)))
        ),
        min_instance_pixels=min(
            1_000_000,
            max(
                0,
                int_override(key("min_instance_pixels"), raw.get("min_instance_pixels", 16)),
            ),
        ),
        preview_max_side=min(
            16384,
            max(256, int_override(key("preview_max_side"), raw.get("preview_max_side", 4096))),
        ),
    )
