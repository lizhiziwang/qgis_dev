"""「在线底图」Dock：列出可加载到画布的在线 XYZ 底图图层。

图层清单来自 ``config/online_basemap_config.yaml``（独立清单文件，配置驱动），
按 v2 schema 的 ``groups`` 字段顺序动态分组（如「天地图 / 高德地图」）——分组是
**独立持久化**的一等公民（空分组也可见）。老版顶层 list schema 由各写回原语自动迁移。

交互（右键都是"加东西"为主诉，目标不同、互不冲突；叶子另有「编辑连接」「删除连接」
「移动到分组」；分组另有「重命名分组」「删除分组」）：

- 双击叶子 / 叶子右键「添加到画布」→ 把该底图**图层加载进工程**
  （回调 :data:`on_add`，加进「参考图层」分组渲染）；
- 叶子右键「编辑连接」→ 让 MainWindow 弹窗编辑这条连接（名称/分组/url/API key/
  反盗链认证 Referer 等，预填现有条目），改完**原位替换**回独立清单文件、key 就近
  同步到 ``map.keys``（回调 :data:`on_edit`；dialog 归 MainWindow 所有）；
- 叶子右键「移动到分组」→ 把该条目 group 字段改写到另一个分组（回调
  :data:`on_move_to_group`；空分组不允许作为目标——空分组会同步从 groups 段移除，
  实现「先建组后加条目」的连贯动作）；
- 叶子右键「删除连接」→ 让 MainWindow 确认后把这条连接**从独立清单文件移除**并
  刷新树（回调 :data:`on_delete`；确认弹窗归 MainWindow 所有，已加载图层不受影响）；
- 空白处 / 分组 / 叶子右键「添加在线底图」→ 从**内置模板清单**
  （:data:`ONLINE_BASEMAP_PRESETS`）选一条，把它加进 Dock 的树并写回
  ``online_basemap_config.yaml``（回调 :data:`on_add_template`，由 MainWindow 持久化后
  刷新树）——用户把官方图源删了可一键补回、或把高德各图层逐个加进来，
  已在树里的模板会置灰（按 url 判重）。子菜单末尾追加「自定义底图...」入口，
  弹窗配置自定义底图（XYZ / ArcGIS 影像服务 / ArcGIS 要素服务(FeatureServer 矢量) /
  OGC WMS / WMTS），确定后同样**进树 + 写回独立清单文件**（回调
  :data:`on_add_custom`，MainWindow 内部再走模板持久化，传入 Dock 当前
  :attr:`_groups_order` 作为 dialog 「分组」字段的预设项 —— 从已有分组里选 / 键入
  新名实现「先建组后加条目」连贯动作）。空白处右键即需求主诉。
- 空白处 / 分组 / 叶子右键「新建分组」→ 弹输入框问分组名，独立写入 ``groups:`` 段
  （回调 :data:`on_add_group`）；分组节点额外有「重命名分组」「删除分组」：
  - 「重命名分组」→ :data:`on_rename_group`（校验重名；成功写回 + 刷树）；
  - 「删除分组」→ :data:`on_delete_group`（若分组下还有条目，MainWindow 会弹选择
    dialog：选目标分组重分配 / 一并删除条目 / 取消；**空分组允许直接删除**）。
- 空白处 / 分组 / 叶子右键「管理 Key」→ 让 MainWindow 弹窗集中管理各
  provider 的 API key（天地图 tk 等），落盘 ``config/app.yaml`` 的
  ``map.keys``（回调 :data:`on_manage_keys`；dialog 归 MainWindow 所有）。

本模块不 import ``qgis.core``，只依赖 PyQt + 配置 dataclass + 模板清单，
可离屏单测。真正建图层 / 加工程 / 写配置由 MainWindow 通过回调完成。
"""

from __future__ import annotations

from typing import Callable, Optional

