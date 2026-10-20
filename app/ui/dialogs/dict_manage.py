"""字典管理对话框（顶部「通用」页签 → 数据组 → 字典管理）。

界面结构：

- 顶部：字典表下拉（可新建）+ 搜索 + 创建字典表 / Excel导入 / Excel导出 / 刷新；
- 左侧主区：字典树，三列「名称 / 代码 / 字典域·说明」；维护操作全部在这里完成 ——
  工具栏 同级添加 / 下级添加 / 新建字典域 / 修改 / 删除，双击单元格直接内联编辑，
  同级节点可拖拽排序，节点右键也有相同的添加 / 删除入口；
- 底部：条目统计（不展示库路径）。

**取数口径**：

- 字典表是**动态**的：默认预置「政区字典 / 资源字典」，「创建字典表」新建的是**字典表**；
- 字典域由「新建字典域」创建，Excel 导入时按「字典域」列自动建；
- Excel 导入全部进**当前下拉选中的字典表**，不再按域名前缀或「类别」列分流。

**大字典不能一次全塞进树**：真实模板里「村」一个域就有 2 万条。这里按
``DEFAULT_PAGE_SIZE`` 分页加载：节点展开时才查第一页，末尾挂「加载更多…」；
顶部搜索走 SQL 模糊查询（最多 1000 条），这是大字典里定位条目的主要方式。

职责边界：本对话框只做展示与流程编排，读写全部经
:class:`~app.core.domain.dict_manage.service.DictService`；文件对话框只负责取路径。
"""

from __future__ import annotations

import re
from contextlib import contextmanager
from pathlib import Path

from app.core.domain.dict_manage import (
    DEFAULT_PAGE_SIZE,
    DEFAULT_SEARCH_LIMIT,
    EXPORT_FILTER,
    IMPORT_FILTER,
    DictError,
    DictExportError,
    DictImportError,
    DictItem,
    DictService,
    export_excel,
    import_excel,
    normalize_export_path,
)
from app.infra.logging import get_logger
from app.ui.common import tool_icons
from app.ui.common.base_dialog import BaseDialog
from app.ui.common.qt_compat import event_pos
from app.ui.common.theme import ThemeManager
from app.ui.common.widgets import open_file_dir, save_file
from qgis.PyQt.QtCore import QSignalBlocker, Qt, QTimer, pyqtSignal
from qgis.PyQt.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QComboBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPushButton,
    QStyledItemDelegate,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

logger = get_logger(__name__)

#: 树节点数据用到的自定义 role（0 = 展示列，从 UserRole 往上排）
_ROLE_KIND = int(Qt.ItemDataRole.UserRole)
_ROLE_ITEM_ID = _ROLE_KIND + 1
_ROLE_DICT_ID = _ROLE_KIND + 2
_ROLE_CODE = _ROLE_KIND + 3
_ROLE_PARENT = _ROLE_KIND + 4
_ROLE_DOMAIN = _ROLE_KIND + 5
_ROLE_LOADED = _ROLE_KIND + 6
_ROLE_SORT = _ROLE_KIND + 7
_ROLE_NAME = _ROLE_KIND + 8
_ROLE_DESC = _ROLE_KIND + 9

#: 节点类型
_KIND_DOMAIN = "domain"
_KIND_ITEM = "item"
_KIND_RESULT = "result"
_KIND_MORE = "more"
_KIND_PLACEHOLDER = "placeholder"

#: 可内联编辑的列（字典项 / 搜索结果也能直接改「字典域」，等于移动到别的域）
_EDITABLE_COLUMNS = {
    _KIND_DOMAIN: frozenset({0, 2}),  # 名称 / 说明
    _KIND_ITEM: frozenset({0, 1, 2}),    # 名称 / 代码 / 字典域
    _KIND_RESULT: frozenset({0, 1, 2}),
}

#: 行高下限：双击编辑时编辑框不会把文字裁掉
_ROW_HEIGHT = 30


class _DictItemDelegate(QStyledItemDelegate):
    """统一加高行，并给「字典项 → 字典域」列提供可编辑下拉框。"""

    def __init__(self, parent, domain_names) -> None:
        super().__init__(parent)
        self._domain_names = domain_names  # 无参 callable → list[str]

    def sizeHint(self, option, index):
        size = super().sizeHint(option, index)
        size.setHeight(max(size.height(), _ROW_HEIGHT))
        return size

    def createEditor(self, parent, option, index):  # noqa: N802 —— Qt 命名
        kind = index.data(_ROLE_KIND)
        if index.column() == 2 and kind in (_KIND_ITEM, _KIND_RESULT):
            combo = QComboBox(parent)
            combo.setEditable(True)
            combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
            combo.addItems(self._domain_names())
            combo.setCurrentText(str(index.data(Qt.ItemDataRole.DisplayRole) or ""))
            return combo
        return super().createEditor(parent, option, index)


class _DictTree(QTreeWidget):
    """字典树：只允许**同父级**上下拖拽重排，不允许跨域 / 跨级移动。"""

    reordered = pyqtSignal(object)  # 被拖动的节点（排序落库时用它定位同级组）

    def dropEvent(self, event) -> None:
        source = self.currentItem()
        target = self.itemAt(event_pos(event))
        kind_source = source.data(0, _ROLE_KIND) if source is not None else None
        kind_target = target.data(0, _ROLE_KIND) if target is not None else None
        indicator = self.dropIndicatorPosition()
        valid_indicator = {
            QAbstractItemView.DropIndicatorPosition.AboveItem,
            QAbstractItemView.DropIndicatorPosition.BelowItem,
        }
        if (
            source is None
            or target is None
            or source is target
            or kind_source not in (_KIND_DOMAIN, _KIND_ITEM)
            or kind_target not in (_KIND_DOMAIN, _KIND_ITEM)
            or indicator not in valid_indicator
            or source.parent() is not target.parent()
        ):
            event.ignore()
            return
        super().dropEvent(event)
        self.reordered.emit(source)


