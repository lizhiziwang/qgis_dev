"""标注数据模型：标签集、灰度 id 掩膜、撤销栈、PNG/JSON 读写。

约定（与 OBIA ``classified_map.tif`` 一致）：

- 内存掩膜是 ``Format_Grayscale8``，像素值 0 = 未标注，1..255 = 标签内部 id；
- 显示层是 ``Format_Indexed8``，索引 0 透明，1..N 取标签颜色；
- 保存到磁盘时按标签列表顺序把内部 id 重映射为连续的 1..N，内存与撤销栈不变；
- 大图支持按工作分辨率降采样，缩放比例由窗口写入 JSON（``scale_x/scale_y``）。

本模块只依赖 QtGui + numpy，不依赖 QWidget，便于单测。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from qgis.PyQt.QtCore import QBuffer, QByteArray, QIODevice, QObject, QPointF, QRect, Qt, pyqtSignal
from qgis.PyQt.QtGui import QColor, QImage, QPainter, QPainterPath, QPen

try:  # Qt6：QUndoStack 在 QtGui；Qt5：在 QtWidgets
    from qgis.PyQt.QtGui import QUndoCommand, QUndoStack
except ImportError:  # pragma: no cover - 仅 Qt5 环境
    from qgis.PyQt.QtWidgets import QUndoCommand, QUndoStack

from app.infra.deep_learning import DEFAULT_PALETTE
from app.infra.logging import get_logger

logger = get_logger(__name__)

#: 标签 id 上限（灰度图 8 位，0 留给未标注）
MAX_LABELS = 255

#: 超过该像素数的撤销补丁压成 PNG 字节，避免撤销栈爆内存
_LARGE_PATCH_PX = 1_000_000

#: mask 版本号（写进 JSON）
MASK_VERSION = 1


@dataclass
class LabelClass:
    """一个标签类别。``id`` 在会话内稳定，删除后不回收。"""

    id: int
    name: str
    color: str

    def as_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "color": self.color}


class LabelSet(QObject):
    """标签集合（所有影像共享一份）。

    信号 ``changed`` 在新增 / 删除 / 改名 / 改色后发出；``AnnotationDocument``
    用它刷新颜色表。
    """

    changed = pyqtSignal()

    def __init__(self, palette: tuple[str, ...] = DEFAULT_PALETTE) -> None:
        super().__init__()
        #: 调色板（新增标签时按顺序取未用色）
        self._palette = tuple(palette) or DEFAULT_PALETTE
        self._labels: list[LabelClass] = []
        self._next_id = 1

    # ------------------------------------------------------------ 查询

    @property
    def labels(self) -> tuple[LabelClass, ...]:
        return tuple(self._labels)

    def __len__(self) -> int:
        return len(self._labels)

    def get(self, label_id: int) -> LabelClass | None:
        for label in self._labels:
            if label.id == label_id:
                return label
        return None

    def by_name(self, name: str) -> LabelClass | None:
        for label in self._labels:
            if label.name == name:
                return label
        return None

    def ids(self) -> list[int]:
        return [label.id for label in self._labels]

    # ------------------------------------------------------------ 修改

    def add(self, name: str, color: str | None = None) -> LabelClass:
        """新增标签；名称重复 / 达到上限时抛 ``ValueError``。"""
        name = (name or "").strip()
        if not name:
            raise ValueError("标签名不能为空")
        if self.by_name(name) is not None:
            raise ValueError(f"标签已存在：{name}")
        if len(self._labels) >= MAX_LABELS:
            raise ValueError(f"标签数量上限为 {MAX_LABELS}")
        label = LabelClass(self._next_id, name, color or self._next_color())
        self._next_id += 1
        self._labels.append(label)
        self.changed.emit()
        return label

    def remove(self, label_id: int) -> bool:
        """删除标签（不从掩膜里清像素，由调用方决定）。"""
        label = self.get(label_id)
        if label is None:
            return False
        self._labels.remove(label)
        self.changed.emit()
        return True

    def rename(self, label_id: int, name: str) -> bool:
        name = (name or "").strip()
        label = self.get(label_id)
        if label is None or not name or name == label.name:
            return False
        other = self.by_name(name)
        if other is not None and other.id != label_id:
            raise ValueError(f"标签已存在：{name}")
        label.name = name
        self.changed.emit()
        return True

    def set_color(self, label_id: int, color: str) -> bool:
        label = self.get(label_id)
        if label is None:
            return False
        label.color = str(color)
        self.changed.emit()
        return True

    def clear(self) -> None:
        self._labels.clear()
        self.changed.emit()

    def replace_from_dicts(self, items: list[dict]) -> None:
        """用 JSON 里的标签列表整体替换（加载标注时用）。"""
        self._labels = []
        self._next_id = 1
        for item in items:
            try:
                label = LabelClass(
                    int(item["id"]),
                    str(item["name"]),
                    str(item.get("color") or self._next_color()),
                )
            except (KeyError, TypeError, ValueError):
                continue
            self._labels.append(label)
            self._next_id = max(self._next_id, label.id + 1)
        self.changed.emit()

    def _next_color(self) -> str:
        used = {label.color.lower() for label in self._labels}
        for color in self._palette:
            if color.lower() not in used:
                return color
        return self._palette[len(self._labels) % len(self._palette)]


def _image_array(image: QImage, width: int, height: int) -> np.ndarray:
    """把 QImage 的像素读成 ``(h, w)`` uint8 视图（忽略 bytesPerLine 尾部 padding）。"""
    buffer = image.constBits()
    buffer.setsize(image.sizeInBytes())
    stride = image.bytesPerLine()
    return np.frombuffer(buffer, np.uint8).reshape(height, stride)[:, :width]


def _writable_array(image: QImage, width: int, height: int) -> np.ndarray:
    """QImage 的可写 numpy 视图（直接改图，不 copy）。"""
    buffer = image.bits()
    buffer.setsize(image.sizeInBytes())
    stride = image.bytesPerLine()
    return np.frombuffer(buffer, np.uint8).reshape(height, stride)[:, :width]


def mask_ids_from_image(image: QImage) -> np.ndarray:
    """从灰度 / Indexed8 掩膜图取 id 数组；其它格式拒绝（颜色→id 不可逆）。"""
    if image.isNull():
        raise ValueError("掩膜图为空")
    if image.format() not in (QImage.Format.Format_Grayscale8, QImage.Format.Format_Indexed8):
        raise ValueError("掩膜必须是 8 位灰度或 Indexed8 格式，不能用 RGB 图当掩膜")
    if image.format() == QImage.Format.Format_Indexed8:
        return _image_array(image, image.width(), image.height()).copy()
    converted = image.convertToFormat(QImage.Format.Format_Grayscale8)
    return _image_array(converted, converted.width(), converted.height()).copy()


def _encode_patch(image: QImage) -> QImage | bytes:
    """撤销补丁：大块压 PNG 字节，小块直接存 QImage。"""
    if image.width() * image.height() <= _LARGE_PATCH_PX:
        return image.copy()
    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    image.save(buffer, "PNG")
    buffer.close()
    return bytes(data)


def _decode_patch(patch: QImage | bytes) -> QImage:
    if isinstance(patch, QImage):
        return patch
    image = QImage()
    image.loadFromData(patch, "PNG")
    return image


class _MaskPatchCommand(QUndoCommand):
    """把 ``rect`` 区域的掩膜恢复成 before（undo）或 after（redo）。"""

    def __init__(self, document: AnnotationDocument, rect: QRect, before, after, text: str) -> None:
        super().__init__(text)
        self._document = document
        self._rect = QRect(rect)
        self._before = before
        self._after = after

    def undo(self) -> None:
        self._document._restore_patch(self._rect, self._before)

    def redo(self) -> None:
        self._document._restore_patch(self._rect, self._after)


class AnnotationDocument(QObject):
    """单张影像的标注文档：底图 + 灰度 id 掩膜 + 标签集 + 撤销栈。"""

    #: 掩膜变更区域（图像坐标；空 QRect 表示整图 / 颜色表变化）
    changed = pyqtSignal(QRect)
    #: dirty 状态变化（掩膜或标签元数据）
    dirty_changed = pyqtSignal(bool)
    #: 撤销栈变化（用于同步撤销/重做按钮）
    undo_state_changed = pyqtSignal()

    def __init__(
        self,
        image: QImage,
        *,
        source_path: str = "",
        labels: LabelSet | None = None,
        undo_limit: int = 40,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        if image.isNull():
            raise ValueError("底图为空")
        self.image = image  # 工作分辨率预览图
        self.source_path = source_path
        self.labels = labels if labels is not None else LabelSet()
        self.labels_dirty = False
        self._loading = False

        width, height = image.width(), image.height()
        self.mask = QImage(width, height, QImage.Format.Format_Grayscale8)
        self.mask.fill(0)
        self.display = QImage(width, height, QImage.Format.Format_Indexed8)
        self.display.fill(0)
        self._apply_color_table()
        self._sync_display(QRect(0, 0, width, height))

        self.undo_stack = QUndoStack(self)
        self.undo_stack.setUndoLimit(max(1, int(undo_limit)))
        self.undo_stack.cleanChanged.connect(self._on_clean_changed)
        self.undo_stack.canUndoChanged.connect(self._on_undo_available)
        self.undo_stack.canRedoChanged.connect(self._on_undo_available)
        self.labels.changed.connect(self._on_labels_changed)

        #: 笔画进行中的快照（None 表示当前没有笔画）
        self._stroke_before: QImage | None = None
        self._stroke_rect = QRect()

    # ------------------------------------------------------------ 基本属性

    @property
    def width(self) -> int:
        return self.mask.width()

    @property
    def height(self) -> int:
        return self.mask.height()

    @property
    def is_dirty(self) -> bool:
        return self.labels_dirty or not self.undo_stack.isClean()

    def set_clean(self) -> None:
        self.undo_stack.setClean()
        self.labels_dirty = False
        self.dirty_changed.emit(False)

    def _mark_labels_dirty(self) -> None:
        if self._loading:
            return
        if not self.labels_dirty:
            self.labels_dirty = True
            self.dirty_changed.emit(True)

    # ------------------------------------------------------------ 显示

    def _on_clean_changed(self, _clean: bool) -> None:
        self.dirty_changed.emit(self.is_dirty)

    def _on_undo_available(self, _available: bool) -> None:
        self.undo_state_changed.emit()

    def _apply_color_table(self) -> None:
        """按标签颜色重建 Indexed8 颜色表（索引 0 透明）。

        颜色表按 **id 直接索引**（而不是按标签顺序），否则删除中间标签后
        id 出现空洞，显示图会取到错误颜色甚至越界索引。空洞填透明。
        """
        max_id = max((label.id for label in self.labels.labels), default=0)
        colors = [0] * (max_id + 1)
        for label in self.labels.labels:
            if 0 < label.id <= 255:
                colors[label.id] = QColor(label.color).rgba()
        self.display.setColorTable(colors)

    def _on_labels_changed(self) -> None:
        self._apply_color_table()
        self._sync_display(QRect(0, 0, self.width, self.height))
        self._mark_labels_dirty()
        self.changed.emit(QRect(0, 0, self.width, self.height))

    def _sync_display(self, rect: QRect) -> None:
        rect = rect.intersected(QRect(0, 0, self.width, self.height))
        if rect.isEmpty():
            return
        m = _writable_array(self.mask, self.width, self.height)
        d = _writable_array(self.display, self.width, self.height)
        d[rect.y() : rect.y() + rect.height(), rect.x() : rect.x() + rect.width()] = m[
            rect.y() : rect.y() + rect.height(), rect.x() : rect.x() + rect.width()
        ]

    # ------------------------------------------------------------ 绘制

    def begin_stroke(self) -> None:
        """开始一笔（画笔 / 橡皮擦）：记录整图快照，松开时只存脏矩形补丁。"""
        if self._stroke_before is not None:
            self.finish_stroke()
        self._stroke_before = self.mask.copy()
        self._stroke_rect = QRect()

    def stroke_segment(
        self,
        start: QPointF,
        end: QPointF,
        radius: int,
        *,
        class_id: int = 0,
        erase: bool = False,
    ) -> None:
        """画一段圆头线段（``radius`` 为图像像素半径）；整笔由 begin/finish 提交。"""
        if self._stroke_before is None:
            self.begin_stroke()
        radius = max(1, int(radius))
        value = 0 if erase else int(class_id)
        if value < 0 or value > 255:
            return
        dirty = QRect(
            int(min(start.x(), end.x())) - radius - 2,
            int(min(start.y(), end.y())) - radius - 2,
            abs(int(end.x() - start.x())) + radius * 2 + 4,
            abs(int(end.y() - start.y())) + radius * 2 + 4,
        ).intersected(QRect(0, 0, self.width, self.height))
        if dirty.isEmpty():
            return

        painter = QPainter(self.mask)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        if erase:
            painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Source)
        gray = QColor(value, value, value)
        pen = QPen(gray)
        pen.setWidth(radius * 2)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        if abs(end.x() - start.x()) < 1e-6 and abs(end.y() - start.y()) < 1e-6:
            # 单击（起点=终点）：drawLine 零长度线段在 Qt6 下不落墨，改画圆点
            painter.drawPoint(start)
        else:
            painter.drawLine(start, end)
        painter.end()

        self._stroke_rect = self._stroke_rect.united(dirty) if not self._stroke_rect.isNull() else dirty
        self._sync_display(dirty)
        self.changed.emit(dirty)

    def finish_stroke(self, text: str = "画笔") -> None:
        """结束一笔：把 union 脏矩形的前后快照压入撤销栈。"""
        before_full = self._stroke_before
        self._stroke_before = None
        if before_full is None:
            return
        rect = self._stroke_rect.intersected(QRect(0, 0, self.width, self.height))
        self._stroke_rect = QRect()
        if rect.isEmpty():
            return
        before = before_full.copy(rect)
        after = self.mask.copy(rect)
        if _arrays_equal(before, after):
            return
        self.undo_stack.push(_MaskPatchCommand(self, rect, _encode_patch(before), _encode_patch(after), text))

    def fill_polygon(self, points: list[QPointF], class_id: int, text: str = "多边形标注") -> bool:
        """用当前标签填充一个多边形（至少 3 个点）。"""
        if len(points) < 3 or not (0 <= int(class_id) <= 255):
            return False
        value = int(class_id)
        xs = [p.x() for p in points]
        ys = [p.y() for p in points]
        rect = QRect(
            int(min(xs)), int(min(ys)), int(max(xs) - min(xs)) + 2, int(max(ys) - min(ys)) + 2
        ).intersected(QRect(0, 0, self.width, self.height))
        if rect.isEmpty():
            return False
        before = self.mask.copy(rect)

        path = QPainterPath()
        path.addPolygon(_to_polygon(points))
        path.closeSubpath()
        painter = QPainter(self.mask)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(value, value, value))
        painter.drawPath(path)
        painter.end()

        after = self.mask.copy(rect)
        if _arrays_equal(before, after):
            return False
        self.undo_stack.push(_MaskPatchCommand(self, rect, _encode_patch(before), _encode_patch(after), text))
        self._sync_display(rect)
        self.changed.emit(rect)
        return True

    def apply_binary_mask(
        self,
        candidate: QImage,
        class_id: int,
        *,
        only_unlabeled: bool = False,
        text: str = "应用候选掩膜",
    ) -> bool:
        """把 SAM 候选（非 0 像素）用当前标签色写入掩膜。"""
        if candidate.isNull() or not (0 <= int(class_id) <= 255):
            return False
        if candidate.size() != self.mask.size():
            candidate = candidate.scaled(
                self.width,
                self.height,
                Qt.AspectRatioMode.IgnoreAspectRatio,
                Qt.TransformationMode.FastTransformation,
            )
        cand = mask_ids_from_image(candidate)
        mask = _writable_array(self.mask, self.width, self.height)
        region = cand > 0
        if only_unlabeled:
            region &= mask == 0
        if not region.any():
            return False
        ys, xs = np.nonzero(region)
        rect = QRect(
            int(xs.min()), int(ys.min()), int(xs.max() - xs.min()) + 1, int(ys.max() - ys.min()) + 1
        )
        before = self.mask.copy(rect)
        mask[region] = int(class_id)
        after = self.mask.copy(rect)
        self.undo_stack.push(_MaskPatchCommand(self, rect, _encode_patch(before), _encode_patch(after), text))
        self._sync_display(rect)
        self.changed.emit(rect)
        return True

    def apply_candidate_files(
        self,
        paths: list[str],
        class_id: int,
        *,
        only_unlabeled: bool = False,
        text: str = "批量应用候选",
    ) -> int:
        """批量应用候选 PNG，合并成一条撤销命令。返回成功应用的张数。"""
        images = []
        for path in paths:
            image = QImage(path)
            if not image.isNull():
                images.append(image)
        if not images:
            return 0
        before = self.mask.copy()
        count = 0
        for image in images:
            if image.size() != self.mask.size():
                image = image.scaled(
                    self.width,
                    self.height,
                    Qt.AspectRatioMode.IgnoreAspectRatio,
                    Qt.TransformationMode.FastTransformation,
                )
            cand = mask_ids_from_image(image)
            mask = _writable_array(self.mask, self.width, self.height)
            region = cand > 0
            if only_unlabeled:
                region &= mask == 0
            if region.any():
                mask[region] = int(class_id)
                count += 1
        if count == 0:
            return 0
        after = self.mask.copy()
        if not _arrays_equal(before, after):
            rect = QRect(0, 0, self.width, self.height)
            self.undo_stack.push(
                _MaskPatchCommand(self, rect, _encode_patch(before), _encode_patch(after), text)
            )
            self._sync_display(rect)
            self.changed.emit(rect)
        return count

    def clear_class(self, class_id: int) -> bool:
        """把某个标签的所有像素清为未标注（删除标签时用）。"""
        mask = _writable_array(self.mask, self.width, self.height)
        region = mask == int(class_id)
        if not region.any():
            return False
        ys, xs = np.nonzero(region)
        rect = QRect(
            int(xs.min()), int(ys.min()), int(xs.max() - xs.min()) + 1, int(ys.max() - ys.min()) + 1
        )
        before = self.mask.copy(rect)
        mask[region] = 0
        after = self.mask.copy(rect)
        self.undo_stack.push(
            _MaskPatchCommand(self, rect, _encode_patch(before), _encode_patch(after), "清空标签")
        )
        self._sync_display(rect)
        self.changed.emit(rect)
        return True

    def clear_all(self) -> bool:
        """清空整张掩膜（一条撤销命令）。"""
        before = self.mask.copy()
        self.mask.fill(0)
        after = self.mask.copy()
        if _arrays_equal(before, after):
            return False
        rect = QRect(0, 0, self.width, self.height)
        self.undo_stack.push(
            _MaskPatchCommand(self, rect, _encode_patch(before), _encode_patch(after), "清空全部")
        )
        self._sync_display(rect)
        self.changed.emit(rect)
        return True

    def undo(self) -> None:
        self.undo_stack.undo()

    def redo(self) -> None:
        self.undo_stack.redo()

    def _restore_patch(self, rect: QRect, patch) -> None:
        image = _decode_patch(patch)
        painter = QPainter(self.mask)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Source)
        painter.drawImage(rect.topLeft(), image, image.rect())
        painter.end()
        self._sync_display(rect)
        self.changed.emit(rect)

    # ------------------------------------------------------------ 保存 / 加载

    def label_id_array(self) -> np.ndarray:
        """按标签顺序重映射为 1..N 的 id 数组（保存用）。"""
        mask = _image_array(self.mask, self.width, self.height)
        lut = np.zeros(256, np.uint8)
        for new_id, label in enumerate(self.labels.labels, start=1):
            if 0 <= label.id <= 255:
                lut[label.id] = min(new_id, 255)
        return lut[mask].copy()

    def used_label_ids(self) -> set[int]:
        mask = _image_array(self.mask, self.width, self.height)
        return {int(v) for v in np.unique(mask) if v != 0}

    def save(
        self,
        mask_path: str | Path,
        *,
        json_path: str | Path | None = None,
        source_size: tuple[int, int] | None = None,
        extra: dict | None = None,
    ) -> tuple[Path, Path]:
        """写 ``_mask.png`` + ``_mask.json``（原子替换），返回两个路径。"""
        mask_path = Path(mask_path)
        json_path = Path(json_path) if json_path else mask_path.with_suffix(".json")
        mask_path.parent.mkdir(parents=True, exist_ok=True)
        json_path.parent.mkdir(parents=True, exist_ok=True)

        ids = np.ascontiguousarray(self.label_id_array())
        image = QImage(ids.data, self.width, self.height, int(ids.strides[0]), QImage.Format.Format_Grayscale8)

        if source_size is None:
            source_size = (self.width, self.height)
        src_w, src_h = int(source_size[0]), int(source_size[1])
        scale_x = src_w / self.width if self.width else 1.0
        scale_y = src_h / self.height if self.height else 1.0

        # 源图不大时把掩膜最近邻升采样回源尺寸，方便直接进训练脚本
        if src_w > 0 and src_h > 0 and src_w * src_h <= 100_000_000 and (src_w, src_h) != (self.width, self.height):
            image = image.scaled(
                src_w,
                src_h,
                Qt.AspectRatioMode.IgnoreAspectRatio,
                Qt.TransformationMode.FastTransformation,
            )

        tmp_mask = mask_path.with_name(mask_path.name + ".tmp")
        if not image.save(str(tmp_mask), "PNG"):
            raise OSError(f"写入掩膜失败：{mask_path}")
        os.replace(tmp_mask, mask_path)

        labels = []
        for new_id, label in enumerate(self.labels.labels, start=1):
            labels.append(
                {
                    "id": new_id,
                    "name": label.name,
                    "color": label.color,
                    "source_id": label.id,
                }
            )
        payload = {
            "version": MASK_VERSION,
            "image": str(self.source_path),
            "mask": str(mask_path),
            "image_width": src_w,
            "image_height": src_h,
            "mask_width": self.width,
            "mask_height": self.height,
            "scale_x": scale_x,
            "scale_y": scale_y,
            "labels": labels,
            "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        if extra:
            payload.update(extra)
        tmp_json = json_path.with_name(json_path.name + ".tmp")
        tmp_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp_json, json_path)
        return mask_path, json_path

    def load_mask(self, image: QImage, id_map: dict[int, int] | None = None) -> None:
        """把灰度 / Indexed8 掩膜载入本图（尺寸不一致时最近邻缩放）。

        ``id_map`` 把磁盘标签 id 映射到当前会话的内部 id（重开已有成果时使用）。
        """
        ids = mask_ids_from_image(image)
        if id_map:
            lut = np.arange(256, dtype=np.uint8)
            for source_id, target_id in id_map.items():
                if 0 <= source_id <= 255:
                    lut[source_id] = max(0, min(255, int(target_id)))
            ids = lut[ids]
        if ids.shape != (self.height, self.width):
            resized = QImage(ids.data, ids.shape[1], ids.shape[0], int(ids.strides[0]), QImage.Format.Format_Grayscale8)
            resized = resized.scaled(
                self.width,
                self.height,
                Qt.AspectRatioMode.IgnoreAspectRatio,
                Qt.TransformationMode.FastTransformation,
            )
            ids = mask_ids_from_image(resized)
        mask = _writable_array(self.mask, self.width, self.height)
        mask[:, :] = ids
        self._sync_display(QRect(0, 0, self.width, self.height))
        self.changed.emit(QRect(0, 0, self.width, self.height))


def _arrays_equal(left: QImage, right: QImage) -> bool:
    """两幅同尺寸 8bpp 图内容是否一致。"""
    if left.size() != right.size():
        return False
    a = _image_array(left, left.width(), left.height())
    b = _image_array(right, right.width(), right.height())
    return bool(np.array_equal(a, b))


def _to_polygon(points: list[QPointF]):
    from qgis.PyQt.QtGui import QPolygonF

    return QPolygonF(points)
