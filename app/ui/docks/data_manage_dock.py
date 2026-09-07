"""数据管理 Dock(页面下方).

按需求 HTML 的元素结构映射为原生 Qt 组件(不用 QTextBrowser 渲染):

- ``section.dialog / .dialog-title`` -> :class:`QDockWidget` 自身标题栏(含
  右上角关闭按钮, 对应 HTML 的关闭键)
- ``.dialog-body > .dm-split``        -> :class:`QSplitter`(水平两栏)
- ``.dm-tree``                        -> 左栏: 树头「数据库连接」+ QTreeWidget
  + 「新建连接」按钮
- ``.dm-panel``                       -> 右栏: 标题行(选中库名 + 创建数据 /
  导入 / 刷新按钮) + QTableWidget(数据表: 数据名称 / 类型 / 说明)
- ``.dialog-footer``                  -> 底部按钮行(关闭)

本地数据库展示以 ``local_db_util`` 的 ``query_all_table_name()`` 注册表为准
(见 :mod:`app.core.util.local_db_util`), 树按类别分级: 连接文件节点 →
「矢量 / 字典 / 影像」分组节点 → 注册表名叶子; 右侧面板列出选中库(或某类)
的注册数据行(类型列=类别, 说明列=行数)。树尾固定「服务器数据库」占位节点
(服务器连接暂搁置)。创建数据 / 导入 / 刷新仍为回调占位(未传时记日志),
由 MainWindow 提供回调后接入。
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QFont
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

logger = get_logger(__name__)

_ROLE = int(Qt.ItemDataRole.UserRole)
#: 表格行(名称列)自带的定位 tag = f"{category}\\x00{name}", 供叶子行精确高亮
_ROW_TAG_ROLE = int(Qt.ItemDataRole.UserRole) + 1


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
    ) -> None:
        super().__init__("数据管理", parent)
        #: 动作回调占位(未提供时点击只记日志)
        self._on_add_connection = on_add_connection
        self._on_create_data = on_create_data
        self._on_import = on_import
        self._on_refresh = on_refresh
        self.setAllowedAreas(Qt.DockWidgetArea.BottomDockWidgetArea)
        self.setFeatures(
            QDockWidget.DockWidgetFeature.DockWidgetClosable
            | QDockWidget.DockWidgetFeature.DockWidgetMovable
            | QDockWidget.DockWidgetFeature.DockWidgetFloatable
        )
        self._build_ui()
        self._databases: list[dict] = []
        self._rebuild_tree()

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
        """左栏: 数据库连接树(.dm-tree)."""
        pane = QWidget(parent)
        lay = QVBoxLayout(pane)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(2)

        hd = QLabel("数据库连接")
        font = hd.font()
        font.setBold(True)
        hd.setFont(font)
        lay.addWidget(hd)

        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.setColumnCount(2)
        self.tree.setIndentation(16)
        self.tree.setAnimated(True)
        self.tree.setRootIsDecorated(True)
        self.tree.itemClicked.connect(self._on_tree_item_clicked)
        lay.addWidget(self.tree, 1)

        self.add_btn = QPushButton("+ 新建连接")
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

        # 数据表
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["数据名称", "类型", "说明"])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
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
             "counts": {表名: 行数|None}}
        """
        self._databases = list(databases)
        self._rebuild_tree()

    def add_connection(self, db: dict) -> None:
        """新建连接成功后追加一个本地数据库节点并选中联动右侧面板."""
        conn = dict(db)
        self._databases.append(conn)
        self._rebuild_tree()
        for i in range(self.tree.topLevelItemCount()):
            item = self.tree.topLevelItem(i)
            payload = item.data(0, _ROLE) or {}
            if payload.get("db") is conn:
                self.tree.setCurrentItem(item)
                self._show_database(conn)
                return
        if self._databases:
            self._show_database(self._databases[0])

    def _rebuild_tree(self) -> None:
        """重建左树: 本地连接节点在前(按类别分级), 末尾固定服务器占位."""
        self.tree.clear()
        for db in self._databases:
            top = QTreeWidgetItem()
            top.setText(0, self._db_title(db))
            top.setToolTip(0, str(db.get("path", "")))
            font = top.font(0)
            font.setBold(True)
            top.setFont(0, font)
            top.setData(0, _ROLE, {"db": db})
            self._append_category_groups(top, db)
            if top.childCount():
                top.setExpanded(True)
            self.tree.addTopLevelItem(top)

        server = QTreeWidgetItem()
        server.setText(0, "服务器数据库")
        server.setToolTip(0, "服务器数据库连接暂未开放")
        sfont = server.font(0)
        sfont.setBold(True)
        server.setFont(0, sfont)
        server.setData(
            0, _ROLE, {"db": {"kind": "server", "name": "服务器数据库"}}
        )
        self.tree.addTopLevelItem(server)

        if self._databases:
            self.tree.setCurrentItem(self.tree.topLevelItem(0))
            self._show_database(self._databases[0])
        else:
            self._show_database(None)

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
                    small = QFont(leaf.font(1))
                    small.setPointSizeF(max(6.0, small.pointSizeF() - 1.5))
                    leaf.setFont(1, small)
                    leaf.setText(1, f"{count} {_CATEGORY_UNIT[cat]}")
                leaf.setData(0, _ROLE, {"db": db, "cat": cat, "name": name})
            group.setExpanded(True)

    @staticmethod
    def _db_title(db: dict) -> str:
        """节点标题: 有 name 用 name, 否则取文件名的 stem."""
        return str(db.get("name") or Path(str(db.get("path", ""))).stem or "数据库")

    @staticmethod
    def _rows(db: dict, category: str | None = None) -> list[dict]:
        """展平某库(可按类别过滤)的注册数据行: [{name, category, count}]."""
        cats: dict = db.get("categories") or {}
        counts: dict = db.get("counts") or {}
        out: list[dict] = []
        for cat in _CATEGORY_ORDER:
            if category is not None and cat != category:
                continue
            for name in cats.get(cat) or []:
                out.append(
                    {"name": name, "category": cat, "count": counts.get(name)}
                )
        return out

    def _show_database(self, db: dict | None, *, category: str | None = None) -> None:
        """联动右侧面板: 更新标题并重建数据表 (db=None 显示空提示)."""
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
            name_item = QTableWidgetItem(row["name"])
            name_item.setData(_ROW_TAG_ROLE, f"{row['category']}\x00{row['name']}")
            count = row["count"]
            desc = (
                "—"
                if count is None
                else f"{count} {_CATEGORY_UNIT.get(row['category'], '')}"
            )
            items = (
                name_item,
                QTableWidgetItem(_CATEGORY_LABEL.get(row["category"], row["category"])),
                QTableWidgetItem(desc),
            )
            for item in items:
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            for col, item in enumerate(items):
                self.table.setItem(r, col, item)

    # ------------------------------------------------------------ 交互

    def _on_tree_item_clicked(self, item: QTreeWidgetItem, column: int) -> None:
        payload = item.data(0, _ROLE)
        if not isinstance(payload, dict):
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
            # 数据库节点(本地连接 / 服务器占位): 显示全部
            self._show_database(db)

    def _on_add_connection_clicked(self) -> None:
        """「+ 新建连接」: 有回调交回调(MainWindow 弹窗后回填), 否则自己弹窗."""
        if self._on_add_connection is not None:
            self._fire("新建连接", self._on_add_connection)
            return
        from app.ui.dialogs.add_connection import AddConnectionDialog

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
