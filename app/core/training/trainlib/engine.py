"""训练 / 验证循环、指标、checkpoint。

由 ``train_sidecar.py`` 调用；所有进度通过 :func:`trainlib.common.emit` 发回 GUI。
"""

from __future__ import annotations

import random
import time
from pathlib import Path

import numpy as np

from . import common, finetune
from .data import InstanceDataset, SemanticDataset, count_instances
from .models import build_model, count_parameters


class _StopTrainingError(Exception):
    """用户从 GUI 请求停止。"""


def _should_drop_last(count: int, batch_size: int) -> bool:
    """末尾只剩 1 个样本时丢弃该批：ASPP 的 1×1 批归一化在训练态会直接报错。"""
    return batch_size > 1 and count > batch_size and count % batch_size == 1


def _seed_everything(seed: int) -> None:
    random.seed(seed)
    np.random.seed(seed % (2**32 - 1))
    try:
        import torch

        torch.manual_seed(seed)
        if torch.cuda.is_available():
            torch.cuda.manual_seed_all(seed)
    except Exception:  # noqa: BLE001
        pass


def _resolve_device(requested: str) -> str:
    import torch

    device = str(requested or "auto").lower()
    if device == "auto":
        return "cuda" if torch.cuda.is_available() else "cpu"
    if device == "cuda" and not torch.cuda.is_available():
        common.log("请求 CUDA 但当前环境不可用，回退 CPU 训练")
        return "cpu"
    return device if device in ("cuda", "cpu") else "cpu"


def _amp_context(device: str, enabled: bool):
    import contextlib

    if not enabled or device != "cuda":
        return contextlib.nullcontext()
    import torch

    try:
        return torch.amp.autocast(device_type="cuda", dtype=torch.float16)
    except Exception:  # noqa: BLE001 —— 老版本 torch
        return torch.cuda.amp.autocast()


def _make_grad_scaler(device: str, enabled: bool):
    import torch

    if not enabled or device != "cuda":
        return None
    try:
        return torch.amp.GradScaler("cuda", enabled=True)
    except Exception:  # noqa: BLE001
        try:
            return torch.cuda.amp.GradScaler(enabled=True)
        except Exception:  # noqa: BLE001
            return None


# ---------------------------------------------------------------------- 语义
def _dice_loss(logits, target, num_classes: int, ignore_index: int | None = None):
    import torch

    probabilities = torch.softmax(logits, dim=1)
    one_hot = torch.nn.functional.one_hot(
        target.clamp_min(0), num_classes=num_classes
    ).permute(0, 3, 1, 2).float()
    if ignore_index is not None:
        # 未标注像素不参与 Dice：分子分母同时清零，避免它们被当成背景
        valid = (target != ignore_index).unsqueeze(1).to(probabilities.dtype)
        probabilities = probabilities * valid
        one_hot = one_hot * valid
    dims = (0, 2, 3)
    intersection = (probabilities * one_hot).sum(dim=dims)
    cardinality = (probabilities + one_hot).sum(dim=dims)
    dice = (2 * intersection + 1.0) / (cardinality + 1.0)
    foreground = dice[1:] if num_classes > 1 else dice
    return 1.0 - foreground.mean()


def _semantic_loss(logits, target, loss_name: str, num_classes: int, ignore_index: int | None = None):
    import torch

    if ignore_index is not None and not bool((target != ignore_index).any()):
        # 整批都是未标注像素：CE 的分母为 0，返回 0 而不是 NaN
        return logits.sum() * 0.0
    ce = torch.nn.functional.cross_entropy(
        logits, target, ignore_index=-100 if ignore_index is None else int(ignore_index)
    )
    if loss_name == "dice_ce":
        return ce + _dice_loss(logits, target, num_classes, ignore_index)
    return ce


def _update_confusion(confusion, target, prediction, num_classes: int) -> None:
    flat_target = target.reshape(-1).detach().cpu().numpy().astype(np.int64)
    flat_prediction = prediction.reshape(-1).detach().cpu().numpy().astype(np.int64)
    valid = (flat_target >= 0) & (flat_target < num_classes)
    indices = flat_target[valid] * num_classes + flat_prediction[valid]
    counts = np.bincount(indices, minlength=num_classes * num_classes)
    confusion += counts.reshape(num_classes, num_classes)


