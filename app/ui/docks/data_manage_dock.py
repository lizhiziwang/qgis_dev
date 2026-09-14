"""数据管理 Dock(页面下方).

按需求 HTML 的元素结构映射为原生 Qt 组件(不用 QTextBrowser 渲染):

- ``section.dialog / .dialog-title`` -> :class:`QDockWidget` 自身标题栏(含
  右上角关闭按钮, 对应 HTML 的关闭键)
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
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from qgis.PyQt.QtCore import QEvent, QSize, Qt, QTimer
from qgis.PyQt.QtGui import QBrush, QColor, QFont, QIcon
from qgis.PyQt.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDockWidget,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QSplitter,
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
)
from app.infra.logging import get_logger
from app.ui.common import tool_icons
from app.ui.common.theme import ThemeManager
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


class DataManageDock(QDockWidget):
    """数据管理面板(底部 Dock): 左数据库树 + 右数据表. """

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        on_add_connection: Callable[[], None] | None = None,
        on_create_data: Callable[[], None] | None = None,
        on_import: Callable[[], None] | None = None,
        on_refresh: Callable[[], None] | None = None,
        on_add_layer: Callable[[dict, str, str], None] | None = None,
    ) -> None:
        super().__init__("数据管理", parent)
        #: QSS 作用域锚点(见 app.ui.common.theme.window_qss 的 #DataManageDock 规则)
        self.setObjectName("DataManageDock")
        #: 动作回调占位(未提供时点击只记日志)
        self._on_add_connection = on_add_connection
        self._on_create_data = on_create_data
        self._on_import = on_import
        self._on_refresh = on_refresh
        #: 双击表格行回调: (连接 dict, category, 表名) → 加入地图(由 MainWindow 提供)
        self._on_add_layer = on_add_layer
        #: 当前联动展示的库(双击行定位用)
        self._current_db: dict | None = None
        #: 列宽程序化调整中(防止信号重入)
        self._fitting = False
        #: 服务器连接清单(kind=server, 挂在「服务器数据库」分组下)
        self._server_connections: list[dict] = []
        self.setAllowedAreas(Qt.DockWidgetArea.BottomDockWidgetArea)
        self.setFeatures(
            QDockWidget.DockWidgetFeature.DockWidgetClosable
            | QDockWidget.DockWidgetFeature.DockWidgetMovable
            | QDockWidget.DockWidgetFeature.DockWidgetFloatable
        )
        self._build_ui()
        self._databases: list[dict] = []
        self._rebuild_tree()
        # 树图标是 QPainter 画的(颜色烧在 PNG 里), 切主题需重刷
        ThemeManager.instance().theme_changed.connect(self._on_theme_changed)

    # ------------------------------------------------------------ UI

    def _build_ui(self) -> None:
        root = QWidget(self)
        v = QVBoxLayout(root)
        v.setContentsMargins(4, 4, 4, 4)
        v.setSpacing(2)

        self.split = QSplitter(Qt.Orientation.Horizontal, root)
        self.split.addWidget(self._build_tree_pane(self.split))
        self.split.addWidget(self._build_panel_pane(self.split))
        self.split.setStretchFactor(0, 0)
        self.split.setStretchFactor(1, 1)
        self.split.setSizes([240, 640])
        v.addWidget(self.split, 1)

        footer = self._build_footer(root)
        v.addWidget(footer)

        self.setWidget(root)

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
        lay.addWidget(self.tree, 1)

        self.add_btn = QPushButton("新建连接")
        self.add_btn.setObjectName("DataManageAddConn")
        self.add_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.add_btn.setToolTip("连接本地库文件或服务器数据库")
        self.add_btn.clicked.connect(self._on_add_connection_clicked)
        lay.addWidget(self.add_btn)
        return pane

    def _build_panel_pane(self, parent: QWidget) -> QWidget:
        """右栏: 选中库的数据表(.dm-panel)."""
        pane = QWidget(parent)
        lay = QVBoxLayout(pane)
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
        for btn in (self.create_btn, self.import_btn, self.refresh_btn):
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
        return pane

    def _build_footer(self, parent: QWidget) -> QWidget:
        """底部按钮行(.dialog-footer)."""
        foot = QWidget(parent)
        h = QHBoxLayout(foot)
        h.setContentsMargins(0, 2, 0, 0)
        h.addStretch(1)
        close_btn = QPushButton("关闭")
        close_btn.clicked.connect(self.close)
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
        服务器连接(``self._server_connections``)不受影响。"""
        self._databases = list(databases)
        self._rebuild_tree()

    def add_connection(self, db: dict) -> None:
        """新建连接成功后入树并联动: kind=local 挂「本地数据库」根下; kind=server 挂「服务器数据库」根下."""
        conn = dict(db)
        if conn.get("kind") == "server":
            self._server_connections.append(conn)
            self._rebuild_tree()
            # 服务器根固定在第 1 个顶层节点; 刚加入的就是其最后一个子节点
            root = self.tree.topLevelItem(1)
            item = root.child(len(self._server_connections) - 1)
            self._select_node(root, item)
            self._show_server_connection(conn)
            return
        self._databases.append(conn)
        self._rebuild_tree()
        # 本地根固定在第 0 个顶层节点; 刚加入的就是其最后一个子节点
        root = self.tree.topLevelItem(0)
        item = root.child(len(self._databases) - 1)
        self._select_node(root, item)
        self._show_database(conn)

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

        # 服务器数据库根节点: 服务器连接(PostgreSQL/Oracle/SQL Server)挂在下面
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
        elif self._server_connections:
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
            if not names:
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
        """联动右侧面板: 更新标题并重建数据表 (db=None 显示空提示)."""
        self._current_db = db
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
            self._current_db = None
            self.table.setRowCount(0)
            self.sel_name.setText(
                "本地数据库" if scope == "local" else "服务器数据库"
            )
            self.sel_name.setToolTip("")
            return
        db = payload.get("db")
        if "name" in payload and isinstance(payload.get("name"), str) and db:
            # 表名叶子: 联动所属库的该类别, 并高亮表格对应行
            self._show_database(db, category=payload["cat"])
            tag = f"{payload['cat']}\x00{payload['name']}"
            for r in range(self.table.rowCount()):
                it = self.table.item(r, 0)
                if it is not None and it.data(_ROW_TAG_ROLE) == tag:
                    self.table.selectRow(r)
                    return
            return
        if "cat" in payload and db:
            # 类别分组节点: 只显示该类数据
            self._show_database(db, category=payload["cat"])
            return
        if isinstance(db, dict):
            # 数据库节点(本地连接 / 服务器分组/连接): 本地显示全部, 服务器显示摘要
            if db.get("kind") == "server":
                self._show_server_connection(db)
                return
            self._show_database(db)

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

    def _on_add_connection_clicked(self) -> None:
        """「+ 新建连接」: 有回调交回调(MainWindow 弹窗后回填), 否则自己弹窗."""
        if self._on_add_connection is not None:
            self._fire("新建连接", self._on_add_connection)
            return

        dlg = AddConnectionDialog(self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        conn = dlg.connection()
        if conn:
            self.add_connection(conn)

    def _on_create_data_clicked(self) -> None:
        self._fire("创建数据", self._on_create_data)

    def _on_import_clicked(self) -> None:
        self._fire("导入", self._on_import)

    def _on_refresh_clicked(self) -> None:
        self._fire("刷新", self._on_refresh)

    def _fire(self, label: str, cb: Callable[[], None] | None) -> None:
        """动作分发: 有回调执行回调, 未接入时记日志(后续补充实现)."""
        if cb is not None:
            try:
                cb()
            except Exception as exc:  # 槽内异常会 native 崩溃
                logger.error("数据管理[%s]回调异常: %s", label, exc, exc_info=True)
            return
        logger.debug("数据管理[%s]: 功能待接入", label)


__all__ = ["DataManageDock"]
