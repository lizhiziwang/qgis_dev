"""「在线底图」Dock：列出可加载到画布的在线 XYZ 底图图层。

图层清单来自 ``config/online_basemap_config.yaml``（独立清单文件，配置驱动），
按条目的 ``group`` 字段动态分组（如「天地图 / 高德地图」）。

交互（右键都是"加东西"，目标不同、互不冲突；叶子另有「编辑连接」「删除连接」）：
- 双击叶子 / 叶子右键「添加到画布」→ 把该底图**图层加载进工程**
  （回调 :data:`on_add`，加进「参考图层」分组渲染）；
- 叶子右键「编辑连接」→ 让 MainWindow 弹窗编辑这条连接（名称/分组/url/API key/
  反盗链认证 Referer 等，预填现有条目），改完**原位替换**回独立清单文件、key 就近
  同步到 ``map.keys``（回调 :data:`on_edit`；dialog 归 MainWindow 所有）；
- 叶子右键「删除连接」→ 让 MainWindow 确认后把这条连接**从独立清单文件移除**并
  刷新树（回调 :data:`on_delete`；确认弹窗归 MainWindow 所有，已加载图层不受影响）；
- 空白处 / 分组 / 叶子右键「添加在线底图」→ 从**内置模板清单**
  （:data:`ONLINE_BASEMAP_PRESETS`）选一条，把它加进 Dock 的树并写回
  ``online_basemap_config.yaml``（回调 :data:`on_add_template`，由 MainWindow 持久化后
  刷新树）——用户把官方图源删了可一键补回、或把高德各图层逐个加进来，
  已在树里的模板会置灰（按 url 判重）。空白处右键即需求主诉。
- 空白处 / 分组 / 叶子右键「添加自定义底图」→ 弹窗配置自定义底图
  （XYZ / ArcGIS 影像服务 / ArcGIS 要素服务(FeatureServer 矢量) / OGC WMS / WMTS），
  确定后同样**进树 + 写回
  独立清单文件**（回调 :data:`on_add_custom`，MainWindow 内部再走模板持久化，
  这里只发意图，dialog 归 MainWindow 所有）。
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
    QMenu,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.core.domain.basemap import canonical_basemap_url
from app.core.domain.online_basemap_templates import ONLINE_BASEMAP_PRESETS
from app.env.config import OnlineBasemapDef

#: 空配置时的占位提示（树里没有可选图层）
_EMPTY_HINT = "（config 未配置在线底图，空白处右键可添加）"
#: 叶子悬浮提示（不展示 url，避免 tk token 悬浮泄露）
_LEAF_TIP = "双击或右键添加到画布"


class OnlineBasemapDock(QDockWidget):
    """在线底图图层列表 dock。"""

    def __init__(
        self,
        parent=None,
        on_add: Optional[Callable[[OnlineBasemapDef], bool]] = None,
        on_add_template: Optional[Callable[[OnlineBasemapDef], bool]] = None,
        on_add_custom: Optional[Callable[[], bool]] = None,
        on_manage_keys: Optional[Callable[[], bool]] = None,
        on_edit: Optional[Callable[[OnlineBasemapDef], bool]] = None,
        on_delete: Optional[Callable[[OnlineBasemapDef], bool]] = None,
    ) -> None:
        super().__init__("在线底图", parent)
        self._on_add: Optional[Callable[[OnlineBasemapDef], bool]] = on_add
        #: 右键「添加在线底图」模板 → 持久化回调（MainWindow 写 app.yaml 后刷新树）
        self._on_add_template: Optional[Callable[[OnlineBasemapDef], bool]] = on_add_template
        #: 右键「添加自定义底图」→ 无参回调（MainWindow 弹配置窗，dialog 归它管）
        self._on_add_custom: Optional[Callable[[], bool]] = on_add_custom
        #: 右键「管理 Key」→ 无参回调（MainWindow 弹管理窗并写回 map.keys）
        self._on_manage_keys: Optional[Callable[[], bool]] = on_manage_keys
        #: 叶子右键「编辑连接」→ 把要编辑的条目递给 MainWindow（弹窗编辑 + 落盘）
        self._on_edit: Optional[Callable[[OnlineBasemapDef], bool]] = on_edit
        #: 叶子右键「删除连接」→ 把要删除的条目递给 MainWindow（确认 + 移除 + 刷新树）
        self._on_delete: Optional[Callable[[OnlineBasemapDef], bool]] = on_delete
        self._layers: list[OnlineBasemapDef] = []
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
        layout.addWidget(self.tree, 1)

        self.setWidget(wrapper)

    def set_layers(self, defs: list[OnlineBasemapDef]) -> None:
        """按配置条目重建图层树；分组取 ``group`` 首次出现顺序。"""
        self._layers = list(defs)
        self.tree.clear()

        if not self._layers:
            hint = QTreeWidgetItem(self.tree)
            hint.setText(0, _EMPTY_HINT)
            return

        group_font = self.tree.font()
        group_font.setBold(True)
        groups: dict[str, QTreeWidgetItem] = {}
        for d in self._layers:
            group_item = groups.get(d.group)
            if group_item is None:
                group_item = QTreeWidgetItem(self.tree)
                group_item.setText(0, d.group)
                group_item.setFont(0, group_font)
                group_item.setSizeHint(0, QSize(0, 26))
                group_item.setExpanded(True)
                groups[d.group] = group_item
            item = QTreeWidgetItem(group_item)
            item.setText(0, d.name)
            item.setSizeHint(0, QSize(0, 26))
            item.setToolTip(0, _LEAF_TIP)
            item._def_ = d  # 叶子挂配置条目（仿 tools_dock 的 _code_ 惯用法）

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

    def _build_context_menu(self, item: Optional[QTreeWidgetItem]) -> Optional[QMenu]:
        """按命中的位置组菜单（不 exec，便于单测断言菜单结构）。

        - 叶子：首项「添加到画布」（把该底图加载到画布），紧跟「编辑连接」
          （弹窗编辑这条连接并原位写回清单）与「删除连接」（确认后从清单移除）；
        - 分组标题：首项「展开 / 折叠」；
        - 之后统一挂「添加在线底图 ▸（内置模板，已在树里则置灰）」和
          「添加自定义底图」（弹窗自定义服务），覆盖空白处右键（需求主诉）。
        """
        menu = QMenu(self.tree)
        if item is not None:
            d = getattr(item, "_def_", None)
            if d is not None:
                act = menu.addAction("添加到画布")
                act.triggered.connect(lambda checked=False, d=d: self._add_def(d))
                act = menu.addAction("编辑连接")
                act.triggered.connect(lambda checked=False, d=d: self._edit_def(d))
                act = menu.addAction("删除连接")
                act.triggered.connect(lambda checked=False, d=d: self._delete_def(d))
                menu.addSeparator()
            else:
                act_text = "折叠" if item.isExpanded() else "展开"
                act = menu.addAction(act_text)
                act.triggered.connect(lambda: item.setExpanded(not item.isExpanded()))
                menu.addSeparator()
        self._append_add_submenu(menu)
        menu.addSeparator()
        custom = menu.addAction("添加自定义底图")
        custom.triggered.connect(lambda checked=False: self._add_custom())
        menu.addSeparator()
        manage = menu.addAction("管理 Key")
        manage.triggered.connect(lambda checked=False: self._manage_keys())
        if not menu.actions():
            return None
        return menu

    def _append_add_submenu(self, menu: QMenu) -> QMenu:
        """挂「添加在线底图 ▸ 分组 ▸ 模板」，返回子菜单。

        模板清单是内置的（:data:`ONLINE_BASEMAP_PRESETS`）；已在树里的模板
        （url 判重，经 :func:`canonical_basemap_url` 归一——老配置内嵌真实
        tk 的 url 与 ``{tk}`` 模板等价）置灰——加过就不让重复加。触发走
        :meth:`_add_template`。
        """
        add_menu = menu.addMenu("添加在线底图")
        if not ONLINE_BASEMAP_PRESETS:
            disabled = add_menu.addAction("（暂无内置模板）")
            disabled.setEnabled(False)
            return add_menu
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
        """右键「添加自定义底图」：让 MainWindow 弹配置窗并持久化。

        本模块不 import 对话框（dialog 归 MainWindow 管，这里只发意图）。
        """
        if self._on_add_custom is None:
            return False
        return self._on_add_custom()

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