def _train_semantic(context: dict) -> int:
    import torch
    from torch.utils.data import DataLoader

    params = context["params"]
    meta = context["meta"]
    num_classes = context["num_classes"]
    classes = context["classes"]
    train_samples = context["train_samples"]
    val_samples = context["val_samples"]
    out_dir = context["out_dir"]
    epochs = context["epochs"]

    train_set = SemanticDataset(
        train_samples,
        tile_size=params["tile_size"],
        num_classes=num_classes,
        augment=params["augment"],
        seed=params["seed"],
    )
    val_set = SemanticDataset(
        val_samples,
        tile_size=params["tile_size"],
        num_classes=num_classes,
        augment=False,
        seed=params["seed"],
    )
    loader_kwargs = {"num_workers": 0, "pin_memory": context["device"] == "cuda"}
    train_batch_size = min(params["batch_size"], max(1, len(train_set)))
    if train_batch_size == 1 and meta["model"] in ("deeplabv3", "deeplabv3plus"):
        raise ValueError(
            "DeepLabV3 / DeepLabV3+ 的 ASPP 含 1×1 批归一化，批大小为 1 时无法训练；"
            "请把批大小调到 2 以上"
        )
    drop_last = _should_drop_last(len(train_set), train_batch_size)
    train_loader = DataLoader(
        train_set,
        batch_size=train_batch_size,
        shuffle=True,
        drop_last=drop_last,
        **loader_kwargs,
    )
    if drop_last:
        common.log(
            "训练样本数除以批大小余 1，已丢弃最后一个单样本批次"
            "（ASPP 等 1×1 批归一化需要多于 1 个样本）"
        )
    val_loader = DataLoader(
        val_set,
        batch_size=min(params["batch_size"], max(1, len(val_set))),
        shuffle=False,
        drop_last=False,
        **loader_kwargs,
    )

    model = build_model(
        meta["task"],
        meta["model"],
        meta["encoder"],
        num_classes,
        pretrained=params["pretrained"],
        tile_size=params["tile_size"],
        log=common.log,
    )
    model.to(context["device"])
    common.log(f"模型参数量：{count_parameters(model) / 1e6:.2f} M")
    _load_initial_weights(model, params, meta, num_classes, classes)

    optimizer = torch.optim.AdamW(model.parameters(), lr=params["lr"], weight_decay=1e-4)
    scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(
        optimizer, T_max=max(1, epochs), eta_min=params["lr"] * 0.01
    )
    freeze_parts = _prepare_freeze(model, params, meta, epochs)
    freeze_state: dict = {"frozen": None}
    use_amp = bool(params["amp"] and context["device"] == "cuda")
    scaler = _make_grad_scaler(context["device"], use_amp)
    if use_amp:
        common.log("已启用 CUDA 混合精度（AMP）")

    best_value = -1.0
    best_epoch = 0
    best_path = out_dir / "best.pt"
    last_path = out_dir / "last.pt"
    history: list[dict] = []
    started = time.time()

    for epoch in range(1, epochs + 1):
        if common.stop_requested():
            raise _StopTrainingError
        model.train()
        _apply_epoch_freeze(freeze_parts, params, epoch, freeze_state)
        running_loss = 0.0
        seen = 0
        steps = len(train_loader)
        epoch_started = time.time()
        for step, (images, targets) in enumerate(train_loader, start=1):
            if common.stop_requested():
                raise _StopTrainingError
            images = images.to(context["device"], non_blocking=True)
            targets = targets.to(context["device"], non_blocking=True)
            optimizer.zero_grad(set_to_none=True)
            with _amp_context(context["device"], use_amp):
                logits = model(images)
                loss = _semantic_loss(
                    logits, targets, params["loss"], num_classes, context.get("ignore_index")
                )
            if scaler is not None:
                scaler.scale(loss).backward()
                scaler.step(optimizer)
                scaler.update()
            else:
                loss.backward()
                optimizer.step()
            batch = images.shape[0]
            running_loss += float(loss.detach().cpu()) * batch
            seen += batch
            if step == 1 or step == steps or step % max(1, steps // 10) == 0:
                common.emit(
                    "progress",
                    epoch=epoch,
                    total=epochs,
                    step=step,
                    steps=steps,
                    loss=round(running_loss / max(1, seen), 6),
                    lr=round(optimizer.param_groups[0]["lr"], 9),
                )

        train_loss = running_loss / max(1, seen)
        learning_rate = optimizer.param_groups[0]["lr"]
        scheduler.step()
        val_loss, metrics = _evaluate_semantic(model, val_loader, context, num_classes)
        metrics.update(
            {
                "epoch": epoch,
                "train_loss": round(train_loss, 6),
                "val_loss": None if val_loss is None else round(val_loss, 6),
                "lr": round(learning_rate, 9),
                "elapsed": round(time.time() - epoch_started, 3),
            }
        )
        primary = float(metrics.get("miou") or 0.0)
        metrics["primary"] = round(primary, 6)
        metrics["primary_label"] = "验证 mIoU"
        history.append(metrics)
        common.append_jsonl(out_dir / "metrics.jsonl", metrics)
        common.emit("epoch", total=epochs, **metrics)
        common.log(
            f"第 {epoch}/{epochs} 轮：训练损失 {train_loss:.4f}，"
            f"验证损失 {'—' if val_loss is None else f'{val_loss:.4f}'}，"
            f"mIoU {metrics.get('miou', 0):.4f}，用时 {common.format_seconds(metrics['elapsed'])}"
        )

        payload = _checkpoint(model, meta, num_classes, epoch, metrics, classes)
        common.atomic_torch_save(payload, last_path)
        if primary > best_value:
            best_value = primary
            best_epoch = epoch
            dict(metrics)
            common.atomic_torch_save(payload, best_path)
            common.log(f"验证指标提升，已保存 best.pt（mIoU={primary:.4f}）")

        _write_metrics(
            out_dir,
            meta,
            classes,
            epochs,
            history,
            best_epoch,
            best_value,
            "训练完成" if epoch == epochs else "训练中",
            started,
        )
        if params["early_stop"] > 0 and epoch - best_epoch >= params["early_stop"]:
            common.log(f"连续 {params['early_stop']} 轮未提升，提前结束训练")
            break

    _write_metrics(
        out_dir,
        meta,
        classes,
        epochs,
        history,
        best_epoch,
        best_value,
        "训练完成",
        started,
    )
    common.emit(
        "done",
        best=str(best_path),
        last=str(last_path),
        metrics=str(out_dir / "metrics.json"),
        best_value=round(best_value, 6),
        best_epoch=best_epoch,
        epochs=len(history),
    )
    return 0


def _evaluate_semantic(model, loader, context: dict, num_classes: int):
    import torch

    model.eval()
    confusion = np.zeros((num_classes, num_classes), dtype=np.int64)
    total_loss = 0.0
    seen = 0
    with torch.no_grad():
        for images, targets in loader:
            images = images.to(context["device"], non_blocking=True)
            targets = targets.to(context["device"], non_blocking=True)
            with _amp_context(context["device"], context["device"] == "cuda"):
                logits = model(images)
                loss = _semantic_loss(
                    logits,
                    targets,
                    context["params"]["loss"],
                    num_classes,
                    context.get("ignore_index"),
                )
            total_loss += float(loss.detach().cpu()) * images.shape[0]
            seen += images.shape[0]
            prediction = logits.argmax(dim=1)
            _update_confusion(confusion, targets, prediction, num_classes)
    ignore_background = context.get("ignore_index") == 0
    metrics = common.confusion_metrics(
        confusion, num_classes, ignore_background=ignore_background
    )
    if ignore_background:
        # 同时保留旧口径，便于和历史 run 对比
        raw = common.confusion_metrics(confusion, num_classes)
        metrics["miou_all"] = raw["miou"]
        metrics["acc_all"] = raw["acc"]
    return (total_loss / seen if seen else None), metrics


# ---------------------------------------------------------------------- 实例
def _instance_collate(batch):
    """检测任务不能走默认 collate（每个样本的框/掩膜数量不同）。"""
    images, targets = zip(*batch, strict=False)
    return list(images), list(targets)


def _move_targets(targets, device: str):
    return [
        {key: value.to(device, non_blocking=True) for key, value in target.items()}
        for target in targets
    ]


def _filter_instance_samples(
    samples: list[dict],
    *,
    tile_size: int,
    num_classes: int,
    min_area: int,
    label: str,
) -> list[dict]:
    usable = []
    for sample in samples:
        try:
            if count_instances(
                sample, tile_size=tile_size, num_classes=num_classes, min_area=min_area
            ) > 0:
                usable.append(sample)
        except Exception as exc:  # noqa: BLE001 —— 单个坏样本不终止整次训练
            common.log(f"{label}样本无法解析，已跳过：{sample.get('mask')}（{exc}）")
    return usable


def _train_instances(context: dict) -> int:
    import torch
    from torch.utils.data import DataLoader

    params = context["params"]
    meta = context["meta"]
    num_classes = context["num_classes"]
    classes = context["classes"]
    out_dir = context["out_dir"]
    epochs = context["epochs"]
    tile_size = params["tile_size"]

    train_samples = _filter_instance_samples(
        context["train_samples"],
        tile_size=tile_size,
        num_classes=num_classes,
        min_area=params["min_instance_area"],
        label="训练",
    )
    val_samples = _filter_instance_samples(
        context["val_samples"],
        tile_size=tile_size,
        num_classes=num_classes,
        min_area=params["min_instance_area"],
        label="验证",
    )
    if not train_samples:
        raise ValueError("所有训练样本都没有可用实例（可降低「实例最小面积」后重试）")
    if not val_samples:
        val_samples = train_samples[: max(1, len(train_samples) // 5)]
        common.log("验证集没有实例，改用部分训练样本做验证")

    train_set = InstanceDataset(
        train_samples,
        tile_size=tile_size,
        num_classes=num_classes,
        min_area=params["min_instance_area"],
        augment=params["augment"],
        seed=params["seed"],
    )
    val_set = InstanceDataset(
        val_samples,
        tile_size=tile_size,
        num_classes=num_classes,
        min_area=params["min_instance_area"],
        augment=False,
        seed=params["seed"],
    )
    loader_kwargs = {"num_workers": 0, "pin_memory": context["device"] == "cuda"}
    train_loader = DataLoader(
        train_set,
        batch_size=min(params["batch_size"], max(1, len(train_set))),
        shuffle=True,
        drop_last=False,
        collate_fn=_instance_collate,
        **loader_kwargs,
    )
    val_loader = DataLoader(
        val_set,
        batch_size=min(params["batch_size"], max(1, len(val_set))),
        shuffle=False,
        drop_last=False,
        collate_fn=_instance_collate,
        **loader_kwargs,
    )

    model = build_model(
        meta["task"],
        meta["model"],
        meta["encoder"],
        num_classes,
        pretrained=params["pretrained"],
        tile_size=tile_size,
        log=common.log,
    )
    model.to(context["device"])
    common.log(f"模型参数量：{count_parameters(model) / 1e6:.2f} M")
    _load_initial_weights(model, params, meta, num_classes, classes)

    optimizer = torch.optim.AdamW(model.parameters(), lr=params["lr"], weight_decay=1e-4)
    scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(
        optimizer, T_max=max(1, epochs), eta_min=params["lr"] * 0.01
    )
    freeze_parts = _prepare_freeze(model, params, meta, epochs)
    freeze_state: dict = {"frozen": None}

    best_value = -1.0
    best_epoch = 0
    best_path = out_dir / "best.pt"
    last_path = out_dir / "last.pt"
    history: list[dict] = []
    started = time.time()

    for epoch in range(1, epochs + 1):
        if common.stop_requested():
            raise _StopTrainingError
        model.train()
        _apply_epoch_freeze(freeze_parts, params, epoch, freeze_state)
        running_loss = 0.0
        seen = 0
        steps = len(train_loader)
        epoch_started = time.time()
        for step, (images, targets) in enumerate(train_loader, start=1):
            if common.stop_requested():
                raise _StopTrainingError
            images = [image.to(context["device"], non_blocking=True) for image in images]
            targets = _move_targets(targets, context["device"])
            if not any(target["boxes"].numel() for target in targets):
                continue
            optimizer.zero_grad(set_to_none=True)
            losses = model(images, targets)
            loss = sum(losses.values())
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), 5.0)
            optimizer.step()
            batch = len(images)
            running_loss += float(loss.detach().cpu()) * batch
            seen += batch
            if step == 1 or step == steps or step % max(1, steps // 10) == 0:
                common.emit(
                    "progress",
                    epoch=epoch,
                    total=epochs,
                    step=step,
                    steps=steps,
                    loss=round(running_loss / max(1, seen), 6),
                    lr=round(optimizer.param_groups[0]["lr"], 9),
                )

        train_loss = running_loss / max(1, seen)
        learning_rate = optimizer.param_groups[0]["lr"]
        scheduler.step()
        metrics = _evaluate_instances(model, val_loader, context)
        metrics.update(
            {
                "epoch": epoch,
                "train_loss": round(train_loss, 6),
                "val_loss": None,
                "lr": round(learning_rate, 9),
                "elapsed": round(time.time() - epoch_started, 3),
            }
        )
        primary = float(metrics.get("f1") or 0.0)
        metrics["primary"] = round(primary, 6)
        metrics["primary_label"] = "验证 F1"
        history.append(metrics)
        common.append_jsonl(out_dir / "metrics.jsonl", metrics)
        common.emit("epoch", total=epochs, **metrics)
        common.log(
            f"第 {epoch}/{epochs} 轮：训练损失 {train_loss:.4f}，"
            f"F1 {metrics.get('f1', 0):.4f}，mask IoU {metrics.get('mask_iou', 0):.4f}"
        )

        payload = _checkpoint(model, meta, num_classes, epoch, metrics, classes)
        common.atomic_torch_save(payload, last_path)
        if primary > best_value:
            best_value = primary
            best_epoch = epoch
            common.atomic_torch_save(payload, best_path)
            common.log(f"验证指标提升，已保存 best.pt（F1={primary:.4f}）")
        _write_metrics(
            out_dir,
            meta,
            classes,
            epochs,
            history,
            best_epoch,
            best_value,
            "训练完成" if epoch == epochs else "训练中",
            started,
        )
        if params["early_stop"] > 0 and epoch - best_epoch >= params["early_stop"]:
            common.log(f"连续 {params['early_stop']} 轮未提升，提前结束训练")
            break

    _write_metrics(
        out_dir,
        meta,
        classes,
        epochs,
        history,
        best_epoch,
        best_value,
        "训练完成",
        started,
    )
    common.emit(
        "done",
        best=str(best_path),
        last=str(last_path),
        metrics=str(out_dir / "metrics.json"),
        best_value=round(best_value, 6),
        best_epoch=best_epoch,
        epochs=len(history),
    )
    return 0


def _evaluate_instances(model, loader, context: dict) -> dict:
    import torch

    model.eval()
    true_positive = 0
    false_positive = 0
    false_negative = 0
    matched_ious: list[float] = []
    with torch.no_grad():
        for images, targets in loader:
            images = [image.to(context["device"], non_blocking=True) for image in images]
            predictions = model(images)
            for prediction, target in zip(predictions, targets, strict=False):
                matched, false_pos, false_neg, ious = _match_masks(prediction, target)
                true_positive += matched
                false_positive += false_pos
                false_negative += false_neg
                matched_ious.extend(ious)
    precision = true_positive / max(1, true_positive + false_positive)
    recall = true_positive / max(1, true_positive + false_negative)
    f1 = 2 * precision * recall / max(1e-9, precision + recall)
    mask_iou = float(np.mean(matched_ious)) if matched_ious else 0.0
    return {
        "precision": round(precision, 6),
        "recall": round(recall, 6),
        "f1": round(f1, 6),
        "mask_iou": round(mask_iou, 6),
        "tp": true_positive,
        "fp": false_positive,
        "fn": false_negative,
    }


def _match_masks(prediction: dict, target: dict):
    """按类别做 mask IoU > 0.5 的贪心匹配。"""
    predicted_masks = prediction.get("masks")
    predicted_labels = prediction.get("labels")
    if predicted_masks is None or predicted_masks.numel() == 0:
        ground_truth = target["masks"].numpy() > 0
        return 0, 0, int(ground_truth.reshape(len(ground_truth), -1).any(axis=1).sum()), []
    predicted_binary = (predicted_masks[:, 0].cpu().numpy() > 0.5)
    predicted_labels = predicted_labels.cpu().numpy()
    ground_truth_masks = target["masks"].cpu().numpy() > 0
    ground_truth_labels = target["labels"].cpu().numpy()

    matched = false_positive = 0
    ious: list[float] = []
    used_predicted: set[int] = set()
    for class_id in sorted(set(ground_truth_labels.tolist()) | set(predicted_labels.tolist())):
        gt_indices = [index for index, label in enumerate(ground_truth_labels) if label == class_id]
        pred_indices = [index for index, label in enumerate(predicted_labels) if label == class_id]
        for gt_index in gt_indices:
            best_iou = 0.0
            best_predicted = -1
            gt_mask = ground_truth_masks[gt_index]
            for pred_index in pred_indices:
                if pred_index in used_predicted:
                    continue
                pred_mask = predicted_binary[pred_index]
                intersection = float(np.logical_and(gt_mask, pred_mask).sum())
                union = float(np.logical_or(gt_mask, pred_mask).sum())
                iou = intersection / union if union > 0 else 0.0
                if iou > best_iou:
                    best_iou, best_predicted = iou, pred_index
            if best_predicted >= 0 and best_iou >= 0.5:
                matched += 1
                used_predicted.add(best_predicted)
                ious.append(best_iou)
            else:
                false_positive += 0 if best_predicted < 0 else 1
                if best_predicted >= 0:
                    used_predicted.add(best_predicted)
    false_negative = len(ground_truth_masks) - matched
    false_positive += len(predicted_binary) - len(used_predicted)
    return matched, max(0, false_positive), max(0, false_negative), ious


# ---------------------------------------------------------------------- 公共
def _checkpoint(model, meta: dict, num_classes: int, epoch: int, metrics: dict, classes: list[dict]) -> dict:
    return {
        "version": 1,
        "arch": meta["model"],
        "model": meta["model"],
        "encoder": meta["encoder"],
        "task": meta["task"],
        "mode": str(meta.get("mode") or ""),
        "base_checkpoint": str(meta.get("base_checkpoint") or ""),
        "num_classes": int(num_classes),
        "class_ids": [int(item.get("id") or index + 1) for index, item in enumerate(classes)],
        "class_names": [str(item.get("name") or "") for item in classes],
        "class_colors": [str(item.get("color") or "") for item in classes],
        "tile_size": int(meta["tile_size"]),
        "epoch": int(epoch),
        "metrics": metrics,
        "state_dict": {key: value.detach().cpu() for key, value in model.state_dict().items()},
    }


def _torch_load_checkpoint(checkpoint_path: Path):
    """优先用 ``weights_only=True`` 安全反序列化；旧 torch / 旧 checkpoint 回退。"""
    import torch  # noqa: PLC0415 - 只有外部训练 Python 才有 torch

    try:
        return torch.load(checkpoint_path, map_location="cpu", weights_only=True)
    except TypeError:
        # torch < 1.13 没有 weights_only 参数
        return torch.load(checkpoint_path, map_location="cpu")
    except Exception as exc:  # noqa: BLE001 —— 旧 checkpoint 可能含非基础类型
        common.log(f"权重无法以安全模式读取（{exc}），回退到完整反序列化；请只使用可信文件")
        return torch.load(checkpoint_path, map_location="cpu", weights_only=False)


def _load_checkpoint(model, path: str, meta: dict, num_classes: int) -> None:
    if not path:
        return
    checkpoint_path = Path(path)
    if not checkpoint_path.is_file():
        raise ValueError(f"继续训练的权重不存在：{checkpoint_path}")
    payload = _torch_load_checkpoint(checkpoint_path)
    if not isinstance(payload, dict) or "state_dict" not in payload:
        raise ValueError(f"不是本工具保存的 checkpoint：{checkpoint_path}")
    expected = {
        "arch": meta["model"],
        "encoder": meta["encoder"],
        "task": meta["task"],
        "num_classes": int(num_classes),
    }
    for key, value in expected.items():
        actual = payload.get(key)
        if actual is not None and str(actual) != str(value):
            raise ValueError(
                f"继续训练的权重与当前配置不一致：{key}={actual}，当前为 {value}"
            )
    try:
        model.load_state_dict(payload["state_dict"], strict=True)
    except RuntimeError as exc:
        raise ValueError(f"权重结构与当前模型不匹配：{exc}") from exc
    common.log(f"已加载继续训练权重：{checkpoint_path}（第 {payload.get('epoch', '?')} 轮）")


def _load_finetune_checkpoint(
    model,
    path: str,
    meta: dict,
    num_classes: int,
    classes: list[dict],
) -> None:
    """微调加载：复用结构与形状兼容的权重，类别相关输出头按需重建。"""
    if not path:
        raise ValueError("微调需要选择基础模型（.pt）")
    checkpoint_path = Path(path)
    if not checkpoint_path.is_file():
        raise ValueError(f"微调的基础模型不存在：{checkpoint_path}")

    payload = _torch_load_checkpoint(checkpoint_path)
    payload_meta: dict = {}
    if isinstance(payload, dict) and isinstance(payload.get("state_dict"), dict):
        payload_meta = payload
        saved_state = payload["state_dict"]
    elif isinstance(payload, dict) and payload and all(
        hasattr(value, "shape") for value in payload.values()
    ):
        # 裸 state_dict（来自其他工具或手工导出），没有元数据可校验
        saved_state = payload
    else:
        raise ValueError(f"不是可识别的 checkpoint：{checkpoint_path}")

    problem = finetune.finetune_meta_problem(payload_meta, meta)
    if problem:
        raise ValueError(problem)

    saved_state = finetune.strip_common_prefix(saved_state)
    saved_names = [str(item) for item in payload_meta.get("class_names") or []]
    current_names = [str(item.get("name") or "") for item in classes]
    saved_num_classes = payload_meta.get("num_classes")
    saved_task = str(payload_meta.get("task") or "")
    try:
        num_classes_changed = (
            saved_num_classes is not None and int(saved_num_classes) != int(num_classes)
        )
    except (TypeError, ValueError):
        num_classes_changed = False
    skip_head = bool(
        num_classes_changed
        or (saved_names and saved_names != current_names)
        or (saved_task and saved_task != str(meta["task"]))
    )

    match = finetune.matching_state_entries(
        saved_state,
        model.state_dict(),
        task=str(meta["task"]),
        model_id=str(meta["model"]),
        skip_head=skip_head,
    )
    if match.shape_mismatch:
        shown = "、".join(match.shape_mismatch[:3])
        more = f" 等 {len(match.shape_mismatch)} 个" if len(match.shape_mismatch) > 3 else ""
        raise ValueError(
            f"基础模型结构与当前模型不匹配（{shown}{more}）；"
            "请确认模型 / 骨干与基础模型一致"
        )
    if not match.loadable:
        raise ValueError(
            "基础模型没有可复用的权重：结构与当前模型完全不一致，"
            "请选择模型 / 骨干相同的 checkpoint"
        )

    result = model.load_state_dict(match.loadable, strict=False)
    unexpected = getattr(result, "unexpected_keys", [])
    if unexpected:
        # matching_state_entries 已保证键都存在；这里只做防御
        raise ValueError(f"基础模型加载失败：{unexpected[:3]}")

    common.log(
        f"微调：已加载 {len(match.loadable)}/{len(saved_state)} 个权重张量"
        f"（跳过输出头 {len(match.head_skipped)} 个、结构不匹配 "
        f"{len(match.shape_mismatch)} 个、当前模型缺失 {len(match.absent)} 个）"
    )
    if skip_head:
        common.log("基础模型的类别 / 类别数 / 任务与当前数据集不同，分类头将重新初始化")
    common.log(f"微调基础模型：{checkpoint_path}（第 {payload_meta.get('epoch', '?')} 轮）")


def _load_initial_weights(
    model, params: dict, meta: dict, num_classes: int, classes: list[dict]
) -> None:
    """按训练方式加载初始权重；从零训练不做任何加载。"""
    mode = str(meta.get("mode") or finetune.resolve_mode(params))
    if mode == "resume":
        _load_checkpoint(model, str(params.get("init_checkpoint") or ""), meta, num_classes)
    elif mode == "finetune":
        _load_finetune_checkpoint(
            model,
            str(params.get("finetune_checkpoint") or ""),
            meta,
            num_classes,
            classes,
        )


def _prepare_freeze(model, params: dict, meta: dict, epochs: int) -> list:
    """微调 + 冻结骨干时应用初始冻结，返回需要逐轮维护的骨干模块。"""
    if not bool(params.get("freeze_backbone")):
        return []
    if str(meta.get("mode") or "") != "finetune":
        common.log("冻结骨干只在微调模式下生效，本次训练忽略")
        return []
    parts = finetune.backbone_parts(model, str(meta["task"]))
    if not parts:
        common.log("当前模型没有可识别的骨干，忽略冻结骨干设置")
        return []
    finetune.set_backbone_frozen(parts, True)
    common.log(f"已冻结骨干：可训练参数 {count_parameters(model) / 1e6:.2f} M")
    try:
        limit = int(params.get("unfreeze_epochs") or 0)
    except (TypeError, ValueError):
        limit = 0
    if limit and limit >= int(epochs):
        common.log(f"前 {limit} 轮冻结，但总轮数为 {epochs}，训练结束前不会解冻")
    return parts


def _apply_epoch_freeze(parts: list, params: dict, epoch: int, state: dict) -> None:
    """``model.train()`` 会递归恢复训练态，每轮都要重新应用冻结。"""
    if not parts:
        return
    frozen = finetune.backbone_should_be_frozen(params, epoch)
    finetune.set_backbone_frozen(parts, frozen)
    if state.get("frozen") != frozen:
        state["frozen"] = frozen
        if frozen:
            common.log("骨干保持冻结（只训练输出头）")
        else:
            common.log(f"第 {epoch} 轮起解冻骨干，全部参数参与训练")


def _validate_training_mode(params: dict, mode: str) -> None:
    """训练方式与权重路径的前置校验（加载数据前执行）。"""
    if mode == "resume":
        path = str(params.get("init_checkpoint") or "").strip()
        if not path:
            raise ValueError("继续训练需要选择上一次训练的权重（best.pt / last.pt）")
        if not Path(path).is_file():
            raise ValueError(f"继续训练的权重不存在：{path}")
        return
    if mode != "finetune":
        return
    path = str(params.get("finetune_checkpoint") or "").strip()
    if not path:
        raise ValueError("微调需要选择基础模型（.pt）")
    if not Path(path).is_file():
        raise ValueError(f"微调的基础模型不存在：{path}")
    if params.get("pretrained"):
        # 基础模型会覆盖 ImageNet 权重；关闭可避免无意义的下载与误导性回退日志
        params["pretrained"] = False
        common.log("微调模式不使用 ImageNet 预训练初始化，直接加载基础模型")


def _write_metrics(
    out_dir: Path,
    meta: dict,
    classes: list[dict],
    epochs: int,
    history: list[dict],
    best_epoch: int,
    best_value: float,
    status: str,
    started: float,
) -> None:
    common.atomic_write_json(
        out_dir / "metrics.json",
        {
            "run": out_dir.name,
            "task": meta["task"],
            "model": meta["model"],
            "encoder": meta["encoder"],
            "mode": str(meta.get("mode") or ""),
            "base_checkpoint": str(meta.get("base_checkpoint") or ""),
            "classes": classes,
            "planned_epochs": int(epochs),
            "completed_epochs": len(history),
            "best_epoch": int(best_epoch),
            "best_value": round(float(best_value), 6),
            "best_metric": history[-1].get("primary_label", "") if history else "",
            "status": status,
            "duration_seconds": round(time.time() - started, 3),
            "history": history,
        },
    )


# ---------------------------------------------------------------------- 入口
def run_training(manifest: dict) -> int:
    """执行一次训练。``manifest`` 由 GUI 写入，字段见训练页 ``_build_manifest``。"""
    import torch

    params = dict(manifest.get("params") or {})
    params.setdefault("epochs", 30)
    params.setdefault("batch_size", 4)
    params.setdefault("lr", 0.0001)
    params.setdefault("tile_size", 512)
    params.setdefault("val_percent", 20)
    params.setdefault("early_stop", 8)
    params.setdefault("seed", 42)
    params.setdefault("device", "auto")
    params.setdefault("pretrained", False)
    params.setdefault("augment", True)
    params.setdefault("loss", "dice_ce")
    params.setdefault("min_instance_area", 16)
    params.setdefault("init_checkpoint", "")
    params.setdefault("amp", True)
    params.setdefault("mode", "")
    params.setdefault("finetune_checkpoint", "")
    params.setdefault("freeze_backbone", False)
    params.setdefault("unfreeze_epochs", 0)

    mode = finetune.resolve_mode(params)
    params["mode"] = mode
    _validate_training_mode(params, mode)
    if mode == "finetune":
        base_checkpoint = str(params.get("finetune_checkpoint") or "")
    elif mode == "resume":
        base_checkpoint = str(params.get("init_checkpoint") or "")
    else:
        base_checkpoint = ""

    task = str(manifest.get("task") or "objects")
    model_id = str(manifest.get("model") or "unet")
    encoder = str(manifest.get("encoder") or "")
    dataset = dict(manifest.get("dataset") or {})
    samples = [dict(item) for item in dataset.get("samples") or [] if isinstance(item, dict)]
    classes = [dict(item) for item in dataset.get("classes") or [] if isinstance(item, dict)]
    out_dir = Path(manifest.get("output_dir") or ".")
    if not samples:
        raise ValueError("训练清单里没有样本")
    out_dir.mkdir(parents=True, exist_ok=True)

    ignore_index: int | None = None
    if task == "landcover":
        # 多分类里 0 = 未标注：全空掩膜没有任何监督信号，先剔除
        samples = common.filter_empty_masks(samples, log_fn=common.log)
        if not samples:
            raise ValueError("训练清单里的掩膜全部为空（未标注），请先完成标注")
        ignore_index = 0

    device = _resolve_device(params["device"])
    _seed_everything(int(params["seed"]))
    torch.backends.cudnn.benchmark = device == "cuda"
    classes, num_classes = common.prepare_classes(task, samples, classes)
    train_indices, val_indices = common.split_indices(
        len(samples), int(params["val_percent"]), int(params["seed"])
    )
    train_samples = [samples[index] for index in train_indices]
    val_samples = [samples[index] for index in val_indices]

    meta = {
        "task": task,
        "model": model_id,
        "encoder": encoder,
        "tile_size": int(params["tile_size"]),
        "mode": mode,
        "base_checkpoint": base_checkpoint,
    }
    context = {
        "params": params,
        "meta": meta,
        "classes": classes,
        "num_classes": num_classes,
        "train_samples": train_samples,
        "val_samples": val_samples,
        "out_dir": out_dir,
        "device": device,
        "epochs": int(params["epochs"]),
        "ignore_index": ignore_index,
    }
    common.emit(
        "started",
        epochs=int(params["epochs"]),
        train=len(train_samples),
        val=len(val_samples),
        classes=classes,
        device=device,
        model=model_id,
        encoder=encoder,
        task=task,
        mode=mode,
        base_checkpoint=base_checkpoint,
    )
    common.log(
        f"训练开始：{common.now_stamp()} / {task} / {model_id} / {encoder} / {device}；"
        f"训练 {len(train_samples)} 个样本，验证 {len(val_samples)} 个"
    )
    if mode == "finetune":
        common.log(f"训练方式：微调（基础模型 {base_checkpoint}）")
        if params.get("freeze_backbone"):
            try:
                limit = int(params.get("unfreeze_epochs") or 0)
            except (TypeError, ValueError):
                limit = 0
            common.log(f"冻结骨干：前 {limit} 轮" if limit else "冻结骨干：全程冻结")
    elif mode == "resume":
        common.log(f"训练方式：继续训练（{base_checkpoint}）")
    else:
        common.log("训练方式：从零训练")
    if ignore_index == 0:
        common.log("类别 0 视为未标注区域：损失与验证指标均忽略这些像素")

    try:
        if task == "instances":
            return _train_instances(context)
        return _train_semantic(context)
    except _StopTrainingError:
        last_path = out_dir / "last.pt"
        completed = 0
        try:
            payload = torch.load(last_path, map_location="cpu")
            completed = int(payload.get("epoch") or 0)
        except Exception:  # noqa: BLE001
            pass
        common.log(f"收到停止请求，训练已停止（已完成 {completed} 轮）")
        common.emit("stopped", last=str(last_path) if last_path.is_file() else "", epoch=completed)
        return 0


__all__ = ["run_training"]
