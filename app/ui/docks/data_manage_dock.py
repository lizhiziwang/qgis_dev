"""数据管理面板（放在 DataManageDialog 对话框里，不再使用底部 Dock）。

按需求 HTML 的元素结构映射为原生 Qt 组件(不用 QTextBrowser 渲染):

- ``section.dialog / .dialog-title`` -> :class:`~app.ui.common.base_dialog.BaseDialog` 的标题栏
  (含右上角关闭按钮, 对应 HTML 的关闭键)
- ``.dialog-body > .dm-split``        -> :class:`QSplitter`(水平两栏)
- ``.dm-tree``                        -> 左栏: 树头「数据库连接」+ QTreeWidget
  + 「新建连接」按钮(本地库 / 服务器连接均可入树); 树固定**两个根节点**
  「本地数据库」/「服务器数据库」, 连接作为对应根的子节点
- ``.dm-panel``                       -> 右栏: 标题行(选中库名 + 创建数据 /
  导入 / 刷新按钮) + QTableWidget(数据表: 数据名称 / 类型 / 几何类型 /
  坐标系 / 说明, 全列可手动拖宽且自动撑满可用宽度, 行悬停显示字段列表
  元信息, **双击行 → on_add_layer 回调**(把该注册表加入地图)
- ``.dialog-footer``                  -> 底部按钮行(关闭)

本地数据库展示以 ``local_db_util`` 的 ``query_all_table_name()`` 注册表为准
(见 :mod:`app.core.util.local_db_util`), 树按类别分级: 「本地数据库」根 →
连接节点(名称=文件名 stem)→「矢量 / 字典 / 影像」分组节点 → 注册表名叶子;
右侧面板列出选中库(或某类)的注册数据行(类型列=类别, 说明列=行数)。
「服务器数据库」根下挂服务器连接(PostgreSQL / Oracle / SQL Server, 来自
「新建连接」对话框服务器页), 右侧面板展示连接摘要 —— 表目录浏览待后续
版本接入。创建数据 / 导入 / 刷新仍为回调占位(未传时记日志), 由 MainWindow
提供回调后接入; 双击数据表行触发 ``on_add_layer`` 回调, 由 MainWindow 把
该表作为图层加入工程 —— 矢量 OGR provider 直开: 新规 gpkg 内容库本体即
``.gpkg`` 后缀 → GPKG 驱动直开、无别名无第二文件; 仅兼容老 ``.db`` 后缀
gpkg 库(经同 inode 的 ``.gpkg`` 硬链接别名打开 —— GDAL 3.11 按文件名
后缀裁决驱动, .db 会被 SQLite 驱动抢先认领成纯属性表, 别名让 GPKG 驱动
接管, 零拷贝; 别名是**临时的**: 仅在图层存续期间存在, 图层移除/会话结束
即删, 见 local_db_util.release_gpkg_alias / sweep_startup_gpkg_aliases);
老 spatialite 库由 OGR SQLite 驱动原样读,
影像 gpkg 内容经 GDAL/GPKG 栅格驱动直开(普通文件路径, 经免 .aux.xml
边车的打开方式); 直开失败(非 GeoPackage 内容)提示改用 .gpkg 库重落 ——
无本地 XYZ 回落(该服务已移除), 见
:mod:`app.core.util.local_layer_loader`。

树节点另有右键菜单(见 :meth:`DataManageDock._build_context_menu`), 菜单项按
节点层级显隐:

- 「添加数据库连接」—— 只在「本地数据库」/「服务器数据库」两个根节点
  （把**根节点的 scope 带下去**，弹窗默认停在对应页签）;
- 「导入图层」/「创建数据」/「导入数据」/「删除连接」/「打开文件所在位置」——
  只在库连接节点（本地 / 服务器；后四项只给本地库）。「删除连接」**只解绑、
  不删库文件**，确认由 MainWindow 负责;
- 「移除全部图层」—— 只在**有注册数据的本地库连接节点**（一次移除该库下的
  全部注册表 + 注册行，并移出工程里已打开的对应图层；确认由 MainWindow 负责）;
- 「加到地图上展示」—— 只在空间 / 影像叶子（双击同效；字典表没有几何不显示）;
- 「删除图层」—— 只在注册数据叶子(库内表 + ``user_relation`` 注册行由
  MainWindow 侧一并删除, 并移出工程里已打开的对应图层);
- 「导入数据」/「导出数据」—— 只在**空间**叶子上（字典表 / 影像表不适用）：
  导入是往这张表里写其他数据源的要素（可追加或替换、可多选），导出是写成 shp / gdb / gpkg / sqlite
  （可带属性条件）;
- 「刷新」—— 任意节点(重扫该节点所属库并重建左树)。

所有动作都带参数回调（右键的是哪个库 / 哪个图层），回调签名见 ``__init__``。
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path

from qgis.PyQt.QtCore import QEvent, QSize, Qt, QTimer, pyqtSignal
from qgis.PyQt.QtGui import QAction, QBrush, QColor, QFont, QIcon
from qgis.PyQt.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QMenu,
    QMessageBox,
    QPushButton,
    QSplitter,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.core.util.local_db_util import (
    _CATEGORY_LABEL,
    _CATEGORY_ORDER,
    _CATEGORY_UNIT,
    count_local_db_rows,
    describe_local_db,
)
from app.infra.logging import get_logger
from app.ui.common import tool_icons
from app.ui.common.base_dialog import BaseDialog
from app.ui.common.theme import ThemeManager
from app.ui.common.widgets import reveal_in_file_manager, save_file
from app.ui.dialogs.add_connection import AddConnectionDialog

logger = get_logger(__name__)

_ROLE = int(Qt.ItemDataRole.UserRole)
#: 表格行(名称列)自带的定位 tag = f"{category}\\x00{name}", 供叶子行精确高亮
_ROW_TAG_ROLE = int(Qt.ItemDataRole.UserRole) + 1

#: 树节点层级 → Font Awesome 图标键(见 :mod:`app.ui.common.tool_icons`)
_TREE_ICONS = {
    "root_local": "db_local",
    "root_server": "db_server",
    "conn_local": "db_file",
    "conn_server": "db_remote",
    "spatial": "cat_vector",
    "dict": "cat_dict",
    "image": "cat_image",
    "leaf": "cat_data",
}
#: 加粗层级(作用域根 / 连接节点); 类别分组与表叶子用常规字重
_BOLD_KINDS = frozenset({"root_local", "root_server", "conn_local", "conn_server"})
#: 仅右列数字用的缩小字号(相对基准字号的点差)
_COUNT_FONT_DELTA = 1.5

#: **服务器数据库先隐藏**（用户要求）：服务器版还没接入，树里留着这棵树只会让人误点。
#: 连接清单本身仍照常读写（``export_state`` 包含 server），把这个开关打开就回来。
_SHOW_SERVER_DB = False


class DataManageDock(QWidget):
    """数据管理面板（对话框内）: 左数据库树 + 右数据表 / 详情编辑. """

    #: 登记的连接清单变了（加库 / 整体替换）。调用方据此**立刻**把它落盘到系统级状态
    #: 文件里 —— 用户要的是「绑定好之后即使当前工程没保存，下次打开也还在」
    #: （见 :mod:`app.infra.fs.state`）。
    databasesChanged = pyqtSignal()
    #: 后台行数统计完成：``(库路径, {表名: 行数})``
    counts_ready = pyqtSignal(str, object)

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        on_add_connection: Callable[[str], None] | None = None,
        on_remove_connection: Callable[[dict], None] | None = None,
        on_create_data: Callable[[dict | None], None] | None = None,
        on_import: Callable[[dict | None], None] | None = None,
        on_refresh: Callable[[], None] | None = None,
        on_add_layer: Callable[[dict, str, str], None] | None = None,
        on_delete_layer: Callable[[dict, str, str], None] | None = None,
        on_remove_all_layers: Callable[[dict], None] | None = None,
        on_import_data: Callable[[dict, str, str], None] | None = None,
        on_export_layer: Callable[[dict, str, str], None] | None = None,
        on_before_table_change: Callable[[dict, str, str], bool] | None = None,
        on_rename_table: Callable[[dict, str, str, str], tuple[bool, str]] | None = None,
        on_dict_domains_deleted: Callable[[dict, str, str, list[str]], None] | None = None,
        on_remove_unused_dicts: Callable[[dict], None] | None = None,
    ) -> None:
        super().__init__(parent)
        #: QSS 作用域锚点(见 app.ui.common.theme.window_qss 的 #DataManageDock 规则)
        self.setObjectName("DataManageDock")
        #: 动作回调占位(未提供时点击只记日志)
        #: 回调**带参数**是刻意的：右键哪个库 / 哪个节点，动作就该作用在它上面
        #: （新建连接的 ``scope``、创建/导入的 ``default_db``、追加/导出的
        #: ``(db, category, table)``）。
        self._on_add_connection = on_add_connection
        self._on_remove_connection = on_remove_connection
        self._on_create_data = on_create_data
        self._on_import = on_import
        self._on_refresh = on_refresh
        #: 双击表格行回调: (连接 dict, category, 表名) → 加入地图(由 MainWindow 提供)
        self._on_add_layer = on_add_layer
        #: 右键树「删除图层」回调: (连接 dict, category, 表名) → 删库内表 + 注册
        #: 并移出工程图层(由 MainWindow 提供)
        self._on_delete_layer = on_delete_layer
        #: 右键库连接「移除全部图层」回调: (连接 dict) → 批量删库内注册表 + 注册行
        #: 并移出工程图层(由 MainWindow 提供；确认框也在那边)
        self._on_remove_all_layers = on_remove_all_layers
        #: 右键图层「导入数据」: (连接 dict, category, 表名) —— 可追加或替换
        self._on_import_data = on_import_data
        #: 右键图层「导出数据」: (连接 dict, category, 表名)
        self._on_export_layer = on_export_layer
        #: 字段结构变更前回调（MainWindow 负责把引用该表的已打开图层移出工程）
        self._on_before_table_change = on_before_table_change
        #: 表重命名回调（MainWindow 负责同步已打开图层的名称 / 数据源）
        self._on_rename_table = on_rename_table
        #: 字典域被整域删除：MainWindow 据此清掉已打开图层上的字典挂接属性
        self._on_dict_domains_deleted = on_dict_domains_deleted
        #: 字典分组右键「移除未使用字典」：MainWindow 负责确认 + 删表
        self._on_remove_unused_dicts = on_remove_unused_dicts
        #: 当前右栏详情（库 dict / category / 表名）；无则显示数据表列表
        self._current_detail: tuple[dict, str, str] | None = None
        #: 当前联动展示的库(双击行定位用)
        self._current_db: dict | None = None
        #: 当前选中的类别过滤(类别分组节点点击时设; refresh_all 重画用)
        self._current_category: str | None = None
        #: 正在后台统计行数的库路径（避免重复起线程）
        self._counting_paths: set[str] = set()
        self.counts_ready.connect(self._on_counts_ready)
        #: 列宽程序化调整中(防止信号重入)
        self._fitting = False
        #: 服务器连接清单(kind=server, 挂在「服务器数据库」分组下)
        self._server_connections: list[dict] = []
        self._build_ui()
        self._databases: list[dict] = []
        self._rebuild_tree()
        # 树图标是 QPainter 画的(颜色烧在 PNG 里), 切主题需重刷
        ThemeManager.instance().theme_changed.connect(self._on_theme_changed)

    # ------------------------------------------------------------ UI

    def _build_ui(self) -> None:
        v = QVBoxLayout(self)
        v.setContentsMargins(4, 4, 4, 4)
        v.setSpacing(2)

        self.split = QSplitter(Qt.Orientation.Horizontal, self)
        self.split.addWidget(self._build_tree_pane(self.split))
        self.split.addWidget(self._build_panel_pane(self.split))
        self.split.setStretchFactor(0, 0)
        self.split.setStretchFactor(1, 1)
        self.split.setSizes([240, 760])
        v.addWidget(self.split, 1)

        footer = self._build_footer(self)
        v.addWidget(footer)

    def _build_tree_pane(self, parent: QWidget) -> QWidget:
        """左栏: 数据库连接树(.dm-tree).

        视觉规格(字号 / 行距 / 悬停 / 选中 / 「新建连接」按钮)集中到
        :func:`app.ui.common.theme.window_qss` 的 ``#DataManageTree`` /
        ``#DataManageAddConn`` 规则, 主题切换自动跟随; 这里只管布局、
        图标尺寸与列宽策略, 节点级配色见 :meth:`_apply_tree_theme`。
        """
        pane = QWidget(parent)
        pane.setObjectName("DataManageTreePane")
        lay = QVBoxLayout(pane)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(2)

        hd = QLabel("数据库连接")
        hd.setObjectName("DataManageTreeHeader")
        lay.addWidget(hd)

        self.tree = QTreeWidget()
        self.tree.setObjectName("DataManageTree")
        self.tree.setHeaderHidden(True)
        self.tree.setColumnCount(2)
        self.tree.setIndentation(14)
        self.tree.setAnimated(True)
        self.tree.setRootIsDecorated(True)
        self.tree.setIconSize(QSize(14, 14))
        self.tree.setUniformRowHeights(True)
        self.tree.setAllColumnsShowFocus(True)  # 选中态铺满整行
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        # 列宽: 名称列吃满剩余宽度(长名省略号 + tooltip 兜底), 行数列紧贴右侧。
        # 不这么设的话默认「末列拉伸」会让行数列抢走全部富余宽度, 名称列固定
        # 100px 被截断。行数列不用 ResizeToContents —— 那个模式会在每次
        # setText 后重算整列, 大库(上千表)重建树是 O(n²); 改成 Interactive
        # 由 _apply_tree_theme 末尾 resizeColumnToContents 算一次(仍可手拖)。
        header = self.tree.header()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)
        self.tree.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.tree.itemClicked.connect(self._on_tree_item_clicked)
        # 右键菜单: 走自定义策略(QTreeWidget 默认策略不弹菜单), 菜单项按
        # 节点层级显隐, 见 _on_tree_context_menu / _build_context_menu
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._on_tree_context_menu)
        lay.addWidget(self.tree, 1)

        self.add_btn = QPushButton("新建连接")
        self.add_btn.setObjectName("DataManageAddConn")
        self.add_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.add_btn.setToolTip("连接本地库文件或服务器数据库")
        self.add_btn.clicked.connect(self._on_add_connection_clicked)
        lay.addWidget(self.add_btn)
        return pane

    def _build_panel_pane(self, parent: QWidget) -> QWidget:
        """右栏: 数据表列表 / 选中叶子后的详情编辑页（QStackedWidget）。"""
        pane = QWidget(parent)
        outer = QVBoxLayout(pane)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        list_page = QWidget(pane)
        lay = QVBoxLayout(list_page)
        lay.setContentsMargins(4, 0, 0, 0)
        lay.setSpacing(4)

        # 面板头: 选中库名 + 动作按钮
        hd_row = QHBoxLayout()
        hd_row.setContentsMargins(0, 0, 0, 0)
        self.sel_name = QLabel("")
        font = self.sel_name.font()
        font.setBold(True)
        self.sel_name.setFont(font)
        hd_row.addWidget(self.sel_name)
        hd_row.addStretch(1)

        self.create_btn = QPushButton("创建数据")
        self.import_btn = QPushButton("导入")
        self.refresh_btn = QPushButton("刷新")
        self.add_dict_btn = QPushButton("添加字典表")
        self.add_dict_btn.clicked.connect(self._on_add_dict_table_clicked)
        self.add_dict_btn.setVisible(False)
        for btn in (self.create_btn, self.import_btn, self.refresh_btn, self.add_dict_btn):
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            hd_row.addWidget(btn)
        self.create_btn.clicked.connect(self._on_create_data_clicked)
        self.import_btn.clicked.connect(self._on_import_clicked)
        self.refresh_btn.clicked.connect(self._on_refresh_clicked)
        lay.addLayout(hd_row)

        # 数据表: 数据名称 | 类型 | 几何类型 | 坐标系 | 说明
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(
            ["数据名称", "类型", "几何类型", "坐标系", "说明"]
        )
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        header = self.table.horizontalHeader()
        # 全列 Interactive: 用户可拖动表头手动调宽; 宽度不足视口时由
        # ``_fill_available_width`` 按比例放大到撑满(仍可拖窄, 余量走滚动条)
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setMinimumSectionSize(48)
        for col, width in ((0, 200), (1, 70), (2, 90), (3, 230), (4, 150)):
            header.resizeSection(col, width)
        self.table.itemDoubleClicked.connect(self._on_table_item_double_clicked)
        # 表格尺寸变化(窗口/分隔条拖动) → 延迟横向撑满
        self.table.installEventFilter(self)
        lay.addWidget(self.table, 1)

        from app.ui.docks.table_detail import TableDetailPage

        self.detail_page = TableDetailPage(pane)
        self.detail_page.set_before_change(self._before_table_change)
        self.detail_page.dictionary.dictDomainsDeleted.connect(
            self._on_dict_domains_deleted_signal
        )
        self.detail_page.backRequested.connect(self._show_list_page)
        self.detail_page.dataChanged.connect(self._on_detail_data_changed)
        self.stack = QStackedWidget(pane)
        self.stack.addWidget(list_page)
        self.stack.addWidget(self.detail_page)
        outer.addWidget(self.stack, 1)
        return pane

    def _build_footer(self, parent: QWidget) -> QWidget:
        """底部按钮行(.dialog-footer)."""
        foot = QWidget(parent)
        h = QHBoxLayout(foot)
        h.setContentsMargins(0, 2, 0, 0)
        h.addStretch(1)
        close_btn = QPushButton("关闭")
        close_btn.clicked.connect(lambda: self.window().close())
        h.addWidget(close_btn)
        return foot

    # ------------------------------------------------------------ 数据

    def set_databases(self, databases: list[dict]) -> None:
        """整体替换本地数据库清单并重建树.

        每项连接 dict::

            {"kind": "local", "name": str, "path": str,
             "categories": {"spatial": [表名…], "dict": [表名…], "image": [表名…]},
             "counts": {表名: 行数|None},
             "meta": {表名: {geom_label, geom_col, srid, crs, fields}}}  # 可省略

        meta 缺省时表格「几何类型 / 坐标系」列显示 —(行 tooltip 无字段清单)。
        服务器连接(``self._server_connections``)不受影响。

        列表顺序即展示顺序（**新的在上**，见 :meth:`add_connection`）——传进来的
        就是最终顺序，这里不再排序。"""
        self._databases = list(databases)
        self._rebuild_tree()
        self.databasesChanged.emit()

    def add_connection(self, db: dict) -> None:
        """新建连接成功后入树并联动: kind=local 挂「本地数据库」根下; kind=server 挂「服务器数据库」根下。

        **本地库插在最前面（新的在上）**: :attr:`_databases` 的顺序就是展示顺序
        —— 左树节点、创建数据的「目标库」下拉、导入对话框的「目标数据库」下拉
        都照这个顺序排；导出的工程状态也按这个顺序存，重启后顺序不变。
        用户刚建 / 刚导入的那个库永远在最上面（用户明确要的：最后建的放最上）。
        """
        conn = dict(db)
        if conn.get("kind") == "server":
            self._server_connections.append(conn)
            self._rebuild_tree()
            # 服务器根固定在第 1 个顶层节点; 刚加入的就是其最后一个子节点
            root = self.tree.topLevelItem(1)
            item = root.child(len(self._server_connections) - 1)
            self._select_node(root, item)
            self._show_server_connection(conn)
            # 服务器连接也属于「登记好的连接」，一并落盘（凭据由调用方决定存不存）
            self.databasesChanged.emit()
            return
        path = str(conn.get("path") or "")
        if path:
            with suppress(Exception):
                from app.core.domain.create_data.dict_tables import (
                    normalize_local_dict_names,
                )

                normalize_local_dict_names(path)
        self._databases.insert(0, conn)
        self._rebuild_tree()
        # 本地根固定在第 0 个顶层节点; 刚加入的就是其**第一个**子节点（新的在上）
        root = self.tree.topLevelItem(0)
        item = root.child(0)
        self._select_node(root, item)
        self._show_database(conn)
        self.databasesChanged.emit()

    def _refresh_db_cache(self, db: dict) -> bool:
        """从 db 文件实时重扫 → 写回 :attr:`_databases[i]["categories"/"counts"/"meta"]`。

        背景：:meth:`add_connection` / :meth:`restore_state` 一次性把
        :func:`describe_local_db` 的快照写进 db dict；后续导入 / 删除 / 外部
        变更不会自动同步。点「刷新」按钮 / :meth:`refresh_database` 走
        本方法拿最新快照。失败（库文件被占用、格式不是 gpkg/spatialite）→ 返回
        False，不动 db dict。 调用方决定是否调 :meth:`_show_database`。
        """
        path = db.get("path")
        if not path:
            return False
        try:
            p = Path(str(path))
            if not p.is_file():
                return False
            catalog = describe_local_db(str(path))
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "数据管理[刷新缓存]: 重扫本地库 %s 失败: %s", path, exc
            )
            return False
        # 老库把本地字典的逻辑名记成"复制来源的系统类别"；统一成库内表名，
        # 这样字段列表 / 挂接框里不会出现两个名字（重命名也会同步字段）。
        with suppress(Exception):
            from app.core.domain.create_data.dict_tables import (
                normalize_local_dict_names,
            )

            normalize_local_dict_names(str(path))
        db["categories"] = catalog["categories"]
        db["counts"] = catalog["counts"]
        db["meta"] = catalog["meta"]
        return True

    def refresh_database(self, db: dict) -> None:
        """库内容变更后重扫该库并重画（导入 / 删除 / 创建数据往库里加图层后用）。

        ``db`` 必须在 :attr:`_databases`（local）或 :attr:`_server_connections`
        （server）里——路径不识别则 no-op。``kind=local`` 时调 :meth:`_refresh_db_cache`
        重扫 db 文件 → 拿到最新的 ``user_relation`` 注册表 → 调
        :meth:`_show_database` 重画右表、:meth:`_rebuild_tree_for` 重建左树
        （**两边都要**：只重画右表的话，左树里看不到新加的那个图层）；
        ``kind=server`` 调 :meth:`_show_server_connection` 重画摘要（服务器库表
        目录不缓存，刷新不发起网络请求）。
        """
        kind = db.get("kind", "")
        if kind == "local":
            # 传进来的常常是本连接 dict 的**副本**（Qt 的 itemData / 对话框回传都会深拷
            # 一份），而缓存是挂在 _databases 里的原件上的 —— 不换回原件，重扫只会写进
            # 副本，左树按旧缓存重建、等于没刷新（见 _cache_db）。
            live = self._cache_db(db)
            if live is None:
                return
            # 失败 (文件被占用) 仍尝试重画旧缓存 → 让 UI 至少不丢
            self._refresh_db_cache(live)
            self._show_database(live)
            self._rebuild_tree_for(live)
            detail = self._current_detail
            if detail is not None and self._same_db(detail[0], live):
                self._show_detail(live, detail[1], detail[2])
            else:
                self._show_list_page()
        elif kind == "server" and db in self._server_connections:
            self._show_server_connection(db)

    def refresh_all(self) -> int:
        """刷新所有已注册的本地库 + 服务器连接——点「刷新」按钮触发。

        本地库走 :meth:`_refresh_db_cache` 重扫 db 文件并写回缓存，
        服务器连接只计数。 最后对 :attr:`_current_db`（如果还在
        :attr:`_databases` / :attr:`_server_connections` 里）调一次
        :meth:`_show_database` / :meth:`_show_server_connection` 重画右表，
        并重建左树（类别分组 / 图叶子都是重扫前的快照，见 :meth:`_rebuild_tree_for`）——
        避免全部重画后选中状态乱跳。 库文件重扫失败 → 缓存不变
        （记 warning），不影响其他 db 刷新。

        Returns:
            实际刷新的连接数（local + server 之和）。无连接时返回 0。
        """
        n = 0
        for db in list(self._databases):
            self._refresh_db_cache(db)
            n += 1
        for conn in list(self._server_connections):
            n += 1
        # 仅重画当前选中，保持选中状态不变
        if self._current_db is not None:
            if self._current_db in self._databases:
                self._show_database(
                    self._current_db, category=self._current_category
                )
            elif self._current_db in self._server_connections:
                self._show_server_connection(self._current_db)
        # 左树里「类别分组 → 图层叶子」和分组标题的计数都是重扫前的快照，
        # 库内容变了必须整体重建（见 _rebuild_tree_for）
        self._rebuild_tree_for()
        detail = self._current_detail
        live_detail = self._cache_db(detail[0]) if detail is not None else None
        if detail is not None and live_detail is not None:
            self._show_detail(live_detail, detail[1], detail[2])
        else:
            self._show_list_page()
        return n

    def _rebuild_tree_for(self, db: dict | None = None) -> None:
        """重建左树并把选中节点找回。

        **库内容一变就得重建**：:meth:`_refresh_db_cache` 只更新缓存、
        :meth:`_show_database` 只重画右表，左树的「矢量 (N) → 图层叶子」不重建
        就一直是旧的 —— 用户报的「往库里加完图层，数据管理里看不到，只有手动
        刷新才有」就是这里（其实手动刷新走的 :meth:`refresh_all` 当时也没重建）。

        ``db`` 指定要选中的库（找不到就退回 :attr:`_current_db`）：创建数据往某个
        库里加完图层后，用户要看的就是那个库。
        """
        target = db if db is not None else self._current_db
        # 重建会把展开状态全收起来 —— 刷新不该改变用户看到的形状，先存后还原
        expansion = self._expanded_snapshot()
        # 类别要**先**记下来：_rebuild_tree 的收尾联动会重画右表（把 _current_category
        # 冲掉），target 就是原来那个库时得把类别还回去（见下面的 _show_database）
        keep_category = (
            self._current_category if self._same_db(target, self._current_db) else None
        )
        self._rebuild_tree()
        self._restore_expansion(expansion)
        if target is None:
            return
        # 连接节点固定挂在作用域根下（见 _rebuild_tree_items），只在这一层找，
        # 不用 _find_item —— 类别分组 / 叶子节点的 payload 里也带 db，会先撞上
        for i in range(self.tree.topLevelItemCount()):
            root = self.tree.topLevelItem(i)
            for j in range(root.childCount()):
                child = root.child(j)
                payload = child.data(0, _ROLE)
                if isinstance(payload, dict) and self._same_db(payload.get("db"), target):
                    self._select_node(root, child)
                    # 右表也要切到 target：_rebuild_tree 的收尾联动把右表落在
                    # 「列表第一个库」（新的在上 = 最新的那个）上，只高亮不换表会
                    # 出现「高亮 A、右表显示 B」
                    self._show_database(target, category=keep_category)
                    return

    # ---- 展开状态（重建树时保住用户看到的形状）----

    @staticmethod
    def _item_path(item: QTreeWidgetItem) -> str:
        """库节点的标识（连接路径）；不是库节点返回空串。"""
        payload = item.data(0, _ROLE)
        if not isinstance(payload, dict):
            return ""
        return str((payload.get("db") or {}).get("path") or "")

    def _expanded_snapshot(self) -> tuple[set, set, set]:
        """取展开状态快照：``(展开的根 scope, 展开的库 path, 展开的类别(库path, 分组名))``。"""
        scopes: set = set()
        dbs: set = set()
        groups: set = set()
        for i in range(self.tree.topLevelItemCount()):
            root = self.tree.topLevelItem(i)
            scope = (root.data(0, _ROLE) or {}).get("scope")
            if root.isExpanded():
                scopes.add(scope)
            for j in range(root.childCount()):
                node = root.child(j)
                path = self._item_path(node)
                if not path:
                    continue
                if node.isExpanded():
                    dbs.add(path)
                for k in range(node.childCount()):
                    group = node.child(k)
                    if group.isExpanded():
                        groups.add((path, group.text(0)))
        return scopes, dbs, groups

    def _restore_expansion(self, snapshot: tuple[set, set, set]) -> None:
        """:meth:`_expanded_snapshot` 的还原端。"""
        scopes, dbs, groups = snapshot
        for i in range(self.tree.topLevelItemCount()):
            root = self.tree.topLevelItem(i)
            if (root.data(0, _ROLE) or {}).get("scope") in scopes:
                root.setExpanded(True)
            for j in range(root.childCount()):
                node = root.child(j)
                path = self._item_path(node)
                if not path:
                    continue
                if path in dbs:
                    node.setExpanded(True)
                for k in range(node.childCount()):
                    group = node.child(k)
                    if (path, group.text(0)) in groups:
                        group.setExpanded(True)

    def remove_registered_node(self, db: dict, category: str, name: str) -> None:
        """删除成功后从缓存摘掉该注册项并重建左树(尽量保持选中该库)。

        走缓存改动而非重扫库文件: 删表刚由 MainWindow 在库里做完, 结果确定,
        重开库文件只是把同一件事再读一遍。``db`` 可能是树节点 payload 里的
        **副本**(见 :meth:`_same_db`), 先换回 :attr:`_databases` 里的原件再改;
        换不回(服务器库 / 已被替换的连接) → no-op, 只当没这个节点。
        """
        target = self._cache_db(db)
        if target is None:
            return
        cats = target.get("categories")
        if isinstance(cats, dict) and isinstance(cats.get(category), list):
            with suppress(ValueError):
                cats[category].remove(name)
        for key in ("counts", "meta"):
            mapping = target.get(key)
            if isinstance(mapping, dict):
                mapping.pop(name, None)
        self._rebuild_tree()
        self._select_db_node(target)
        self._show_database(target, category=category)

    # ------------------------------------------------------------ 工程持久化

    def export_state(self) -> dict:
        """导出连接状态供写入工程文件（:meth:`restore_state` 可还原）。

        本地库只存 ``name`` / ``path``（分类、行数、元信息下次打开重新扫，
        避免文件变动后展示陈旧数据）；服务器连接整条存下（含凭据，以便
        下次直接可用）。
        """
        return {
            "local": [
                {"name": self._db_title(db), "path": str(db.get("path") or "")}
                for db in self._databases
                if db.get("path")
            ],
            "server": [dict(c) for c in self._server_connections],
        }

    def restore_state(self, state: dict | None) -> None:
        """用工程里保存的连接状态整体替换当前列表（本地库重新扫描）。

        文件不存在的本地库 / 读取失败的库跳过并记日志；全部缺失时树回到空
        状态。服务器连接原样恢复。
        """
        state = state or {}
        databases: list[dict] = []
        for item in state.get("local") or []:
            if not isinstance(item, dict):
                continue
            path = str(item.get("path") or "")
            if not path:
                continue
            if not Path(path).is_file():
                logger.warning("数据管理[恢复]: 本地库文件不存在, 跳过: %s", path)
                continue
            try:
                catalog = describe_local_db(path, with_counts=False)
            except Exception as exc:  # noqa: BLE001
                logger.warning("数据管理[恢复]: 读取本地库失败, 跳过: %s (%s)", path, exc)
                continue
            with suppress(Exception):
                from app.core.domain.create_data.dict_tables import (
                    normalize_local_dict_names,
                )

                normalize_local_dict_names(path)
            databases.append(
                {
                    "kind": "local",
                    "name": str(item.get("name") or Path(path).stem),
                    "path": path,
                    "categories": catalog["categories"],
                    "counts": catalog["counts"],
                    "meta": catalog["meta"],
                }
            )
        self._databases = databases
        self._server_connections = [
            dict(c) for c in (state.get("server") or []) if isinstance(c, dict)
        ]
        self._rebuild_tree()
        self._schedule_counts()

    # ------------------------------------------------------ 行数后台补齐

    def _schedule_counts(self) -> None:
        """目录/元信息先上屏，行数交给后台线程（百万行库 COUNT(*) 很慢）。

        ``restore_state`` 走 ``describe_local_db(with_counts=False)`` 秒开窗口；
        这里按库起一个 daemon 线程，统计完通过 ``counts_ready`` 回主线程补标签。
        """
        for db in self._databases:
            path = str(db.get("path") or "")
            if not path or path in self._counting_paths:
                continue
            names = [
                name
                for cat in _CATEGORY_ORDER
                for name in (db.get("categories") or {}).get(cat, [])
            ]
            if not names:
                continue
            self._counting_paths.add(path)
            threading.Thread(
                target=self._count_rows_worker,
                args=(path, names),
                name="data-manage-counts",
                daemon=True,
            ).start()

    def _count_rows_worker(self, path: str, names: list[str]) -> None:
        """后台线程体：只做 SQL + 发信号，绝不碰控件。"""
        try:
            counts = count_local_db_rows(path, names)
        except Exception:  # noqa: BLE001 —— 统计失败不影响目录展示
            logger.warning("数据管理[行数统计] 失败: %s", path, exc_info=True)
            counts = {name: None for name in names}
        with suppress(RuntimeError):
            self.counts_ready.emit(path, counts)

    def _on_counts_ready(self, path: str, counts: object) -> None:
        """主线程：把后台算出的行数写回库 dict，并就地刷新标签 / 右表。"""
        self._counting_paths.discard(path)
        db = next(
            (d for d in self._databases if str(d.get("path") or "") == path),
            None,
        )
        if db is None or not isinstance(counts, dict):
            return
        db["counts"] = counts
        self._refresh_count_labels(db)
        if self._current_db is db:
            self._show_database(db, category=self._current_category)

    def _refresh_count_labels(self, db: dict) -> None:
        """只改叶子节点的行数列，不重建整棵树（保留展开/选中状态）。

        目录刚建时 counts 是空的，``_apply_tree_theme`` 末尾会把行数列宽
        ``resizeColumnToContents`` 成极小值；后台补上行数后必须**重新算列宽**，
        否则新文字会被省略成 ``...``（用户看到的"数量一直不出来"）。
        同时补上行数专用的小字号 / 次要色 / 右对齐（初建时没有文字，样式没套上）。
        """
        counts = db.get("counts") or {}
        theme = ThemeManager.instance().current()
        base = QFont(self.tree.font())
        counts_font = QFont(base)
        counts_font.setPointSizeF(max(6.0, base.pointSizeF() - _COUNT_FONT_DELTA))
        secondary = QBrush(QColor(theme.text_secondary))
        root = self.tree.topLevelItem(0)  # 「本地数据库」固定在顶层第 0 个
        if root is None:
            return
        for i in range(root.childCount()):
            conn_item = root.child(i)
            payload = conn_item.data(0, _ROLE)
            payload_db = (payload or {}).get("db") if isinstance(payload, dict) else None
            if not isinstance(payload_db, dict) or str(
                payload_db.get("path") or ""
            ) != str(db.get("path") or ""):
                continue
            for j in range(conn_item.childCount()):
                group = conn_item.child(j)
                group_payload = group.data(0, _ROLE)
                cat = (group_payload or {}).get("cat")
                for k in range(group.childCount()):
                    leaf = group.child(k)
                    leaf_payload = leaf.data(0, _ROLE)
                    name = (leaf_payload or {}).get("name")
                    count = counts.get(name)
                    if count is None:
                        continue
                    leaf.setText(1, f"{count} {_CATEGORY_UNIT.get(cat, '')}")
                    leaf.setFont(1, counts_font)
                    leaf.setForeground(1, secondary)
                    leaf.setTextAlignment(
                        1, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
                    )
            # 列宽按新内容重算一次（放在最后，所有叶子都填完）
            self.tree.resizeColumnToContents(1)
            return

    @staticmethod
    def _select_node(root: QTreeWidgetItem, item: QTreeWidgetItem) -> None:
        """展开根并选中节点(只改高亮, 不触发 itemClicked 信号)."""
        root.setExpanded(True)
        item.setExpanded(True)
        root.treeWidget().setCurrentItem(item)

    def _make_root(self, title: str, tooltip: str, scope: str) -> QTreeWidgetItem:
        """构造作用域根节点; payload 记作用域(local/server), 点击只显示根名.

        字重 / 颜色 / 图标由 :meth:`_apply_tree_theme` 统一刷。
        """
        root = QTreeWidgetItem()
        root.setText(0, title)
        root.setToolTip(0, tooltip)
        root.setData(0, _ROLE, {"scope": scope})
        return root

    def _rebuild_tree(self) -> None:
        """重建左树: 固定两棵根节点「本地数据库」/「服务器数据库」,
        本地/服务器连接作为各自根节点的子节点(连接内按类别分级)。

        重建期间关掉重绘(大库上千节点, 逐条插入会反复触发重排/重绘), 建完
        再统一配色并打开重绘。
        """
        self.tree.setUpdatesEnabled(False)
        try:
            self._rebuild_tree_items()
        finally:
            self.tree.setUpdatesEnabled(True)
        self._apply_tree_theme()

    def _rebuild_tree_items(self) -> None:
        """往树上灌节点(不含配色与重绘开关, 见 :meth:`_rebuild_tree`)。"""
        self.tree.clear()

        # 本地数据库根节点
        local_root = self._make_root(
            "本地数据库", "本地数据库连接 (SQLite / GeoPackage)", "local"
        )
        for db in self._databases:
            top = QTreeWidgetItem(local_root)
            top.setText(0, self._db_title(db))
            top.setToolTip(0, str(db.get("path", "")))
            top.setData(0, _ROLE, {"db": db})
            self._append_category_groups(top, db)
        self.tree.addTopLevelItem(local_root)

        # 服务器数据库根节点: 服务器连接(PostgreSQL/Oracle/SQL Server)挂在下面。
        # **先隐藏**（见 _SHOW_SERVER_DB）：服务器版未接入，连接清单仍留在状态文件里。
        server_root = None
        if _SHOW_SERVER_DB:
            server_root = self._make_root(
                "服务器数据库",
                "服务器数据库连接 (PostgreSQL / Oracle / SQL Server)",
                "server",
            )
            for conn in self._server_connections:
                child = QTreeWidgetItem(server_root)
                child.setText(0, self._server_title(conn))
                child.setToolTip(0, self._server_tooltip(conn))
                child.setData(0, _ROLE, {"db": conn})
            self.tree.addTopLevelItem(server_root)

        self._expand_tree()

        # 收尾联动: 有本地库选第一个本地连接; 只有服务器连接选最后一个;
        # 都没有则显示空提示
        if self._databases:
            first = local_root.child(0)
            self._select_node(local_root, first)
            self._show_database(self._databases[0])
        elif server_root is not None and self._server_connections:
            last = server_root.child(len(self._server_connections) - 1)
            self._select_node(server_root, last)
            self._show_server_connection(self._server_connections[-1])
        else:
            self._show_database(None)

    def _expand_tree(self) -> None:
        """展开全部根 / 连接 / 类别分组节点.

        必须在节点挂到树上之后调 —— ``QTreeWidgetItem.setExpanded()`` 对
        **尚未插入视图** 的节点(含只挂在未挂载父节点下的子孙)是静默 no-op,
        只有顶层节点会被记录。之前在建节点时逐条 ``setExpanded(True)``,
        于是除首个连接外, 所有连接/类别分组都以折叠态出现, 看着像空壳,
        用户得挨个手点展开。
        """
        for i in range(self.tree.topLevelItemCount()):
            root = self.tree.topLevelItem(i)
            root.setExpanded(True)
            for j in range(root.childCount()):
                conn = root.child(j)
                conn.setExpanded(True)  # 本地: 展开「矢量/字典/影像」分组
                for k in range(conn.childCount()):
                    conn.child(k).setExpanded(True)

    def _append_category_groups(self, top: QTreeWidgetItem, db: dict) -> None:
        """给连接节点按 spatial/dict/image 追加分级子节点(空类别不显示)."""
        cats: dict = db.get("categories") or {}
        counts: dict = db.get("counts") or {}
        for cat in _CATEGORY_ORDER:
            names = list(cats.get(cat) or [])
            # 字典分组即使为空也显示：用户要在里面「添加字典表」
            if not names and cat != "dict":
                continue
            group = QTreeWidgetItem(top)
            group.setText(0, f"{_CATEGORY_LABEL[cat]} ({len(names)})")
            group.setData(0, _ROLE, {"db": db, "cat": cat})
            for name in names:
                leaf = QTreeWidgetItem(group)
                leaf.setText(0, name)
                count = counts.get(name)
                if count is not None:
                    leaf.setText(1, f"{count} {_CATEGORY_UNIT[cat]}")
                leaf.setData(0, _ROLE, {"db": db, "cat": cat, "name": name})

    # ------------------------------------------------------------ 样式

    @staticmethod
    def _payload_kind(payload: dict) -> str | None:
        """payload → 节点层级键(见 :data:`_TREE_ICONS` / :data:`_BOLD_KINDS`)。"""
        if payload.get("scope"):
            return f"root_{payload['scope']}"
        if "name" in payload:
            return "leaf"
        if "cat" in payload:
            return str(payload["cat"])
        db = payload.get("db")
        if isinstance(db, dict):
            return "conn_server" if db.get("kind") == "server" else "conn_local"
        return None

    def _apply_tree_theme(self) -> None:
        """按当前主题重刷全树节点(字重 / 颜色 / 图标 + 「新建连接」按钮图标)。

        QSS 选不中单个 item, 节点级配色只能走 ``setFont`` / ``setForeground`` /
        ``setIcon``; 图标是 QPainter 画的、颜色烧在 PNG 里, 所以切主题要整树重刷。
        """
        theme = ThemeManager.instance().current()
        text = QColor(theme.menu_text)
        secondary = QColor(theme.text_secondary)
        base = QFont(self.tree.font())
        counts_font = QFont(base)
        counts_font.setPointSizeF(max(6.0, base.pointSizeF() - _COUNT_FONT_DELTA))

        def visit(item: QTreeWidgetItem) -> None:
            payload = item.data(0, _ROLE)
            kind = self._payload_kind(payload) if isinstance(payload, dict) else None
            font = QFont(base)
            font.setBold(kind in _BOLD_KINDS)
            item.setFont(0, font)
            # 类别分组是容器节点, 用次要色退到背景里, 让库连接与数据叶子更抢眼
            item.setForeground(0, QBrush(secondary if kind in _CATEGORY_ORDER else text))
            icon_key = _TREE_ICONS.get(kind or "")
            item.setIcon(0, tool_icons.icon(icon_key, theme.menu_text) if icon_key else QIcon())
            if item.text(1):
                # 右列行数: 小一号 + 次要色, 右对齐成"徽标"感
                item.setFont(1, counts_font)
                item.setForeground(1, QBrush(secondary))
                item.setTextAlignment(
                    1, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
                )
            for i in range(item.childCount()):
                visit(item.child(i))

        for i in range(self.tree.topLevelItemCount()):
            visit(self.tree.topLevelItem(i))
        # 行数列宽度按内容算一次(字号刚被刷过, 必须放在最后)
        self.tree.resizeColumnToContents(1)
        self.add_btn.setIcon(tool_icons.icon("plus_circle", theme.input_focus))

    def _on_theme_changed(self, _theme=None) -> None:
        """主题切换: 重刷树节点配色与图标。"""
        self._apply_tree_theme()

    @staticmethod
    def _db_title(db: dict) -> str:
        """节点标题: 有 name 用 name, 否则取文件名的 stem."""
        return str(db.get("name") or Path(str(db.get("path", ""))).stem or "数据库")

    @staticmethod
    def _server_title(conn: dict) -> str:
        """服务器连接标题: "<类型标签> · <host>:<port>/<db>"."""
        label = str(conn.get("db_type_label") or conn.get("db_type") or "服务器")
        name = str(conn.get("name") or "")
        if not name and conn.get("host"):
            name = f"{conn.get('host')}:{conn.get('port', '')}/{conn.get('database', '')}"
        return f"{label} · {name}" if name else label

    @staticmethod
    def _server_tooltip(conn: dict) -> str:
        """服务器连接悬停提示: 逐行列出连接要素。"""
        lines = [
            f"数据库类型: {conn.get('db_type_label') or conn.get('db_type') or '-'}",
            f"主机: {conn.get('host') or '-'}",
            f"端口: {conn.get('port') or '-'}",
            f"数据库名: {conn.get('database') or '-'}",
            f"用户名: {conn.get('username') or '-'}",
        ]
        return "\n".join(lines)

    @staticmethod
    def _rows(db: dict, category: str | None = None) -> list[dict]:
        """展平某库(可按类别过滤)的注册数据行: [{name, category, count, meta}]。"""
        cats: dict = db.get("categories") or {}
        counts: dict = db.get("counts") or {}
        metas: dict = db.get("meta") or {}
        out: list[dict] = []
        for cat in _CATEGORY_ORDER:
            if category is not None and cat != category:
                continue
            for name in cats.get(cat) or []:
                out.append(
                    {
                        "name": name,
                        "category": cat,
                        "count": counts.get(name),
                        "meta": metas.get(name) or {},
                    }
                )
        return out

    def _show_database(self, db: dict | None, *, category: str | None = None) -> None:
        """联动右侧面板: 更新标题并重建数据表 (db=None 显示空提示).

        记下当前选中的库 + 类别，供 :meth:`refresh_all` 末尾重画时保持
        选中状态不变（避免全部本地库重扫后 UI 跳到最后一个 db）。
        """
        self._current_db = db
        self._current_category = category
        add_dict = getattr(self, "add_dict_btn", None)
        if add_dict is not None:
            add_dict.setVisible(db is not None and category == "dict")
        if db is None:
            self.sel_name.setText("未连接本地数据库")
            self.sel_name.setToolTip("")
            self.table.setRowCount(0)
            return
        self.sel_name.setText(self._db_title(db))
        self.sel_name.setToolTip(str(db.get("path", "")))
        rows = self._rows(db, category)
        self.table.setRowCount(0)
        for row in rows:
            r = self.table.rowCount()
            self.table.insertRow(r)
            cat_label = _CATEGORY_LABEL.get(row["category"], row["category"])
            meta = row["meta"]
            geom_text = meta.get("geom_label") or "—"
            crs_text = meta.get("crs") or "—"
            count = row["count"]
            desc = (
                "—"
                if count is None
                else f"{count} {_CATEGORY_UNIT.get(row['category'], '')}"
            )
            name_item = QTableWidgetItem(row["name"])
            name_item.setData(_ROW_TAG_ROLE, f"{row['category']}\x00{row['name']}")
            tip = self._row_tooltip(
                row["name"], cat_label, geom_text, crs_text, desc, meta
            )
            items = (
                name_item,
                QTableWidgetItem(cat_label),
                QTableWidgetItem(geom_text),
                QTableWidgetItem(crs_text),
                QTableWidgetItem(desc),
            )
            for item in items:
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                item.setToolTip(tip)
            for col, item in enumerate(items):
                self.table.setItem(r, col, item)
        self._fill_available_width()

    @staticmethod
    def _row_tooltip(
        name: str,
        cat_label: str,
        geom_text: str,
        crs_text: str,
        desc: str,
        meta: dict,
    ) -> str:
        """表格行悬停提示: 基本项 + 字段列表(几何列标注; 超长截断)。"""
        lines = [
            f"数据名称: {name}",
            f"类型: {cat_label}",
            f"几何类型: {geom_text}",
            f"坐标系: {crs_text}",
            f"说明: {desc}",
        ]
        geom_col = meta.get("geom_col")
        fields = list(meta.get("fields") or [])
        if fields:
            total = len(fields)
            lines.append(f"字段 ({total}):")
            for col, typ in fields[:30]:
                marker = ""
                if geom_col and col.lower() == str(geom_col).lower():
                    marker = "  ← 几何列"
                lines.append(f"  {col}: {typ}{marker}")
            if total > 30:
                lines.append(f"  … 其余 {total - 30} 个字段")
        return "\n".join(lines)

    # ------------------------------------------------------------ 交互

    def _show_server_connection(self, conn: dict) -> None:
        """选中服务器连接: 右侧标题/摘要展示(表目录浏览待后续版本接入)."""
        self._current_db = None
        self._current_category = None
        self.table.setRowCount(0)
        self.sel_name.setText(self._server_title(conn))
        self.sel_name.setToolTip(self._server_tooltip(conn))

    def _on_tree_item_clicked(self, item: QTreeWidgetItem, column: int) -> None:
        payload = item.data(0, _ROLE)
        if not isinstance(payload, dict):
            return
        scope = payload.get("scope")
        if scope:
            # 根节点(本地数据库 / 服务器数据库): 只显示根名, 数据区清空
            self._show_list_page()
            self._current_db = None
            self._current_category = None
            self.table.setRowCount(0)
            self.sel_name.setText(
                "本地数据库" if scope == "local" else "服务器数据库"
            )
            self.sel_name.setToolTip("")
            return
        db = payload.get("db")
        if "name" in payload and isinstance(payload.get("name"), str) and db:
            # 表名叶子: 联动所属库的该类别, 并高亮表格对应行；矢量/字典叶子进详情页
            self._show_database(db, category=payload["cat"])
            self._show_detail(db, payload["cat"], payload["name"])
            tag = f"{payload['cat']}\x00{payload['name']}"
            for r in range(self.table.rowCount()):
                it = self.table.item(r, 0)
                if it is not None and it.data(_ROW_TAG_ROLE) == tag:
                    self.table.selectRow(r)
                    return
            return
        if "cat" in payload and db:
            # 类别分组节点: 只显示该类数据
            self._show_list_page()
            self._show_database(db, category=payload["cat"])
            return
        if isinstance(db, dict):
            # 数据库节点(本地连接 / 服务器分组/连接): 本地显示全部, 服务器显示摘要
            self._show_list_page()
            if db.get("kind") == "server":
                self._show_server_connection(db)
                return
            self._show_database(db)

    # ------------------------------------------------------------ 右栏详情页

    def _show_list_page(self) -> None:
        self._current_detail = None
        stack = getattr(self, "stack", None)
        if stack is not None:
            stack.setCurrentIndex(0)

    def _show_detail(self, db: dict, category: str, name: str) -> None:
        if category not in ("spatial", "dict"):
            self._show_list_page()
            return
        live = self._cache_db(db) or db
        self._current_detail = (live, category, name)
        self.detail_page.show_table(live, category, name)
        self.stack.setCurrentWidget(self.detail_page)

    def _before_table_change(self, db: dict, category: str, name: str) -> bool:
        callback = self._on_before_table_change
        if callback is None:
            return True
        try:
            return bool(callback(db, category, name))
        except Exception:  # noqa: BLE001 —— 回调失败按「不允许改」处理
            logger.exception("字段结构变更前回调失败")
            return False

    def _on_dict_domains_deleted_signal(
        self, db: dict, category: str, table: str, domains: object
    ) -> None:
        """字典域整域删除 → 交回调处理（清已打开图层上的属性）。"""
        callback = self._on_dict_domains_deleted
        if callback is None:
            return
        try:
            callback(db, category, table, list(domains or []))
        except Exception:  # noqa: BLE001 —— 槽内异常会 native 崩溃
            logger.exception("字典域删除后的回调失败")

    def _on_detail_data_changed(self, category: str, name: str) -> None:
        detail = self._current_detail
        if detail is None:
            return
        db, _cat, _table = detail
        live = self._cache_db(db)
        if live is None:
            return
        self._refresh_db_cache(live)
        self._show_database(live, category=category)
        self._rebuild_tree_for(live)
        self._show_detail(live, category, name)

    # ------------------------------------------------------------ 右键菜单

    def _on_tree_context_menu(self, pos) -> None:
        """树节点右键 → 上下文菜单(项按节点层级显隐, 见 :meth:`_build_context_menu`)。

        空白处(没点中节点)不弹: 菜单项都针对具体节点, 弹出来用户不知道动作
        落在谁身上。右键同时把该节点设为当前项, 先高亮"这一行"再选动作。
        """
        item = self.tree.itemAt(pos)
        if item is None:
            return
        payload = item.data(0, _ROLE)
        if not isinstance(payload, dict):
            return
        self.tree.setCurrentItem(item)
        menu = self._build_context_menu(payload)
        menu.exec(self.tree.viewport().mapToGlobal(pos))
        # 每次右键都是新菜单: exec 返回后立刻交还给事件循环销毁, 不然会以
        # Dock 为 parent 一直攒着。
        menu.deleteLater()

    def _build_context_menu(self, payload: dict) -> QMenu:
        """按节点层级组装右键菜单(显隐规则见模块 docstring)。

        菜单外观(QMenu / QMenu::item / ::separator)由全局 QSS 统一管, 这里
        只给文案与图标; 图标是 QPainter 画的、颜色烧在 PNG 里, 取当前主题的
        主文字色(与左树节点图标同源), 主题切换时下次右键自然是新颜色。
        """
        theme = ThemeManager.instance().current()
        kind = self._payload_kind(payload)
        # payload 里的 db 是 QVariantMap 深拷出来的副本(见 _same_db): 动作要改
        # 缓存时得拿原件, 先换回 _databases 里的整条; 服务器连接换不回, 留副本
        raw_db = payload.get("db")
        raw_db = raw_db if isinstance(raw_db, dict) else None
        db = self._cache_db(raw_db) or raw_db
        #: 节点专属项(显隐规则) + 通用项「刷新」拼成一份菜单
        head: list[tuple[str, str, Callable[[], None]]] = []
        if payload.get("scope"):
            # 作用域根(本地 / 服务器数据库): 库里还没东西, 只有"添连接"说得通。
            # **把 scope 带下去**：在「服务器数据库」上右键，弹窗就该默认停在服务器页
            scope = str(payload.get("scope") or "local")
            head.append(
                (
                    "添加数据库连接",
                    "plus_circle",
                    # 第一个参数必须吃掉 triggered 带来的 checked(bool)，否则它会顶掉
                    # 默认值 scope（`lambda s=scope:` 会收到 s=False，scope 永远变 local）
                    lambda _checked=False, s=scope: self._on_add_connection_clicked(s),
                )
            )
        elif kind == "dict" and db is not None:
            head.append(
                (
                    "添加字典表",
                    "plus_circle",
                    lambda: self._add_dict_table(db),
                )
            )
            has_dicts = bool((db.get("categories") or {}).get("dict"))
            head.append(
                (
                    "移除未使用字典",
                    "trash",
                    lambda: self._request_remove_unused_dicts(db),
                    has_dicts,
                )
            )
        elif kind == "leaf" and db is not None:
            # 注册数据叶子: 加到地图 / 删这张表 / 导入数据 / 导出数据
            name = str(payload.get("name") or "")
            cat = str(payload.get("cat") or "")
            # 空间 / 影像能直接上图（字典表没有几何，不显示这条）
            if cat in ("spatial", "image"):
                head.append(
                    (
                        "加到地图上展示",
                        "cat_image" if cat == "image" else "cat_vector",
                        lambda: self._request_layer_action(
                            "加到地图上展示", self._on_add_layer, db, cat, name
                        ),
                    )
                )
            if cat == "spatial":
                head.append(
                    (
                        "导出字段结构(XLS)",
                        "export_csv",
                        lambda: self._export_field_structure(db, name),
                    )
                )
                head.append(
                    (
                        "检查字段结构",
                        "quality",
                        lambda: self._check_field_structure(db, name),
                    )
                )
            if cat in ("spatial", "dict"):
                head.append(
                    ("编辑名称", "edit_settings", lambda: self._rename_leaf(db, cat, name))
                )
            head.append(
                ("删除图层", "trash", lambda: self._request_delete(db, cat, name))
            )
            if cat == "spatial":
                head.append(
                    ("导入数据", "upload", lambda: self._request_layer_action(
                        "导入数据", self._on_import_data, db, cat, name))
                )
                head.append(
                    ("导出数据", "export_csv", lambda: self._request_layer_action(
                        "导出数据", self._on_export_layer, db, cat, name))
                )
        elif db is not None and "cat" not in payload:
            # 库连接节点: 导入图层 / 创建数据 / 导入数据 / 删除连接 / 打开文件所在位置
            head.append(
                # **从库节点右键进来：目标库锁定为这个库**（locked=True），
                # 从右侧面板的「导入」按钮进来则可自由选。
                ("导入图层", "upload", lambda: self._fire(
                    "导入图层", self._on_import, db, True))
            )
            if db.get("kind") == "local":
                head.append(
                    ("创建数据", "plus_circle", lambda: self._fire(
                        "创建数据", self._on_create_data, db))
                )
                head.append(
                    ("打开文件所在位置", "folder_open", lambda: reveal_in_file_manager(
                        str(db.get("path") or "")))
                )
                # 库里一个注册图层都没有时没什么可移除的，不弹这条（避免点了空转）
                if self._registered_count(db):
                    head.append(
                        ("移除全部图层", "trash",
                         lambda: self._request_remove_all_layers(db))
                    )
                head.append(
                    ("删除连接", "trash", lambda: self._request_remove_connection(db))
                )

        menu = QMenu(self)
        for entry in head:
            label, icon_key, slot = entry[:3]
            enabled = bool(entry[3]) if len(entry) > 3 else True
            act = QAction(tool_icons.icon(icon_key, theme.menu_text), label, menu)
            act.setEnabled(enabled)
            act.triggered.connect(slot)
            menu.addAction(act)
        if head:
            menu.addSeparator()
        refresh = QAction(tool_icons.icon("refresh", theme.menu_text), "刷新", menu)
        # 根节点没有 db → 刷新全部; 其余节点刷新所属库
        refresh.triggered.connect(lambda: self._on_tree_refresh(db))
        menu.addAction(refresh)
        return menu

    def _request_delete(self, db: dict, category: str, name: str) -> None:
        """菜单「删除图层」: 交回调(MainWindow 确认后删库内表 + 注册 + 工程图层)。

        未接回调(单独用 Dock)只记日志 —— 删除是破坏性操作, Dock 不自己动手。
        """
        if not category or self._on_delete_layer is None:
            logger.debug("数据管理[删除图层]: 未接回调, %s/%s", category, name)
            return
        try:
            self._on_delete_layer(db, category, name)
        except Exception as exc:  # 槽内异常会 native 崩溃
            logger.error("数据管理[删除图层]回调异常: %s", exc, exc_info=True)

    def _rename_leaf(self, db: dict, category: str, name: str) -> None:
        """叶子右键「编辑名称」：改库内表名，并让已打开图层 / 详情页跟上。"""
        new_name, ok = QInputDialog.getText(
            self, "编辑名称", "请输入新的表名：", text=name
        )
        if not ok:
            return
        new_name = str(new_name).strip()
        if not new_name or new_name == name:
            return
        live = self._cache_db(db) or db
        if self._on_rename_table is not None:
            try:
                ok, error = self._on_rename_table(live, category, name, new_name)
            except Exception as exc:  # noqa: BLE001 —— 槽内异常会 native 崩溃
                logger.error("数据管理[编辑名称]回调异常: %s", exc, exc_info=True)
                ok, error = False, str(exc)
        else:
            from app.core.domain.local_table_edit import (
                LocalTableError,
                rename_local_table,
            )

            try:
                rename_local_table(str(live.get("path") or ""), name, new_name)
                ok, error = True, ""
            except LocalTableError as exc:
                ok, error = False, str(exc)
        if not ok:
            QMessageBox.warning(self, "编辑名称", error or "重命名失败")
            return
        self._current_detail = None
        self.refresh_database(live)
        self._show_detail(live, category, new_name)
        self._select_leaf(live, category, new_name)

    def _select_leaf(self, db: dict, category: str, name: str) -> None:
        """重建树后把刚改名的叶子重新选中。"""
        stack = [
            self.tree.topLevelItem(i) for i in range(self.tree.topLevelItemCount())
        ]
        while stack:
            item = stack.pop()
            payload = item.data(0, _ROLE)
            if (
                isinstance(payload, dict)
                and payload.get("cat") == category
                and payload.get("name") == name
                and self._same_db(payload.get("db"), db)
            ):
                self.tree.setCurrentItem(item)
                self.tree.scrollToItem(item)
                return
            stack.extend(item.child(i) for i in range(item.childCount()))

    def _on_add_dict_table_clicked(self) -> None:
        db = self._current_db
        if db is not None and self._current_category == "dict":
            self._add_dict_table(db)

    def _add_dict_table(self, db: dict) -> None:
        """在当前本地库新建字典表（可复制应用级字典表的整表内容）。"""
        live = self._cache_db(db) or db
        path = str(live.get("path") or "")
        if not path:
            QMessageBox.warning(self, "添加字典表", "缺少库文件路径")
            return
        from app.core.domain.create_data.dict_tables import (
            DictTableError,
            create_local_dict_table,
        )
        from app.core.domain.dict_manage import DictService
        from app.core.domain.import_layer import unique_table_name
        from app.ui.dialogs.add_local_dict import AddLocalDictDialog

        try:
            service = DictService()
            categories = [item.name for item in service.list_categories()]
        except Exception:  # noqa: BLE001 —— 字典管理读不到时只允许建空表
            service = None
            categories = []
        suggested = unique_table_name(path, "字典表")
        dialog = AddLocalDictDialog(
            self, suggested_name=suggested, categories=categories
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            dialog.deleteLater()
            return
        name = dialog.table_name()
        source = dialog.source_category()
        dialog.deleteLater()
        try:
            count = create_local_dict_table(
                path, name, source_category=source, dict_service=service
            )
        except DictTableError as exc:
            QMessageBox.warning(self, "添加字典表", str(exc))
            return
        self.refresh_database(live)
        self._select_leaf(live, "dict", name)
        self._show_detail(live, "dict", name)
        QMessageBox.information(
            self,
            "添加字典表",
            f"已创建字典表「{name}」" + (f"，复制 {count} 条字典项" if count else "（空表）"),
        )

    def _export_field_structure(self, db: dict, name: str) -> None:
        """叶子右键「导出字段结构(XLS)」：导出 9 列模板格式的 .xls。"""
        live = self._cache_db(db) or db
        path = str(live.get("path") or "")
        target = save_file(
            self,
            title="导出字段结构",
            init_path=str(Path.home() / f"{name}_字段结构.xls"),
            filter_="Excel 97-2003 工作簿 (*.xls);;所有文件 (*)",
        )
        if not target:
            return
        from app.core.domain.field_structure import (
            FieldStructureError,
            export_field_structure_xls,
        )

        try:
            written, count = export_field_structure_xls(target, path, name)
        except FieldStructureError as exc:
            QMessageBox.warning(self, "导出字段结构", str(exc))
            return
        QMessageBox.information(
            self,
            "导出字段结构",
            f"已导出 {count} 个字段到：\n{written}",
        )

    def _check_field_structure(self, db: dict, name: str) -> None:
        """叶子右键「检查字段结构」：检查字段规范 / 字典缺失 / 字典值是否匹配。"""
        live = self._cache_db(db) or db
        path = str(live.get("path") or "")
        from app.core.domain.dict_manage import DictService
        from app.core.domain.field_structure import check_field_structure

        try:
            issues = check_field_structure(path, name, dict_service=DictService())
        except Exception as exc:  # noqa: BLE001 —— 槽内异常会 native 崩溃
            logger.error("字段结构检查失败：%s", exc, exc_info=True)
            QMessageBox.warning(self, "检查字段结构", f"检查失败：{exc}")
            return
        if not issues:
            QMessageBox.information(self, "检查字段结构", "字段结构检查通过")
            return
        from app.ui.common.modeless import show_modeless
        from app.ui.dialogs.field_check_dialog import FieldCheckDialog

        show_modeless(
            self,
            ("field_check", path, name),
            lambda: FieldCheckDialog(self, name, issues),
        )

    def _request_remove_unused_dicts(self, db: dict) -> None:
        """字典分组右键「移除未使用字典」：交回调（确认 + 删表由 MainWindow 做）。"""
        if self._on_remove_unused_dicts is None:
            logger.debug("数据管理[移除未使用字典]: 未接回调, %s", db.get("name"))
            return
        try:
            self._on_remove_unused_dicts(db)
        except Exception as exc:  # noqa: BLE001 —— 槽内异常会 native 崩溃
            logger.error("数据管理[移除未使用字典]回调异常: %s", exc, exc_info=True)

    def _request_remove_all_layers(self, db: dict) -> None:
        """菜单「移除全部图层」: 交回调(MainWindow 确认后批量删库内表 + 工程图层)。

        与「删除图层」同一套守卫：未接回调只记日志，破坏性操作 Dock 不自己动手。
        """
        if self._on_remove_all_layers is None:
            logger.debug("数据管理[移除全部图层]: 未接回调, %s", db.get("name"))
            return
        try:
            self._on_remove_all_layers(db)
        except Exception as exc:  # noqa: BLE001 —— 槽内异常会 native 崩溃
            logger.error("数据管理[移除全部图层]回调异常: %s", exc, exc_info=True)

    @staticmethod
    def _registered_count(db: dict) -> int:
        """该库注册数据的总条数（各类别表名之和），用来决定菜单项是否出现。"""
        categories = db.get("categories") or {}
        return sum(len(names or []) for names in categories.values())

    def _request_layer_action(
        self,
        label: str,
        callback: Callable[[dict, str, str], None] | None,
        db: dict,
        category: str,
        name: str,
    ) -> None:
        """「导入数据 / 导出数据」这类图层级动作：统一做回调守卫与异常兜底。

        未接回调（单独用 Dock）只记日志；回调里抛异常也不能冒到 Qt 槽外 —— 那会把
        进程直接带走（见项目里那条「PyQt6 槽里抛异常直接杀进程」的经验）。
        """
        if callback is None:
            logger.debug("数据管理[%s]: 未接回调, %s/%s", label, category, name)
            return
        try:
            callback(db, category, name)
        except Exception as exc:  # noqa: BLE001
            logger.error("数据管理[%s]回调异常: %s", label, exc, exc_info=True)

    def _request_remove_connection(self, db: dict) -> None:
        """菜单「删除连接」: 交回调（MainWindow 确认后调 :meth:`remove_connection`）。

        **只解绑、不删库文件** —— 用户断开的是"这个库出现在列表里"，不是删数据。
        """
        if self._on_remove_connection is None:
            logger.debug("数据管理[删除连接]: 未接回调, %s", db.get("name"))
            return
        try:
            self._on_remove_connection(db)
        except Exception as exc:  # noqa: BLE001
            logger.error("数据管理[删除连接]回调异常: %s", exc, exc_info=True)

    def remove_connection(self, db: dict) -> bool:
        """从树里摘掉一个连接（不删文件），并通知调用方落盘。

        ``db`` 可能是树节点 payload 里的副本，先换回 :attr:`_databases` /
        :attr:`_server_connections` 里的原件；换不回返回 ``False``（已经不在列表里）。
        """
        live = self._cache_db(db)
        if live is None:
            live = db if db in self._server_connections else None
        if live is None:
            return False
        if live in self._databases:
            self._databases.remove(live)
        elif live in self._server_connections:
            self._server_connections.remove(live)
        else:
            return False
        if self._current_db is live:
            self._current_db = None
            self.table.setRowCount(0)
        self._rebuild_tree()
        self.databasesChanged.emit()
        logger.info("数据管理：已移除连接 %s（未删除文件）", live.get("name"))
        return True

    def _on_tree_refresh(self, db: dict | None) -> int:
        """菜单「刷新」: 重扫目标库(``db=None`` 为全部)并**重建左树**, 保持选中。

        与「刷新」按钮(:meth:`refresh_all`)的差别: 按钮只重扫缓存 + 重画右表,
        左树的类别分组 / 行数保持旧值; 右键刷新按节点重扫后重建树, 新导入 /
        外部新增的表也会冒出来, 选中的连接节点不跳走。

        Returns:
            本次重扫的本地库 + 服务器连接数(与 :meth:`refresh_all` 同口径)。
        """
        if db is None:
            n = self.refresh_all()
            self._rebuild_tree()
            return n
        if db.get("kind") == "server":
            self._show_server_connection(db)
            return 1
        # 右键拿到的可能是 payload 副本 → 换回缓存里的原件再重扫(否则扫的是副本)
        target = self._cache_db(db)
        if target is None:
            return 0
        keep_category = (
            self._current_category if self._same_db(self._current_db, target) else None
        )
        self._refresh_db_cache(target)
        self._rebuild_tree()
        self._select_db_node(target)
        self._show_database(target, category=keep_category)
        return 1

    def _select_db_node(self, db: dict) -> None:
        """在(刚重建的)树里定位 ``db`` 的连接节点并选中(只改高亮, 不触发点击)."""
        item = self._find_item(lambda p: self._same_db(p.get("db"), db))
        if item is None:
            return
        parent = item.parent()
        if parent is not None:
            self._select_node(parent, item)

    @staticmethod
    def _same_db(a: dict | None, b: dict | None) -> bool:
        """两个连接 dict 是否指向同一个库。

        树节点 payload 里的 db 是 ``QTreeWidgetItem.setData`` 存 QVariantMap
        时**深拷**出来的副本(顶层和嵌套 list 都不是同一个对象) —— 所以这里
        只比内容: 两边都有 ``path`` 就比 ``path``(缓存被重扫过也能对上),
        否则退回整体相等。
        """
        if not isinstance(a, dict) or not isinstance(b, dict):
            return False
        pa, pb = a.get("path"), b.get("path")
        if pa and pb:
            return str(pa) == str(pb)
        return a == b

    def _cache_db(self, db: dict | None) -> dict | None:
        """把 payload 副本换回 :attr:`_databases` 里的原件(找不到 → None)."""
        if not isinstance(db, dict):
            return None
        for cand in self._databases:
            if self._same_db(cand, db):
                return cand
        return None

    def _find_item(self, pred: Callable[[dict], bool]) -> QTreeWidgetItem | None:
        """深度优先找第一个 payload 满足 ``pred`` 的节点(找不到返回 None)."""
        stack = [
            self.tree.topLevelItem(i) for i in range(self.tree.topLevelItemCount())
        ]
        while stack:
            item = stack.pop()
            payload = item.data(0, _ROLE)
            if isinstance(payload, dict) and pred(payload):
                return item
            stack.extend(item.child(j) for j in range(item.childCount()))
        return None

    def eventFilter(self, obj, ev) -> bool:  # noqa: N802 —— Qt 命名
        """表格尺寸变化(窗口 / 分隔条拖动)后延迟横向撑满(等布局稳定)."""
        if obj is self.table and ev.type() == QEvent.Type.Resize:
            QTimer.singleShot(0, self._fill_available_width)
        return super().eventFilter(obj, ev)

    def _fill_available_width(self) -> None:
        """横向撑满可用宽度: 列宽总和小于视口时按比例放大(列仍可手动拖窄)."""
        if self._fitting or self.table.columnCount() == 0:
            return
        header = self.table.horizontalHeader()
        view = header.viewport().width()
        if view <= 0:
            return
        cols = self.table.columnCount()
        total = sum(header.sectionSize(c) for c in range(cols))
        if total <= 0 or total >= view - 2 or view < cols * 48:
            return
        self._fitting = True
        try:
            factor = view / total
            widths = [max(48, int(header.sectionSize(c) * factor)) for c in range(cols)]
            widths[-1] = max(48, widths[-1] + view - sum(widths))
            for c, w in enumerate(widths):
                header.resizeSection(c, w)
        finally:
            self._fitting = False

    def _on_table_item_double_clicked(self, item) -> None:
        """表格行双击 → on_add_layer(db, category, 表名): 把该表加入地图.

        未提供回调时记日志(占位); 槽内异常捕获防 native 崩溃。
        """
        if item is None:
            return
        col0 = self.table.item(item.row(), 0)
        if col0 is None:
            return
        tag = col0.data(_ROW_TAG_ROLE)
        if not isinstance(tag, str) or "\x00" not in tag:
            return
        cat, name = tag.split("\x00", 1)
        db = self._current_db
        if db is None:
            return
        if self._on_add_layer is None:
            logger.debug("数据管理[加入地图]: 未接回调, %s/%s", cat, name)
            return
        try:
            self._on_add_layer(db, cat, name)
        except Exception as exc:  # 槽内异常会 native 崩溃
            logger.error("数据管理[加入地图]回调异常: %s", exc, exc_info=True)

    def _on_add_connection_clicked(self, scope: str | None = None) -> None:
        """「+ 新建连接」: 有回调交回调(MainWindow 弹窗后回填), 否则自己弹窗.

        ``scope`` 是右键时点的那个根节点（``local`` / ``server``）：传下去让弹窗
        **默认停在该页签上**（在「服务器数据库」上右键，弹出的却停在本地页，很别扭）。
        """
        scope = scope or "local"
        if self._on_add_connection is not None:
            self._fire("新建连接", self._on_add_connection, scope)
            return

        dlg = AddConnectionDialog(self, default_kind=scope)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        conn = dlg.connection()
        if conn:
            self.add_connection(conn)

    def _on_create_data_clicked(self) -> None:
        self._fire("创建数据", self._on_create_data, self._current_db)

    def _on_import_clicked(self) -> None:
        # 右侧面板的「导入」：目标库可自由选（locked=False）
        self._fire("导入", self._on_import, self._current_db, False)

    def _on_refresh_clicked(self) -> None:
        self._fire("刷新", self._on_refresh)

    def _fire(self, label: str, cb: Callable | None, *args: object) -> None:
        """动作分发: 有回调执行回调, 未接入时记日志(后续补充实现)."""
        if cb is not None:
            try:
                cb(*args)
            except Exception as exc:  # noqa: BLE001 —— 槽内异常会 native 崩溃
                logger.error("数据管理[%s]回调异常: %s", label, exc, exc_info=True)
            return
        logger.debug("数据管理[%s]: 功能待接入", label)


class DataManageDialog(BaseDialog):
    """数据管理对话框（替代原来的底部 Dock，操作方式与「创建数据」一致）。"""

    def __init__(self, parent=None, **kwargs) -> None:
        # 非模态对话框：最小化走 BaseDialog 的「画布底部小条」（与属性录入一致）
        super().__init__(parent, title="数据管理", show_min_button=True)
        self.setMinimumSize(0, 0)
        self.resize(1280, 820)
        self.panel = DataManageDock(self, **kwargs)
        layout = QVBoxLayout()
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.panel, 1)
        self.setLayout(layout)


__all__ = ["DataManageDialog", "DataManageDock"]
