"""推理子进程核心：checkpoint 载入、滑动窗口前向、结果写入与统计。

本模块运行在外部 Python（torch / rasterio），不 import ``app.*``；
``trainlib`` 通过 :mod:`inferlib._trainlib_path` 注入 ``sys.path`` 后复用。
"""

from __future__ import annotations

import math
import time
from datetime import datetime
from pathlib import Path

import numpy as np

from ._trainlib_path import add_training_path

add_training_path()

from trainlib import common  # noqa: E402

from . import raster_io, tiles  # noqa: E402
from .instances import BlockRect, BoxSuppressor, mask_centroid, paste_mask, select_instances  # noqa: E402

STATUS_DONE = "推理完成"
STATUS_STOPPED = "已停止（结果不完整）"

#: checkpoint 缺少颜色时的兜底调色板
_FALLBACK_COLORS = (
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
)


# ---------------------------------------------------------------------- checkpoint
def _load_checkpoint(path: str | Path) -> dict:
    import torch

    checkpoint_path = Path(path)
    if not checkpoint_path.is_file():
        raise ValueError(f"权重文件不存在：{checkpoint_path}")
    try:
        payload = torch.load(checkpoint_path, map_location="cpu")
    except Exception:  # noqa: BLE001 —— 老版本 torch 不认识 weights_only
        payload = torch.load(checkpoint_path, map_location="cpu", weights_only=False)
    if not isinstance(payload, dict) or "state_dict" not in payload:
        raise ValueError(f"不是本工具保存的 checkpoint：{checkpoint_path}")
    return payload


def inspect_checkpoint(path: str | Path) -> dict:
    """只读 checkpoint 元数据（供 ``--inspect`` 与界面校验）。"""
    payload = _load_checkpoint(path)
    return {
        "checkpoint": str(path),
        "task": str(payload.get("task") or ""),
        "model": str(payload.get("model") or payload.get("arch") or ""),
        "encoder": str(payload.get("encoder") or ""),
        "num_classes": int(payload.get("num_classes") or 0),
        "class_names": [str(item) for item in payload.get("class_names") or []],
        "class_colors": [str(item) for item in payload.get("class_colors") or []],
        "tile_size": int(payload.get("tile_size") or 512),
        "epoch": int(payload.get("epoch") or 0),
        "metrics": payload.get("metrics") or {},
    }


def class_table(
    task: str,
    class_names,
    class_colors,
    num_classes: int,
) -> tuple[list[dict], bool]:
    """按任务推导类别表；返回 ``(classes, include_background)``。

    不能用 checkpoint 的 ``class_ids``：训练侧保存时 ``int(id or index+1)``
    会把 ``objects`` 的背景 id=0 写成 1。这里按 ``num_classes`` 与类别表长度
    重新推导——两类任务的“通道索引 == 类别 id”都成立（objects 0..K，
    landcover/instances 1..K）。
    """
    names = [str(item) for item in class_names or []]
    colors = [str(item) for item in class_colors or []]
    count = max(1, int(num_classes))
    # objects 的类别表含背景（id 0 起）；landcover / instances 的表不含背景，
    # 通道 / 类别 id 1..K 对应 names[0..K-1]。
    include_background = task == "objects" and len(names) >= count

    def entry(class_id: int) -> dict:
        source = class_id if include_background else class_id - 1
        return {
            "id": class_id,
            "name": names[source]
            if 0 <= source < len(names) and names[source]
            else f"类别{class_id}",
            "color": colors[source]
            if 0 <= source < len(colors) and colors[source]
            else _FALLBACK_COLORS[(source if source >= 0 else class_id) % len(_FALLBACK_COLORS)],
        }

    if include_background:
        return [entry(index) for index in range(count)], True
    return [entry(index + 1) for index in range(max(0, count - 1))], False


def _as_bool(value, default: bool) -> bool:
    """manifest 里可能是 bool 或 JSON 字符串；无法识别时用默认值。"""
    if value is None:
        return bool(default)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ("1", "true", "yes", "on"):
            return True
        if text in ("0", "false", "no", "off", ""):
            return False
        return bool(default)
    return bool(value)


