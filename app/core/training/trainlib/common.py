"""训练子进程公共工具：事件输出、类别对齐、数据划分、指标。

本模块只依赖标准库 + numpy / PIL（延迟导入），**不能** import ``app.*``。
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
import threading
import time
from pathlib import Path

_EMIT_LOCK = threading.Lock()
_STOP_EVENT = threading.Event()
# 协议流在 import 时固定下来：训练期间第三方库往 sys.stdout 打印也不会污染协议
_PROTOCOL_STREAM = sys.stdout
_LOG_HANDLE = None

#: 停止请求文件名（位于 run 目录）。GUI 写文件，训练循环轮询——
#: 不能用后台线程阻塞读 stdin：Windows + Python 3.10 下会与 DLL 加载死锁。
STOP_REQUEST_NAME = "stop.request"
_STOP_FILE: Path | None = None


# ---------------------------------------------------------------------- 事件
def emit(event: str, **payload) -> None:
    """向 stdout 写一行协议 JSON（线程安全、立即 flush）。"""
    message = {"event": str(event)}
    message.update(payload)
    line = json.dumps(message, ensure_ascii=False, default=_json_default)
    with _EMIT_LOCK:
        try:
            _PROTOCOL_STREAM.write(line + "\n")
            _PROTOCOL_STREAM.flush()
        except (BrokenPipeError, OSError):
            # 宿主已经退出：停止训练循环，别再刷屏
            _STOP_EVENT.set()


def set_log_file(path: str | Path) -> None:
    """把 ``log()`` 同时写一份到训练日志文件（run 目录下的 train.log）。"""
    global _LOG_HANDLE
    try:
        # 句柄要在整个训练进程生命周期内保持打开，不能交给 with 块
        _LOG_HANDLE = Path(path).open("a", encoding="utf-8", buffering=1)  # noqa: SIM115
    except OSError:
        _LOG_HANDLE = None


def log(message: str) -> None:
    if _LOG_HANDLE is not None:
        with contextlib.suppress(OSError):
            _LOG_HANDLE.write(f"[{now_stamp()}] {message}\n")
    emit("log", message=str(message))


def request_stop() -> None:
    _STOP_EVENT.set()


def set_stop_file(path: str | Path | None) -> None:
    """设置停止请求文件路径（run 目录下的 ``stop.request``）。"""
    global _STOP_FILE
    _STOP_FILE = Path(path) if path else None


def stop_requested() -> bool:
    if _STOP_EVENT.is_set():
        return True
    if _STOP_FILE is not None:
        try:
            return _STOP_FILE.is_file()
        except OSError:
            return False
    return False


def _json_default(value):
    try:
        import numpy as np

        if isinstance(value, np.generic):
            return value.item()
        if isinstance(value, np.ndarray):
            return value.tolist()
    except Exception:  # noqa: BLE001
        pass
    if isinstance(value, Path):
        return str(value)
    return str(value)


# ---------------------------------------------------------------------- 文件
def atomic_write_json(path: str | Path, payload: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def append_jsonl(path: str | Path, payload: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, default=_json_default) + "\n")


def atomic_torch_save(payload: dict, path: str | Path) -> None:
    import torch

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    torch.save(payload, tmp)
    # Windows 上 torch.save 的文件句柄释放后再替换
    os.replace(tmp, path)


# ---------------------------------------------------------------------- 掩膜
def read_mask_ids(path: str | Path):
    """读取灰度标签掩膜（``H×W`` uint8）；彩色图拒绝，避免悄悄错位。"""
    import numpy as np
    from PIL import Image

    with Image.open(path) as image:
        if image.mode in ("1", "L"):
            array = np.asarray(image.convert("L"), dtype=np.uint8)
        elif image.mode == "P":
            array = np.asarray(image, dtype=np.uint8)
        elif image.mode in ("I", "I;16", "I;16B", "I;16L"):
            raw = np.asarray(image)
            if raw.size and int(raw.max()) > 255:
                raise ValueError(f"掩膜像素值超过 255，无法作为类别 id：{path}")
            array = raw.astype(np.uint8)
        else:
            raise ValueError(f"掩膜必须是 8 位灰度 / 调色板 PNG，实际为 {image.mode}：{path}")
    return np.ascontiguousarray(array)


def unique_mask_ids(path: str | Path, *, strict: bool = False) -> set[int]:
    import numpy as np

    try:
        array = read_mask_ids(path)
    except Exception:
        if strict:
            raise
        return set()
    return {int(value) for value in np.unique(array) if int(value) != 0}


def filter_empty_masks(samples: list[dict], *, log_fn=None) -> list[dict]:
    """剔除全空掩膜（整块未标注）。

    多分类任务里 0 表示未标注区域，全 0 掩膜不含任何监督信号，只会在训练中
    被当成整块背景，必须丢掉。读不出来的掩膜保留，交给训练阶段报错。
    """
    import numpy as np

    kept: list[dict] = []
    dropped: list[str] = []
    for sample in samples:
        path = str(sample.get("mask") or "")
        try:
            array = read_mask_ids(path)
        except Exception:  # noqa: BLE001 —— 坏掩膜不在这里吞掉
            kept.append(sample)
            continue
        if np.any(array):
            kept.append(sample)
        else:
            dropped.append(Path(path).name)
    if dropped and log_fn is not None:
        names = "、".join(dropped[:5])
        suffix = "" if len(dropped) <= 5 else f" 等 {len(dropped)} 个"
        log_fn(f"跳过全空（未标注）掩膜：{names}{suffix}")
    return kept


def build_lut(class_map: dict[int, int], num_classes: int):
    """原始掩膜 id -> 全局类别下标（0 = 背景）的 256 项查找表。"""
    import numpy as np

    lut = np.zeros(256, dtype=np.uint8)
    limit = max(0, min(255, int(num_classes) - 1))
    for raw, global_id in dict(class_map).items():
        try:
            raw_id, target = int(raw), int(global_id)
        except (TypeError, ValueError):
            continue
        if 0 <= raw_id <= 255 and 1 <= target <= limit:
            lut[raw_id] = target
    return lut


# ---------------------------------------------------------------------- 类别
OBJECT_CLASSES: tuple[dict, ...] = (
    {"id": 0, "name": "背景", "color": "#000000"},
    {"id": 1, "name": "目标", "color": "#e6194b"},
)


def _as_class_map(sample: dict) -> dict[int, int]:
    raw = sample.get("class_map") or {}
    result: dict[int, int] = {}
    if isinstance(raw, dict):
        items = raw.items()
    elif isinstance(raw, (list, tuple)):
        items = raw
    else:
        items = ()
    for item in items:
        try:
            if isinstance(item, (list, tuple)):
                key, value = item[0], item[1]
            else:
                key, value = item
            result[int(key)] = int(value)
        except (TypeError, ValueError, IndexError):
            continue
    return result


def prepare_classes(task: str, samples: list[dict], classes: list[dict]) -> tuple[list[dict], int]:
    """补齐类别表并保证每个样本的 ``class_map`` 覆盖掩膜里的所有 id。

    - ``objects``：强制背景 / 目标两类，忽略原始 id（任何非 0 都是目标）；
    - ``landcover`` / ``instances``：已声明的 JSON 类别按名字对齐；掩膜里出现
      但 JSON 没记录的 id 自动补成 ``类别{id}``，避免整片像素被当成背景。
    """
    if task == "objects":
        return [dict(item) for item in OBJECT_CLASSES], 2

    # 类别 id 统一重排成连续的 1..K（模型输出通道数依赖它）
    prepared: list[dict] = []
    id_remap: dict[int, int] = {}
    for item in classes:
        if not isinstance(item, dict):
            continue
        try:
            old_id = int(item.get("id"))
        except (TypeError, ValueError):
            old_id = len(prepared) + 1
        new_id = len(prepared) + 1
        id_remap[old_id] = new_id
        prepared.append(
            {
                "id": new_id,
                "name": str(item.get("name") or f"类别{old_id}"),
                "color": str(item.get("color") or ""),
            }
        )
    by_name: dict[str, int] = {}
    for item in prepared:
        by_name.setdefault(item["name"], item["id"])
    next_id = len(prepared) + 1

    fallback_colors = (
        "#e6194b", "#3cb44b", "#ffe119", "#4363d8", "#f58231", "#911eb4",
        "#46f0f0", "#f032e6", "#bcf60c", "#fabebe", "#008080", "#e6beff",
    )
    for item in prepared:
        if not item["color"]:
            item["color"] = fallback_colors[(item["id"] - 1) % len(fallback_colors)]

    for sample in samples:
        mapping = {
            raw_id: id_remap.get(global_id, 0)
            for raw_id, global_id in _as_class_map(sample).items()
        }
        mapping = {raw: value for raw, value in mapping.items() if value > 0}
        for raw_id in sorted(unique_mask_ids(sample.get("mask") or "", strict=True)):
            if raw_id in mapping:
                continue
            name = f"类别{raw_id}"
            global_id = by_name.get(name)
            if global_id is None:
                global_id = next_id
                next_id += 1
                by_name[name] = global_id
                prepared.append(
                    {
                        "id": global_id,
                        "name": name,
                        "color": fallback_colors[(global_id - 1) % len(fallback_colors)],
                    }
                )
                log(f"掩膜里发现未声明类别 id={raw_id}，已补为「{name}」")
            mapping[raw_id] = global_id
        sample["class_map"] = {str(key): int(value) for key, value in sorted(mapping.items())}

    if not prepared:
        raise ValueError("没有可用的类别信息，且掩膜为空；无法确定类别数")
    return prepared, len(prepared) + 1


# ---------------------------------------------------------------------- 划分
def split_indices(count: int, val_percent: int, seed: int) -> tuple[list[int], list[int]]:
    """按比例划分训练 / 验证；样本很少时至少保证各一侧非空。"""
    import random

    indices = list(range(int(count)))
    random.Random(int(seed)).shuffle(indices)
    if count <= 1:
        return indices, indices
    percent = max(0, min(90, int(val_percent)))
    val_count = round(count * percent / 100.0)
    val_count = max(1, min(count - 1, val_count)) if percent > 0 else 0
    val = indices[:val_count]
    train = indices[val_count:]
    if not train:
        train, val = indices, []
    if not val and count > 1:
        # 验证集为空时用一小部分训练样本评估，指标至少能反映过拟合趋势
        val = train[: max(1, count // 5)]
    return train, val


# ---------------------------------------------------------------------- 指标
def confusion_metrics(confusion, num_classes: int, *, ignore_background: bool = False) -> dict:
    """由混淆矩阵计算像素精度与前景 mIoU。

    ``ignore_background=True`` 时丢掉 GT=0 的整行：0 是标注工具里的「未标注」
    区域，既不算召回，也不应把模型在未标注区的预测计为假阳性。
    """
    import numpy as np

    matrix = np.asarray(confusion, dtype=np.float64)
    if ignore_background and matrix.ndim == 2:
        matrix = matrix.copy()
        matrix[0, :] = 0
    total = float(matrix.sum())
    correct = float(np.trace(matrix)) if matrix.ndim == 2 else 0.0
    accuracy = correct / total if total > 0 else 0.0
    ious: list[float] = []
    class_ious: dict[str, float] = {}
    for class_id in range(1, int(num_classes)):
        true_positive = float(matrix[class_id, class_id])
        false_positive = float(matrix[:, class_id].sum()) - true_positive
        false_negative = float(matrix[class_id, :].sum()) - true_positive
        denominator = true_positive + false_positive + false_negative
        iou = true_positive / denominator if denominator > 0 else float("nan")
        if denominator > 0:
            ious.append(iou)
            class_ious[str(class_id)] = round(iou, 6)
    miou = float(sum(ious) / len(ious)) if ious else 0.0
    return {
        "acc": round(accuracy, 6),
        "miou": round(miou, 6),
        "class_iou": class_ious,
    }


def format_seconds(value: float) -> str:
    seconds = max(0, int(value))
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h{minutes:02d}m{seconds:02d}s"
    if minutes:
        return f"{minutes}m{seconds:02d}s"
    return f"{seconds}s"


def now_stamp() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


__all__ = [
    "OBJECT_CLASSES",
    "STOP_REQUEST_NAME",
    "append_jsonl",
    "atomic_torch_save",
    "atomic_write_json",
    "build_lut",
    "confusion_metrics",
    "emit",
    "filter_empty_masks",
    "format_seconds",
    "log",
    "now_stamp",
    "prepare_classes",
    "read_mask_ids",
    "request_stop",
    "set_log_file",
    "set_stop_file",
    "split_indices",
    "stop_requested",
    "unique_mask_ids",
]
