"""栅格属性表（RAT）查看对话框。

QGIS 自带的 ``QgsRasterAttributeTableDialog`` 是一套独立窗口 chrome，不跟随
本应用的无边框标题栏与主题；这里只读取 :class:`QgsRasterAttributeTable` 的
字段与行，用自建 :class:`BaseDialog` + 只读表格展示。编辑能力未开放：栅格
RAT 的编辑/写回使用频率低，先保证“能看”。
"""

from __future__ import annotations

from qgis.core import Qgis, QgsRasterLayer
from qgis.PyQt.QtGui import QBrush, QColor
from qgis.PyQt.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from app.infra.logging import get_logger
from app.ui.common.base_dialog import BaseDialog

logger = get_logger(__name__)

_USAGE = Qgis.RasterAttributeTableFieldUsage

#: 颜色字段不单独占一列：RAT 的颜色统一画到最前面的色块列里，
#: 否则 ArcGIS 风格的一张表会多出 4～8 列 Red / Green / Blue / Alpha。
_COLOR_USAGES = {
    int(_USAGE.Red),
    int(_USAGE.Green),
    int(_USAGE.Blue),
    int(_USAGE.Alpha),
    int(_USAGE.RedMin),
    int(_USAGE.RedMax),
    int(_USAGE.GreenMin),
    int(_USAGE.GreenMax),
    int(_USAGE.BlueMin),
    int(_USAGE.BlueMax),
    int(_USAGE.AlphaMin),
    int(_USAGE.AlphaMax),
}


class RasterAttributeTableDialog(BaseDialog):
    """按波段展示栅格属性表；有颜色列的 RAT 额外显示色块。"""

    def __init__(self, layer: QgsRasterLayer, parent=None) -> None:
        super().__init__(parent, f"属性表 - {layer.name()}")
        self._layer = layer
        self._table = None
        self._bands: list[int] = []
        self._build()
        self._load_bands()

    # ---------------------------------------------------------------- UI

    def _build(self) -> None:
        self.resize(900, 620)
        layout = QVBoxLayout()
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(8)

        top = QHBoxLayout()
        top.setSpacing(8)
        top.addWidget(QLabel("波段：", self))
        self.band_combo = QComboBox(self)
        self.band_combo.setMinimumWidth(120)
        self.band_combo.currentIndexChanged.connect(self._on_band_changed)
        top.addWidget(self.band_combo)
        self.count_label = QLabel("", self)
        top.addWidget(self.count_label)
        top.addStretch(1)
        layout.addLayout(top)

        self.table = QTableWidget(self)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(True)
        self.table.verticalHeader().setDefaultSectionSize(24)
        layout.addWidget(self.table, 1)

        bottom = QHBoxLayout()
        bottom.addStretch(1)
        close_button = QPushButton("关闭", self)
        close_button.clicked.connect(self.close)
        bottom.addWidget(close_button)
        layout.addLayout(bottom)

        self.setLayout(layout)

    # ---------------------------------------------------------------- 数据

    def _load_bands(self) -> None:
        """列出真正带 RAT 的波段；没有就禁用表格。"""
        self._bands = [
            band
            for band in range(1, self._layer.bandCount() + 1)
            if self._layer.attributeTable(band) is not None
        ]
        self.band_combo.blockSignals(True)
        self.band_combo.clear()
        for band in self._bands:
            self.band_combo.addItem(f"波段 {band}", band)
        self.band_combo.blockSignals(False)
        if not self._bands:
            self.count_label.setText("该图层没有栅格属性表")
            self.table.setRowCount(0)
            self.table.setColumnCount(0)
            self.band_combo.setEnabled(False)
            return
        self.band_combo.setEnabled(len(self._bands) > 1)
        self.band_combo.setCurrentIndex(0)
        self._refresh()

    def _on_band_changed(self, _index: int) -> None:
        self._refresh()

    def _refresh(self) -> None:
        band = self.band_combo.currentData()
        self._table = self._layer.attributeTable(band) if band else None
        if self._table is None:
            self.table.setRowCount(0)
            self.table.setColumnCount(0)
            self.count_label.setText("该波段没有栅格属性表")
            return

        fields = self._table.fields()
        rows = self._table.data()
        show_color = bool(self._table.hasColor())
        offset = 1 if show_color else 0
        columns = _display_columns(fields)

        self.table.setRowCount(len(rows))
        self.table.setColumnCount(len(columns) + offset)
        headers = [title for title, _source in columns]
        if show_color:
            headers.insert(0, "颜色")
        self.table.setHorizontalHeaderLabels(headers)
        header = self.table.horizontalHeader()
        if show_color:
            header.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
            self.table.setColumnWidth(0, 56)
        for column in range(offset, self.table.columnCount()):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        header.setStretchLastSection(True)

        for row, values in enumerate(rows):
            if show_color:
                color = self._table.color(row)
                item = QTableWidgetItem()
                if isinstance(color, QColor) and color.isValid():
                    if color.alpha() == 0:
                        item.setToolTip("透明")
                    else:
                        item.setBackground(QBrush(color))
                        item.setToolTip(color.name())
                self.table.setItem(row, 0, item)
            for index, (_title, source) in enumerate(columns):
                item = QTableWidgetItem(_cell_for(source, values))
                self.table.setItem(row, index + offset, item)

        self.count_label.setText(
            f"共 {len(rows)} 行，{len(columns) + offset} 列"
        )
        self.table.resizeRowsToContents()


def _display_columns(fields) -> list[tuple[str, int | tuple[int, int]]]:
    """把 RAT 字段映射成表格列：合并 Min/Max，隐藏颜色字段。

    - ``MinMax`` / ``Min + Max`` → 一列「值」（区间显示成 ``最小 ~ 最大``）
    - ``PixelCount`` → 「像元数」
    - ``Name`` → 「分类」
    - 颜色字段由最前面的色块列统一展示，不再占列
    """
    infos = []
    for index, field in enumerate(fields):
        try:
            usage = int(field.usage)
        except (TypeError, ValueError):
            usage = -1
        infos.append((index, field, usage))

    minimums = [index for index, _f, usage in infos if usage == int(_USAGE.Min)]
    maximums = [index for index, _f, usage in infos if usage == int(_USAGE.Max)]
    pair = (
        (minimums[0], maximums[0])
        if len(minimums) == 1 and len(maximums) == 1
        else None
    )

    columns: list[tuple[str, int | tuple[int, int]]] = []
    for index, field, usage in infos:
        if usage in _COLOR_USAGES:
            continue
        if pair is not None and index in pair:
            if index == pair[0]:
                columns.append(("值", pair))
            continue
        if usage == int(_USAGE.MinMax):
            columns.append(("值", index))
        elif usage == int(_USAGE.PixelCount):
            columns.append(("像元数", index))
        elif usage == int(_USAGE.Name):
            columns.append(("分类", index))
        else:
            columns.append((str(field.name), index))
    return columns


def _cell_for(source, values) -> str:
    if isinstance(source, tuple):
        minimum = values[source[0]]
        maximum = values[source[1]]
        low, high = _cell_text(minimum), _cell_text(maximum)
        return low if low == high else f"{low} ~ {high}"
    return _cell_text(values[source])


def _cell_text(value) -> str:
    """整数值的浮点去掉 ``.0``，避免 Value 列整列显示成 0.0 / 1.0。"""
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)