def _resolve_device(torch, requested) -> str:
    requested = str(requested or "auto").strip().lower()
    if requested == "cuda":
        if not torch.cuda.is_available():
            raise ValueError("请求使用 CUDA，但当前 Python 环境的 CUDA 不可用")
        return "cuda"
    if requested == "cpu":
        return "cpu"
    return "cuda" if torch.cuda.is_available() else "cpu"


def _build_model(task: str, model_id: str, encoder: str, num_classes: int, window: int):
    from trainlib.models import build_model

    return build_model(
        task,
        model_id,
        encoder,
        int(num_classes),
        pretrained=False,
        tile_size=int(window),
        log=common.log,
    )


# ---------------------------------------------------------------------- 统计
def _area_per_pixel(meta: raster_io.RasterMeta) -> tuple[float | None, bool]:
    """返回 ``(每像元面积 m², 是否近似)``；无地理参考时面积为 None。"""
    if not meta.crs:
        return None, False
    transform = meta.transform
    a = float(getattr(transform, "a", 0.0))
    b = float(getattr(transform, "b", 0.0))
    d = float(getattr(transform, "d", 0.0))
    e = float(getattr(transform, "e", 0.0))
    pixel_area = abs(a * e - b * d)
    if pixel_area <= 0:
        return None, False
    if meta.is_geographic:
        f = float(getattr(transform, "f", 0.0))
        center_y = d * (meta.width / 2.0) + e * (meta.height / 2.0) + f
        meters_per_degree = 111320.0
        meters_lon = meters_per_degree * max(0.01, math.cos(math.radians(center_y)))
        return pixel_area * meters_per_degree * meters_lon, True
    factor = float(meta.linear_unit_factor or 1.0)
    return pixel_area * factor * factor, False


def _format_area(area_m2: float | None, approximate: bool) -> str:
    if area_m2 is None or not math.isfinite(area_m2):
        return ""
    if area_m2 >= 1_000_000:
        text = f"{area_m2 / 1_000_000:.2f} km²"
    elif area_m2 >= 1:
        text = f"{area_m2:.0f} m²"
    else:
        text = f"{area_m2:.2f} m²"
    return f"约 {text}" if approximate else text


def _class_statistics(
    classes: list[dict],
    *,
    include_background: bool,
    counts: np.ndarray,
    instance_counts: np.ndarray,
    valid_pixels: int,
    area_per_pixel: float | None,
    area_approximate: bool,
    task: str,
) -> list[dict]:
    entries: list[dict] = []
    if not include_background:
        entries.append({"id": 0, "name": "背景/未分类", "color": ""})
    entries.extend(classes)

    result = []
    denominator = max(1, int(valid_pixels))
    for item in entries:
        class_id = int(item.get("id") or 0)
        pixels = int(counts[class_id]) if 0 <= class_id < counts.size else 0
        area = pixels * area_per_pixel if area_per_pixel is not None else None
        result.append(
            {
                "id": class_id,
                "name": str(item.get("name") or f"类别{class_id}"),
                "color": str(item.get("color") or ""),
                "pixels": pixels,
                "ratio": round(pixels / denominator, 6),
                "area_m2": round(area, 2) if area is not None else None,
                "area_text": _format_area(area, area_approximate),
                "instances": int(instance_counts[class_id])
                if task == "instances" and 0 <= class_id < instance_counts.size
                else 0,
            }
        )
    return result


