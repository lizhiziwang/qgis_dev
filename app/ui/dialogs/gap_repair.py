"""「补缝」Dialog：为每处检出的封闭缝隙指定并入的目标多边形。

一行一处缝隙：序号 / 缺口面积（**平方米**）/ 目标面下拉。下拉首项固定为
「不补（跳过）」，其余只列**参与围成该缝隙**的要素（非参与面不能作目标）。
在任一行选定目标面时：

- 地图上**高亮闪烁**该要素（闪几下后保持高亮），便于确认选的是哪一块；
- 下方「目标要素属性」表同步加载该要素的全部字段值。

点「确定」时若一处都没分配，提示后**不关闭**——避免静默无操作。取值走
:meth:`GapRepairDialog.assignments`。

只做展示与收集，不做几何与图层写入（那是
:mod:`app.ui.tools.gap_repair` 的事）。主题由全局 QSS 下发，本类不写 setStyleSheet。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Optional

from qgis.core import QgsVectorLayer
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (
    QComboBox,
    QDialogButtonBox,
    QHeaderView,
    QLabel,
    QMessageBox,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.ui.common.base_dialog import BaseDialog
from app.ui.common.feature_highlight import FeatureHighlighter
from app.ui.common.widgets import make_table

#: 下拉首项：不处理该缝隙（``currentData()`` 为 None）
_SKIP_TEXT = "不补（跳过）"


#: 视为「无值」的文本：真 NULL 之外，数据里常把空值写成字面量字符串
_NULL_TOKENS = {"", "null", "none", "nil", "nil", "<null>", "undefined"}


def _usable_value(value) -> Optional[str]:
    """把字段值整理成可展示的文本；空 / 字面量 ``NULL`` 一律当无值返回 None。"""
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() in _NULL_TOKENS:
        return None
    return text


def _feature_label(layer: QgsVectorLayer, fid: int) -> str:
    """要素在下拉里的显示名。

    - **只认图层自己配置的显示字段**（``displayField()``）。不再拿「主键 → 第 0
      个字段」兜底：shapefile 没有主键属性，兜底会落到第一个字段（实测是
      ``group_ulid``），把里面存着的**字面量字符串 ``'NULL'``** 原样显示出来——
      既没有信息量，又会让多个不同要素显示成同一个 ``NULL`` 无法区分。
    - 字面量 ``NULL`` / 空白与真 NULL 一样按「无值」处理。
    - 显示字段取到值时**一律带上 fid**，避免两个要素显示名相同时无从分辨。
    """
    try:
        field = layer.displayField()
        idx = layer.fields().indexOf(field) if field else -1
        if idx >= 0:
            feature = layer.getFeature(fid)
            if feature.isValid():
                value = _usable_value(feature.attributes()[idx])
                if value:
                    return f"{value}（{fid}）"
    except Exception:  # noqa: BLE001 —— 显示名取不到不该阻断补缝流程
        pass
    return f"要素 {fid}"


class GapRepairDialog(BaseDialog):
    """列出全部检出缝隙，逐条指定并入的目标面。"""

    def __init__(
        self,
        layer: QgsVectorLayer,
        targets: Sequence,
        canvas=None,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent, title="补缝")
        # 缩小 BaseDialog 默认 800×600 的最小尺寸到表格量级
        self.setMinimumSize(0, 0)
        self.resize(760, 660)
        self.setModal(True)
        self._layer = layer
        self._targets = list(targets)
        # 选中目标面时在画布上闪烁定位（见 app.ui.common.feature_highlight）
        self._highlighter = FeatureHighlighter(canvas, layer, parent=self)
        self._build_ui()

    # ============================================================== UI

    def _build_ui(self) -> None:
        layout = QVBoxLayout()
        layout.setContentsMargins(18, 16, 18, 14)
        layout.setSpacing(6)

        head = QLabel(
            f"检测到 {len(self._targets)} 处封闭缝隙（面积单位：平方米）。"
            f"为每处选择并入的目标多边形；保持「{_SKIP_TEXT}」则不处理。"
            "选择目标后地图上会高亮闪烁该要素。"
        )
        head.setWordWrap(True)
        layout.addWidget(head)

        self.table = make_table(
            ["序号", "缺口面积(㎡)", "合并到（目标多边形）"], parent=self
        )
        self.table.setColumnWidth(0, 60)
        self.table.setColumnWidth(1, 130)
        self.table.setRowCount(len(self._targets))
        for row, item in enumerate(self._targets):
            gap = item.gap
            num_item = QTableWidgetItem(str(gap.index + 1))
            # 行 → 缺口序号：用户可能改行序，序号不从 row 反推
            num_item.setData(Qt.ItemDataRole.UserRole, gap.index)
            self.table.setItem(row, 0, num_item)
            self.table.setItem(row, 1, QTableWidgetItem(f"{gap.area:,.2f}"))

            combo = QComboBox(self.table)
            combo.addItem(_SKIP_TEXT, None)
            for fid in item.target_fids:
                combo.addItem(_feature_label(self._layer, fid), fid)
            combo.currentIndexChanged.connect(
                lambda _index, c=combo: self._on_target_changed(c)
            )
            self.table.setCellWidget(row, 2, combo)

        # 缝隙列表给一个克制的高度（多了就滚动），把纵向空间尽量让给属性表
        self.table.setMaximumHeight(200)
        layout.addWidget(self.table, 0)

        # ---- 选中目标面的属性（占满剩余高度）----
        self.attr_head = QLabel("目标要素属性：未选择")
        layout.addWidget(self.attr_head)
        self.attr_table = make_table(["字段", "值"], parent=self)
        self.attr_table.verticalHeader().setVisible(False)
        self.attr_table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.ResizeToContents
        )
        layout.addWidget(self.attr_table, 1)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
            Qt.Orientation.Horizontal,
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("确定")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.button(QDialogButtonBox.StandardButton.Ok).setDefault(True)
        buttons.accepted.connect(self._on_ok)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.setLayout(layout)
        self.table.cellDoubleClicked.connect(lambda *_: self._on_ok())

    # ============================================================== 选中反馈

    def _on_target_changed(self, combo: QComboBox) -> None:
        """下拉换目标面 → 地图高亮闪烁 + 下方加载其属性。"""
        fid = combo.currentData()
        if fid is None:
            self._highlighter.clear()
            self._load_attributes(None)
            return
        self._highlighter.flash(int(fid))
        self._load_attributes(int(fid))

    def _load_attributes(self, fid: Optional[int]) -> None:
        """把 ``fid`` 的全部字段值填进属性表；``None`` 清空。"""
        self.attr_table.setRowCount(0)
        if fid is None:
            self.attr_head.setText("目标要素属性：未选择")
            return
        self.attr_head.setText(f"目标要素属性：{_feature_label(self._layer, fid)}")
        try:
            feature = self._layer.getFeature(fid)
            if not feature.isValid():
                return
            fields = list(self._layer.fields())
            attrs = feature.attributes()
            self.attr_table.setRowCount(len(fields))
            for row, field in enumerate(fields):
                name_item = QTableWidgetItem(field.name())
                name_item.setData(Qt.ItemDataRole.UserRole, field.name())
                self.attr_table.setItem(row, 0, name_item)
                value = attrs[row] if row < len(attrs) else None
                self.attr_table.setItem(
                    row, 1, QTableWidgetItem("NULL" if value is None else str(value))
                )
        except Exception:  # noqa: BLE001 —— 属性展示失败不该阻断补缝流程
            self.attr_table.setRowCount(0)

    # ============================================================== 取值

    def assignments(self) -> list[tuple[int, int]]:
        """返回 ``[(缺口序号, 目标 fid), ...]``；选「不补」的缝隙不出现。"""
        assignments: list[tuple[int, int]] = []
        for row in range(self.table.rowCount()):
            num_item = self.table.item(row, 0)
            combo = self.table.cellWidget(row, 2)
            if num_item is None or combo is None:
                continue
            gap_index = num_item.data(Qt.ItemDataRole.UserRole)
            target_fid = combo.currentData()
            if gap_index is None or target_fid is None:
                continue
            assignments.append((int(gap_index), int(target_fid)))
        return assignments

    def _on_ok(self) -> None:
        if not self.assignments():
            QMessageBox.information(
                self, "提示", "请至少为一处缝隙选择目标多边形，或点「取消」"
            )
            return
        self.accept()

    # ============================================================== 收尾

    def done(self, result: int) -> None:  # noqa: N802 —— Qt 命名
        """关闭（确定 / 取消 / Esc）时务必收掉高亮带与定时器。"""
        self._highlighter.close()
        super().done(result)
