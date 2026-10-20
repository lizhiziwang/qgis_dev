"""Meta SAM 常驻子进程（由标注窗口用外部 Python 启动）。

协议：stdin/stdout 各一行一个 UTF-8 JSON。stdout **只**走协议，日志走 stderr。

启动后先输出 ``hello``（此时不导入 torch）；收到 ``load`` 才真正加载模型。
支持的请求：``load`` / ``set_image`` / ``predict`` / ``text`` / ``exemplar`` / ``auto`` / ``shutdown``。

本脚本不得导入 QGIS / PyQt；运行环境由 ``config/deep_learning.yaml`` 指定，
默认 ``D:/zsh/app/miniconda3/envs/autolabel/python.exe`` + 官方
``segment-anything`` 源码目录。
"""

from __future__ import annotations

import argparse
import contextlib
import gc
import hashlib
import json
import os
import sys
import threading
import time
import traceback

_MODEL = None
_PREDICTOR = None
_DEVICE = "cpu"
_FAMILY = "sam1"
_MODEL_ID = ""
_SAM2_CONFIG = ""
_CHECKPOINT = ""
_CURRENT_RGB = None
_CURRENT_PATH = ""
_OUT_DIR = ""
_BUSY = False
_OUT_LOCK = threading.Lock()

_CACHE_CHUNK = 1024 * 1024


def _emit(payload: dict) -> None:
    with _OUT_LOCK:
        sys.__stdout__.write(json.dumps(payload, ensure_ascii=False) + "\n")
        sys.__stdout__.flush()


def _log(message: str) -> None:
    sys.stderr.write(f"[sam_sidecar] {message}\n")
    sys.stderr.flush()


def _error(request_id, error_type: str, message: str, *, fatal: bool = False) -> None:
    _emit(
        {
            "event": "error",
            "id": request_id,
            "error_type": error_type,
            "message": message,
            "fatal": fatal,
        }
    )


def _read_image(path: str):
    """读取影像为 RGB uint8（兼容中文路径，不依赖 cv2.imread）。"""
    import cv2
    import numpy as np

    data = np.fromfile(path, dtype=np.uint8)
    image = cv2.imdecode(data, cv2.IMREAD_UNCHANGED)
    if image is None:
        raise ValueError(f"无法读取影像：{path}")
    if image.ndim == 2:
        image = np.stack([image] * 3, axis=-1)
    elif image.shape[2] == 4:
        image = image[:, :, :3]
    elif image.shape[2] == 1:
        image = np.repeat(image, 3, axis=2)
    # OpenCV 解码为 BGR
    return np.ascontiguousarray(image[:, :, ::-1])


def _write_mask(mask, path: str) -> None:
    import cv2
    import numpy as np

    array = np.asarray(mask)
    if array.ndim == 3:
        array = array[0]
    array = (array > 0).astype("uint8") * 255
    ok, encoded = cv2.imencode(".png", array)
    if not ok:
        raise OSError(f"编码掩膜失败：{path}")
    encoded.tofile(path)


def _mask_bbox(mask) -> list[int]:
    """二维布尔掩膜的 inclusive bbox ``[x0, y0, x1, y1]``；空掩膜返回全 0。"""
    import numpy as np

    array = np.asarray(mask)
    if array.ndim == 3:
        array = array[0]
    rows = np.any(array, axis=1)
    cols = np.any(array, axis=0)
    if not rows.any() or not cols.any():
        return [0, 0, 0, 0]
    y0, y1 = np.where(rows)[0][[0, -1]]
    x0, x1 = np.where(cols)[0][[0, -1]]
    return [int(x0), int(y0), int(x1), int(y1)]


def _resolve_device(requested: str | None) -> str:
    import torch

    if requested and requested != "auto":
        return requested
    return "cuda" if torch.cuda.is_available() else "cpu"


def _infer_model_type(checkpoint: str, requested: str) -> str:
    if requested and requested != "auto":
        return requested
    name = os.path.basename(checkpoint).lower()
    for key in ("vit_h", "vit_l", "vit_b"):
        if key in name:
            return key
    return "vit_h"


def _infer_family(checkpoint: str, requested: str) -> str:
    family = str(requested or "").lower()
    if family in ("sam1", "sam2", "sam3"):
        return family
    name = os.path.basename(checkpoint).lower()
    if "sam3" in name:
        return "sam3"
    return "sam2" if "sam2" in name else "sam1"


