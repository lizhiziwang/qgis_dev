"""矢量瓦片「批量处理」弹窗：勾选多个图层，每个图层输出一套独立瓦片。"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (
    QCheckBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
)

from app.ui.common.base_dialog import BaseDialog
from app.ui.common.widgets import open_file_dir

#: Windows / Linux 文件名非法字符
_INVALID_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]+')
_WHITESPACE = re.compile(r"\s+")
_MAX_NAME_LEN = 60


def safe_subdir_name(name: str, existing: set[str] | None = None) -> str:
    """把图层名转换成安全、唯一的子目录名（大小写不敏感去重）。"""
    existing = existing if existing is not None else set()
    text = _INVALID_CHARS.sub("_", str(name or ""))
    text = _WHITESPACE.sub("_", text).strip(" ._")
    if not text:
        text = "layer"
    text = text[:_MAX_NAME_LEN]
    candidate = text
    index = 2
    while candidate.casefold() in existing:
        candidate = f"{text}_{index}"
        index += 1
    existing.add(candidate.casefold())
    return candidate


@dataclass(frozen=True)
class VectorTileBatchTarget:
    """批量处理中的一个图层 → 一个输出子目录。"""

    layer: object
    subdir: str


class VectorTileBatchDialog(BaseDialog):
    """选择批量图层与输出根目录，确定后由主对话框顺序执行。"""

    def __init__(
        self,
        parent,
        layers,
        *,
        preselected=None,
        output_root: str = "",
    ) -> None:
        super().__init__(
            parent, title="矢量瓦片批量处理", show_min_button=False, show_max_button=False
        )
        self.setMinimumSize(0, 0)
        self.resize(680, 520)
        self._layers = list(layers)
        self._preselected = {layer.id() for layer in (preselected or []) if layer is not None}

        layout = QVBoxLayout()
        hint = QLabel(
            "勾选要批量生成的矢量图层。每个图层会输出到「输出根目录/子目录」下的一套"
            "独立瓦片，缩放级别、模板与高级选项沿用主窗口设置；执行时按顺序逐个生成。"
        )
        hint.setWordWrap(True)
        layout.addWidget(hint)
        layout.addSpacing(6)

        layout.addWidget(QLabel("输出根目录"))
        root_row = QHBoxLayout()
        self.output_edit = QLineEdit(output_root or "")
        self.output_edit.setPlaceholderText("选择批量输出的根目录")
        self.output_edit.setReadOnly(True)
        root_row.addWidget(self.output_edit, 1)
        choose = QPushButton("选择")
        choose.clicked.connect(self._choose_root)
        root_row.addWidget(choose)
        layout.addLayout(root_row)
        layout.addSpacing(6)

        self.use_layer_extent_check = QCheckBox("每个图层使用自己的范围（推荐）")
        self.use_layer_extent_check.setChecked(True)
        self.use_layer_extent_check.setToolTip(
            "取消勾选时，所有图层共用主窗口「输出范围」设置。"
        )
        layout.addWidget(self.use_layer_extent_check)
        layout.addSpacing(6)

        layout.addWidget(QLabel("要处理的图层"))
        self.list = QListWidget()
        used: set[str] = set()
        for layer in self._layers:
            subdir = safe_subdir_name(layer.name(), used)
            item = QListWidgetItem(f"{layer.name()}  →  {subdir}")
            item.setData(Qt.ItemDataRole.UserRole, layer)
            item.setData(Qt.ItemDataRole.UserRole + 1, subdir)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(
                Qt.CheckState.Checked
                if layer.id() in self._preselected
                else Qt.CheckState.Unchecked
            )
            item.setToolTip(f"输出到：{os.path.join(self.output_edit.text(), subdir)}")
            self.list.addItem(item)
        if self._layers and not self._preselected:
            # 没有预选图层时默认全选，符合「批量处理」的直觉
            for row in range(self.list.count()):
                self.list.item(row).setCheckState(Qt.CheckState.Checked)
        layout.addWidget(self.list, 1)

        actions = QHBoxLayout()
        all_btn = QPushButton("全选")
        all_btn.clicked.connect(lambda: self._set_all(Qt.CheckState.Checked))
        none_btn = QPushButton("全不选")
        none_btn.clicked.connect(lambda: self._set_all(Qt.CheckState.Unchecked))
        actions.addWidget(all_btn)
        actions.addWidget(none_btn)
        actions.addStretch(1)
        cancel = QPushButton("取消")
        cancel.clicked.connect(self.reject)
        actions.addWidget(cancel)
        ok = QPushButton("开始处理")
        ok.setDefault(True)
        ok.clicked.connect(self._validate)
        actions.addWidget(ok)
        layout.addLayout(actions)

        self.setLayout(layout)

    # ------------------------------------------------------------ 交互

    def _choose_root(self) -> None:
        path = open_file_dir(self, is_dir=True, title="选择批量输出根目录")
        if path:
            self.output_edit.setText(path)
            for row in range(self.list.count()):
                item = self.list.item(row)
                subdir = str(item.data(Qt.ItemDataRole.UserRole + 1) or "")
                item.setToolTip(f"输出到：{os.path.join(path, subdir)}")

    def _set_all(self, state: Qt.CheckState) -> None:
        for row in range(self.list.count()):
            self.list.item(row).setCheckState(state)

    def output_root(self) -> str:
        return self.output_edit.text().strip()

    def use_layer_extent(self) -> bool:
        return self.use_layer_extent_check.isChecked()

    def selected_targets(self) -> list[VectorTileBatchTarget]:
        """勾选的图层及其输出子目录（保持列表顺序）。"""
        targets: list[VectorTileBatchTarget] = []
        for row in range(self.list.count()):
            item = self.list.item(row)
            if item.checkState() != Qt.CheckState.Checked:
                continue
            layer = item.data(Qt.ItemDataRole.UserRole)
            subdir = str(item.data(Qt.ItemDataRole.UserRole + 1) or "")
            if layer is not None and subdir:
                targets.append(VectorTileBatchTarget(layer, subdir))
        return targets

    def _validate(self) -> None:
        if not self.output_root():
            QMessageBox.warning(self, "矢量瓦片批量处理", "请先选择输出根目录")
            return
        if not self.selected_targets():
            QMessageBox.warning(self, "矢量瓦片批量处理", "请至少勾选一个图层")
            return
        self.accept()


__all__ = [
    "VectorTileBatchDialog",
    "VectorTileBatchTarget",
    "safe_subdir_name",
]