from qgis.PyQt.QtCore import QSize, Qt
from qgis.PyQt.QtWidgets import (
    QAbstractItemView,
    QDockWidget,
    QInputDialog,
    QMenu,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.core.domain.basemap import canonical_basemap_url
from app.core.domain.online_basemap_templates import ONLINE_BASEMAP_PRESETS
from app.env.config import OnlineBasemapDef

#: 空配置时的占位提示（树里没有可选图层；config 缺失 / 解析空时走此提示）
_EMPTY_HINT = "（config 未配置在线底图，空白处右键可添加）"
#: 空分组的占位提示（分组存在但还没有叶子，右键可往里加底图）
_EMPTY_GROUP_HINT = "（右键可添加底图）"
#: 叶子悬浮提示（不展示 url，避免 tk token 悬浮泄露）
_LEAF_TIP = "双击或右键添加到画布"

#: 叶子显示后缀：type 为空 / 为默认 xyz 时不挂（避免“都是 xyz”冗余）。
#: 其余全部加 “ (type)” 后缀——帮用户一眼识别同分组下 wms / wmts / arcgis / arcgisfeature
#: 不同服务类型（同一分组可能有 wms + xyz 混合）。
_TYPE_SUFFIX = {"wms", "wmts", "arcgis", "arcgisfeature"}


def _format_leaf_text(d: "OnlineBasemapDef") -> str:
    """叶子显示文本：``name`` 或 ``name  (type)``（仅 type 为非常规时）。

    双空格 + 圆括号统一后缀格式，便于在 QTreeWidgetItem 文本里扫读。"""
    if d.type and d.type in _TYPE_SUFFIX:
        return f"{d.name}  ({d.type})"
    return d.name


class OnlineBasemapDock(QDockWidget):
    """在线底图图层列表 dock。"""

    def __init__(
        self,
        parent=None,
        on_add: Optional[Callable[[OnlineBasemapDef], bool]] = None,
        on_add_template: Optional[Callable[[OnlineBasemapDef], bool]] = None,
        on_add_custom: Optional[Callable[[list[str]], bool]] = None,
        on_manage_keys: Optional[Callable[[], bool]] = None,
        on_edit: Optional[Callable[[OnlineBasemapDef], bool]] = None,
        on_delete: Optional[Callable[[OnlineBasemapDef], bool]] = None,
        on_add_group: Optional[Callable[[str], bool]] = None,
        on_rename_group: Optional[Callable[[str, str], bool]] = None,
        on_delete_group: Optional[Callable[[str], bool]] = None,
        on_move_to_group: Optional[Callable[[OnlineBasemapDef, str], bool]] = None,
        on_reorder_groups: Optional[Callable[[list[str]], bool]] = None,
    ) -> None:
        super().__init__("在线底图", parent)
        self._on_add: Optional[Callable[[OnlineBasemapDef], bool]] = on_add
        #: 右键「添加在线底图」模板 → 持久化回调（MainWindow 写 app.yaml 后刷新树）
        self._on_add_template: Optional[Callable[[OnlineBasemapDef], bool]] = on_add_template
        #: 右键「添加在线底图 ▸ 自定义底图...」→ 拿到 Dock 当前分组列表后交
        #: MainWindow（弹 dialog + 走同一条模板持久化路径）。传 :attr:`_groups_order`
        #: 让 dialog 「分组」字段成为可编辑下拉——从已有分组里选 / 键入新名均顺。
        self._on_add_custom: Optional[Callable[[list[str]], bool]] = on_add_custom
        #: 右键「管理 Key」→ 无参回调（MainWindow 弹管理窗并写回 map.keys）
        self._on_manage_keys: Optional[Callable[[], bool]] = on_manage_keys
        #: 叶子右键「编辑连接」→ 把要编辑的条目递给 MainWindow（弹窗编辑 + 落盘）
        self._on_edit: Optional[Callable[[OnlineBasemapDef], bool]] = on_edit
        #: 叶子右键「删除连接」→ 把要删除的条目递给 MainWindow（确认 + 移除 + 刷新树）
        self._on_delete: Optional[Callable[[OnlineBasemapDef], bool]] = on_delete
        #: 空白 / 分组 / 叶子右键「新建分组」→ 把新分组名递给 MainWindow（写入 groups: 段）
        self._on_add_group: Optional[Callable[[str], bool]] = on_add_group
        #: 分组右键「重命名分组」→ 把 (old, new) 递给 MainWindow
        self._on_rename_group: Optional[Callable[[str, str], bool]] = on_rename_group
        #: 分组右键「删除分组」→ 把要删的分组名递给 MainWindow（含条目重分配 / 二次确认）
        self._on_delete_group: Optional[Callable[[str], bool]] = on_delete_group
        #: 叶子右键「移动到分组」→ 把 (def, new_group) 递给 MainWindow
        self._on_move_to_group: Optional[Callable[[OnlineBasemapDef, str], bool]] = on_move_to_group
        #: 分组拖拽排序完成后 → 把新分组顺序递给 MainWindow（写回 groups: 段）
        self._on_reorder_groups: Optional[Callable[[list[str]], bool]] = on_reorder_groups
        self._layers: list[OnlineBasemapDef] = []
        #: v2 schema 持久化顺序的分组名列表（= Dock 显示顺序）；set_layers 调用后由 dock 维护
        self._groups_order: list[str] = []
        self.setFeatures(
            QDockWidget.DockWidgetFeature.DockWidgetClosable
            | QDockWidget.DockWidgetFeature.DockWidgetMovable
            | QDockWidget.DockWidgetFeature.DockWidgetFloatable
        )
        self.setAllowedAreas(
            Qt.DockWidgetArea.LeftDockWidgetArea
            | Qt.DockWidgetArea.RightDockWidgetArea
            | Qt.DockWidgetArea.BottomDockWidgetArea
        )
        self._build_ui()

    # ------------------------------------------------------------ UI

    def _build_ui(self) -> None:
        wrapper = QWidget(self)
        layout = QVBoxLayout(wrapper)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(4)

        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.setAnimated(True)
        self.tree.setIndentation(18)
        self.tree.setUniformRowHeights(True)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.itemDoubleClicked.connect(self._on_item_double_clicked)
        self.tree.customContextMenuRequested.connect(self._on_context_menu)
        # 内部拖拽排序：仅 topLevel 分组可拖 / 可接受 drop（叶子与占位节点
        # 在 set_layers 里 setFlags 关闭拖拽 flag）。QTreeWidget 内置 row 重排后
        # 手动补上 ItemIsDropEnabled（默认仅 ItemIsDragEnabled = false = 不可拖）。
        self.tree.setDragEnabled(True)
        self.tree.setAcceptDrops(True)
        self.tree.setDropIndicatorShown(True)
        self.tree.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.tree.setDefaultDropAction(Qt.DropAction.MoveAction)
        # 拖拽行为说明：Movement 走 QTreeView 默认的 Free（Snap 主要给网格类
        # 视图用，文本行场景视觉无差）。PyQt5 / QGIS 打包的 sip binding 偶尔
        # 不把 QAbstractItemView.setMovement 暴露在 QTreeWidget 实例上，
        # 索性不在这里显式 setMovement——不依赖该 setter 也能完成分组重排。
        self.tree.model().rowsMoved.connect(self._on_tree_rows_moved)
        layout.addWidget(self.tree, 1)

        self.setWidget(wrapper)

    def set_layers(
        self,
        defs: list[OnlineBasemapDef],
        groups: Optional[list[str]] = None,
    ) -> None:
        """按 ``groups`` + defs 重建图层树。

        - 分组顺序优先取 ``groups``（v2 schema 持久化顺序 = Dock 显示顺序）；缺省时按
          ``def.group`` 首次出现顺序推导（兼容老调用方，也保证「defs 顺序同手填」语义）。
        - 分组**独立持久化**（v2 schema）：即使该分组下无条目也显示（占位提示“右键可添加底图”），
          由 :meth:`OnlineBasemapMixin._on_online_basemap` 在制调本方法时传入 ``groups``。
        - 叶子挂配置条目（``item._def_``），分组节点挂分组名（``group._group_``），空分组
          占位提示叶子挂 ``item._placeholder_`` 以与正常叶子区分。
        """
        self._layers = list(defs)
        self.tree.clear()

        # 推导分组顺序：groups 优先；缺省从 defs 推导（与老版行为一致）
        if groups is None:
            order: list[str] = []
            seen: set[str] = set()
            for d in defs:
                g = d.group or "在线底图"
                if g not in seen:
                    seen.add(g)
                    order.append(g)
            groups = order
        self._groups_order = list(groups)

        if not self._layers and not groups:
            hint = QTreeWidgetItem(self.tree)
            hint.setText(0, _EMPTY_HINT)
            return

        group_font = self.tree.font()
        group_font.setBold(True)
        # defs 按 group 索引
        by_group: dict[str, list[OnlineBasemapDef]] = {}
        for d in defs:
            by_group.setdefault(d.group or "在线底图", []).append(d)
        for g in groups:
            group_item = QTreeWidgetItem(self.tree)
            group_item.setText(0, g)
            group_item.setFont(0, group_font)
            group_item.setSizeHint(0, QSize(0, 26))
            group_item.setExpanded(True)
            group_item._group_ = g  # 分组节点挂分组名（便于右键读当前分组）
            # 分组节点：可拖 / 可接受 drop（仅限 topLevel 互拖，由 _on_tree_rows_moved
            # 负责验证 dest 也是 invisibleRoot，避免拖动被子节点拦截 / 误判）。
            group_item.setFlags(
                group_item.flags()
                | Qt.ItemFlag.ItemIsDragEnabled
                | Qt.ItemFlag.ItemIsDropEnabled
            )
            leaves = by_group.get(g, [])
            if not leaves:
                # 空分组占位提示（不可点击、右键走“新建分组 / 移动到分组”可能仕）
                ph = QTreeWidgetItem(group_item)
                ph.setText(0, _EMPTY_GROUP_HINT)
                ph.setSizeHint(0, QSize(0, 24))
                ph._placeholder_ = True
                ph.setFlags(
                    ph.flags()
                    & ~Qt.ItemFlag.ItemIsSelectable
                    & ~Qt.ItemFlag.ItemIsDragEnabled
                    & ~Qt.ItemFlag.ItemIsDropEnabled
                )
            else:
                for d in leaves:
                    item = QTreeWidgetItem(group_item)
                    item.setText(0, _format_leaf_text(d))
                    item.setSizeHint(0, QSize(0, 26))
                    item.setToolTip(0, _LEAF_TIP)
                    item._def_ = d  # 叶子挂配置条目（仿 tools_dock 的 _code_ 惯用法）
                    # 叶子：不可拖 / 不可接受 drop（避免拖到另一分组下误改 group 字段）
                    item.setFlags(
                        item.flags()
                        & ~Qt.ItemFlag.ItemIsDragEnabled
                        & ~Qt.ItemFlag.ItemIsDropEnabled
                    )

    # ------------------------------------------------------------ 交互

    def _on_item_double_clicked(self, item: QTreeWidgetItem, column: int) -> None:
        """双击叶子 → 把该底图加载到画布。"""
        d = getattr(item, "_def_", None)
        if d is not None:
            self._add_def(d)

    def _on_context_menu(self, pos) -> None:
        """右键：叶子/分组/空白都挂「添加在线底图」（模板）；空白为主诉。"""
        menu = self._build_context_menu(self.tree.itemAt(pos))
        if menu is not None:
            menu.exec(self.tree.viewport().mapToGlobal(pos))

    def _on_tree_rows_moved(self, parent, start, end, dest, dest_row) -> None:
        """分组拖拽完成后由 QTreeWidget 触发：验证合法后调用 :attr:`_on_reorder_groups`。

        仅接受 topLevel 互拖（``parent`` 和 ``dest`` 都是 :attr:`invisibleRootItem`），
        且移动后顺序与原顺序**不同**（同序拖回不调用回调）。叶子 / 占位节点的拖拽
        flag 已被 :meth:`set_layers` 关闭，正常路径不会到达本 handler——本防御性检查
        是"双重保险"，避免依赖层旗变。
        """
        root = self.tree.invisibleRootItem()
        if parent is not root or dest is not root:
            return  # 子节点 / 跨层移动 → 忽略
        if start == dest_row or (start < dest_row <= end + 1):
            return  # 拖回原位 / 空拖
        new_order = [
            self.tree.topLevelItem(i)._group_ for i in range(self.tree.topLevelItemCount())
        ]
        if new_order == self._groups_order:
            return  # 顺序未变
        self._groups_order = new_order
        if self._on_reorder_groups is None:
            return
        self._on_reorder_groups(list(new_order))

    def _build_context_menu(self, item: Optional[QTreeWidgetItem]) -> Optional[QMenu]:
        """按命中的位置组菜单（不 exec，便于单测断言菜单结构）。

        布局：
            叶子 / 分组 / 空白 → 添加在线底图 ▸（含模板 + 自定义底图...）/ 新建分组 → 管理 Key
            叶子额外：　　添加到画布 / 编辑连接 / 移动到分组 ▸ / 删除连接
            分组额外：　　折叠或展开 / 重命名分组 / 删除分组

        - 位置识别用 :attr:`_placeholder_`（空分组占位提示）/ :attr:`_def_`（叶子）
          / :attr:`_group_`（分组标题）3 个 ``QTreeWidgetItem`` 属性；占位点右键同空白处理。
        """
        menu = QMenu(self.tree)
        group_name: Optional[str] = None
        leaf_def: Optional[OnlineBasemapDef] = None
        is_placeholder = bool(getattr(item, "_placeholder_", False))
        if item is not None and not is_placeholder:
            d = getattr(item, "_def_", None)
            if d is not None:
                leaf_def = d
            else:
                group_name = getattr(item, "_group_", None)

        # 1）位置专属动作
        if leaf_def is not None:
            act = menu.addAction("添加到画布")
            act.triggered.connect(lambda checked=False, d=leaf_def: self._add_def(d))
            act = menu.addAction("编辑连接")
            act.triggered.connect(lambda checked=False, d=leaf_def: self._edit_def(d))
            self._append_move_to_group_submenu(menu, leaf_def)
            act = menu.addAction("删除连接")
            act.triggered.connect(lambda checked=False, d=leaf_def: self._delete_def(d))
            menu.addSeparator()
        elif item is not None and group_name is not None:
            act_text = "折叠" if item.isExpanded() else "展开"
            act = menu.addAction(act_text)
            act.triggered.connect(lambda: item.setExpanded(not item.isExpanded()))
            menu.addSeparator()

        # 2）通用动作（三个位置都有）
        self._append_add_submenu(menu)
        menu.addSeparator()
        new_group = menu.addAction("新建分组")
        new_group.triggered.connect(lambda checked=False: self._add_group())

        # 3）分组专属动作
        if group_name is not None:
            menu.addSeparator()
            rename = menu.addAction("重命名分组")
            rename.triggered.connect(lambda checked=False, g=group_name: self._rename_group(g))
            delete = menu.addAction("删除分组")
            delete.triggered.connect(lambda checked=False, g=group_name: self._delete_group(g))

        menu.addSeparator()
        manage = menu.addAction("管理 Key")
        manage.triggered.connect(lambda checked=False: self._manage_keys())
        if not menu.actions():
            return None
        return menu

    def _append_add_submenu(self, menu: QMenu) -> QMenu:
        """挂「添加在线底图 ▸ 分组 ▸ 模板」+ 末尾「自定义底图...」。

        模板清单是内置的（:data:`ONLINE_BASEMAP_PRESETS`）；已在树里的模板
        （url 判重，经 :func:`canonical_basemap_url` 归一——老配置内嵌真实
        tk 的 url 与 ``{tk}`` 模板等价）置灰——加过就不让重复加。触发走
        :meth:`_add_template`。

        底部追加「自定义底图...」入口，触发走 :meth:`_add_custom`（dialog
        「分组」字段为可编辑下拉，传入 Dock 当前 ``_groups_order``）。
        """
        add_menu = menu.addMenu("添加在线底图")
        if not ONLINE_BASEMAP_PRESETS:
            disabled = add_menu.addAction("（暂无内置模板）")
            disabled.setEnabled(False)
        else:
            existing_urls = {canonical_basemap_url(d.url) for d in self._layers if d.url}
            for group, group_defs in self._grouped_presets():
                sub = add_menu.addMenu(group)
                for d in group_defs:
                    act = sub.addAction(d.name)
                    # 默认参数绑定 d：避免循环变量晚绑定，触发时全指最后一条
                    act.triggered.connect(lambda checked=False, dd=d: self._add_template(dd))
                    if d.url and canonical_basemap_url(d.url) in existing_urls:
                        act.setEnabled(False)
                        act.setToolTip("已在在线底图清单中")
        # 自定义底图始终可用——与内置模板并列在「添加在线底图」下，避免
        # “添加在线底图 ▸”与“添加自定义底图”两项同名的设置语义错位。
        add_menu.addSeparator()
        custom = add_menu.addAction("自定义底图...")
        custom.triggered.connect(lambda checked=False: self._add_custom())
        return add_menu

    def _grouped_presets(self):
        """把内置模板按 ``group`` 首次出现顺序分组（保持书写顺序）。"""
        order: list[str] = []
        by_group: dict[str, list[OnlineBasemapDef]] = {}
        for d in ONLINE_BASEMAP_PRESETS:
            if d.group not in by_group:
                by_group[d.group] = []
                order.append(d.group)
            by_group[d.group].append(d)
        return [(g, by_group[g]) for g in order]

    def _append_move_to_group_submenu(self, menu: QMenu, d: OnlineBasemapDef) -> QMenu:
        """挂「移动到分组 ▸ 分组名」子菜单（排除当前 ``d.group`` 本身）。

        仅以 Dock 当前可见的分组集合（_groups_order）作为目标；无其他分组则置灰
        “（无其他分组）”。触发走 :meth:`_move_to_group`。
        """
        move_menu = menu.addMenu("移动到分组")
        current = d.group or "在线底图"
        targets = [g for g in self._groups_order if g != current]
        if not targets:
            disabled = move_menu.addAction("（无其他分组）")
            disabled.setEnabled(False)
            return move_menu
        for g in targets:
            act = move_menu.addAction(g)
            act.triggered.connect(lambda checked=False, gg=g, dd=d: self._move_to_group(dd, gg))
        return move_menu

    def _add_def(self, d: OnlineBasemapDef) -> bool:
        """把回调交给 MainWindow 加载该底图到画布；返回其执行结果。"""
        if self._on_add is None:
            return False
        return self._on_add(d)

    def _add_template(self, d: OnlineBasemapDef) -> bool:
        """把模板交给 MainWindow（写回独立清单文件并刷新树）；返回其执行结果。"""
        if self._on_add_template is None:
            return False
        return self._on_add_template(d)

    def _add_custom(self) -> bool:
        """右键「添加在线底图 ▸ 自定义底图...」：让 MainWindow 弹配置窗并持久化。

        本模块不 import 对话框（dialog 归 MainWindow 管，这里只发意图 + 当前
        ``_groups_order`` 作为 dialog 「分组」下拉字段的预设项）。
        """
        if self._on_add_custom is None:
            return False
        return self._on_add_custom(list(self._groups_order))

    def _manage_keys(self) -> bool:
        """右键「管理 Key」：让 MainWindow 弹管理窗并写回 ``map.keys``。

        无回调（未接 MainWindow）时返回 ``False``——便于 dock 单测断言。
        """
        if self._on_manage_keys is None:
            return False
        return self._on_manage_keys()

    def _edit_def(self, d: OnlineBasemapDef) -> bool:
        """叶子右键「编辑连接」：把要编辑的条目交给 MainWindow（弹窗编辑 + 落盘）。

        无回调（未接 MainWindow）时返回 ``False``——便于 dock 单测断言。
        """
        if self._on_edit is None:
            return False
        return self._on_edit(d)

    def _delete_def(self, d: OnlineBasemapDef) -> bool:
        """叶子右键「删除连接」：把要删除的条目交给 MainWindow（确认 + 移除 + 刷新树）。

        无回调（未接 MainWindow）时返回 ``False``——便于 dock 单测断言。
        """
        if self._on_delete is None:
            return False
        return self._on_delete(d)

    def _add_group(self) -> bool:
        """右键「新建分组」：弹输入框问分组名，后交 MainWindow 写入 ``groups:`` 段。

        校验由 MainWindow 负责（含「重名」与「YAML 保留字符」），dock 只负责发意图。
        无回调时返回 ``False``。
        """
        if self._on_add_group is None:
            return False
        name, ok = QInputDialog.getText(self, "新建分组", "分组名：", text="")
        if not ok:
            return False
        return self._on_add_group(name.strip())

    def _rename_group(self, old: str) -> bool:
        """分组右键「重命名分组」：弹输入框预填 ``old``，后交 MainWindow 重命名。

        无回调时返回 ``False``；用户取消也返回 ``False``。
        """
        if self._on_rename_group is None:
            return False
        new, ok = QInputDialog.getText(
            self, "重命名分组", f"新名（当前「{old}」）：", text=old
        )
        if not ok:
            return False
        return self._on_rename_group(old, new.strip())

    def _delete_group(self, name: str) -> bool:
        """分组右键「删除分组」：把要删的分组名交给 MainWindow（重分配 / 二次确认）。

        确认弹窗、重分配选择、cascade 选项都在 MainWindow 里走；dock 不负责选择逻辑。
        """
        if self._on_delete_group is None:
            return False
        return self._on_delete_group(name)

    def _move_to_group(self, d: OnlineBasemapDef, new_group: str) -> bool:
        """叶子右键「移动到分组」：把 (def, new_group) 交给 MainWindow 改写 group 字段。

        无回调时返回 ``False``。
        """
        if self._on_move_to_group is None:
            return False
        return self._on_move_to_group(d, new_group)