def _infer_sam2_config(filename: str) -> str:
    """按文件名推断 SAM2 hydra 配置（与 app.core.ai.sam_models 保持同一规则）。"""
    name = os.path.basename(filename).lower()
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


def _grounding_arrays(state: dict):
    """把 SAM3 grounding 的 ``state`` 转成 ``(masks[K,H,W] bool, scores[K])``。"""
    import numpy as np

    masks = state["masks"].detach().cpu().numpy()
    if masks.ndim == 4:
        # [K, 1, H, W] → [K, H, W]；不能裸 squeeze，K=1 时会丢掉候选维度
        masks = masks[:, 0]
    scores = state["scores"].detach().float().cpu().numpy().reshape(-1)
    # 全分辨率 float logits 很占显存，结果转成 numpy 后立即释放
    state.pop("masks_logits", None)
    return np.asarray(masks).astype(bool, copy=False), np.asarray(scores).reshape(-1)


class _Sam3Predictor:
    """把 ``Sam3Processor`` + ``Sam3Image`` 包装成 SAM1/2 风格的预测器。

    SAM3 的图像模型没有 ``SamAutomaticMaskGenerator``，交互式分割需要
    ``enable_inst_interactivity=True`` 后调用 ``model.predict_inst(state, ...)``。
    这里保持 ``set_image`` / ``predict`` 接口一致，``_handle_set_image``、
    ``_handle_predict`` 与特征缓存都能原样复用。
    """

    def __init__(self, model, processor) -> None:
        self.model = model
        self.processor = processor
        self.state = None
        self._orig_hw = None
        self._is_image_set = False

    def set_image(self, image) -> None:
        from PIL import Image

        if getattr(image, "ndim", 0) == 3:
            image = Image.fromarray(image)
        self.state = self.processor.set_image(image)
        height = int(self.state.get("original_height") or 0)
        width = int(self.state.get("original_width") or 0)
        self._orig_hw = [(height, width)]
        self._is_image_set = True

    def predict(
        self,
        point_coords=None,
        point_labels=None,
        box=None,
        multimask_output=True,
        **_kwargs,
    ):
        import numpy as np

        if self.state is None:
            raise RuntimeError("尚未设置影像（set_image）")
        call_kwargs = {"multimask_output": bool(multimask_output)}
        if point_coords is not None and len(point_coords):
            call_kwargs["point_coords"] = np.asarray(point_coords, dtype=np.float32)
            labels = point_labels if point_labels is not None else [1] * len(point_coords)
            call_kwargs["point_labels"] = np.asarray(labels, dtype=np.int32)
        if box is not None:
            call_kwargs["box"] = np.asarray(box, dtype=np.float32)
        masks, scores, logits = self.model.predict_inst(self.state, **call_kwargs)
        return np.asarray(masks), np.asarray(scores).reshape(-1), logits

    def predict_text(self, text: str, threshold: float = 0.5):
        """文字提示分割（SAM3 专属）。

        返回 ``(masks, scores)``：masks 为 ``[K, H, W]`` 布尔数组，scores 为 ``[K]``。
        """
        if self.state is None:
            raise RuntimeError("尚未设置影像（set_image）")
        # 范例框等几何提示不能混进纯文字提示，否则会用框+文字一起推理
        self.state.pop("geometric_prompt", None)
        # 不要把 state 传给 set_confidence_threshold：那会用上一次提示重跑推理
        self.processor.confidence_threshold = float(threshold)
        state = self.processor.set_text_prompt(text, self.state)
        return _grounding_arrays(state)

    def predict_exemplar(self, box, threshold: float = 0.6):
        """范例分割（SAM3 专属）：框住 1 个样本，找出所有同类实例。

        ``box`` 为原图像素坐标 ``[x0, y0, x1, y1]``；模型要求归一化
        ``[center_x, center_y, width, height]``。返回同 ``predict_text``。
        """
        if self.state is None:
            raise RuntimeError("尚未设置影像（set_image）")
        width = int(self.state.get("original_width") or 0)
        height = int(self.state.get("original_height") or 0)
        if width <= 0 or height <= 0:
            raise RuntimeError("影像尺寸无效，请重新送入影像")
        try:
            x0, y0, x1, y1 = (float(value) for value in box)
        except (TypeError, ValueError):
            raise ValueError("范例框坐标无效") from None
        x0, x1 = sorted((x0, x1))
        y0, y1 = sorted((y0, y1))
        x0 = min(max(x0, 0.0), float(width))
        x1 = min(max(x1, 0.0), float(width))
        y0 = min(max(y0, 0.0), float(height))
        y1 = min(max(y1, 0.0), float(height))
        if x1 - x0 < 1.0 or y1 - y0 < 1.0:
            raise ValueError("范例框太小，请重新框选样本")

        # 清掉旧文字/几何提示，避免把上一次提示与范例框混在一起；图像特征保留
        self.processor.reset_all_prompts(self.state)
        self.processor.confidence_threshold = float(threshold)
        prompt_box = [
            (x0 + x1) / 2.0 / width,
            (y0 + y1) / 2.0 / height,
            (x1 - x0) / width,
            (y1 - y0) / height,
        ]
        self.state = self.processor.add_geometric_prompt(prompt_box, True, self.state)
        return _grounding_arrays(self.state)