class DictManageDialog(BaseDialog):
    """业务字典维护对话框（应用级共享库，见 ``DictService.default_db_path``）。"""

    def __init__(
        self,
        parent=None,
        *,
        service: DictService | None = None,
        default_category: str | None = None,
    ) -> None:
        super().__init__(parent, title="字典管理", show_min_button=True, show_max_button=True)
        self._service = service if service is not None else DictService()
        self._category = default_category or ""
        self._search_text = ""
        #: 程序化改树（加载 / 回滚 / 同步域名）时不要触发 itemChanged 落库
        self._tree_updating = False

        self.setMinimumSize(0, 0)
        self.resize(1000, 700)

        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(300)
        self._search_timer.timeout.connect(self._run_search)

        self._build_ui()
        self._apply_icons()
        ThemeManager.instance().theme_changed.connect(self._apply_icons)
        self._reload_categories()

    # ===================================================== 界面搭建

    def _build_ui(self) -> None:
        layout = QVBoxLayout()
        # 顶部：类别下拉 + 搜索 + 全局动作
        top = QHBoxLayout()
        top.addWidget(QLabel("字典表："))
        self._category_combo = QComboBox()
        self._category_combo.setMinimumWidth(160)
        self._category_combo.currentIndexChanged.connect(self._on_category_changed)
        top.addWidget(self._category_combo)

        top.addSpacing(16)
        self._search_edit = QLineEdit()
        self._search_edit.setPlaceholderText("搜索代码 / 名称 / 字典域（大字典请用搜索定位）")
        self._search_edit.setClearButtonEnabled(True)
        self._search_edit.setMinimumWidth(260)
        self._search_edit.textChanged.connect(lambda _text: self._search_timer.start())
        top.addWidget(self._search_edit, 1)

        self._btn_create_category = QPushButton("创建字典表")
        self._btn_create_category.clicked.connect(self._create_category)
        top.addWidget(self._btn_create_category)
        self._btn_delete_category = QPushButton("删除字典表")
        self._btn_delete_category.clicked.connect(self._delete_category)
        top.addWidget(self._btn_delete_category)
        self._btn_import = QPushButton("Excel导入")
        self._btn_import.clicked.connect(self._on_import)
        top.addWidget(self._btn_import)
        self._btn_export = QPushButton("Excel导出")
        self._btn_export.clicked.connect(self._on_export)
        top.addWidget(self._btn_export)
        self._btn_refresh = QPushButton("刷新")
        self._btn_refresh.clicked.connect(lambda _checked=False: self._reload_categories())
        top.addWidget(self._btn_refresh)
        layout.addLayout(top)

        # 树上方：维护动作全部放在左侧面板
        actions = QHBoxLayout()
        self._btn_sibling = QPushButton("同级添加")
        self._btn_sibling.clicked.connect(self._on_add_sibling)
        actions.addWidget(self._btn_sibling)
        self._btn_child = QPushButton("下级添加")
        self._btn_child.clicked.connect(self._on_add_child)
        actions.addWidget(self._btn_child)
        self._btn_new_domain = QPushButton("新建字典域")
        self._btn_new_domain.clicked.connect(self._on_new_domain)
        actions.addWidget(self._btn_new_domain)
        self._btn_edit = QPushButton("修改")
        self._btn_edit.clicked.connect(self._on_edit)
        actions.addWidget(self._btn_edit)
        self._btn_delete = QPushButton("删除")
        self._btn_delete.clicked.connect(self._on_delete)
        actions.addWidget(self._btn_delete)
        actions.addStretch(1)
        self._hint_label = QLabel(
            "双击单元格可直接修改；同级的字典域 / 字典项可拖拽排序；右键可添加 / 删除"
        )
        actions.addWidget(self._hint_label)
        layout.addLayout(actions)

        self._tree = self._build_tree()
        layout.addWidget(self._tree, 1)

        footer = QHBoxLayout()
        self._status_label = QLabel("")
        self._status_label.setWordWrap(True)
        footer.addWidget(self._status_label, 1)
        btn_close = QPushButton("关闭")
        btn_close.clicked.connect(self.accept)
        footer.addWidget(btn_close)
        layout.addLayout(footer)

        self.setLayout(layout)

    def _build_tree(self) -> QWidget:
        tree = _DictTree()
        tree.setColumnCount(3)
        tree.setHeaderLabels(["名称", "代码", "字典域 / 说明"])
        tree.setUniformRowHeights(True)
        tree.setAnimated(False)
        tree.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        # 编辑入口统一走双击 /【修改】（见 _start_edit），避免点到只读列也弹编辑框。
        tree.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        tree.header().setStretchLastSection(True)
        tree.setColumnWidth(0, 430)
        tree.setColumnWidth(1, 90)
        tree.setItemDelegate(_DictItemDelegate(tree, self._domain_names))
        tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        tree.customContextMenuRequested.connect(self._show_context_menu)
        # 拖拽仅用于同级重排（见 _DictTree.dropEvent）
        tree.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        tree.setDragDropOverwriteMode(False)
        tree.setDragEnabled(True)
        tree.setAcceptDrops(True)
        tree.setDropIndicatorShown(True)
        tree.setDefaultDropAction(Qt.DropAction.MoveAction)
        tree.reordered.connect(self._persist_order)
        tree.itemExpanded.connect(self._on_item_expanded)
        tree.itemClicked.connect(self._on_item_clicked)
        tree.itemDoubleClicked.connect(self._on_item_double_clicked)
        tree.itemChanged.connect(self._on_item_changed)
        tree.currentItemChanged.connect(self._on_current_changed)
        return tree

    def _domain_names(self) -> list[str]:
        """当前字典表下的全部字典域名（编辑「字典域」列时的下拉候选）。"""
        if not self._category:
            return []
        try:
            return [d.domain for d in self._service.list_domains(self._category)]
        except DictError:
            return []

    # ===================================================== 主题 / 图标

    def _apply_icons(self, *_args) -> None:
        """主题切换时重刷树节点图标颜色。"""
        theme = ThemeManager.instance().current()
        try:
            self._domain_icon = tool_icons.icon("cat_dict", theme.input_focus, 16)
            self._item_icon = tool_icons.icon("cat_data", theme.input_focus, 16)
        except Exception:
            logger.warning("字典管理图标加载失败", exc_info=True)
            return
        self._retint_tree(self._tree.invisibleRootItem())

    def _retint_tree(self, node: QTreeWidgetItem) -> None:
        for index in range(node.childCount()):
            child = node.child(index)
            kind = child.data(0, _ROLE_KIND)
            if kind == _KIND_DOMAIN:
                child.setIcon(0, self._domain_icon)
            elif kind in (_KIND_ITEM, _KIND_RESULT):
                child.setIcon(0, self._item_icon)
            self._retint_tree(child)

    # ===================================================== 类别

    def _reload_categories(self, *, select: str | None = None) -> None:
        """重载字典表下拉；``select`` 指定要选中的字典表名。"""
        try:
            categories = self._service.list_categories()
        except DictError as exc:
            QMessageBox.warning(self, "字典管理", str(exc))
            return
        target = select or self._category
        self._category_combo.blockSignals(True)
        self._category_combo.clear()
        for category in categories:
            self._category_combo.addItem(category.name, category.name)
        index = self._category_combo.findData(target) if target else -1
        if index < 0 and self._category_combo.count():
            index = 0
        if index >= 0:
            self._category_combo.setCurrentIndex(index)
            self._category = str(self._category_combo.itemData(index) or "")
        else:
            self._category = ""
        self._category_combo.blockSignals(False)
        self._load_domains()

    def _delete_category(self) -> None:
        """删除当前字典表（类别）及其全部字典域 / 字典项。"""
        if not self._category:
            QMessageBox.information(self, "删除字典表", "当前没有选中的字典表")
            return
        try:
            domains = self._service.list_domains(self._category)
        except DictError as exc:
            QMessageBox.warning(self, "删除字典表", str(exc))
            return
        total = sum(domain.item_count for domain in domains)
        ret = QMessageBox.question(
            self,
            "删除字典表",
            f"确定删除字典表「{self._category}」吗？\n\n"
            f"将删除 {len(domains)} 个字典域、{total} 条字典项。\n"
            "本地库里已经落库的字典副本不受影响。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if ret != QMessageBox.StandardButton.Yes:
            return
        try:
            removed = self._service.delete_category(self._category)
        except DictError as exc:
            QMessageBox.warning(self, "删除字典表", str(exc))
            return
        name = self._category
        self._reload_categories()
        self._status_label.setText(f"已删除字典表「{name}」（{removed} 条字典项）")

    def _on_category_changed(self, _index: int) -> None:
        name = str(self._category_combo.currentData() or "")
        if not name or name == self._category:
            return
        # 切换字典表时保留搜索词：用户往往是在两个字典表里找同一个词。
        keyword = self._search_edit.text().strip()
        self._category = name
        self._load_domains()
        if keyword:
            self._search_edit.setText(keyword)
            self._run_search()
            self._search_timer.stop()

    def _create_category(self) -> None:
        """「创建字典表」= 新建一个字典表（下拉里的一档）。"""
        name, ok = QInputDialog.getText(self, "创建字典表", "请输入新字典表名称：")
        if not ok:
            return
        name = str(name).strip()
        if not name:
            QMessageBox.information(self, "创建字典表", "字典表名称不能为空")
            return
        try:
            category = self._service.create_category(name)
        except DictError as exc:
            QMessageBox.warning(self, "创建字典表", str(exc))
            return
        self._reload_categories(select=category.name)
        self._status_label.setText(f"已创建字典表「{category.name}」")

    # ===================================================== 加载 / 分页

    @contextmanager
    def _tree_guard(self):
        """程序化改树期间屏蔽 ``itemChanged``，避免把加载动作当成用户编辑。"""
        previous = self._tree_updating
        self._tree_updating = True
        try:
            yield
        finally:
            self._tree_updating = previous

    def _load_domains(
        self, *, select_domain_id: int | None = None, keep_search: bool = False
    ) -> None:
        """重载当前字典表下的字典域。

        ``keep_search=True`` 时保留搜索关键词与 ``_search_text``：搜索改为
        「在原有域树上隐藏不匹配数据」，需要先重建域节点再过滤（见
        :meth:`_run_search`），不能顺手把搜索状态清掉。
        """
        if not keep_search:
            self._search_text = ""
            if self._search_edit.text():
                # 触发 textChanged 会再排一次搜索；这里先静音定时器，避免刚载入就被搜索结果覆盖。
                self._search_edit.blockSignals(True)
                self._search_edit.clear()
                self._search_edit.blockSignals(False)
            self._search_timer.stop()

        with self._tree_guard():
            self._tree.clear()
        if not self._category:
            self._status_label.setText("请先点「创建字典表」新建一个字典表")
            self._update_action_buttons()
            return
        try:
            domains = self._service.list_domains(self._category)
        except DictError as exc:
            QMessageBox.warning(self, "字典管理", str(exc))
            return
        with self._tree_guard():
            for domain in domains:
                self._tree.addTopLevelItem(self._domain_node(domain))
            self._tree.setCurrentItem(None)
        total = sum(domain.item_count for domain in domains)

        selected = None
        if select_domain_id is not None:
            for index in range(self._tree.topLevelItemCount()):
                node = self._tree.topLevelItem(index)
                if node.data(0, _ROLE_DICT_ID) == select_domain_id:
                    selected = node
                    break
        if selected is not None:
            self._tree.setCurrentItem(selected)
            self._tree.scrollToItem(selected)

        self._status_label.setText(
            f"字典表「{self._category}」：共 {len(domains)} 个字典域、{total} 条字典项"
        )
        self._update_action_buttons()

    def _domain_node(self, domain) -> QTreeWidgetItem:
        node = QTreeWidgetItem([domain.domain, "", domain.description])
        node.setData(0, _ROLE_KIND, _KIND_DOMAIN)
        node.setData(0, _ROLE_DICT_ID, domain.id)
        node.setData(0, _ROLE_DOMAIN, domain.domain)
        node.setData(0, _ROLE_DESC, domain.description)
        node.setData(0, _ROLE_LOADED, 0)
        node.setData(0, _ROLE_SORT, domain.sort_order)
        node.setFlags(
            Qt.ItemFlag.ItemIsSelectable
            | Qt.ItemFlag.ItemIsEnabled
            | Qt.ItemFlag.ItemIsEditable
            | Qt.ItemFlag.ItemIsDragEnabled
        )
        node.setToolTip(
            0,
            f"字典域：{domain.domain}\n{domain.description or '（无说明）'}\n"
            f"共 {domain.item_count} 条",
        )
        font = node.font(0)
        font.setBold(True)
        node.setFont(0, font)
        node.setIcon(
            0,
            getattr(self, "_domain_icon", None)
            or self.style().standardIcon(self.style().StandardPixmap.SP_DirIcon),
        )
        node.addChild(self._placeholder_node())
        return node

    def _item_node(self, item: DictItem, domain_name: str) -> QTreeWidgetItem:
        node = QTreeWidgetItem([item.name, item.code, domain_name])
        node.setData(0, _ROLE_KIND, _KIND_ITEM)
        node.setData(0, _ROLE_ITEM_ID, item.id)
        node.setData(0, _ROLE_DICT_ID, item.dict_id)
        node.setData(0, _ROLE_CODE, item.code)
        node.setData(0, _ROLE_PARENT, item.parent_code)
        node.setData(0, _ROLE_DOMAIN, domain_name)
        node.setData(0, _ROLE_NAME, item.name)
        node.setData(0, _ROLE_LOADED, 0)
        node.setData(0, _ROLE_SORT, item.sort_order)
        node.setFlags(
            Qt.ItemFlag.ItemIsSelectable
            | Qt.ItemFlag.ItemIsEnabled
            | Qt.ItemFlag.ItemIsEditable
            | Qt.ItemFlag.ItemIsDragEnabled
        )
        node.setToolTip(
            0,
            f"{item.name}\n代码：{item.code}\n上级代码：{item.parent_code or '（无）'}",
        )
        node.setIcon(
            0,
            getattr(self, "_item_icon", None)
            or self.style().standardIcon(self.style().StandardPixmap.SP_FileIcon),
        )
        if item.has_children:
            node.addChild(self._placeholder_node())
        return node

    @staticmethod
    def _placeholder_node() -> QTreeWidgetItem:
        node = QTreeWidgetItem(["加载中…", "", ""])
        node.setData(0, _ROLE_KIND, _KIND_PLACEHOLDER)
        node.setFlags(Qt.ItemFlag.NoItemFlags)
        return node

    @staticmethod
    def _more_node(loaded: int, total: int) -> QTreeWidgetItem:
        node = QTreeWidgetItem([f"加载更多（已显示 {loaded} / {total}）", "", ""])
        node.setData(0, _ROLE_KIND, _KIND_MORE)
        # 可点开下一页，但不能被拖拽 / 编辑
        node.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
        return node

    def _on_item_expanded(self, node: QTreeWidgetItem) -> None:
        if node.data(0, _ROLE_KIND) in (_KIND_DOMAIN, _KIND_ITEM):
            self._load_more(node)

    def _on_item_clicked(self, node: QTreeWidgetItem, _column: int) -> None:
        if node.data(0, _ROLE_KIND) == _KIND_MORE:
            parent = node.parent()
            if parent is not None:
                self._load_more(parent)
                self._tree.setCurrentItem(parent)

    def _load_more(self, node: QTreeWidgetItem) -> None:
        """给节点补一页子项（首次展开 / 点「加载更多」都走这里）。"""
        kind = node.data(0, _ROLE_KIND)
        if kind not in (_KIND_DOMAIN, _KIND_ITEM):
            return
        loaded = int(node.data(0, _ROLE_LOADED) or 0)
        # 已经加载完（只剩真实子节点、没有占位/更多）就不重复查。
        has_sentinel = any(
            node.child(i).data(0, _ROLE_KIND) in (_KIND_PLACEHOLDER, _KIND_MORE)
            for i in range(node.childCount())
        )
        if not has_sentinel:
            return
        dict_id = int(node.data(0, _ROLE_DICT_ID))
        parent_code = "" if kind == _KIND_DOMAIN else str(node.data(0, _ROLE_CODE) or "")
        domain_name = str(node.data(0, _ROLE_DOMAIN) or "")

        with self._tree_guard():
            # 清掉占位/更多哨兵，换成这一页的真实子节点。
            for index in reversed(range(node.childCount())):
                child = node.child(index)
                if child.data(0, _ROLE_KIND) in (_KIND_PLACEHOLDER, _KIND_MORE):
                    node.removeChild(child)
            try:
                items = self._service.children(
                    dict_id, parent_code, limit=DEFAULT_PAGE_SIZE, offset=loaded
                )
                total = self._service.count_children(dict_id, parent_code)
            except DictError as exc:
                QMessageBox.warning(self, "字典管理", str(exc))
                return
            for item in items:
                node.addChild(self._item_node(item, domain_name))
            loaded += len(items)
            node.setData(0, _ROLE_LOADED, loaded)
            if loaded < total:
                node.addChild(self._more_node(loaded, total))

    def _reload_branch(self, node: QTreeWidgetItem | None) -> None:
        """分支内容变了（新增条目）后按原有排序重载第一页，避免顺序错乱。"""
        if node is None:
            return
        blocker = QSignalBlocker(self._tree)
        try:
            node.setData(0, _ROLE_LOADED, 0)
            node.takeChildren()
            node.addChild(self._placeholder_node())
            self._load_more(node)
            node.setExpanded(True)
        finally:
            del blocker

    def _domain_node_by_id(self, dict_id: int) -> QTreeWidgetItem | None:
        for index in range(self._tree.topLevelItemCount()):
            node = self._tree.topLevelItem(index)
            if node.data(0, _ROLE_DICT_ID) == dict_id:
                return node
        return None

    # ===================================================== 选择 / 动作按钮

    def _current_kind(self) -> str | None:
        node = self._tree.currentItem()
        return node.data(0, _ROLE_KIND) if node is not None else None

    def _on_current_changed(
        self, _current: QTreeWidgetItem | None, _previous: QTreeWidgetItem | None
    ) -> None:
        self._update_action_buttons()

    def _update_action_buttons(self) -> None:
        kind = self._current_kind()
        has_category = bool(self._category)
        self._btn_sibling.setEnabled(has_category and kind in (_KIND_DOMAIN, _KIND_ITEM))
        self._btn_child.setEnabled(has_category and kind in (_KIND_DOMAIN, _KIND_ITEM))
        self._btn_new_domain.setEnabled(has_category)
        self._btn_edit.setEnabled(kind in (_KIND_DOMAIN, _KIND_ITEM, _KIND_RESULT))
        self._btn_delete.setEnabled(kind in (_KIND_DOMAIN, _KIND_ITEM, _KIND_RESULT))

    def _show_context_menu(self, pos) -> None:
        """节点右键菜单：添加 / 修改 / 删除都在左侧面板内完成。"""
        node = self._tree.itemAt(pos)
        kind = node.data(0, _ROLE_KIND) if node is not None else None
        if kind in (_KIND_DOMAIN, _KIND_ITEM, _KIND_RESULT):
            self._tree.setCurrentItem(node)
        menu = QMenu(self)
        if kind == _KIND_DOMAIN:
            menu.addAction("同级添加字典域", self._on_add_sibling)
            menu.addAction("下级添加字典项", self._on_add_child)
            menu.addSeparator()
            menu.addAction("修改", self._on_edit)
            menu.addAction("删除", self._on_delete)
        elif kind == _KIND_ITEM:
            menu.addAction("同级添加字典项", self._on_add_sibling)
            menu.addAction("下级添加字典项", self._on_add_child)
            menu.addSeparator()
            menu.addAction("修改名称 / 代码", self._on_edit)
            menu.addAction(
                "修改字典域",
                lambda _checked=False, n=node: self._start_edit(n, 2),
            )
            menu.addSeparator()
            menu.addAction("删除", self._on_delete)
        elif kind == _KIND_RESULT:
            menu.addAction("修改名称 / 代码", self._on_edit)
            menu.addAction(
                "修改字典域",
                lambda _checked=False, n=node: self._start_edit(n, 2),
            )
            menu.addSeparator()
            menu.addAction("删除", self._on_delete)
        else:
            menu.addAction("新建字典域", self._on_new_domain)
        menu.exec(self._tree.viewport().mapToGlobal(pos))

    # ===================================================== 拖拽排序

    def _persist_order(self, node: QTreeWidgetItem | None = None) -> None:
        """拖拽结束后把可见同级节点的顺序写回库。

        只重排**已加载的同级节点**：把这一组节点原有的 ``sort_order`` 值排序后
        按新视觉顺序重新分配，未加载的剩余页保持原来的值，不会和它们撞号。
        """
        if node is None:
            node = self._tree.currentItem()
        if node is None:
            return
        kind = node.data(0, _ROLE_KIND)
        try:
            if kind == _KIND_DOMAIN:
                siblings = [
                    self._tree.topLevelItem(i)
                    for i in range(self._tree.topLevelItemCount())
                    if self._tree.topLevelItem(i).data(0, _ROLE_KIND) == _KIND_DOMAIN
                ]
                orders = sorted(int(n.data(0, _ROLE_SORT) or 0) for n in siblings)
                self._service.reorder_domains(
                    self._category,
                    [
                        (int(n.data(0, _ROLE_DICT_ID)), order)
                        for n, order in zip(siblings, orders, strict=True)
                    ],
                )
            elif kind == _KIND_ITEM:
                parent = node.parent()
                if parent is None:
                    return
                siblings = [
                    parent.child(i)
                    for i in range(parent.childCount())
                    if parent.child(i).data(0, _ROLE_KIND) == _KIND_ITEM
                ]
                orders = sorted(int(n.data(0, _ROLE_SORT) or 0) for n in siblings)
                self._service.reorder_items(
                    [
                        (int(n.data(0, _ROLE_ITEM_ID)), order)
                        for n, order in zip(siblings, orders, strict=True)
                    ],
                )
            else:
                return
        except DictError as exc:
            QMessageBox.warning(self, "字典管理", f"保存排序失败：{exc}")
            self._load_domains()
            return
        with self._tree_guard():
            for sibling, order in zip(siblings, orders, strict=True):
                sibling.setData(0, _ROLE_SORT, order)
        self._status_label.setText("已保存排序")

    # ===================================================== 内联修改

    def _on_item_double_clicked(self, node: QTreeWidgetItem, column: int) -> None:
        self._start_edit(node, column)

    def _on_edit(self) -> None:
        """【修改】：从选中节点的名称列开始内联编辑。"""
        node = self._tree.currentItem()
        if node is None:
            QMessageBox.information(self, "字典管理", "请先选择一个字典域或字典项")
            return
        if node.data(0, _ROLE_KIND) not in (_KIND_DOMAIN, _KIND_ITEM, _KIND_RESULT):
            QMessageBox.information(self, "字典管理", "请选择一个可修改的字典域或字典项")
            return
        self._start_edit(node, 0)

    def _start_edit(self, node: QTreeWidgetItem, column: int) -> None:
        kind = node.data(0, _ROLE_KIND)
        editable = _EDITABLE_COLUMNS.get(kind)
        if editable is None or column not in editable:
            return
        self._tree.setCurrentItem(node)
        self._tree.editItem(node, column)

    def _on_item_changed(self, node: QTreeWidgetItem, column: int) -> None:
        """单元格编辑完成 → 立即落库；失败则把显示值回滚。"""
        if self._tree_updating:
            return
        kind = node.data(0, _ROLE_KIND)
        try:
            if kind == _KIND_DOMAIN:
                self._commit_domain_edit(node, column)
            elif kind in (_KIND_ITEM, _KIND_RESULT):
                self._commit_item_edit(node, column)
        except DictError as exc:
            QMessageBox.warning(self, "字典管理", str(exc))
            self._revert_cell(node, column)
        except Exception as exc:
            logger.exception("保存字典编辑失败")
            QMessageBox.critical(self, "字典管理", f"保存失败：{exc}")
            self._revert_cell(node, column)

    def _commit_domain_edit(self, node: QTreeWidgetItem, column: int) -> None:
        dict_id = int(node.data(0, _ROLE_DICT_ID))
        if column == 0:
            old = str(node.data(0, _ROLE_DOMAIN) or "")
            new = node.text(0).strip()
            if new == old:
                return
            if not new:
                raise DictError("字典域名称不能为空")
            domain = self._service.update_domain(dict_id, domain=new)
            with self._tree_guard():
                node.setText(0, domain.domain)
                node.setData(0, _ROLE_DOMAIN, domain.domain)
                # 已加载的下级行第三列跟着改成新域名
                self._sync_child_domain_column(node, domain.domain)
                node.setToolTip(
                    0,
                    f"字典域：{domain.domain}\n{domain.description or '（无说明）'}\n"
                    f"共 {domain.item_count} 条",
                )
            self._status_label.setText(f"已保存字典域「{domain.domain}」")
        elif column == 2:
            old = str(node.data(0, _ROLE_DESC) or "")
            new = node.text(2).strip()
            if new == old:
                return
            domain = self._service.update_domain(dict_id, description=new)
            with self._tree_guard():
                node.setData(0, _ROLE_DESC, domain.description)
                node.setToolTip(
                    0,
                    f"字典域：{domain.domain}\n{domain.description or '（无说明）'}\n"
                    f"共 {domain.item_count} 条",
                )
            self._status_label.setText(f"已保存字典域「{domain.domain}」的说明")

    def _commit_item_edit(self, node: QTreeWidgetItem, column: int) -> None:
        item_id = int(node.data(0, _ROLE_ITEM_ID))
        if column == 0:
            old = str(node.data(0, _ROLE_NAME) or "")
            new = node.text(0).strip()
            if new == old:
                return
            if not new:
                raise DictError("名称不能为空")
            item = self._service.update_item(item_id, name=new)
            with self._tree_guard():
                node.setText(0, item.name)
                node.setData(0, _ROLE_NAME, item.name)
                self._refresh_item_tooltip(node, item)
            self._status_label.setText(f"已保存字典项「{item.name}」")
        elif column == 1:
            old = str(node.data(0, _ROLE_CODE) or "")
            new = node.text(1).strip()
            if new == old:
                return
            if not new:
                raise DictError("代码不能为空")
            item = self._service.update_item(item_id, code=new)
            with self._tree_guard():
                node.setText(1, item.code)
                node.setData(0, _ROLE_CODE, item.code)
                self._refresh_item_tooltip(node, item)
                if item.code != old:
                    self._sync_child_parent_code(node, old, item.code)
            self._status_label.setText(f"已保存字典项「{item.name}」（代码 {item.code}）")
        elif column == 2:
            self._commit_item_domain_edit(node)

    def _commit_item_domain_edit(self, node: QTreeWidgetItem) -> None:
        """把字典项（连同下级）移动到另一个字典域。

        树内编辑发生在 ``itemChanged`` 槽里，此时直接删 / 重建节点不安全：
        落库成功后把界面刷新排到下一拍事件循环。
        """
        item_id = int(node.data(0, _ROLE_ITEM_ID))
        item = self._service.item_by_id(item_id)
        new_name = node.text(2).strip()
        if new_name == item.domain:
            return
        if not new_name:
            raise DictError("字典域不能为空")
        target = next(
            (d for d in self._service.list_domains(self._category) if d.domain == new_name),
            None,
        )
        if target is None:
            raise DictError(f"当前字典表里没有字典域「{new_name}」")
        moved = self._service.move_item_to_domain(item_id, target.id)
        self._status_label.setText(
            f"已把 {moved} 条字典项移动到字典域「{target.domain}」"
        )
        if self._search_text:
            QTimer.singleShot(0, self._run_search)
        else:
            QTimer.singleShot(
                0,
                lambda: self._reload_after_domain_move(node, target.id, item.code),
            )

    def _reload_after_domain_move(
        self, node: QTreeWidgetItem, target_dict_id: int, code: str
    ) -> None:
        """移动完成后刷新旧 / 新两个分支，避免分页计数与展开箭头失真。"""
        target_node = self._domain_node_by_id(target_dict_id)
        if target_node is None:
            # 目标域节点不在树里（理论上不会）：整类重载。
            self._load_domains(select_domain_id=target_dict_id)
            target_node = self._domain_node_by_id(target_dict_id)
            if target_node is None:
                return
            self._reload_branch(target_node)
            found = self._find_node(target_node, code)
            if found is not None:
                self._tree.setCurrentItem(found)
            return
        old_parent = node.parent()
        if old_parent is not None:
            with self._tree_guard():
                old_parent.removeChild(node)
            if old_parent.data(0, _ROLE_KIND) in (_KIND_DOMAIN, _KIND_ITEM):
                self._reload_branch(old_parent)
        self._reload_branch(target_node)
        found = self._find_node(target_node, code)
        if found is not None:
            self._tree.setCurrentItem(found)
            self._tree.scrollToItem(found)
        else:
            self._tree.setCurrentItem(target_node)

    def _revert_cell(self, node: QTreeWidgetItem, column: int) -> None:
        kind = node.data(0, _ROLE_KIND)
        if kind == _KIND_DOMAIN and column == 0:
            value = str(node.data(0, _ROLE_DOMAIN) or "")
        elif kind == _KIND_DOMAIN and column == 2:
            value = str(node.data(0, _ROLE_DESC) or "")
        elif kind in (_KIND_ITEM, _KIND_RESULT) and column == 0:
            value = str(node.data(0, _ROLE_NAME) or "")
        elif kind in (_KIND_ITEM, _KIND_RESULT) and column == 1:
            value = str(node.data(0, _ROLE_CODE) or "")
        elif kind in (_KIND_ITEM, _KIND_RESULT) and column == 2:
            value = str(node.data(0, _ROLE_DOMAIN) or "")
        else:
            return
        with self._tree_guard():
            node.setText(column, value)

    @staticmethod
    def _refresh_item_tooltip(node: QTreeWidgetItem, item: DictItem) -> None:
        node.setToolTip(
            0,
            f"{item.name}\n代码：{item.code}\n上级代码：{item.parent_code or '（无）'}",
        )

    def _sync_child_domain_column(
        self, domain_node: QTreeWidgetItem, domain_name: str
    ) -> None:
        """字典域改名后，已加载的下级行第三列同步显示新域名。"""
        stack = [domain_node]
        while stack:
            parent = stack.pop()
            for index in range(parent.childCount()):
                child = parent.child(index)
                if child.data(0, _ROLE_KIND) == _KIND_ITEM:
                    child.setText(2, domain_name)
                    child.setData(0, _ROLE_DOMAIN, domain_name)
                    stack.append(child)

    def _sync_child_parent_code(
        self, item_node: QTreeWidgetItem, old_code: str, new_code: str
    ) -> None:
        """改代码后，已加载的直接下级行的「上级代码」提示同步更新。"""
        for index in range(item_node.childCount()):
            child = item_node.child(index)
            if child.data(0, _ROLE_KIND) != _KIND_ITEM:
                continue
            if str(child.data(0, _ROLE_PARENT) or "") != old_code:
                continue
            child.setData(0, _ROLE_PARENT, new_code)
            child.setToolTip(
                0,
                f"{child.text(0)}\n代码：{child.text(1)}\n上级代码：{new_code}",
            )

    # ===================================================== 新增 / 删除

    def _on_add_sibling(self) -> None:
        node = self._tree.currentItem()
        kind = node.data(0, _ROLE_KIND) if node is not None else None
        if kind == _KIND_ITEM:
            self._create_item(
                int(node.data(0, _ROLE_DICT_ID)),
                str(node.data(0, _ROLE_DOMAIN) or ""),
                str(node.data(0, _ROLE_PARENT) or ""),
                node.parent(),
                "同级添加",
            )
        elif kind == _KIND_DOMAIN:
            # 字典域的同级 = 新建另一个字典域（新建域默认置顶）。
            self._new_domain()
        else:
            QMessageBox.information(self, "字典管理", "请先选择一个字典域或字典项")

    def _on_add_child(self) -> None:
        node = self._tree.currentItem()
        kind = node.data(0, _ROLE_KIND) if node is not None else None
        if kind == _KIND_ITEM:
            self._create_item(
                int(node.data(0, _ROLE_DICT_ID)),
                str(node.data(0, _ROLE_DOMAIN) or ""),
                str(node.data(0, _ROLE_CODE) or ""),
                node,
                "下级添加",
            )
        elif kind == _KIND_DOMAIN:
            self._create_item(
                int(node.data(0, _ROLE_DICT_ID)),
                str(node.data(0, _ROLE_DOMAIN) or ""),
                "",
                node,
                "下级添加",
            )
        else:
            QMessageBox.information(self, "字典管理", "请先选择一个字典域或字典项")

    def _on_new_domain(self) -> None:
        """左下 / 右键「新建字典域」：在当前字典表下新建一个空字典域。"""
        self._new_domain()

    def _new_domain(self) -> None:
        """新建空字典域并置顶，随后直接进入改名（可再改说明）。"""
        if not self._category:
            QMessageBox.information(self, "字典管理", "请先创建或选择一个字典表")
            return
        try:
            existing = {d.domain for d in self._service.list_domains(self._category)}
            base = "新字典域"
            name = base
            index = 2
            while name in existing:
                name = f"{base}{index}"
                index += 1
            # at_top=True：新域排在最上面（用户要求）。
            domain = self._service.create_domain(
                self._category, name, at_top=True
            )
        except DictError as exc:
            QMessageBox.warning(self, "字典管理", str(exc))
            return
        self._load_domains(select_domain_id=domain.id)
        node = self._domain_node_by_id(domain.id)
        if node is not None:
            self._tree.scrollToItem(node)
            self._start_edit(node, 0)
        self._status_label.setText(f"已新建字典域「{domain.domain}」，可直接改名")

    def _create_item(
        self,
        dict_id: int,
        domain_name: str,
        parent_code: str,
        parent_node: QTreeWidgetItem | None,
        action_label: str,
    ) -> None:
        """新增字典项：先建一条占位记录，再让用户在树里直接改名 / 改代码。"""
        try:
            code = self._unique_item_code(dict_id)
            item = self._service.add_item(dict_id, code, "新字典项", parent_code)
        except DictError as exc:
            QMessageBox.warning(self, "字典管理", str(exc))
            return
        if parent_node is None:
            parent_node = self._domain_node_by_id(dict_id)
        self._reload_branch(parent_node)
        found = self._find_node(parent_node, item.code)
        if found is not None:
            self._tree.setCurrentItem(found)
            self._tree.scrollToItem(found)
            self._status_label.setText(
                f"{action_label}：已新建字典项「{item.name}」（{item.code}），可直接修改"
            )
            self._start_edit(found, 0)
        else:
            self._status_label.setText(
                f"{action_label}：已新建字典项「{item.name}」（{item.code}）；"
                "条目较多，可用搜索定位"
            )

    def _unique_item_code(self, dict_id: int) -> str:
        """生成一个域内未占用的占位代码（NEW1 / NEW2 …）。"""
        index = 1
        while self._service.item_by_code(dict_id, f"NEW{index}") is not None:
            index += 1
        return f"NEW{index}"

    def _find_node(
        self, parent: QTreeWidgetItem | None, code: str
    ) -> QTreeWidgetItem | None:
        """在已加载的子树里按代码找节点（只用于新增后选中，不触发全量加载）。"""
        if parent is None:
            return None
        stack = [parent]
        while stack:
            node = stack.pop()
            for index in range(node.childCount()):
                child = node.child(index)
                if (
                    child.data(0, _ROLE_KIND) == _KIND_ITEM
                    and str(child.data(0, _ROLE_CODE)) == code
                ):
                    return child
                stack.append(child)
        return None

    def _on_delete(self) -> None:
        node = self._tree.currentItem()
        if node is None:
            QMessageBox.information(self, "字典管理", "请先选择一个字典域或字典项")
            return
        kind = node.data(0, _ROLE_KIND)
        try:
            if kind in (_KIND_ITEM, _KIND_RESULT):
                item_id = int(node.data(0, _ROLE_ITEM_ID))
                item = self._service.item_by_id(item_id)
                count = self._service.subtree_count(item_id)
                text = f"确定删除字典项「{item.name}」（{item.code}）吗？"
                if count > 1:
                    text += f"\n\n将连同 {count - 1} 个下级一起删除。"
                if not self._confirm(text):
                    return
                removed = self._service.delete_item(item_id)
                if kind == _KIND_RESULT or self._search_text:
                    self._run_search()
                elif node.parent() is not None:
                    with self._tree_guard():
                        node.parent().removeChild(node)
                self._update_action_buttons()
                self._status_label.setText(f"已删除 {removed} 条字典项")
                return
            if kind == _KIND_DOMAIN:
                domain_id = int(node.data(0, _ROLE_DICT_ID))
                domain = self._service.domain_by_id(domain_id)
                if not self._confirm(
                    f"确定删除字典域「{domain.domain}」吗？\n\n"
                    f"将连同域内 {domain.item_count} 条字典项一起删除。"
                ):
                    return
                removed = self._service.delete_domain(domain_id)
                self._load_domains()
                self._status_label.setText(f"已删除字典域「{domain.domain}」（{removed} 条）")
                return
            QMessageBox.information(self, "字典管理", "请选择一个可删除的字典域或字典项")
        except DictError as exc:
            QMessageBox.warning(self, "字典管理", str(exc))
        except Exception as exc:
            logger.exception("删除字典项失败")
            QMessageBox.critical(self, "字典管理", f"删除失败：{exc}")

    def _confirm(self, text: str) -> bool:
        ret = QMessageBox.question(
            self,
            "删除确认",
            text,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return ret == QMessageBox.StandardButton.Yes

    # ===================================================== 搜索

    def _run_search(self) -> None:
        keyword = self._search_edit.text().strip()
        if not keyword:
            if not self._search_text:
                return
            self._search_text = ""
            self._load_domains()
            return
        if not self._category:
            return
        self._search_text = keyword
        try:
            items = self._service.search(self._category, keyword, limit=DEFAULT_SEARCH_LIMIT)
        except DictError as exc:
            QMessageBox.warning(self, "字典管理", str(exc))
            return
        # **保持原来的「字典域 → 字典项」结构**：先建域树，再隐藏不匹配的
        # 数据。这样搜「地貌」时域节点本身还在，可以直接选中后删掉整个域
        # （扁平搜索结果做不到这一点）。
        self._load_domains(keep_search=True)
        by_domain: dict[str, list[DictItem]] = {}
        for item in items:
            by_domain.setdefault(item.domain, []).append(item)
        visible_domains = 0
        with self._tree_guard():
            for index in range(self._tree.topLevelItemCount()):
                node = self._tree.topLevelItem(index)
                if node.data(0, _ROLE_KIND) != _KIND_DOMAIN:
                    node.setHidden(True)
                    continue
                domain_name = str(node.data(0, _ROLE_DOMAIN) or node.text(0))
                matched = by_domain.get(domain_name, [])
                name_matched = keyword.casefold() in domain_name.casefold()
                if not matched and not name_matched:
                    node.setHidden(True)
                    continue
                # 清掉分页占位：搜索结果按匹配项重建，展开时不再加载未过滤的数据
                for child_index in reversed(range(node.childCount())):
                    child = node.child(child_index)
                    if child.data(0, _ROLE_KIND) in (_KIND_PLACEHOLDER, _KIND_MORE):
                        node.removeChild(child)
                for item in matched:
                    child = self._item_node(item, domain_name)
                    # 匹配到的下级项已经由 SQL 搜索一并返回（扁平挂在域下）；
                    # 去掉占位符，避免展开时加载整页未过滤数据。
                    for sub_index in reversed(range(child.childCount())):
                        if (
                            child.child(sub_index).data(0, _ROLE_KIND)
                            == _KIND_PLACEHOLDER
                        ):
                            child.removeChild(child.child(sub_index))
                    node.addChild(child)
                node.setHidden(False)
                if node.childCount():
                    node.setExpanded(True)
                visible_domains += 1
            self._tree.setCurrentItem(None)
        more = (
            f"（仅显示前 {DEFAULT_SEARCH_LIMIT} 条，请输入更精确的关键词）"
            if len(items) >= DEFAULT_SEARCH_LIMIT
            else ""
        )
        if visible_domains:
            self._status_label.setText(
                f"搜索「{keyword}」：命中 {visible_domains} 个字典域、"
                f"{len(items)} 条字典项（已隐藏其它数据）{more}"
            )
        else:
            self._status_label.setText(f"没有找到匹配「{keyword}」的字典域 / 字典项")
        self._update_action_buttons()

    # ===================================================== Excel 导入 / 导出

    @staticmethod
    @contextmanager
    def _wait_cursor():
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            yield
        finally:
            QApplication.restoreOverrideCursor()

    def _on_import(self) -> None:
        if not self._category:
            QMessageBox.information(self, "Excel导入", "请先创建或选择一个字典表")
            return
        path = open_file_dir(
            self,
            is_dir=False,
            title=f"Excel导入业务字典 → 字典表「{self._category}」",
            init_path=str(Path.home()),
            filter_=IMPORT_FILTER,
        )
        if not path:
            return
        ret = QMessageBox.question(
            self,
            "Excel导入",
            f"文件里的全部字典将导入当前字典表「{self._category}」。\n\n"
            "导入前是否清空文件中出现的同名字典域？\n"
            "「是」= 同名域先清空再导入（替换）\n"
            "「否」= 合并更新（保留原有多余条目）",
            QMessageBox.StandardButton.Yes
            | QMessageBox.StandardButton.No
            | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.No,
        )
        if ret == QMessageBox.StandardButton.Cancel:
            return
        try:
            with self._wait_cursor():
                report = import_excel(
                    path,
                    service=self._service,
                    category=self._category,
                    replace_domains=ret == QMessageBox.StandardButton.Yes,
                )
        except (DictImportError, DictError) as exc:
            QMessageBox.warning(self, "Excel导入失败", str(exc))
            return
        except Exception as exc:
            logger.exception("Excel导入失败")
            QMessageBox.critical(self, "Excel导入失败", str(exc))
            return
        self._load_domains()
        lines = [
            f"已导入字典表「{self._category}」",
            f"共读取 {report.rows_total} 行数据",
            f"新建字典域 {report.domains_created} 个",
            f"新增字典项 {report.items_inserted} 条，更新 {report.items_updated} 条",
        ]
        if report.rows_skipped:
            lines.append(f"跳过 {report.rows_skipped} 行")
        if report.warning_total:
            lines.append("")
            lines.append(f"告警 {report.warning_total} 条：")
            lines.extend(f"· {message}" for message in report.warnings[:8])
            if report.has_more_warnings:
                lines.append(f"……另有 {report.warning_total - len(report.warnings)} 条，详见日志")
        QMessageBox.information(self, "Excel导入完成", "\n".join(lines))

    def _on_export(self) -> None:
        if not self._category:
            QMessageBox.information(self, "Excel导出", "请先创建或选择一个字典表")
            return
        safe_name = re.sub(r'[\\/:*?"<>|]+', "_", self._category).strip() or "业务字典"
        path = save_file(
            self,
            title=f"Excel导出字典表「{self._category}」",
            init_path=str(Path.home() / f"{safe_name}.xlsx"),
            filter_=EXPORT_FILTER,
        )
        if not path:
            return
        try:
            written = normalize_export_path(path)
            with self._wait_cursor():
                written, count = export_excel(
                    written, service=self._service, category=self._category
                )
        except DictExportError as exc:
            QMessageBox.warning(self, "Excel导出失败", str(exc))
            return
        except Exception as exc:
            logger.exception("Excel导出失败")
            QMessageBox.critical(self, "Excel导出失败", str(exc))
            return
        QMessageBox.information(
            self,
            "Excel导出完成",
            f"已导出字典表「{self._category}」的 {count} 条字典项到：\n{written}",
        )
