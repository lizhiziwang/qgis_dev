"""Meta SAM1 / SAM2 / SAM2.1 / SAM3 模型注册表与默认目录。

设置页的「模型管理」按本表展示、下载、选择模型；sidecar 通过 ``family`` /
``model_type`` / ``sam2_config`` 区分不同加载 API：

- SAM1：``segment_anything.sam_model_registry[model_type](checkpoint=...)``；
- SAM2 / SAM2.1：``sam2.build_sam.build_sam2(config, checkpoint, device=...)``；
- SAM3 / SAM3.1：``sam3.build_sam3_image_model(checkpoint_path=..., device=...,
  enable_inst_interactivity=True)`` + ``Sam3Processor``。

权重不随安装包分发；SAM1/SAM2 从 Meta 官方地址下载，SAM3 权重托管在
HuggingFace（需申请访问），下载时会尝试读取 ``HF_TOKEN``。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path

from app.core.auth._user_paths import user_config_dir
from app.infra.deep_learning import SamConfig

SAM1_BASE_URL = "https://dl.fbaipublicfiles.com/segment_anything/"
SAM2_BASE_URL = "https://dl.fbaipublicfiles.com/segment_anything_2/072824/"
SAM21_BASE_URL = "https://dl.fbaipublicfiles.com/segment_anything_2/092824/"
#: SAM3 权重托管在 HuggingFace 的受限仓库（需申请访问，HF_TOKEN 可下载）
SAM3_PAGE_URL = "https://huggingface.co/facebook/sam3"
SAM3_URL = SAM3_PAGE_URL + "/resolve/main/sam3.pt?download=true"
SAM31_PAGE_URL = "https://huggingface.co/facebook/sam3.1"
SAM31_URL = SAM31_PAGE_URL + "/resolve/main/sam3.1_multiplex.pt?download=true"

_MB = 1024 * 1024


@dataclass(frozen=True)
class SamModel:
    """一个可下载/可选择的 SAM 权重。"""

    id: str
    name: str
    family: str  # "sam1" | "sam2" | "sam3"
    filename: str
    url: str
    size_bytes: int
    #: SAM1 的模型类型（vit_h / vit_l / vit_b）
    model_type: str = ""
    #: SAM2 的 hydra 配置（相对 sam2 包根目录）
    config: str = ""
    #: 权重是否位于需授权的 HuggingFace 仓库
    gated: bool = False
    #: 权重说明/申请页
    page_url: str = ""

    @property
    def version_label(self) -> str:
        """设置页筛选/展示用的系列名：SAM1 / SAM2 / SAM2.1 / SAM3 / SAM3.1。"""
        if self.family == "sam1":
            return "SAM1"
        if self.family == "sam3":
            return "SAM3.1" if "3.1" in self.filename else "SAM3"
        return "SAM2.1" if "2.1" in self.filename else "SAM2"


def _sam1(model_id: str, name: str, filename: str, size_mb: int, model_type: str) -> SamModel:
    return SamModel(
        id=model_id,
        name=name,
        family="sam1",
        filename=filename,
        url=SAM1_BASE_URL + filename,
        size_bytes=size_mb * _MB,
        model_type=model_type,
    )


def _sam2(
    model_id: str,
    name: str,
    filename: str,
    size_mb: int,
    config: str,
    *,
    base_url: str,
) -> SamModel:
    return SamModel(
        id=model_id,
        name=name,
        family="sam2",
        filename=filename,
        url=base_url + filename,
        size_bytes=size_mb * _MB,
        config=config,
    )


def _sam3(
    model_id: str,
    name: str,
    filename: str,
    size_mb: int,
    *,
    url: str,
    page_url: str,
) -> SamModel:
    return SamModel(
        id=model_id,
        name=name,
        family="sam3",
        filename=filename,
        url=url,
        size_bytes=size_mb * _MB,
        gated=True,
        page_url=page_url,
    )


#: 注册表顺序即设置页默认顺序（SAM1 在前，SAM2.1 在前于 SAM2）
MODELS: tuple[SamModel, ...] = (
    _sam1("sam1_vit_h", "SAM ViT-H", "sam_vit_h_4b8939.pth", 2560, "vit_h"),
    _sam1("sam1_vit_l", "SAM ViT-L", "sam_vit_l_0b3195.pth", 1250, "vit_l"),
    _sam1("sam1_vit_b", "SAM ViT-B", "sam_vit_b_01ec64.pth", 375, "vit_b"),
    _sam2(
        "sam2.1_hiera_tiny",
        "SAM2.1 Hiera-Tiny",
        "sam2.1_hiera_tiny.pt",
        149,
        "configs/sam2.1/sam2.1_hiera_t.yaml",
        base_url=SAM21_BASE_URL,
    ),
    _sam2(
        "sam2.1_hiera_small",
        "SAM2.1 Hiera-Small",
        "sam2.1_hiera_small.pt",
        185,
        "configs/sam2.1/sam2.1_hiera_s.yaml",
        base_url=SAM21_BASE_URL,
    ),
    _sam2(
        "sam2.1_hiera_base_plus",
        "SAM2.1 Hiera-Base+",
        "sam2.1_hiera_base_plus.pt",
        323,
        "configs/sam2.1/sam2.1_hiera_b+.yaml",
        base_url=SAM21_BASE_URL,
    ),
    _sam2(
        "sam2.1_hiera_large",
        "SAM2.1 Hiera-Large",
        "sam2.1_hiera_large.pt",
        898,
        "configs/sam2.1/sam2.1_hiera_l.yaml",
        base_url=SAM21_BASE_URL,
    ),
    _sam2(
        "sam2_hiera_tiny",
        "SAM2 Hiera-Tiny",
        "sam2_hiera_tiny.pt",
        149,
        "configs/sam2/sam2_hiera_t.yaml",
        base_url=SAM2_BASE_URL,
    ),
    _sam2(
        "sam2_hiera_small",
        "SAM2 Hiera-Small",
        "sam2_hiera_small.pt",
        185,
        "configs/sam2/sam2_hiera_s.yaml",
        base_url=SAM2_BASE_URL,
    ),
    _sam2(
        "sam2_hiera_base_plus",
        "SAM2 Hiera-Base+",
        "sam2_hiera_base_plus.pt",
        323,
        "configs/sam2/sam2_hiera_b+.yaml",
        base_url=SAM2_BASE_URL,
    ),
    _sam2(
        "sam2_hiera_large",
        "SAM2 Hiera-Large",
        "sam2_hiera_large.pt",
        898,
        "configs/sam2/sam2_hiera_l.yaml",
        base_url=SAM2_BASE_URL,
    ),
    _sam3(
        "sam3",
        "SAM3",
        "sam3.pt",
        3482,
        url=SAM3_URL,
        page_url=SAM3_PAGE_URL,
    ),
    _sam3(
        "sam3.1_multiplex",
        "SAM3.1 Multiplex",
        "sam3.1_multiplex.pt",
        3341,
        url=SAM31_URL,
        page_url=SAM31_PAGE_URL,
    ),
)

_MODELS_BY_ID = {model.id: model for model in MODELS}
_MODELS_BY_FILENAME = {model.filename.lower(): model for model in MODELS}


def model_by_id(model_id: str) -> SamModel | None:
    return _MODELS_BY_ID.get(str(model_id or "").strip())


def model_for_checkpoint(path: str, model_id: str = "") -> SamModel | None:
    """按实际文件名识别模型；文件名不认识时再退回注册表 id。"""
    name = Path(str(path or "")).name.lower()
    if name:
        matched = _MODELS_BY_FILENAME.get(name)
        if matched is not None:
            return matched
    return model_by_id(model_id)


def model_path(model: SamModel, models_dir: str) -> Path:
    return Path(models_dir).expanduser() / model.filename


def infer_family(checkpoint: str) -> str:
    """按权重文件名判断 SAM1 / SAM2 / SAM3；都不匹配时按 SAM1。"""
    name = Path(str(checkpoint or "")).name.lower()
    if "sam3" in name:
        return "sam3"
    return "sam2" if "sam2" in name else "sam1"


def sam2_config_for_filename(filename: str) -> str:
    """从 ``sam2[_2.1]_hiera_{tiny,small,base_plus,large}.pt`` 推断 hydra 配置。"""
    name = Path(str(filename or "")).name.lower()
    if "sam2" not in name or "hiera" not in name:
        return ""
    if "base_plus" in name:
        size = "b+"
    elif "tiny" in name:
        size = "t"
    elif "small" in name:
        size = "s"
    elif "large" in name:
        size = "l"
    else:
        return ""
    if "2.1" in name:
        return f"configs/sam2.1/sam2.1_hiera_{size}.yaml"
    return f"configs/sam2/sam2_hiera_{size}.yaml"


def default_models_dir() -> Path:
    return user_config_dir() / "models" / "sam"


def default_cache_dir() -> Path:
    return user_config_dir() / "cache" / "sam"


def resolve_sam_paths(config: SamConfig) -> SamConfig:
    """补全模型/缓存目录的默认值（标注窗口与设置页共用）。"""
    return replace(
        config,
        models_dir=config.models_dir or str(default_models_dir()),
        cache_dir=config.cache_dir or str(default_cache_dir()),
    )


__all__ = [
    "MODELS",
    "SAM3_PAGE_URL",
    "SAM3_URL",
    "SAM31_PAGE_URL",
    "SAM31_URL",
    "SamModel",
    "default_cache_dir",
    "default_models_dir",
    "infer_family",
    "model_by_id",
    "model_for_checkpoint",
    "model_path",
    "resolve_sam_paths",
    "sam2_config_for_filename",
]