def _handle_load(request_id: int, request: dict) -> None:
    global _MODEL, _PREDICTOR, _DEVICE, _FAMILY, _MODEL_ID, _SAM2_CONFIG, _CHECKPOINT
    global _CURRENT_RGB, _CURRENT_PATH
    try:
        sam_src = request.get("sam_src") or ""
        if sam_src and sam_src not in sys.path:
            sys.path.insert(0, sam_src)
        checkpoint = request.get("checkpoint") or ""
        if not checkpoint or not os.path.isfile(checkpoint):
            _error(request_id, "checkpoint_missing", f"未找到权重文件：{checkpoint}")
            return
        _emit({"event": "status", "id": request_id, "stage": "loading_model", "message": "正在加载 SAM…"})
        import torch

        family = _infer_family(checkpoint, request.get("family") or "")
        _DEVICE = _resolve_device(request.get("device"))
        fallback = False
        if _DEVICE == "cuda" and not torch.cuda.is_available():
            _DEVICE = "cpu"
            fallback = True

        # 切换模型前先释放旧模型，避免 GPU/内存同时占用
        _PREDICTOR = None
        if _MODEL is not None:
            _MODEL = None
            gc.collect()
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        _CURRENT_RGB = None
        _CURRENT_PATH = ""

        config_file = ""
        if family == "sam2":
            config_file = str(request.get("sam2_config") or "") or _infer_sam2_config(checkpoint)
            if not config_file:
                _error(
                    request_id,
                    "missing_config",
                    "无法推断 SAM2 配置；请在「SAM 设置 → 模型管理」选择注册表模型",
                )
                return
            from sam2.build_sam import build_sam2
            from sam2.sam2_image_predictor import SAM2ImagePredictor

            _log(f"loading SAM2 {config_file} on {_DEVICE}")
            model = build_sam2(config_file, checkpoint, device=_DEVICE)
            model.eval()
            _MODEL = model
            _PREDICTOR = SAM2ImagePredictor(model)
        elif family == "sam3":
            from sam3 import build_sam3_image_model
            from sam3.model.sam3_image_processor import Sam3Processor

            _log(f"loading SAM3 {os.path.basename(checkpoint)} on {_DEVICE}")
            model = build_sam3_image_model(
                device=_DEVICE,
                eval_mode=True,
                checkpoint_path=checkpoint,
                load_from_HF=False,
                enable_inst_interactivity=True,
            )
            _MODEL = model
            _PREDICTOR = _Sam3Predictor(model, Sam3Processor(model, device=_DEVICE))
        else:
            from segment_anything import SamPredictor, sam_model_registry

            model_type = _infer_model_type(checkpoint, request.get("model_type") or "auto")
            _log(f"loading {model_type} on {_DEVICE}")
            model = sam_model_registry[model_type](checkpoint=checkpoint)
            model.to(device=_DEVICE)
            model.eval()
            _MODEL = model
            _PREDICTOR = SamPredictor(model)

        _FAMILY = family
        _MODEL_ID = str(request.get("model_id") or "")
        _SAM2_CONFIG = config_file
        _CHECKPOINT = checkpoint
        _emit(
            {
                "event": "ready",
                "id": request_id,
                "device": _DEVICE,
                "family": family,
                "model_type": _infer_model_type(checkpoint, request.get("model_type") or "auto")
                if family == "sam1"
                else "",
                "model_id": _MODEL_ID,
                "fallback": fallback,
                "torch": torch.__version__,
            }
        )
    except Exception as exc:  # noqa: BLE001 —— 子进程必须把错误回传而不是直接崩
        _log(traceback.format_exc())
        _error(request_id, _classify_error(exc), str(exc))