# ---------------------------------------------------------------------- 主流程
def run_inference(manifest: dict) -> int:
    """执行一次推理；返回进程退出码（0 = done/stopped 都算正常结束）。"""
    import torch

    params = dict(manifest.get("params") or {})
    checkpoint_path = str(manifest.get("checkpoint") or "")
    image_path = str(manifest.get("image") or "")
    out_dir = Path(manifest.get("output_dir") or ".")
    out_dir.mkdir(parents=True, exist_ok=True)

    common.log(f"推理子进程启动（Python {torch.__version__}，{checkpoint_path}）")
    payload = _load_checkpoint(checkpoint_path)
    task = str(payload.get("task") or "")
    model_id = str(payload.get("model") or payload.get("arch") or "")
    encoder = str(payload.get("encoder") or "")
    num_classes = int(payload.get("num_classes") or 0)
    tile_size = int(payload.get("tile_size") or 512)
    class_names = [str(item) for item in payload.get("class_names") or []]
    class_colors = [str(item) for item in payload.get("class_colors") or []]
    if task not in ("objects", "landcover", "instances"):
        raise ValueError(f"不支持的 checkpoint 任务：{task or '（空）'}")
    if not model_id:
        raise ValueError("checkpoint 缺少模型类型（model/arch）")
    if num_classes < 1 or not Path(image_path).is_file():
        raise ValueError("推理参数不完整（checkpoint 类别数或影像路径无效）")

    requested_window = int(params.get("window_size") or 0)
    window = requested_window or tile_size
    if task == "instances":
        # torchvision 检测器的 transform 按训练尺寸固定，改窗口会导致二次缩放
        window = tile_size
    window = max(tiles.ALIGN, int(window))
    overlap = min(50, max(0, int(params.get("overlap_percent", 25))))
    keep = tiles.window_geometry(window, overlap)[1]

    meta = raster_io.open_meta(image_path)
    bands = raster_io.resolve_bands(params.get("bands"), meta.count)
    device = _resolve_device(torch, params.get("device"))
    common.log(
        f"设备 {device}；影像 {meta.width}×{meta.height}，{meta.count} 波段 "
        f"（使用 {bands}）；窗口 {window}，重叠 {overlap}%（步长 {keep}）"
    )

    model = _build_model(task, model_id, encoder, num_classes, window)
    try:
        model.load_state_dict(payload["state_dict"], strict=True)
    except RuntimeError as exc:
        raise ValueError(f"权重结构与模型不匹配：{exc}") from exc
    model.to(device).eval()

    classes, include_background = class_table(task, class_names, class_colors, num_classes)
    class_by_id = {int(item["id"]): item for item in classes}
    colormap = raster_io.colormap_from_classes(classes, include_background=include_background)

    windows = tiles.plan_windows(meta.width, meta.height, window, overlap)
    total = len(windows)
    output_path = out_dir / "prediction.tif"
    score_threshold = float(params.get("score_threshold", 0.5))
    min_instance_pixels = max(0, int(params.get("min_instance_pixels", 16)))
    batch_limit = max(1, int(params.get("batch_size") or 4))
    build_overviews = bool(params.get("build_overviews", True))
    zero_as_nodata = _as_bool(params.get("zero_as_nodata"), True)

    source = raster_io.SourceRaster(
        image_path, bands, meta, zero_as_nodata=zero_as_nodata
    )
    writer = raster_io.OutputRaster(output_path, meta, colormap=colormap)
    counts = np.zeros(256, dtype=np.int64)
    instance_counts = np.zeros(256, dtype=np.int64)
    suppressor = BoxSuppressor()
    valid_total = 0
    nodata_total = 0
    done = 0
    started_at = time.monotonic()
    last_progress = started_at
    stopped = False

    area_per_pixel, area_approximate = _area_per_pixel(meta)

    common.emit(
        "started",
        checkpoint=checkpoint_path,
        task=task,
        model=model_id,
        encoder=encoder,
        classes=classes,
        include_background=include_background,
        image=image_path,
        width=meta.width,
        height=meta.height,
        band_count=meta.count,
        band_names=list(meta.band_names),
        bands=list(bands),
        crs=meta.crs_name,
        pixel_size=list(meta.pixel_size),
        is_geographic=meta.is_geographic,
        window_size=window,
        overlap_percent=overlap,
        keep_size=keep,
        windows=total,
        device=device,
        zero_as_nodata=zero_as_nodata,
        output=str(output_path),
        area_approximate=area_approximate,
    )

    def report_progress(done_count: int, *, force: bool = False) -> None:
        nonlocal last_progress
        now = time.monotonic()
        if not force and done_count < total and now - last_progress < 0.3:
            return
        elapsed = now - started_at
        eta = elapsed / max(1, done_count) * max(0, total - done_count)
        last_progress = now
        common.emit(
            "progress",
            stage="inferring",
            done=int(done_count),
            total=int(total),
            elapsed=round(elapsed, 1),
            eta=round(eta, 1),
        )

    def semantic_batch(chunk) -> None:
        nonlocal valid_total, nodata_total
        from trainlib.data import IMAGENET_MEAN, IMAGENET_STD

        arrays = []
        kept = []
        for item in chunk:
            rgb, valid = source.read_rgb(item.ix, item.iy, item.iw, item.ih)
            x0 = item.ox - item.ix
            y0 = item.oy - item.iy
            valid_block = valid[y0 : y0 + item.oh, x0 : x0 + item.ow]
            if not valid_block.any():
                # 输出块全是 nodata：不做前向，文件里保持 0
                nodata_total += item.ow * item.oh
                continue
            if not valid.all():
                # 用窗口内有效像元均值填充 nodata，避免黑边/填充值参与卷积
                fill = np.rint(rgb[valid].mean(axis=0)).astype(np.uint8)
                rgb[~valid] = fill
            pad_h, pad_w = item.padded_size
            if (pad_h, pad_w) != (item.ih, item.iw):
                rgb = np.pad(
                    rgb,
                    ((0, pad_h - item.ih), (0, pad_w - item.iw), (0, 0)),
                    mode="constant",
                )
            array = rgb.astype(np.float32) / 255.0
            array = (array - IMAGENET_MEAN) / IMAGENET_STD
            arrays.append(np.ascontiguousarray(array.transpose(2, 0, 1)))
            kept.append((item, valid_block))
        if not arrays:
            return

        tensor = torch.from_numpy(np.stack(arrays)).to(device)
        with torch.inference_mode():
            if device == "cuda":
                with torch.autocast(device_type="cuda", dtype=torch.float16):
                    logits = model(tensor)
            else:
                logits = model(tensor)
        prediction = logits.argmax(1).to("cpu").numpy()
        del logits, tensor

        for row, (item, valid_block) in enumerate(kept):
            x0 = item.ox - item.ix
            y0 = item.oy - item.iy
            block = prediction[row][
                y0 : y0 + item.oh,
                x0 : x0 + item.ow,
            ].astype(np.uint8).copy()
            block[~valid_block] = 0
            writer.write(item.ox, item.oy, block)
            valid_total += int(valid_block.sum())
            nodata_total += int((~valid_block).sum())
            if valid_block.any():
                counts[:] += np.bincount(block[valid_block].ravel(), minlength=256)

    def instance_batch(chunk) -> None:
        nonlocal valid_total, nodata_total
        arrays = []
        kept = []
        for item in chunk:
            rgb, valid = source.read_rgb(item.ix, item.iy, item.iw, item.ih)
            x0 = item.ox - item.ix
            y0 = item.oy - item.iy
            valid_block = valid[y0 : y0 + item.oh, x0 : x0 + item.ow]
            if not valid_block.any():
                nodata_total += item.ow * item.oh
                continue
            if not valid.all():
                fill = np.rint(rgb[valid].mean(axis=0)).astype(np.uint8)
                rgb[~valid] = fill
            array = rgb.astype(np.float32) / 255.0
            arrays.append(np.ascontiguousarray(array.transpose(2, 0, 1)))
            kept.append((item, valid_block))
        if not arrays:
            return

        tensor = torch.from_numpy(np.stack(arrays)).to(device)
        with torch.inference_mode():
            if device == "cuda":
                with torch.autocast(device_type="cuda", dtype=torch.float16):
                    outputs = model(tensor)
            else:
                outputs = model(tensor)
        del tensor

        for row, (item, valid_block) in enumerate(kept):
            output = outputs[row]
            scores = output["scores"].detach().cpu().numpy()
            labels = output["labels"].detach().cpu().numpy()
            boxes = output["boxes"].detach().cpu().numpy()
            candidates = np.nonzero(scores >= score_threshold)[0]
            if candidates.size:
                masks = (output["masks"][candidates, 0] > 0.5).detach().cpu().numpy()
            else:
                masks = np.zeros((0, item.ih, item.iw), dtype=bool)
            selected = select_instances(
                boxes[candidates],
                scores[candidates],
                masks,
                score_threshold=score_threshold,
                min_instance_pixels=min_instance_pixels,
            )

            block = np.zeros((item.oh, item.ow), dtype=np.uint8)
            order = sorted(selected, key=lambda index: float(scores[candidates[index]]))
            for local in order:
                global_index = int(candidates[local])
                class_id = int(labels[global_index])
                if class_id not in class_by_id:
                    continue
                mask = masks[local]
                centroid = mask_centroid(mask)
                if centroid is None:
                    continue
                center_x = item.ix + centroid[0]
                center_y = item.iy + centroid[1]
                if not (
                    item.ox <= center_x < item.ox + item.ow
                    and item.oy <= center_y < item.oy + item.oh
                ):
                    continue
                scene_box = boxes[global_index] + np.array(
                    [item.ix, item.iy, item.ix, item.iy], dtype=np.float32
                )
                if suppressor.duplicate(class_id, scene_box):
                    continue
                suppressor.add(class_id, scene_box)
                paste_mask(
                    block,
                    mask,
                    window_origin=(item.ix, item.iy),
                    block_rect=BlockRect(item.ox, item.oy, item.ow, item.oh),
                    class_id=class_id,
                )
                instance_counts[class_id] += 1

            block[~valid_block] = 0
            writer.write(item.ox, item.oy, block)
            valid_total += int(valid_block.sum())
            nodata_total += int((~valid_block).sum())
            if valid_block.any():
                counts[:] += np.bincount(block[valid_block].ravel(), minlength=256)

        del outputs

    process_batch = instance_batch if task == "instances" else semantic_batch

    try:
        index = 0
        while index < total:
            if common.stop_requested():
                stopped = True
                common.log("收到停止请求：当前批次结束后退出")
                break
            chunk = windows[index : index + batch_limit]
            try:
                process_batch(chunk)
            except RuntimeError as exc:
                if "out of memory" not in str(exc).lower() or batch_limit <= 1:
                    raise
                batch_limit = max(1, batch_limit // 2)
                if device == "cuda":
                    torch.cuda.empty_cache()
                common.log(f"显存不足，批大小减半为 {batch_limit} 后重试")
                continue
            index += len(chunk)
            done += len(chunk)
            report_progress(done)

        report_progress(done, force=True)

        status = STATUS_STOPPED if stopped else STATUS_DONE

        if build_overviews:
            common.emit(
                "progress",
                stage="overview",
                done=int(done),
                total=int(total),
                elapsed=round(time.monotonic() - started_at, 1),
            )
            try:
                writer.build_overviews()
                common.log("已生成概览（2/4/8/16）")
            except Exception as exc:  # noqa: BLE001 —— 概览失败不影响成果
                common.log(f"生成概览失败（不影响成果）：{exc}")
    finally:
        writer.close()
        source.close()

    duration = round(time.monotonic() - started_at, 1)
    class_stats = _class_statistics(
        classes,
        include_background=include_background,
        counts=counts,
        instance_counts=instance_counts,
        valid_pixels=valid_total,
        area_per_pixel=area_per_pixel,
        area_approximate=area_approximate,
        task=task,
    )
    stats = {
        "version": 1,
        "status": status,
        "partial": bool(stopped),
        "created": datetime.now().astimezone().isoformat(timespec="seconds"),
        "checkpoint": checkpoint_path,
        "image": image_path,
        "output": str(output_path),
        "task": task,
        "model": model_id,
        "encoder": encoder,
        "device": device,
        "width": meta.width,
        "height": meta.height,
        "bands": list(bands),
        "window_size": window,
        "overlap_percent": overlap,
        "keep_size": keep,
        "windows_total": total,
        "windows_done": int(done),
        "valid_pixels": int(valid_total),
        "nodata_pixels": int(nodata_total),
        "duration_seconds": duration,
        "area_per_pixel_m2": round(area_per_pixel, 6) if area_per_pixel else None,
        "area_approximate": area_approximate,
        "classes": class_stats,
    }
    common.atomic_write_json(out_dir / "result.json", stats)

    common.emit(
        "stopped" if stopped else "done",
        output=str(output_path),
        stats=stats,
        partial=bool(stopped),
        duration=duration,
        windows=int(total),
        done=int(done),
        classes=class_stats,
    )
    common.log(f"{status}：{output_path}（{duration}s）")
    return 0


__all__ = [
    "STATUS_DONE",
    "STATUS_STOPPED",
    "class_table",
    "inspect_checkpoint",
    "run_inference",
]
