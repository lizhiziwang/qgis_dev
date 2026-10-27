"""通用「添加数据」弹窗：本机盘符 / 目录浏览 + 空间数据库就地展开、多选。

替代过去「添加图层」按钮弹出的两行菜单（添加数据文件 / 添加 GDB），把文件与
数据库里的子图层放进**同一棵可多选的树**里：

- **左栏**：最近打开目录、快速访问（主目录 / 桌面 / 文档 / 下载）、我的电脑
  （Windows 盘符 / Linux 挂载点）。跨平台枚举在
  :mod:`app.ui.common.fs_places`；
- **右侧**：可输入 + 下拉位置树的路径栏（与左栏同源的层级树）、类型筛选
  （矢量 / 栅格 / 表格 / 本地数据库四大类 + GDB、GeoPackage、SQLite、Shapefile、
  GeoJSON、KML、DXF、GeoTIFF、Excel、CSV 等常用后缀快捷过滤）+ 文件树。普通
  文件夹双击进入；``.gdb`` / ``.gpkg`` / ``.sqlite`` / ``.db`` / Excel 在树里
  **就地展开**成子图层，和硬盘上的文件一起多选；
- **图标**：点 / 线 / 面 / 矢量 / 栅格 / 表格 / GDB / GeoPackage / SQLite 全部使用
  QGIS 官方图标主题（``QgsApplication.getThemeIcon``），文件夹用 QGIS 文件夹图标、
  盘符用系统标准图标，保证与 QGIS 桌面一致；
- **取值**：:meth:`AddDataDialog.selected_sources` 返回 :class:`DataSelection`
  列表，加载逻辑留在主窗口 mixin（本弹窗不依赖主窗口、不 import qgis.core 图层类）。

结构从「打开地图文档」启动弹窗（:class:`~app.ui.dialogs.startup_project.StartupProjectDialog`）
沿用：左窄栏 + 右明细 + 底部路径 / 主次按钮，视觉与交互保持一套语言。
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from qgis.core import QgsApplication
from qgis.PyQt.QtCore import QSize, Qt, QTimer
from qgis.PyQt.QtGui import QBrush, QColor, QFont, QIcon, QStandardItem, QStandardItemModel
from qgis.PyQt.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QComboBox,
    QCompleter,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QPushButton,
    QSizePolicy,
    QStyle,
    QToolButton,
    QTreeView,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.core.util.data_formats import (
    EXCEL_EXTS,
    GDB_SUFFIX,
    KIND_CONTAINER,
    KIND_DIR,
    KIND_GDB,
    KIND_RASTER,
    KIND_TABLE,
    KIND_VECTOR,
    classify_entry,
    format_label,
    is_multi_layer_source,
    is_valid_gdb_dir,
    resolve_gdb_dir,
    suffix_of,
)
from app.core.util.sublayers import Sublayer, list_sublayers
from app.infra.logging import get_logger
from app.ui.common.base_dialog import BaseDialog
from app.ui.common.fs_places import (
    KIND_DRIVE,
    KIND_HOME,
    place_sections,
)
from app.ui.common.icons import Icons
from app.ui.common.recent_dirs import (
    add_recent_dir,
    clear_recent_dirs,
    load_recent_dirs,
    remove_recent_dir,
)
from app.ui.common.theme import ThemeManager, add_data_qss

logger = get_logger(__name__)

# ---- item data roles（nav 用 path / kind；树用 path / 分类 / 子图层 / 容器 / 是否已读）----
_ROLE_PATH = int(Qt.ItemDataRole.UserRole)
_ROLE_KIND = _ROLE_PATH + 1
_ROLE_SUBLAYER = _ROLE_PATH + 2
_ROLE_CONTAINER = _ROLE_PATH + 3
_ROLE_LOADED = _ROLE_PATH + 4

#: 树里“子图层”行的分类（文件分类之外的自有种类）
KIND_SUBLAYER = "sublayer"

#: 左栏“最近”项的 kind
_KIND_RECENT = "recent"

#: 类型筛选项（combo 的 itemData）：大类 + 常用后缀两段
_FILTER_ALL = "all"
_FILTER_VECTOR = "vector"
_FILTER_RASTER = "raster"
_FILTER_TABLE = "table"
_FILTER_DB = "database"

#: 大类筛选项：(显示名, key, QGIS 主题图标)
_CATEGORY_FILTERS: tuple[tuple[str, str, str], ...] = (
    ("全部数据", _FILTER_ALL, "mActionAddLayer.svg"),
    ("矢量数据", _FILTER_VECTOR, "mIconVector.svg"),
    ("栅格数据", _FILTER_RASTER, "mIconRaster.svg"),
    ("表格数据", _FILTER_TABLE, "mIconTableLayer.svg"),
    ("本地数据库", _FILTER_DB, "mActionAddGeoPackageLayer.svg"),
)

#: 常用后缀快速筛选：(显示名, key, 扩展名集合, QGIS 主题图标)
_FORMAT_FILTERS: tuple[tuple[str, str, frozenset[str], str], ...] = (
    ("GDB 数据库 (.gdb)", "fmt:gdb", frozenset({".gdb"}), "mActionNewFileGeodatabase.svg"),
    ("GeoPackage (.gpkg)", "fmt:gpkg", frozenset({".gpkg"}), "mGeoPackage.svg"),
    (
        "SQLite 数据库 (.sqlite / .db)",
        "fmt:sqlite",
        frozenset({".sqlite", ".sqlite3", ".db"}),
        "mActionAddSpatiaLiteLayer.svg",
    ),
    ("Shapefile (.shp)", "fmt:shp", frozenset({".shp"}), "mIconVector.svg"),
    (
        "GeoJSON (.geojson / .json)",
        "fmt:geojson",
        frozenset({".geojson", ".json"}),
        "mIconVector.svg",
    ),
    ("KML (.kml)", "fmt:kml", frozenset({".kml"}), "mIconVector.svg"),
    ("GML (.gml)", "fmt:gml", frozenset({".gml"}), "mIconVector.svg"),
    ("DXF (.dxf)", "fmt:dxf", frozenset({".dxf"}), "mIconVector.svg"),
    ("GPX (.gpx)", "fmt:gpx", frozenset({".gpx"}), "mIconGpx.svg"),
    ("GeoTIFF (.tif / .tiff)", "fmt:tif", frozenset({".tif", ".tiff"}), "mIconRaster.svg"),
    ("MBTiles (.mbtiles)", "fmt:mbtiles", frozenset({".mbtiles"}), "mIconRaster.svg"),
    ("Excel 表格 (.xlsx / .xls)", "fmt:excel", EXCEL_EXTS, "mIconTableLayer.svg"),
    ("CSV 表格 (.csv)", "fmt:csv", frozenset({".csv"}), "mIconDelimitedText.svg"),
)

#: 常用后缀 key → 扩展名集合（筛选时只看后缀）
_FORMAT_EXTS: dict[str, frozenset[str]] = {
    key: exts for _label, key, exts, _icon in _FORMAT_FILTERS
}

#: 可多选加载的树行
_LOADABLE_KINDS = frozenset({KIND_VECTOR, KIND_RASTER, KIND_TABLE, KIND_SUBLAYER})

#: 树行分类 → QGIS 主题图标
_KIND_ICONS = {
    KIND_GDB: "mActionNewFileGeodatabase.svg",
    KIND_VECTOR: "mIconVector.svg",
    KIND_RASTER: "mIconRaster.svg",
    KIND_TABLE: "mIconTableLayer.svg",
    KIND_CONTAINER: "mActionAddGeoPackageLayer.svg",
}

#: 子图层几何类别 → QGIS 官方点/线/面图标
_GEOMETRY_ICONS = {
    "point": "mIconPointLayer.svg",
    "line": "mIconLineLayer.svg",
    "polygon": "mIconPolygonLayer.svg",
}

#: Windows ``FILE_ATTRIBUTE_HIDDEN``：老 .db 的 .gpkg 别名会打上这个属性
_FILE_ATTRIBUTE_HIDDEN = 0x2

_DEFAULT_HINT = "双击打开；Ctrl / Shift 多选；点击数据库文件名可展开内部图层"

_GEOMETRY_LABELS = {"point": "点图层", "line": "线图层", "polygon": "面图层"}


@dataclass(frozen=True)
class DataSelection:
    """用户选中的一份数据。

    ``sublayer`` 为空 = 直接按 ``path`` 加载文件；非空 = ``path`` 是容器
    （GDB 目录 / GeoPackage / SQLite），要打开其中的 ``sublayer``。
    """

    path: str
    sublayer: Sublayer | None = None


class AddDataDialog(BaseDialog):
    """浏览本机数据并多选添加（文件 + 空间数据库子图层）。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent, "添加数据")
        self.setMinimumSize(0, 0)
        self.resize(1000, 640)
        self.setModal(True)

        self._theme = ThemeManager.instance().current()
        #: 当前目录（绝对、normpath 过的原生路径）
        self._cwd = ""
        #: 浏览历史（后退按钮用；只存目录）
        self._history: list[str] = []
        self._history_pos = -1
        self._recent: list[str] = []
        self._nav_by_key: dict[str, QListWidgetItem] = {}
        #: 路径弹层待应用的目录（view.activated 与 combo.activated 可能各发一次）
        self._pending_place = ""
        #: 图标按 QGIS 主题图标名 / 标准图标别名缓存，避免每个文件都重新取一遍
        self._icon_cache: dict[str, QIcon] = {}

        self._build_ui()
        self._start_directory()

    # ================================================================ 构建

    def _build_ui(self) -> None:
        self.setObjectName("AddDataDialog")
        self.setStyleSheet(add_data_qss(self._theme))

        outer = QVBoxLayout()
        outer.setContentsMargins(20, 16, 20, 16)
        outer.setSpacing(4)

        heading = QLabel("添加数据")
        heading.setObjectName("AddDataHeading")
        sub = QLabel(
            "从本机选择矢量 / 栅格数据；GDB、GeoPackage、SQLite 可展开后选择内部图层，"
            "MBTiles 直接作为栅格加载，文件和图层可一起多选。"
        )
        sub.setObjectName("AddDataSub")
        outer.addWidget(heading)
        outer.addWidget(sub)
        outer.addSpacing(10)

        body = QHBoxLayout()
        body.setSpacing(12)
        self._nav = self._build_nav()
        body.addWidget(self._nav)
        body.addLayout(self._build_browser(), 1)
        outer.addLayout(body, 1)

        outer.addSpacing(10)

        bottom = QHBoxLayout()
        bottom.setSpacing(8)
        self._summary = QLabel("未选择数据")
        self._summary.setObjectName("AddDataSummary")
        self._summary.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred
        )
        bottom.addWidget(self._summary, 1)

        self._select_all_btn = QPushButton("全选")
        self._select_all_btn.setObjectName("AddDataSecondary")
        self._select_all_btn.setAutoDefault(False)
        self._select_all_btn.setToolTip("全选当前列表中的文件与已展开的数据库图层")
        self._select_all_btn.clicked.connect(self._select_all)
        bottom.addWidget(self._select_all_btn)

        self._ok_btn = QPushButton("添加")
        self._ok_btn.setObjectName("AddDataPrimary")
        self._ok_btn.setDefault(True)
        self._ok_btn.clicked.connect(self._accept_selection)
        cancel_btn = QPushButton("取消")
        cancel_btn.setObjectName("AddDataSecondary")
        cancel_btn.setAutoDefault(False)
        cancel_btn.clicked.connect(self.reject)
        bottom.addWidget(self._ok_btn)
        bottom.addWidget(cancel_btn)
        outer.addLayout(bottom)

        self.setLayout(outer)
        self._refresh_ok()

    def _build_nav(self) -> QListWidget:
        """左栏：最近打开目录 / 快速访问 / 我的电脑。"""
        nav = QListWidget()
        nav.setObjectName("AddDataNav")
        nav.setFixedWidth(212)
        nav.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        nav.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        nav.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        nav.setTextElideMode(Qt.TextElideMode.ElideRight)
        nav.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        nav.itemClicked.connect(self._on_nav_clicked)
        nav.customContextMenuRequested.connect(self._on_nav_context_menu)
        self._nav = nav
        self._rebuild_nav()
        return nav

    def _build_browser(self) -> QVBoxLayout:
        """右侧：路径 / 历史 / 刷新 / 筛选 + 文件树 + 提示。"""
        right = QVBoxLayout()
        right.setSpacing(6)

        toolbar = QHBoxLayout()
        toolbar.setSpacing(4)
        self._back_btn = self._tool_button(
            QStyle.StandardPixmap.SP_ArrowBack, "后退", self._go_back
        )
        self._up_btn = self._tool_button(
            QStyle.StandardPixmap.SP_ArrowUp, "上一级", self._go_up
        )
        self._home_btn = self._tool_button(
            QStyle.StandardPixmap.SP_DirHomeIcon, "主目录", self._go_home
        )
        self._refresh_btn = self._tool_button(
            QStyle.StandardPixmap.SP_BrowserReload, "刷新", self._refresh
        )
        toolbar.addWidget(self._back_btn)
        toolbar.addWidget(self._up_btn)
        toolbar.addWidget(self._home_btn)
        toolbar.addWidget(self._refresh_btn)

        self._path_combo = self._build_path_combo()
        toolbar.addWidget(self._path_combo, 1)

        self._filter = QComboBox()
        self._filter.setObjectName("AddDataFilter")
        for label, key, icon_name in _CATEGORY_FILTERS:
            self._filter.addItem(self._theme_icon(icon_name), label, key)
        self._filter.insertSeparator(self._filter.count())
        for label, key, _exts, icon_name in _FORMAT_FILTERS:
            self._filter.addItem(self._theme_icon(icon_name), label, key)
        self._filter.setMaxVisibleItems(22)
        self._filter.view().setMinimumWidth(270)
        self._filter.currentIndexChanged.connect(self._on_filter_changed)
        toolbar.addWidget(self._filter)
        right.addLayout(toolbar)

        self._tree = QTreeWidget()
        self._tree.setObjectName("AddDataTree")
        self._tree.setColumnCount(4)
        self._tree.setHeaderLabels(["名称", "类型", "大小", "修改时间"])
        self._tree.setRootIsDecorated(True)
        self._tree.setUniformRowHeights(True)
        self._tree.setAlternatingRowColors(True)
        self._tree.setSelectionMode(
            QAbstractItemView.SelectionMode.ExtendedSelection
        )
        self._tree.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._tree.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        # 展开完全由 itemClicked / itemExpanded 控制：双击用于“进入 / 打开”
        self._tree.setExpandsOnDoubleClick(False)
        header = self._tree.header()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self._tree.itemClicked.connect(self._on_item_clicked)
        self._tree.itemDoubleClicked.connect(self._on_item_double_clicked)
        self._tree.itemExpanded.connect(self._on_item_expanded)
        self._tree.itemSelectionChanged.connect(self._on_selection_changed)
        self._tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._tree.customContextMenuRequested.connect(self._on_tree_context_menu)
        right.addWidget(self._tree, 1)

        self._hint = QLabel(_DEFAULT_HINT)
        self._hint.setObjectName("AddDataHint")
        self._hint.setWordWrap(True)
        right.addWidget(self._hint)
        return right

    def _tool_button(self, standard: QStyle.StandardPixmap, tip: str, slot) -> QToolButton:
        button = QToolButton()
        button.setObjectName("AddDataTool")
        button.setIcon(self.style().standardIcon(standard))
        button.setIconSize(QSize(16, 16))
        button.setToolTip(tip)
        button.setAutoRaise(True)
        button.clicked.connect(slot)
        return button

    # ============================================================ 路径下拉树

    def _build_path_combo(self) -> QComboBox:
        """可输入 + 下拉树：最近目录 / 快速访问 / 我的电脑 三层位置树。

        下拉用 ``QTreeView``（不是列表），与左栏保持同一套层级；点叶子把路径回填
        到编辑框并导航。编辑框仍可手输 / 粘贴路径（回车定位），completer 给路径
        前缀建议。
        """
        combo = QComboBox()
        combo.setObjectName("AddDataPath")
        combo.setEditable(True)
        combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        combo.setMaxVisibleItems(22)
        edit = combo.lineEdit()
        edit.setPlaceholderText("输入 / 粘贴路径后回车，或点右侧箭头从位置树选择")
        edit.setClearButtonEnabled(True)
        edit.returnPressed.connect(self._on_path_submitted)

        self._path_model = QStandardItemModel(combo)
        combo.setModel(self._path_model)
        view = QTreeView(combo)
        view.setObjectName("AddDataPathTree")
        view.setHeaderHidden(True)
        view.setRootIsDecorated(True)
        view.setUniformRowHeights(True)
        view.setMinimumWidth(380)
        view.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        view.clicked.connect(self._on_path_place_clicked)
        view.activated.connect(self._on_path_place_clicked)
        combo.setView(view)
        self._path_view = view
        # QComboBox 的弹层会拦截 view.clicked（实测鼠标点击只发 combo.activated），
        # 必须再接一条：取 view 当前节点（combo 的 int 索引是压平后的，不可靠）
        combo.activated.connect(self._on_path_combo_activated)

        self._rebuild_path_combo(combo)
        return combo

    def _rebuild_path_combo(self, combo: QComboBox | None = None) -> None:
        """重建路径下拉树（最近目录变化时同步）。"""
        combo = combo if combo is not None else getattr(self, "_path_combo", None)
        if combo is None or not hasattr(self, "_path_model"):
            return
        current = self._cwd if combo is getattr(self, "_path_combo", None) else combo.currentText()
        model = self._path_model
        model.clear()

        flat_paths: list[str] = []
        recent_root = self._path_group("最近打开目录", self._folder_icon())
        if self._recent:
            for path in self._recent:
                self._append_place(
                    recent_root,
                    self._recent_label(path),
                    path,
                    self._folder_icon(),
                    flat_paths,
                )
        else:
            empty = QStandardItem("（暂无最近目录）")
            empty.setFlags(Qt.ItemFlag.ItemIsEnabled)
            empty.setForeground(QBrush(QColor(self._theme.text_secondary)))
            recent_root.appendRow(empty)
        model.appendRow(recent_root)

        for title, places in place_sections():
            root = self._path_group(
                title,
                self._drive_icon() if title == "我的电脑" else self._folder_icon(),
            )
            for place in places:
                self._append_place(
                    root,
                    place.label,
                    place.path,
                    self._icon_for_place(place.kind),
                    flat_paths,
                )
            model.appendRow(root)

        if current:
            combo.setEditText(current)
        completer = QCompleter(flat_paths, self)
        completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        completer.setFilterMode(Qt.MatchFlag.MatchContains)
        completer.setMaxVisibleItems(12)
        combo.setCompleter(completer)
        # 下拉树默认全展开：分组只是标签，用户直接点叶子
        view = getattr(self, "_path_view", None)
        if view is not None:
            view.expandAll()

    def _path_group(self, title: str, icon: QIcon) -> QStandardItem:
        item = QStandardItem(icon, title)
        # 分组行不参与选择，只作为树的父节点；子树默认展开（showPopup 时）
        item.setFlags(Qt.ItemFlag.ItemIsEnabled)
        item.setEditable(False)
        return item

    @staticmethod
    def _append_place(
        root: QStandardItem,
        label: str,
        path: str,
        icon: QIcon,
        flat_paths: list[str],
    ) -> None:
        child = QStandardItem(icon, label)
        child.setData(path, _ROLE_PATH)
        child.setToolTip(path)
        child.setEditable(False)
        child.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
        root.appendRow(child)
        flat_paths.append(path)

    def _on_path_place_clicked(self, index) -> None:
        """位置树里点中一个目录 → 回填路径并导航。"""
        self._activate_path_index(index)

    def _on_path_combo_activated(self, _index: int) -> None:
        """弹层鼠标点击 / 键盘回车：QComboBox 只给压平的 int 索引，改取树节点。"""
        self._activate_path_index(self._path_view.currentIndex())

    def _activate_path_index(self, index) -> None:
        if index is None or not index.isValid():
            return
        path = index.data(_ROLE_PATH)
        if not path:
            # 分组 / 空占位行：把被组合框写成显示名的编辑框恢复成当前目录
            QTimer.singleShot(0, lambda: self._path_combo.setEditText(self._cwd))
            return
        # 点路径后下面列表必须立即切到该目录。组合框在点击时会先把显示名写进
        # 编辑框，延后一拍导航，让编辑框/列表都落在真实路径上。
        # 用 pending 去重：鼠标点击可能同时触发 view.activated 与 combo.activated。
        self._pending_place = str(path)
        self._path_combo.hidePopup()
        QTimer.singleShot(0, self._apply_pending_place)

    def _apply_pending_place(self) -> None:
        path = getattr(self, "_pending_place", "")
        self._pending_place = ""
        if path:
            self._navigate(path)

    # ================================================================ 左栏

    def _rebuild_nav(self) -> None:
        """重建左栏（最近目录变化 / 初始化时）。"""
        nav = self._nav
        nav.clear()
        self._nav_by_key.clear()
        self._recent = load_recent_dirs()

        self._add_nav_header("最近打开目录")
        if self._recent:
            for path in self._recent:
                self._add_nav_item(
                    self._recent_label(path),
                    path,
                    _KIND_RECENT,
                    self._folder_icon(),
                )
        else:
            self._add_nav_placeholder("（暂无最近目录）")

        for title, places in place_sections():
            self._add_nav_header(title)
            for place in places:
                self._add_nav_item(
                    place.label,
                    place.path,
                    place.kind,
                    self._icon_for_place(place.kind),
                )

        self._select_nav_for(self._cwd)
        # 路径下拉树与左栏同源：最近目录被移除 / 清空后一起刷新
        self._rebuild_path_combo()

    def _add_nav_header(self, text: str) -> None:
        item = QListWidgetItem(text)
        item.setFlags(Qt.ItemFlag.NoItemFlags)
        font = QFont(item.font())
        font.setBold(True)
        item.setFont(font)
        item.setForeground(QBrush(QColor(self._theme.text_secondary)))
        self._nav.addItem(item)

    def _add_nav_placeholder(self, text: str) -> None:
        item = QListWidgetItem(text)
        item.setFlags(Qt.ItemFlag.NoItemFlags)
        item.setForeground(QBrush(QColor(self._theme.text_secondary)))
        self._nav.addItem(item)

    def _add_nav_item(
        self,
        text: str,
        path: str,
        kind: str,
        icon: QIcon,
    ) -> QListWidgetItem:
        item = QListWidgetItem(text)
        item.setData(_ROLE_PATH, path)
        item.setData(_ROLE_KIND, kind)
        item.setIcon(icon)
        item.setToolTip(path)
        self._nav.addItem(item)
        self._nav_by_key[self._key(path)] = item
        return item

    @staticmethod
    def _recent_label(path: str) -> str:
        name = Path(path.rstrip("\\/")).name
        return name or path

    @staticmethod
    def _key(path: str) -> str:
        """比较键：Windows 下大小写不敏感的同一目录视为一项。"""
        return os.path.normcase(os.path.abspath(path))

    def _select_nav_for(self, path: str) -> None:
        item = self._nav_by_key.get(self._key(path)) if path else None
        if item is not None:
            self._nav.setCurrentItem(item)
        else:
            self._nav.setCurrentItem(None)
            self._nav.clearSelection()

    def _on_nav_clicked(self, item: QListWidgetItem) -> None:
        path = item.data(_ROLE_PATH)
        if path:
            self._navigate(str(path))

    def _on_nav_context_menu(self, pos) -> None:
        item = self._nav.itemAt(pos)
        kind = item.data(_ROLE_KIND) if item is not None else None
        path = item.data(_ROLE_PATH) if item is not None else None
        menu = QMenu(self)
        removed = False
        if kind == _KIND_RECENT and path:
            act = menu.addAction("从最近目录移除")
            act.triggered.connect(lambda: self._remove_recent(str(path)))
            removed = True
        act_clear = menu.addAction("清除最近目录")
        act_clear.setEnabled(bool(self._recent))
        act_clear.triggered.connect(self._clear_recent)
        if not removed and not self._recent:
            return
        menu.exec(self._nav.viewport().mapToGlobal(pos))

    def _remove_recent(self, path: str) -> None:
        remove_recent_dir(path)
        self._rebuild_nav()

    def _clear_recent(self) -> None:
        clear_recent_dirs()
        self._rebuild_nav()

    # ================================================================ 导航

    def _start_directory(self) -> None:
        """初始目录：最近目录的第一项，否则主目录。"""
        candidates = [*self._recent, str(Path.home())]
        for path in candidates:
            if path and os.path.isdir(path):
                self._navigate(path)
                return
        # 极端情况（主目录都不存在）：退回文件系统根
        self._navigate(os.path.abspath(os.sep))

    def _navigate(self, path: str, *, record_history: bool = True) -> bool:
        """进入目录；成功返回 ``True``。"""
        target = self._normalize_dir(path)
        if not target:
            self._set_hint(f"目录不存在或无法访问：{path}")
            return False
        if record_history:
            # 从历史中间进入新目录：截断“前进”部分
            if self._history_pos + 1 < len(self._history):
                del self._history[self._history_pos + 1:]
            if not self._history or self._history[self._history_pos] != target:
                self._history.append(target)
                self._history_pos = len(self._history) - 1
        self._cwd = target
        self._path_combo.setEditText(target)
        self._path_combo.lineEdit().setCursorPosition(len(target))
        self._populate()
        self._select_nav_for(target)
        self._update_nav_buttons()
        return True

    @staticmethod
    def _normalize_dir(path: str) -> str:
        raw = str(path or "").strip().strip('"')
        if not raw:
            return ""
        try:
            target = os.path.normpath(os.path.abspath(os.path.expanduser(raw)))
            return target if os.path.isdir(target) else ""
        except OSError:
            return ""

    def _on_path_submitted(self) -> None:
        raw = self._path_combo.currentText().strip().strip('"')
        if not raw:
            return
        candidate = os.path.abspath(os.path.expanduser(raw))
        if os.path.isdir(candidate):
            self._navigate(candidate)
            return
        if os.path.isfile(candidate):
            # 直接粘贴一个文件路径：进入它所在目录并选中它
            parent = os.path.dirname(candidate)
            if self._navigate(parent):
                self._select_item_for_path(candidate)
            return
        self._set_hint(f"路径不存在：{raw}")

    def _select_item_for_path(self, path: str) -> None:
        key = self._key(path)
        for index in range(self._tree.topLevelItemCount()):
            item = self._tree.topLevelItem(index)
            if self._key(str(item.data(0, _ROLE_PATH) or "")) == key:
                item.setSelected(True)
                self._tree.setCurrentItem(item)
                self._tree.scrollToItem(item)
                return

    def _go_back(self) -> None:
        if self._history_pos <= 0:
            return
        self._history_pos -= 1
        self._navigate(self._history[self._history_pos], record_history=False)

    def _go_up(self) -> None:
        if not self._cwd:
            return
        parent = os.path.dirname(self._cwd)
        if parent and parent != self._cwd:
            self._navigate(parent)

    def _go_home(self) -> None:
        home = str(Path.home())
        if os.path.isdir(home):
            self._navigate(home)
        else:
            self._start_directory()

    def _refresh(self) -> None:
        if self._cwd:
            self._populate()

    def _update_nav_buttons(self) -> None:
        self._back_btn.setEnabled(self._history_pos > 0)
        parent = os.path.dirname(self._cwd) if self._cwd else ""
        self._up_btn.setEnabled(bool(parent) and parent != self._cwd)
        self._home_btn.setEnabled(os.path.isdir(str(Path.home())))

    # ================================================================ 文件树

    def _populate(self) -> None:
        """按当前目录 + 筛选条件重建树（只列文件夹与可添加的数据）。"""
        self._tree.clear()
        self._set_hint(_DEFAULT_HINT)
        entries = self._scan(self._cwd)
        if entries is None:
            placeholder = QTreeWidgetItem(["（无法读取此目录）"])
            placeholder.setFlags(Qt.ItemFlag.NoItemFlags)
            placeholder.setForeground(0, QBrush(QColor(self._theme.text_secondary)))
            self._tree.addTopLevelItem(placeholder)
            self._on_selection_changed()
            return
        filter_key = self._filter.currentData()
        rows: list[tuple[int, str, str, str, str]] = []
        for is_dir, name, path in entries:
            kind = classify_entry(path, is_dir=is_dir)
            if kind == KIND_DIR:
                rows.append((0, name.casefold(), name, path, kind))
                continue
            if kind not in (
                KIND_GDB,
                KIND_CONTAINER,
                KIND_VECTOR,
                KIND_RASTER,
                KIND_TABLE,
            ):
                continue
            if not self._passes_filter(path, kind, filter_key):
                continue
            # 文件夹与 .gdb 排最前，其余按名称
            rows.append((0 if kind == KIND_GDB else 1, name.casefold(), name, path, kind))
        rows.sort(key=lambda row: (row[0], row[1]))

        for _group, _fold, name, path, kind in rows:
            self._tree.addTopLevelItem(self._make_item(name, path, kind))
        if not rows:
            placeholder = QTreeWidgetItem(["（这个目录里没有可添加的数据）"])
            placeholder.setFlags(Qt.ItemFlag.NoItemFlags)
            placeholder.setForeground(0, QBrush(QColor(self._theme.text_secondary)))
            self._tree.addTopLevelItem(placeholder)
        self._on_selection_changed()

    def _scan(self, directory: str) -> list[tuple[bool, str, str]] | None:
        """列目录（不递归）；失败时在树里给出提示并返回 ``None``。"""
        if not directory:
            return []
        try:
            with os.scandir(directory) as it:
                entries: list[tuple[bool, str, str]] = []
                for entry in it:
                    name = entry.name
                    # 点文件 / 点目录默认不展示：用户数据极少放在隐藏目录里
                    if not name or name.startswith("."):
                        continue
                    try:
                        is_dir = entry.is_dir()
                        st = entry.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    # Windows 隐藏属性（老 .db 的 .gpkg 别名、desktop.ini 等）不展示
                    if getattr(st, "st_file_attributes", 0) & _FILE_ATTRIBUTE_HIDDEN:
                        continue
                    # Linux 下别名没有隐藏属性：按「同 inode / 同大小时间」识别并跳过
                    if not is_dir and self._is_legacy_gpkg_alias(entry.path):
                        continue
                    entries.append((is_dir, name, entry.path))
            return entries
        except OSError as exc:
            logger.info("添加数据：无法读取目录 %s（%s）", directory, exc)
            self._set_hint(f"无法读取目录（权限或设备不可用）：{directory}")
            return None

    @staticmethod
    def _is_legacy_gpkg_alias(path: str) -> bool:
        """老 ``.db``（GPKG 内容）旁边的 ``.gpkg`` 硬链接别名，不在列表重复展示。

        见 :func:`app.core.util.local_db_util.gpkg_open_path`：老 ``.db`` 库为了让
        GDAL 按 GPKG 驱动识别，会在同目录建一个同 inode 的 ``.gpkg`` 别名（Windows
        带隐藏属性，Linux 上没有）。别名是**临时**的 —— 图层从工程移除后即删除；
        这里只是避免它在文件树里看起来像“又多复制了一份数据”。
        """
        if suffix_of(path) != ".gpkg":
            return False
        db_path = os.path.splitext(path)[0] + ".db"
        if not os.path.isfile(db_path):
            return False
        try:
            if os.path.samefile(path, db_path):
                return True
            alias_stat = os.stat(path)
            db_stat = os.stat(db_path)
        except OSError:
            return False
        # 硬链接不可用时会退化成复制：同大小 + 同修改时间也视为别名
        return (
            alias_stat.st_size == db_stat.st_size
            and abs(alias_stat.st_mtime - db_stat.st_mtime) < 5
        )

    @staticmethod
    def _passes_filter(path: str, kind: str, filter_key: str) -> bool:
        # 常用后缀筛选：只看扩展名（.gdb 目录也能取到 .gdb）
        exts = _FORMAT_EXTS.get(filter_key)
        if exts is not None:
            return suffix_of(path) in exts
        if filter_key == _FILTER_VECTOR:
            # 本地数据库里装的通常就是矢量图层，选「矢量数据」时一起列出
            return kind in (KIND_VECTOR, KIND_CONTAINER, KIND_GDB)
        if filter_key == _FILTER_RASTER:
            return kind == KIND_RASTER
        if filter_key == _FILTER_TABLE:
            return kind == KIND_TABLE
        if filter_key == _FILTER_DB:
            return kind in (KIND_GDB, KIND_CONTAINER)
        return True

    def _make_item(self, name: str, path: str, kind: str) -> QTreeWidgetItem:
        label = format_label(path, kind)
        size = ""
        mtime = ""
        if kind in (KIND_VECTOR, KIND_RASTER, KIND_TABLE, KIND_CONTAINER):
            try:
                stat = os.stat(path)
                size = self._size_text(stat.st_size)
                mtime = datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M")
            except OSError:
                pass
        item = QTreeWidgetItem([name, label, size, mtime])
        item.setData(0, _ROLE_PATH, path)
        item.setData(0, _ROLE_KIND, kind)
        item.setIcon(0, self._icon_for_path(path, kind))
        item.setToolTip(0, path)
        if is_multi_layer_source(path):
            item.setData(0, _ROLE_LOADED, False)
            child = QTreeWidgetItem(["点击展开以读取内部图层…"])
            child.setFlags(Qt.ItemFlag.NoItemFlags)
            child.setForeground(0, QBrush(QColor(self._theme.text_secondary)))
            item.addChild(child)
            item.setToolTip(0, f"{path}\n点击展开内部图层")
        return item

    # ------------------------------------------------------------ 图标

    def _theme_icon(self, name: str) -> QIcon:
        """QGIS 官方图标主题（``QgsApplication.getThemeIcon``），按名字缓存。"""
        icon = self._icon_cache.get(name)
        if icon is None:
            icon = QgsApplication.getThemeIcon(name)
            if icon.isNull():
                icon = self.style().standardIcon(QStyle.StandardPixmap.SP_FileIcon)
            self._icon_cache[name] = icon
        return icon

    def _folder_icon(self) -> QIcon:
        """黄色文件夹（资源自带 SVG；取不到时退回 QGIS 官方文件夹图标）。"""
        icon = self._icon_cache.get("folder:yellow")
        if icon is None:
            icon = Icons.custom("icons", "folder.svg")
            if icon.isNull():
                icon = self._theme_icon("mIconFolder.svg")
            self._icon_cache["folder:yellow"] = icon
        return icon

    def _drive_icon(self) -> QIcon:
        icon = self._icon_cache.get("std:drive")
        if icon is None:
            icon = self.style().standardIcon(QStyle.StandardPixmap.SP_DriveHDIcon)
            self._icon_cache["std:drive"] = icon
        return icon

    def _icon_for_place(self, kind: str) -> QIcon:
        if kind == KIND_HOME:
            return self._theme_icon("mIconFolderHome.svg")
        if kind == KIND_DRIVE:
            return self._drive_icon()
        return self._folder_icon()

    def _icon_for_path(self, path: str, kind: str) -> QIcon:
        if kind == KIND_DIR:
            return self._folder_icon()
        if kind == KIND_CONTAINER:
            name = (
                "mGeoPackage.svg"
                if suffix_of(path) == ".gpkg"
                else "mActionAddSpatiaLiteLayer.svg"
            )
            return self._theme_icon(name)
        if kind == KIND_TABLE:
            name = (
                "mIconDelimitedText.svg"
                if suffix_of(path) == ".csv"
                else "mIconTableLayer.svg"
            )
            return self._theme_icon(name)
        return self._theme_icon(_KIND_ICONS.get(kind, "mIconFile.svg"))

    def _icon_for_sublayer(self, sublayer: Sublayer) -> QIcon:
        if not sublayer.has_geometry:
            return self._theme_icon("mIconTableLayer.svg")
        name = _GEOMETRY_ICONS.get(sublayer.geometry_kind, "mIconVector.svg")
        return self._theme_icon(name)

    @staticmethod
    def _size_text(size: int) -> str:
        value = float(max(0, size))
        for unit in ("B", "KB", "MB", "GB", "TB"):
            if value < 1024.0 or unit == "TB":
                return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
            value /= 1024.0
        return f"{value:.1f} TB"

    # ------------------------------------------------------------ 树事件

    def _is_expandable_item(self, item: QTreeWidgetItem) -> bool:
        """只有容器行（GDB / 本地库 / Excel）能再展开。

        **子图层行也保存着容器路径**（``_ROLE_PATH`` 指向 .gdb/.gpkg），不能再按
        ``is_multi_layer_source(path)`` 判断 —— 否则点一下 GDB 里的 point，会把整个
        GDB 的图层再次列成 point 的子节点，看起来像“点一个图层冒出多个”。
        """
        if item.data(0, _ROLE_KIND) == KIND_SUBLAYER:
            return False
        path = item.data(0, _ROLE_PATH)
        return bool(path) and is_multi_layer_source(str(path))

    def _on_item_clicked(self, item: QTreeWidgetItem, _column: int) -> None:
        """单击数据库 / Excel 行 → 就地展开（用户要求“点击后继续显示里面的文件”）。"""
        if self._is_expandable_item(item) and not item.data(0, _ROLE_LOADED):
            item.setExpanded(True)

    def _on_item_expanded(self, item: QTreeWidgetItem) -> None:
        if self._is_expandable_item(item) and not item.data(0, _ROLE_LOADED):
            self._populate_container(item)

    def _on_item_double_clicked(self, item: QTreeWidgetItem, _column: int) -> None:
        kind = item.data(0, _ROLE_KIND)
        path = item.data(0, _ROLE_PATH)
        if kind == KIND_DIR:
            if path:
                self._navigate(str(path))
            return
        if self._is_expandable_item(item):
            # 双击“有下一级的数据”（GDB / 库 / Excel）一律展开：
            # 第一次单击已经触发展开，这里再 toggle 会把它又收起来。
            item.setExpanded(True)
            return
        if kind in (KIND_VECTOR, KIND_RASTER, KIND_TABLE, KIND_SUBLAYER):
            # 双击已选项 → 直接添加（比再点一次“添加”顺手）
            for other in self._tree.selectedItems():
                if other is not item:
                    other.setSelected(False)
            item.setSelected(True)
            self._accept_selection()

    def _populate_container(self, item: QTreeWidgetItem) -> None:
        """把 GDB / 数据库里的全部图层列成子节点。"""
        # 防御：子图层行不能再被当成容器展开（入口处已判，这里兜底）
        if item.data(0, _ROLE_KIND) == KIND_SUBLAYER:
            return
        item.setData(0, _ROLE_LOADED, True)
        path = str(item.data(0, _ROLE_PATH) or "")
        item.takeChildren()

        # 解压得到的目录常出现「外层 xxx.gdb / 内层 xxx.gdb」：外层没有
        # gdb、*.gdbtable，直接交给 OGR 只会得到一条没有细节的“无法打开”。
        # 唯一内层库可以确定时自动下钻；多个候选则不猜。
        actual_path = path
        resolved = None
        if suffix_of(path) == GDB_SUFFIX:
            resolved = resolve_gdb_dir(path)
            if resolved:
                actual_path = resolved

        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            sublayers = list_sublayers(actual_path)
        except Exception:
            logger.warning("添加数据：列子图层失败 %s", actual_path, exc_info=True)
            sublayers = []
        finally:
            QApplication.restoreOverrideCursor()

        if not sublayers:
            child = QTreeWidgetItem(["（没有可读取的图层）"])
            child.setFlags(Qt.ItemFlag.NoItemFlags)
            child.setForeground(0, QBrush(QColor(self._theme.text_secondary)))
            if suffix_of(path) == GDB_SUFFIX:
                if resolved is None and not is_valid_gdb_dir(path):
                    tooltip = (
                        "所选目录里没有 gdb / *.gdbtable 文件，不是可读取的 "
                        "File Geodatabase。若它是解压得到的外层文件夹，"
                        "请展开并选择里面的 .gdb。"
                    )
                    hint = (
                        f"「{item.text(0)}」不是有效的 GDB 目录；"
                        "如果里面还有一层 .gdb，请展开选择内层库。"
                    )
                elif actual_path != path:
                    tooltip = f"已自动读取内层库：{actual_path}"
                    hint = f"内层库「{os.path.basename(actual_path)}」中没有读到可加载的图层。"
                else:
                    tooltip = "File Geodatabase 中没有要素类或表。"
                    hint = "GDB 可以读取，但其中没有要素类或表。"
                child.setToolTip(0, tooltip)
            else:
                hint = "没有读到可加载的图层。"
            item.addChild(child)
            item.setExpanded(True)
            self._set_hint(hint)
            return

        for sublayer in sublayers:
            kind_text = (
                _GEOMETRY_LABELS.get(sublayer.geometry_kind, "图层")
                if sublayer.has_geometry
                else "属性表"
            )
            child = QTreeWidgetItem([sublayer.name, kind_text, "", ""])
            # 子图层指向真正读取的库：外层目录下钻时不能继续把外层路径传下去
            child.setData(0, _ROLE_PATH, actual_path)
            child.setData(0, _ROLE_KIND, KIND_SUBLAYER)
            child.setData(0, _ROLE_SUBLAYER, sublayer)
            child.setData(0, _ROLE_CONTAINER, actual_path)
            child.setIcon(0, self._icon_for_sublayer(sublayer))
            child.setToolTip(0, sublayer.uri)
            item.addChild(child)
        item.setExpanded(True)
        if actual_path != path:
            self._set_hint(
                f"「{item.text(0)}」是外层目录，已自动读取内层库"
                f"「{os.path.basename(actual_path)}」：共 {len(sublayers)} 个图层"
            )
        else:
            self._set_hint(
                f"已展开「{item.text(0)}」共 {len(sublayers)} 个图层；勾选后可与文件一起添加"
            )

    def _on_selection_changed(self) -> None:
        self._update_summary()
        self._refresh_ok()

    # ------------------------------------------------------------ 多选

    def _loadable_selected(self) -> list[DataSelection]:
        """当前选中的可加载项（文件夹 / 数据库本身不算，必须选到图层或文件）。"""
        out: list[DataSelection] = []
        seen: set[tuple[str, str]] = set()
        for item in self._tree.selectedItems():
            kind = item.data(0, _ROLE_KIND)
            if kind not in _LOADABLE_KINDS:
                continue
            if kind == KIND_SUBLAYER:
                sublayer = item.data(0, _ROLE_SUBLAYER)
                container = str(item.data(0, _ROLE_CONTAINER) or "")
                if sublayer is None or not container:
                    continue
                key = (self._key(container), f"layer:{sublayer.name}")
                selection = DataSelection(container, sublayer)
            else:
                path = str(item.data(0, _ROLE_PATH) or "")
                if not path:
                    continue
                key = (self._key(path), "")
                selection = DataSelection(path)
            if key in seen:
                continue
            seen.add(key)
            out.append(selection)
        return out

    def selected_sources(self) -> list[DataSelection]:
        """用户选择的数据（按树中出现顺序；取消时为空列表）。"""
        return self._loadable_selected()

    def _select_all(self) -> None:
        for item in self._iter_items():
            kind = item.data(0, _ROLE_KIND)
            if kind in _LOADABLE_KINDS:
                item.setSelected(True)

    def _iter_items(self) -> Iterator[QTreeWidgetItem]:
        stack = [
            self._tree.topLevelItem(i) for i in range(self._tree.topLevelItemCount())
        ]
        while stack:
            item = stack.pop()
            yield item
            stack.extend(item.child(i) for i in range(item.childCount()))

    def _update_summary(self) -> None:
        selections = self._loadable_selected()
        if not selections:
            self._summary.setText("未选择数据")
            return
        names: list[str] = []
        for selection in selections[:3]:
            if selection.sublayer is not None:
                names.append(f"{Path(selection.path).name} / {selection.sublayer.name}")
            else:
                names.append(os.path.basename(selection.path))
        suffix = f" 等 {len(selections)} 项" if len(selections) > 3 else ""
        self._summary.setText(f"已选择 {len(selections)} 项：{'、'.join(names)}{suffix}")

    def _refresh_ok(self, *_args: object) -> None:
        self._ok_btn.setEnabled(bool(self._loadable_selected()))

    def _accept_selection(self) -> None:
        selection = self._loadable_selected()
        if not selection:
            self._set_hint("请选择数据文件，或展开数据库后勾选其中的图层")
            return
        # 记“用过的目录”，下次打开直接回到这里（取消不算）
        if self._cwd:
            add_recent_dir(self._cwd)
        self.accept()

    def _on_filter_changed(self, _index: int) -> None:
        if self._cwd:
            self._populate()

    def _set_hint(self, text: str) -> None:
        self._hint.setText(text)

    # ------------------------------------------------------------ 右键菜单

    def _on_tree_context_menu(self, pos) -> None:
        item = self._tree.itemAt(pos)
        menu = QMenu(self)
        act_add = menu.addAction("添加选中项")
        act_add.setEnabled(bool(self._loadable_selected()))
        act_add.triggered.connect(self._accept_selection)
        if item is not None and self._is_expandable_item(item):
            menu.addSeparator()
            act_toggle = menu.addAction(
                "收起数据库 / 工作表" if item.isExpanded() else "展开数据库 / 工作表"
            )
            act_toggle.triggered.connect(
                lambda: item.setExpanded(not item.isExpanded())
            )
        menu.addSeparator()
        act_refresh = menu.addAction("刷新")
        act_refresh.triggered.connect(self._refresh)
        menu.exec(self._tree.viewport().mapToGlobal(pos))


__all__ = ["AddDataDialog", "DataSelection"]
