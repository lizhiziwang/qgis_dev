"""坐标系选择弹窗（原型「坐标系选择」）：图层 / 北京54 / 西安80 / WGS84 / CGCS2000 / 自定义。

「创建数据 → 单图层数据」的坐标系那一行右侧的「选择」按钮打开它，作为
:class:`~app.ui.dialogs.create_data_dialog.CrsComboBox` 那个「自己敲 EPSG 代码 / 搜名称」
之外的另一个入口：绝大多数用户不知道带号对应的代码，但知道自己要的是
「CGCS2000 / 3度分带（带号34）」。

页签与行为照原型（``code01/app.js`` 的 ``cdRenderCrs``）：

- **图层**：列工程里已加载的图层及其空间参考，挑一个就用它的坐标系（用户手里
  往往已经有一份参照数据）；
- **北京54 / 西安80 / WGS84 / CGCS2000**：左边选分带模式、右边挑具体分带
  （列表由 :mod:`app.core.util.srs` 现查 ``srs.db`` 得出，见那边的说明）；
- **自定义**：从一份数据文件或一个 ``.prj`` 里取空间参考。

**本弹窗只负责"选"**，不写任何 IO 出数据源：读文件取 CRS 是取元信息，选完由调用方写回
下拉框。选中的值经 :meth:`CrsPickDialog.crs_value` 给出 —— 可能是 ``EPSG:4547``，
也可能是自定义的 WKT（认不出 authid 时），两种 ``CrsComboBox`` 都吃得下。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (
    QAbstractItemView,
    QDialogButtonBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QRadioButton,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from app.core.util.srs import DATUMS, Datum, ZoneMode, describe_crs, modes_for, zone_entries
from app.infra.logging import get_logger
from app.ui.common.base_dialog import BaseDialog
from app.ui.common.widgets import open_file_dir

if TYPE_CHECKING:
    from collections.abc import Sequence

logger = get_logger(__name__)

#: 「通过地理数据集获取空间参考」的文件过滤器
_DATASET_FILTER = (
    "矢量数据 (*.shp *.gpkg *.geojson *.json *.gml *.kml *.tab *.fgb *.sqlite *.db *.gdb);;所有文件 (*)"
)

#: 「通过坐标文件获取空间参考」的过滤器
_PRJ_FILTER = "投影文件 (*.prj);;所有文件 (*)"


class CrsPickDialog(BaseDialog):
    """坐标系选择：按基准 / 分带挑，或直接沿用某个图层的空间参考。"""

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        current: str = "",
        layers: Sequence[object] = (),
    ) -> None:
        """
        Args:
            parent: 父控件。
            current: 当前坐标系（authid 或文字，用来预选 / 回显）。
            layers: 工程里已加载的图层（「图层」页签用；由调用方注入，本弹窗不问 QgsProject）。
        """
        super().__init__(parent, "坐标系选择")
        #: 当前选中的坐标系值（authid 或 WKT）。**跨页签记着**：选完分带再切到别的页签
        #: 看一眼，不该把选择丢了
        self._value = current
        self._initial = current
        self._layers = list(layers)
        #: 每个基准页签各有一个分带列表（键 = 页签标题），见 :meth:`_build_zone_tab`
        self._zone_lists: dict[str, QListWidget] = {}
        self.setMinimumSize(0, 0)
        self.resize(620, 460)
        self._build()

    # --------------------------------------------------------------- 构建

    def _build(self) -> None:
        root = QVBoxLayout()
        root.setContentsMargins(14, 12, 14, 12)
        root.setSpacing(8)

        self.head = QLabel()
        self.head.setObjectName("SectionLabel")
        self.head.setWordWrap(True)
        root.addWidget(self.head)
        self._refresh_head()

        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_layers_tab(), "图层")
        for datum in DATUMS:
            self.tabs.addTab(self._build_zone_tab(datum), datum.key)
        self.tabs.addTab(self._build_custom_tab(), "自定义")
        root.addWidget(self.tabs, 1)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
            Qt.Orientation.Horizontal,
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("确定(&O)")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消(&C)")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)
        self.setLayout(root)

    def _build_layers_tab(self) -> QWidget:
        """「图层」页签：工程里各图层用的是哪个空间参考，挑一个照用。"""
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        hint = QLabel("数据源：工程里已加载的图层。选中一行即采用该图层的空间参考。")
        hint.setObjectName("PropHint")
        layout.addWidget(hint)

        self.layer_table = QTableWidget(0, 2)
        self.layer_table.setHorizontalHeaderLabels(["图层", "空间参考"])
        self.layer_table.verticalHeader().setVisible(False)
        self.layer_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.layer_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.layer_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        header = self.layer_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.layer_table.setColumnWidth(0, 200)
        self.layer_table.itemSelectionChanged.connect(self._on_layer_row_picked)
        layout.addWidget(self.layer_table, 1)

        for layer in self._layers:
            self._add_layer_row(layer)
        if self.layer_table.rowCount() == 0:
            empty = QLabel("（工程里还没有加载任何图层）")
            empty.setObjectName("PropHint")
            layout.addWidget(empty)
        return page

    def _add_layer_row(self, layer) -> None:
        """往「图层」页签加一行；选中当前坐标系所在的那一行。"""
        try:
            crs = layer.crs()
        except Exception:  # noqa: BLE001 —— 图层对象可能已被销毁（工程里删过图层）
            logger.info("坐标系选择：跳过取不到 CRS 的图层")
            return
        row = self.layer_table.rowCount()
        self.layer_table.insertRow(row)
        self.layer_table.setItem(row, 0, QTableWidgetItem(layer.name()))
        description = crs.description() or crs.authid() or "（未知）"
        self.layer_table.setItem(row, 1, QTableWidgetItem(description))
        value = layer_crs_value(layer)
        self.layer_table.item(row, 0).setData(Qt.ItemDataRole.UserRole, value)
        if value and value == self._initial:
            self.layer_table.selectRow(row)

    def _build_zone_tab(self, datum: Datum) -> QWidget:
        """基准页签：左边分带模式、右边该模式下的分带列表。

        **每个基准各有自己的列表控件**（``self._zone_lists``）：四个页签共用一份不行 ——
        在「北京54」里切模式会把「CGCS2000」那张列表填掉（存成属性就会被后建的页签覆盖）。
        """
        page = QWidget()
        layout = QHBoxLayout(page)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(10)

        # 先建列表（下面几个单选的槽要往它里面填），但**加进布局的顺序照原型**：
        # 左边「分带模式」、右边「投影分带」
        right = QVBoxLayout()
        right_heading = QLabel("投影分带")
        right_heading.setObjectName("PropHint")
        right.addWidget(right_heading)
        zone_list = QListWidget()
        zone_list.itemSelectionChanged.connect(lambda d=datum: self._on_zone_picked(d))
        zone_list.itemDoubleClicked.connect(lambda _item: self.accept())
        zone_list.setMinimumWidth(300)
        self._zone_lists[datum.key] = zone_list
        right.addWidget(zone_list, 1)

        left = QVBoxLayout()
        heading = QLabel("分带模式")
        heading.setObjectName("PropHint")
        left.addWidget(heading)
        modes = modes_for(datum)
        for index, mode in enumerate(modes):
            radio = QRadioButton(mode.label)
            radio.setChecked(index == 0)
            radio.toggled.connect(lambda checked, d=datum, m=mode: checked and self._fill_zones(d, m))
            left.addWidget(radio)
        left.addStretch(1)

        layout.addLayout(left)
        layout.addLayout(right, 1)

        # 页签建好就把第一个模式的分带填上
        self._fill_zones(datum, modes[0])
        return page

    def _build_custom_tab(self) -> QWidget:
        """「自定义」页签：从数据文件或 .prj 里取空间参考。"""
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)

        hint = QLabel("从已有数据的空间参考出发：选一份数据文件，或选它的 .prj 投影文件。")
        hint.setObjectName("PropHint")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        row = QHBoxLayout()
        from_dataset = QPushButton("通过地理数据集获取空间参考")
        from_dataset.clicked.connect(self._pick_from_dataset)
        row.addWidget(from_dataset)
        from_prj = QPushButton("通过坐标文件获取空间参考(*.prj)")
        from_prj.clicked.connect(self._pick_from_prj)
        row.addWidget(from_prj)
        row.addStretch(1)
        layout.addLayout(row)

        self.custom_label = QLabel()
        self.custom_label.setObjectName("PropValue")
        self.custom_label.setWordWrap(True)
        # 还没取到就不显示（用户要的）：空着一句「（还没取到空间参考）」只是噪音，
        # 取到了再把它显出来 —— 见 :meth:`_show_custom`
        self.custom_label.setVisible(False)
        layout.addWidget(self.custom_label)
        layout.addStretch(1)
        return page

    def _show_custom(self, text: str) -> None:
        """把「从数据集 / prj 取到的结果」显示出来（取到才显示，见上）。"""
        self.custom_label.setText(text)
        self.custom_label.setVisible(True)

    # --------------------------------------------------------------- 交互

    def _refresh_head(self) -> None:
        """顶部回显「当前空间参考」（原型也是这么一行）。"""
        text = self._value or self._initial
        if text:
            self.head.setText(f"当前空间参考：{describe_crs(text) or text}")
        else:
            self.head.setText("当前空间参考：（未选）")

    def _take(self, value: str, label: str = "") -> None:
        """记下选择并刷新顶部回显。"""
        if not value:
            return
        self._value = value
        self._refresh_head()
        if label:
            logger.info("坐标系选择：%s（%s）", label, value)

    def _fill_zones(self, datum: Datum, mode: ZoneMode) -> None:
        """把某个基准页签的列表换成「基准 + 模式」下的分带。"""
        zone_list = self._zone_lists[datum.key]
        entries = zone_entries(datum, mode)
        zone_list.blockSignals(True)
        zone_list.clear()
        for entry in entries:
            item = QListWidgetItem(entry.label)
            item.setData(Qt.ItemDataRole.UserRole, entry.authid)
            zone_list.addItem(item)
            if entry.authid == self._initial:
                item.setSelected(True)
                zone_list.setCurrentItem(item)
        zone_list.blockSignals(False)
        if not entries:
            placeholder = QListWidgetItem("（本机的坐标系库没有这组分带，请改用「自定义」或直接输入 EPSG 代码）")
            placeholder.setFlags(Qt.ItemFlag.NoItemFlags)
            zone_list.addItem(placeholder)

    def _on_zone_picked(self, datum: Datum) -> None:
        item = self._zone_lists[datum.key].currentItem()
        if item is None:
            return
        authid = item.data(Qt.ItemDataRole.UserRole)
        if authid:
            self._take(str(authid), f"{datum.key} · {item.text()}")

    def _on_layer_row_picked(self) -> None:
        rows = {index.row() for index in self.layer_table.selectedIndexes()}
        if len(rows) != 1:
            return
        row = rows.pop()
        item = self.layer_table.item(row, 0)
        if item is None:
            return
        value = item.data(Qt.ItemDataRole.UserRole)
        if value:
            self._take(str(value), item.text())

    def _pick_from_dataset(self) -> None:
        """选一份数据文件，用它的空间参考。"""
        from qgis.core import QgsVectorLayer

        path = open_file_dir(self, title="选择数据文件", filter_=_DATASET_FILTER)
        if not path:
            return
        # 必须绑到局部变量：临时 QgsVectorLayer 一被回收，它的 dataProvider 也失效
        layer = QgsVectorLayer(path, "探针", "ogr")
        if not layer.isValid():
            self._show_custom(f"打不开这份数据：{path}")
            return
        value = layer_crs_value(layer)
        if not value:
            self._show_custom(f"这份数据里没有空间参考：{path}")
            return
        self._show_custom(f"{path}\n{layer.crs().description() or value}")
        self._take(value, path)

    def _pick_from_prj(self) -> None:
        """选一个 .prj 文件，用它里面的 WKT。"""
        from qgis.core import QgsCoordinateReferenceSystem

        path = open_file_dir(self, title="选择投影文件", filter_=_PRJ_FILTER)
        if not path:
            return
        try:
            with open(path, encoding="utf-8", errors="replace") as handle:
                wkt = handle.read().strip()
        except OSError as exc:
            self._show_custom(f"读不了这个文件：{exc}")
            return
        crs = QgsCoordinateReferenceSystem.fromWkt(wkt)
        if not crs.isValid():
            self._show_custom(f"这个 .prj 里的坐标系认不出：{path}")
            return
        value = crs.authid() or wkt
        self._show_custom(f"{path}\n{crs.description() or crs.authid() or '（自定义）'}")
        self._take(value, path)

    # --------------------------------------------------------------- 取值

    def crs_value(self) -> str:
        """选中的坐标系值：``EPSG:xxxx``，或认不出代码时的 WKT（没选任何东西时为空串）。"""
        return self._value


def layer_crs_value(layer) -> str:
    """图层 → 可写进坐标系框的值（``EPSG:xxxx`` 优先，自定义坐标系退回 WKT）。"""
    crs = layer.crs()
    if not crs.isValid():
        return ""
    return crs.authid() or crs.toWkt()


__all__ = ["CrsPickDialog", "layer_crs_value"]