def _classify_error(exc: BaseException) -> str:
    text = str(exc).lower()
    if "out of memory" in text:
        return "cuda_oom"
    if isinstance(exc, ImportError) or "no module named" in text:
        return "import_error"
    return "internal"


def _cache_identity() -> str:
    try:
        stat = os.stat(_CHECKPOINT)
        stamp = f"{stat.st_size}:{stat.st_mtime_ns}"
    except OSError:
        stamp = "0:0"
    return f"{_FAMILY}|{_MODEL_ID or 'unknown'}|{os.path.basename(_CHECKPOINT)}|{stamp}"


def _cache_key_for_file(path: str, identity: str) -> str:
    """图像内容 + 模型身份 → 缓存键（纯函数，便于单测）。"""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(_CACHE_CHUNK)
            if not chunk:
                break
            digest.update(chunk)
    digest.update(b"\0")
    digest.update(identity.encode("utf-8"))
    return digest.hexdigest()


def _cache_path(directory: str, key: str) -> str:
    if not key or any(ch not in "0123456789abcdef" for ch in key):
        return ""
    return os.path.join(directory, f"{key}.pt")


def _to_cpu(value):
    import torch

    if isinstance(value, torch.Tensor):
        return value.detach().cpu()
    if isinstance(value, dict):
        return {key: _to_cpu(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_to_cpu(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_to_cpu(item) for item in value)
    return value


def _feature_state() -> dict:
    if _FAMILY == "sam3":
        return {"family": "sam3", "state": _to_cpu(_PREDICTOR.state)}
    if _FAMILY == "sam2":
        return {
            "family": "sam2",
            "features": _to_cpu(_PREDICTOR._features),
            "orig_hw": [tuple(pair) for pair in _PREDICTOR._orig_hw],
        }
    return {
        "family": "sam1",
        "features": _to_cpu(_PREDICTOR.features),
        "original_size": tuple(_PREDICTOR.original_size),
        "input_size": tuple(_PREDICTOR.input_size),
    }


def _save_features(path: str) -> None:
    try:
        import torch

        state = _feature_state()
        tmp = path + ".tmp"
        torch.save(state, tmp)
        os.replace(tmp, path)
    except Exception as exc:  # noqa: BLE001 —— 缓存失败不能影响推理
        _log(f"保存特征缓存失败：{exc}")
        with contextlib.suppress(OSError):
            if os.path.exists(path + ".tmp"):
                os.remove(path + ".tmp")


def _load_feature_state(path: str) -> dict | None:
    import torch

    try:
        state = torch.load(path, map_location=_DEVICE, weights_only=True)
    except TypeError:  # 老版本 torch 没有 weights_only
        state = torch.load(path, map_location=_DEVICE)
    except Exception:  # noqa: BLE001 —— 兼容旧缓存/新版 torch 的安全加载限制
        try:
            state = torch.load(path, map_location=_DEVICE, weights_only=False)
        except Exception as exc:  # noqa: BLE001
            _log(f"读取特征缓存失败：{exc}")
            return None
    return state if isinstance(state, dict) else None


def _restore_features(path: str) -> bool:
    state = _load_feature_state(path)
    if not state or state.get("family") != _FAMILY:
        return False
    try:
        if _FAMILY == "sam3":
            payload = state.get("state")
            if not isinstance(payload, dict):
                return False
            _PREDICTOR.state = payload
            _PREDICTOR._orig_hw = [
                (
                    int(payload.get("original_height") or 0),
                    int(payload.get("original_width") or 0),
                )
            ]
            _PREDICTOR._is_image_set = True
        elif _FAMILY == "sam2":
            reset = getattr(_PREDICTOR, "reset_predictor", None)
            if callable(reset):
                reset()
            _PREDICTOR._features = state["features"]
            _PREDICTOR._orig_hw = [tuple(pair) for pair in state["orig_hw"]]
            _PREDICTOR._is_batch = False
            _PREDICTOR._is_image_set = True
        else:
            _PREDICTOR.features = state["features"]
            _PREDICTOR.original_size = tuple(state["original_size"])
            _PREDICTOR.input_size = tuple(state["input_size"])
            _PREDICTOR.is_image_set = True
        return True
    except Exception as exc:  # noqa: BLE001
        _log(f"恢复特征缓存失败：{exc}")
        return False


def _prune_cache(directory: str, max_bytes: int, keep: str = "") -> None:
    """按 mtime 从旧到新删缓存，直到总量 ≤ 上限；``keep`` 永不删。"""
    if max_bytes <= 0:
        return
    try:
        names = [name for name in os.listdir(directory) if name.endswith(".pt")]
    except OSError:
        return
    entries = []
    total = 0
    for name in names:
        path = os.path.join(directory, name)
        try:
            stat = os.stat(path)
        except OSError:
            continue
        entries.append((stat.st_mtime, stat.st_size, path))
        total += stat.st_size
    if total <= max_bytes:
        return
    entries.sort(key=lambda item: item[0])
    keep_abs = os.path.abspath(keep) if keep else ""
    for _mtime, size, path in entries:
        if total <= max_bytes:
            break
        if keep_abs and os.path.abspath(path) == keep_abs:
            continue
        try:
            os.remove(path)
        except OSError:
            continue
        total -= size


def _handle_set_image(request_id: int, request: dict) -> None:
    global _CURRENT_RGB, _CURRENT_PATH
    try:
        if _PREDICTOR is None:
            _error(request_id, "not_ready", "SAM 模型尚未加载")
            return
        path = request.get("path") or ""
        token = request.get("token") or ""
        image = _read_image(path)
        cache_dir = str(request.get("cache_dir") or "")
        try:
            max_bytes = max(0, int(request.get("cache_max_bytes") or 0))
        except (TypeError, ValueError):
            max_bytes = 0
        cache_file = ""
        if cache_dir:
            try:
                os.makedirs(cache_dir, exist_ok=True)
                key = _cache_key_for_file(path, _cache_identity())
                cache_file = _cache_path(cache_dir, key)
            except Exception as exc:  # noqa: BLE001 —— 缓存失败不影响 set_image
                _log(f"准备特征缓存失败：{exc}")
                cache_file = ""
        cached = False
        if cache_file and os.path.isfile(cache_file):
            cached = _restore_features(cache_file)
            if not cached:
                with contextlib.suppress(OSError):
                    os.remove(cache_file)
        if not cached:
            _PREDICTOR.set_image(image)
        _CURRENT_RGB = image
        _CURRENT_PATH = path
        if cache_file and not cached:
            _save_features(cache_file)
            _prune_cache(cache_dir, max_bytes, keep=cache_file)
        _emit(
            {
                "event": "image_ready",
                "id": request_id,
                "token": token,
                "width": int(image.shape[1]),
                "height": int(image.shape[0]),
                "cached": bool(cached),
            }
        )
    except Exception as exc:  # noqa: BLE001
        _log(traceback.format_exc())
        _error(request_id, _classify_error(exc), str(exc))


def _handle_predict(request_id: int, request: dict) -> None:
    global _BUSY
    try:
        if _PREDICTOR is None:
            _error(request_id, "not_ready", "SAM 模型尚未加载")
            return
        if _CURRENT_RGB is None:
            _error(request_id, "not_ready", "尚未设置影像（set_image）")
            return
        token = request.get("token") or ""
        points = request.get("points") or []
        labels = request.get("labels") or []
        box = request.get("box")
        if not points and not box:
            _error(request_id, "bad_request", "至少需要一个提示点或提示框")
            return
        import numpy as np

        kwargs = {"multimask_output": bool(request.get("multimask", True))}
        if points:
            point_coords = np.asarray(points, dtype=np.float32)
            point_labels = np.asarray(labels or [1] * len(points), dtype=np.int32)
            kwargs["point_coords"] = point_coords
            kwargs["point_labels"] = point_labels
        if box:
            kwargs["box"] = np.asarray(box, dtype=np.float32)
        masks, scores, _ = _PREDICTOR.predict(**kwargs)
        results = []
        for index, (mask, score) in enumerate(zip(masks, scores, strict=True)):
            path = os.path.join(_OUT_DIR, f"pred_{request_id}_{index}.png")
            _write_mask(mask, path)
            results.append(
                {
                    "path": path,
                    "score": float(score),
                    "area": int(np.asarray(mask).sum()),
                    "bbox": _mask_bbox(mask),
                }
            )
        results.sort(key=lambda item: item["score"], reverse=True)
        _emit({"event": "result", "id": request_id, "token": token, "masks": results})
    except Exception as exc:  # noqa: BLE001
        _log(traceback.format_exc())
        if _classify_error(exc) == "cuda_oom":
            _error(request_id, "cuda_oom", f"显存不足：{exc}")
        else:
            _error(request_id, _classify_error(exc), str(exc))


def _collect_grounding_results(
    masks, scores, request_id: int, prefix: str, max_candidates: int
) -> list[dict]:
    """按分数降序写候选 PNG，跳过空掩膜；``text`` / ``exemplar`` 共用。"""
    import numpy as np

    masks = np.asarray(masks)
    if masks.ndim == 4:
        masks = masks[:, 0]
    scores = np.asarray(scores).reshape(-1)
    order = np.argsort(-scores)[:max_candidates]

    results = []
    for index in order:
        index = int(index)
        if index >= len(masks):
            continue
        mask = np.asarray(masks[index]).astype(bool)
        area = int(mask.sum())
        if area == 0:
            # 空掩膜写出来只会成为无用的最高分候选
            continue
        path = os.path.join(_OUT_DIR, f"{prefix}_{request_id}_{index}.png")
        _write_mask(mask, path)
        results.append(
            {
                "path": path,
                "score": float(scores[index]),
                "area": area,
                "bbox": _mask_bbox(mask),
            }
        )
    return results


def _handle_text(request_id: int, request: dict) -> None:
    """SAM3 文字提示分割：结果与 predict / auto 一样写成候选掩膜 PNG。"""
    try:
        if _PREDICTOR is None:
            _error(request_id, "not_ready", "SAM 模型尚未加载")
            return
        if _FAMILY != "sam3" or not hasattr(_PREDICTOR, "predict_text"):
            _error(request_id, "unsupported", "文字提示仅支持 SAM3 / SAM3.1")
            return
        if _CURRENT_RGB is None:
            _error(request_id, "not_ready", "尚未设置影像（set_image）")
            return
        text = str(request.get("text") or "").strip()
        if not text:
            _error(request_id, "bad_request", "文字提示不能为空")
            return
        try:
            threshold = float(request.get("threshold", 0.5))
        except (TypeError, ValueError):
            _error(request_id, "bad_request", "置信度阈值无效")
            return
        threshold = min(0.99, max(0.01, threshold))
        try:
            max_candidates = int(request.get("max_candidates") or 100)
        except (TypeError, ValueError):
            max_candidates = 100
        max_candidates = min(1000, max(1, max_candidates))

        masks, scores = _PREDICTOR.predict_text(text, threshold)
        results = _collect_grounding_results(
            masks,
            scores,
            request_id,
            "text",
            max_candidates,
        )
        _emit(
            {
                "event": "result",
                "id": request_id,
                "token": request.get("token") or "",
                "text": text,
                "masks": results,
            }
        )
    except Exception as exc:  # noqa: BLE001
        _log(traceback.format_exc())
        if _classify_error(exc) == "cuda_oom":
            _error(request_id, "cuda_oom", f"显存不足：{exc}")
        else:
            _error(request_id, _classify_error(exc), str(exc))


def _handle_exemplar(request_id: int, request: dict) -> None:
    """SAM3 范例分割：框住 1 个样本，返回所有同类实例的候选掩膜。"""
    try:
        if _PREDICTOR is None:
            _error(request_id, "not_ready", "SAM 模型尚未加载")
            return
        if _FAMILY != "sam3" or not hasattr(_PREDICTOR, "predict_exemplar"):
            _error(request_id, "unsupported", "范例标注仅支持 SAM3 / SAM3.1")
            return
        if _CURRENT_RGB is None:
            _error(request_id, "not_ready", "尚未设置影像（set_image）")
            return
        box = request.get("box")
        if not isinstance(box, (list, tuple)) or len(box) != 4:
            _error(request_id, "bad_request", "范例框必须是 [x0, y0, x1, y1]")
            return
        try:
            threshold = float(request.get("threshold", 0.6))
        except (TypeError, ValueError):
            _error(request_id, "bad_request", "置信度阈值无效")
            return
        threshold = min(0.99, max(0.01, threshold))
        try:
            max_candidates = int(request.get("max_candidates") or 100)
        except (TypeError, ValueError):
            max_candidates = 100
        max_candidates = min(1000, max(1, max_candidates))

        masks, scores = _PREDICTOR.predict_exemplar(box, threshold)
        results = _collect_grounding_results(
            masks,
            scores,
            request_id,
            "exemplar",
            max_candidates,
        )
        _emit(
            {
                "event": "result",
                "id": request_id,
                "token": request.get("token") or "",
                "box": [float(value) for value in box],
                "masks": results,
            }
        )
    except ValueError as exc:
        _error(request_id, "bad_request", str(exc))
    except Exception as exc:  # noqa: BLE001
        _log(traceback.format_exc())
        if _classify_error(exc) == "cuda_oom":
            _error(request_id, "cuda_oom", f"显存不足：{exc}")
        else:
            _error(request_id, _classify_error(exc), str(exc))


def _progress_loop(stop_event: threading.Event, request_id: int) -> None:
    start = time.time()
    while not stop_event.wait(5.0):
        _emit({"event": "progress", "id": request_id, "stage": "auto", "elapsed": round(time.time() - start, 1)})


def _mask_iou_small(first, second) -> float:
    """在抽稀后的布尔掩膜上算 IoU（自动分割去重用，避免全分辨率开销）。"""
    intersection = int((first & second).sum())
    if intersection == 0:
        return 0.0
    union = int((first | second).sum())
    return intersection / union if union else 0.0


def _sam3_auto_candidates(
    points_per_side: int,
    pred_iou_thresh: float,
    min_mask_region_area: int,
    max_candidates: int,
) -> list[dict]:
    """SAM3 没有自动掩膜生成器：用网格正点逐点预测，再按 IoU 去重。

    只保留分数达标的掩膜；体积过大（几乎整幅图）的候选丢弃。全分辨率布尔
    掩膜只在确认保留后保存，IoU 用抽稀到约 256 像素的副本计算。
    """
    import numpy as np

    rgb = _CURRENT_RGB
    height, width = rgb.shape[:2]
    side = max(2, min(int(points_per_side or 16), 32))
    stride = max(1, max(height, width) // 256)
    total_pixels = max(1, height * width)
    accepted: list[dict] = []
    smalls: list = []
    for row in range(side):
        y = (row + 0.5) * height / side
        for column in range(side):
            x = (column + 0.5) * width / side
            masks, scores, _ = _PREDICTOR.predict(
                point_coords=np.asarray([[x, y]], dtype=np.float32),
                point_labels=np.asarray([1], dtype=np.int32),
                multimask_output=True,
            )
            for mask, score in zip(np.asarray(masks), np.asarray(scores).reshape(-1), strict=False):
                score = float(score)
                if score < pred_iou_thresh:
                    continue
                mask = np.asarray(mask, dtype=bool)
                area = int(mask.sum())
                if area < max(0, int(min_mask_region_area)) or area > total_pixels * 0.95:
                    continue
                small = np.ascontiguousarray(mask[::stride, ::stride])
                if any(_mask_iou_small(small, other) > 0.8 for other in smalls):
                    continue
                smalls.append(small)
                accepted.append({"segmentation": mask, "area": area})
                if len(accepted) >= max_candidates:
                    return accepted
    return accepted


def _mask_generator_class():
    """按当前系列返回自动掩膜生成器类（SAM3 无官方实现，返回 None）。"""
    if _FAMILY == "sam3":
        return None
    if _FAMILY == "sam2":
        from sam2.automatic_mask_generator import SAM2AutomaticMaskGenerator

        return SAM2AutomaticMaskGenerator
    from segment_anything import SamAutomaticMaskGenerator

    return SamAutomaticMaskGenerator


def _handle_auto(request_id: int, request: dict) -> None:
    global _BUSY
    try:
        if _CURRENT_RGB is None:
            _error(request_id, "not_ready", "尚未设置影像（set_image）")
            return
        import cv2
        import numpy as np

        generator_cls = _mask_generator_class()

        rgb = _CURRENT_RGB
        height, width = rgb.shape[:2]
        max_side = max(256, int(request.get("max_side") or 2048))
        scale = min(1.0, max_side / max(width, height))
        _emit({"event": "status", "id": request_id, "stage": "auto", "message": "正在自动分割…"})
        max_candidates = max(1, int(request.get("max_candidates") or 200))
        if generator_cls is None:
            found = _sam3_auto_candidates(
                int(request.get("points_per_side") or 16),
                float(request.get("pred_iou_thresh") or 0.86),
                int(request.get("min_mask_region_area") or 0),
                max_candidates,
            )
            scale = 1.0  # SAM3 候选直接来自全分辨率预测
        else:
            work = rgb
            if scale < 1.0:
                work = cv2.resize(
                    rgb,
                    (max(1, round(width * scale)), max(1, round(height * scale))),
                    interpolation=cv2.INTER_AREA,
                )
            generator = generator_cls(
                model=_MODEL,
                points_per_side=int(request.get("points_per_side") or 24),
                pred_iou_thresh=float(request.get("pred_iou_thresh") or 0.86),
                stability_score_thresh=float(request.get("stability_score_thresh") or 0.92),
                min_mask_region_area=int(request.get("min_mask_region_area") or 0),
                output_mode="binary_mask",
            )
            stop_event = threading.Event()
            reporter = threading.Thread(target=_progress_loop, args=(stop_event, request_id), daemon=True)
            reporter.start()
            try:
                found = generator.generate(work)
            finally:
                stop_event.set()
            # 自动掩膜生成器在缩放图上工作，输出需要放回原分辨率
            # （SAM3 网格候选直接来自全分辨率预测，scale 保持 1.0）

        # 面积降序，截断候选数
        found.sort(key=lambda item: int(item.get("area") or 0), reverse=True)
        found = found[:max_candidates]
        results = []
        for index, item in enumerate(found):
            mask = np.asarray(item["segmentation"])
            if scale < 1.0:
                mask = cv2.resize(
                    mask.astype(np.uint8),
                    (width, height),
                    interpolation=cv2.INTER_NEAREST,
                )
            path = os.path.join(_OUT_DIR, f"auto_{request_id}_{index}.png")
            _write_mask(mask, path)
            results.append(
                {
                    "path": path,
                    "score": float(item.get("predicted_iou") or 0.0),
                    "stability": float(item.get("stability_score") or 0.0),
                    "area": int(item.get("area") or 0),
                    "bbox": [int(v) for v in item.get("bbox") or [0, 0, 0, 0]],
                }
            )
        _emit({"event": "result", "id": request_id, "token": request.get("token") or "", "masks": results})
    except Exception as exc:  # noqa: BLE001
        _log(traceback.format_exc())
        _error(request_id, _classify_error(exc), str(exc))


def main() -> int:
    global _OUT_DIR, _BUSY
    parser = argparse.ArgumentParser(description="Meta SAM sidecar")
    parser.add_argument("--sam-src", default="")
    parser.add_argument("--out-dir", required=True)
    args = parser.parse_args()
    _OUT_DIR = args.out_dir
    if args.sam_src and args.sam_src not in sys.path:
        sys.path.insert(0, args.sam_src)
    os.makedirs(_OUT_DIR, exist_ok=True)

    _emit({"event": "hello", "protocol": 1, "python": sys.version.split()[0], "pid": os.getpid()})
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            _error(None, "bad_request", "无法解析 JSON")
            continue
        request_id = request.get("id")
        command = request.get("cmd")
        _BUSY = True
        try:
            if command == "load":
                _handle_load(request_id, request)
            elif command == "set_image":
                _handle_set_image(request_id, request)
            elif command == "predict":
                _handle_predict(request_id, request)
            elif command == "text":
                _handle_text(request_id, request)
            elif command == "exemplar":
                _handle_exemplar(request_id, request)
            elif command == "auto":
                _handle_auto(request_id, request)
            elif command == "shutdown":
                _emit({"event": "bye", "id": request_id})
                return 0
            elif command == "ping":
                _emit({"event": "pong", "id": request_id, "ready": _PREDICTOR is not None})
            else:
                _error(request_id, "bad_request", f"未知命令：{command}")
        finally:
            _BUSY = False
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(0) from None
