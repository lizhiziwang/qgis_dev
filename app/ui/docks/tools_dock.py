"""工具树 Dock（通用实现）。

迁自 :class:`component.TreeTool.PGISTools`，把"工具树 UI"与"瓦片后台任务"分离。

纯工具树展示：工具按类别分组，顶部提供搜索过滤与展开/折叠控制。
分组默认折叠，避免打开 dock 时铺满整屏；搜索时会自动展开命中的分组。
双击工具节点弹出对应参数对话框（对话框内部是"左参数 + 右算法描述"布局，
描述逻辑见 :mod:`app.ui.common.algo_desc`）；同一算法只维护一个对话框实例，
已打开时点击置前。面板可选择 ``destroy_on_close=True``（关闭即销毁）；
「质检」面板复用实例，由对话框自身在每次显示时复位运行态、保留参数
（见 :mod:`app.ui.docks.quality_dock`）。

支持把常用工具收藏到最前面的「收藏」分组：右键任意工具 → 收藏 / 取消收藏，
收藏状态落盘到 ``<config>/state/tool_favorites.json``（见
:mod:`app.infra.fs.state`）；收藏过的工具在原分组里也会带 ``★`` 前缀。
「收藏」分组内的条目可以直接拖拽调整顺序，顺序与收藏一起保存。

本模块只提供通用树 dock 与「分析工具」这一实例；其它专题面板（如「质检」，
见 :mod:`app.ui.docks.quality_dock`）通过 ``title`` / ``groups`` /
``simple_actions`` / ``tips`` / ``dialog_factories`` 参数复用同一套树 UI。
"""

from __future__ import annotations

from qgis.core import QgsApplication
from qgis.PyQt.QtCore import QSize, Qt, QTimer
from qgis.PyQt.QtWidgets import (
    QAbstractItemView,
    QDockWidget,
    QHBoxLayout,
    QLineEdit,
    QMenu,
    QToolButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.infra.fs.state import load_state, save_state
from app.infra.logging import get_logger
from app.ui.dialogs.buffer import BufferDialog
from app.ui.dialogs.generic_alg import GenericAlgDialog
from app.ui.dialogs.imagery_mbtiles import ImageryMbtilesDialog
from app.ui.dialogs.imagery_tile import ImageryTileDialog
from app.ui.dialogs.oriented_bbox import OrientedBBoxDialog
from app.ui.dialogs.overlay import OverlayDialog
from app.ui.dialogs.reproject import ReprojectDialog
from app.ui.dialogs.shortest_path import ShortestPathDialog
from app.ui.dialogs.vector_format import VectorFormatDialog
from app.ui.dialogs.vector_tile import VectorTileDialog

logger = get_logger(__name__)

#: 收藏工具的落盘状态名（``<config>/state/tool_favorites.json``）
_FAVORITES_STATE = "tool_favorites"

#: 收藏分组标题 / 未收藏时的提示行文字
_FAVORITES_GROUP = "收藏"
_FAVORITES_HINT = "右键任意工具 → 收藏"
#: 收藏项在工具原名前的标记（QGIS 主题没有星形图标，用文本最稳）
_FAVORITE_MARK = "★ "

#: 首次运行时预置的收藏：实际项目里最常用的一批（按使用频率排序）。
#: 只在状态文件还没有 ``seeded`` 标记时写入，之后完全尊重用户自己的增删。
_DEFAULT_FAVORITES: tuple[str, ...] = (
    "vector:convert",  # 格式转换（矢量）
    "native:reprojectlayer",  # 重投影（矢量）
    "overlay",  # 叠加分析
    "gdal:cliprasterbymasklayer",  # 按掩膜裁剪栅格
    "native:buffer",  # 矢量缓冲区
    "gdal:dissolve",  # 融合
    "gdal:merge",  # 拼接栅格
    "gdal:warpreproject",  # 重投影（栅格）
    "native:tilesxyzmbtiles",  # 影像切片（MBTiles）
    "native:tilesxyzdirectory",  # 影像切片（XYZ）
    "native:writevectortiles_xyz",  # 生成矢量瓦片（XYZ）
    "gdal:clipvectorbypolygon",  # 按多边形裁剪矢量
)


def _load_favorites(state: str) -> list[str]:
    """读收藏工具 code 列表（保持顺序、去重）。

    首次运行（或旧状态没有 ``seeded`` 标记）时预置 :data:`_DEFAULT_FAVORITES`
    并写回状态；用户之后取消收藏不会在下次启动时被重新加回来。
    """
    data = load_state(state)
    values = data.get("tools")
    codes: list[str] = []
    seen: set[str] = set()
    if isinstance(values, list):
        for value in values:
            if isinstance(value, str) and value and value not in seen:
                seen.add(value)
                codes.append(value)
    if data.get("seeded"):
        return codes
    merged = codes + [code for code in _DEFAULT_FAVORITES if code not in seen]
    save_state(state, {"tools": merged, "seeded": True})
    return merged


def _save_favorites(state: str, codes: list[str]) -> None:
    save_state(state, {"tools": list(codes), "seeded": True})


def _make_vector_tile_dialog(parent):

    return VectorTileDialog(parent, title="创建矢量瓦片", dia_type="native:writevectortiles_xyz")


def _make_buffer_dialog(parent):

    return BufferDialog(parent, title="矢量缓冲区")


def _make_oriented_bbox_dialog(parent):

    return OrientedBBoxDialog(parent, title="最小外包矩形")


def _make_overlay_dialog(parent):

    return OverlayDialog(parent, title="叠加分析")


def _make_shortest_path_dialog(parent):

    return ShortestPathDialog(parent, title="最短路径分析")


def _make_reproject_dialog(parent):

    return ReprojectDialog(parent, title="重投影（矢量）")


def _make_vector_format_dialog(parent):

    return VectorFormatDialog(parent, title="格式转换（矢量）")


def _make_imagery_tile_dialog(parent):

    return ImageryTileDialog(parent, title="影像切片")


def _make_imagery_mbtiles_dialog(parent):

    return ImageryMbtilesDialog(parent, title="影像切片（MBTiles）")


def _make_flow_editor(parent):
    """流程节点编排工具：打开主窗口内的流程编辑器（懒建单实例）。"""
    opener = getattr(parent, "_open_flow_editor", None)
    if opener is not None:
        opener()


def _make_generic_algo_dialog(algo_id: str):
    """为单个 processing 算法返回构造 ``GenericAlgDialog`` 的工厂（闭包捕获 algo_id）。"""

    def _factory(parent):
        return GenericAlgDialog(parent, algo_id=algo_id)

    return _factory


#: GDAL processing 算法清单（QGIS 4.2 注册的 59 个 gdal:*，这里收录需要上架的 48 个）。
#: 结构：(分组名, [(中文显示名, algo_id), ...])
#: 显示名 = 中文翻译（部分与 QGIS 自家一致，部分自定义）
_GDAL_ALGORITHMS: list[tuple[str, list[tuple[str, str]]]] = [
    (
        "GDAL 栅格分析",
        [
            ("栅格计算器", "gdal:rastercalculator"),
            ("按范围裁剪栅格", "gdal:cliprasterbyextent"),
            ("按掩膜裁剪栅格", "gdal:cliprasterbymasklayer"),
            ("重投影（栅格）", "gdal:warpreproject"),
            ("格式转换（栅格）", "gdal:translate"),
            ("拼接栅格", "gdal:merge"),
            ("重排波段", "gdal:rearrange_bands"),
            ("PCT 转 RGB", "gdal:pcttorgb"),
            ("RGB 转 PCT", "gdal:rgbtopct"),
            ("创建 COG", "gdal:createcog"),
            ("构建虚拟栅格", "gdal:buildvirtualraster"),
            ("栅格矢量化", "gdal:polygonize"),
            ("筛除小图斑", "gdal:sieve"),
            ("矢量转栅格", "gdal:rasterize"),
            ("属性值栅格化（覆盖）", "gdal:rasterize_over"),
            ("固定值栅格化（覆盖）", "gdal:rasterize_over_fixed_value"),
            ("填充 NoData", "gdal:fillnodata"),
            ("去黑边", "gdal:nearblack"),
        ],
    ),
    (
        "GDAL 栅格地形",
        [
            ("坡向", "gdal:aspect"),
            ("坡度", "gdal:slope"),
            ("山体阴影", "gdal:hillshade"),
            ("粗糙度", "gdal:roughness"),
            ("TPI 地形位置指数", "gdal:tpitopographicpositionindex"),
            ("TRI 地表 ruggedness", "gdal:triterrainruggednessindex"),
            ("距离栅格", "gdal:proximity"),
            ("彩色渲染", "gdal:colorrelief"),
            ("等高线", "gdal:contour"),
            ("等高线多边形", "gdal:contour_polygon"),
            ("可视域", "gdal:viewshed"),
        ],
    ),
    (
        "GDAL 栅格数据源",
        [
            ("构建金字塔", "gdal:overviews"),
            ("栅格信息", "gdal:gdalinfo"),
            ("Grid 移动平均", "gdal:gridaverage"),
            ("Grid 数据度量", "gdal:griddatametrics"),
            ("Grid IDW（幂）", "gdal:gridinversedistance"),
            ("Grid IDW（最近邻搜索）", "gdal:gridinversedistancenearestneighbor"),
            ("Grid 线性插值", "gdal:gridlinear"),
            ("Grid 最近邻", "gdal:gridnearestneighbor"),
        ],
    ),
    (
        "GDAL 栅格元数据",
        [
            ("指派投影", "gdal:assignprojection"),
            ("提取投影", "gdal:extractprojection"),
        ],
    ),
    (
        "GDAL 矢量分析",
        [
            ("融合", "gdal:dissolve"),
            ("偏移线", "gdal:offsetcurve"),
            ("单边缓冲区", "gdal:onesidebuffer"),
            ("按范围裁剪矢量", "gdal:clipvectorbyextent"),
            ("按多边形裁剪矢量", "gdal:clipvectorbypolygon"),
            ("OGR 执行 SQL", "gdal:executesql"),
            ("沿线生成点", "gdal:pointsalonglines"),
            ("瓦片索引", "gdal:tileindex"),
            ("矢量信息", "gdal:ogrinfo"),
        ],
    ),
]

#: 「GDAL 数据导出」（PostgreSQL 导出 / 构建虚拟矢量）整体不在此树上架。
#: 算法本身仍由处理框架注册，需要时可用 :class:`GenericAlgDialog` 直接打开。


#: GDAL 工具 tooltip：尽量复用 QGIS 官方算法描述作为兜底文案。
#: 完整描述已在 :func:`app.ui.common.algo_desc.make_description_browser` 通过
#: ``shortHelpString()`` 动态拉取，这里只给出一行工具提示，足够紧凑。
_GDAL_TIPS: dict[str, str] = {
    code: f"GDAL 算法：{name}"
    for _group, items in _GDAL_ALGORITHMS
    for name, code in items
}


#: 工具节点 code → 对话框工厂（懒 import，避免 dock 构造时拉进全部 dialog）
_DIALOG_FACTORIES: dict = {
    "native:writevectortiles_xyz": _make_vector_tile_dialog,
    "native:buffer": _make_buffer_dialog,
    "native:orientedminimumboundingbox": _make_oriented_bbox_dialog,
    "overlay": _make_overlay_dialog,
    "native:shortestpathpointtopoint": _make_shortest_path_dialog,
    "native:reprojectlayer": _make_reproject_dialog,
    "vector:convert": _make_vector_format_dialog,
    "native:tilesxyzdirectory": _make_imagery_tile_dialog,
    "native:tilesxyzmbtiles": _make_imagery_mbtiles_dialog,
}
#: 把 GDAL 算法全部注册到通用 dialog 工厂（同时记下这批 code，供 :func:`ToolsDock._build_tree` 过滤）
#: ——**只有这些**才需要检查"当前 QGIS 有没有"；手写对话框（如 ``overlay``）本来
#: 就不是 Processing 算法 id，拿去查永远查不到，会把工具误删。
_GENERIC_ALGO_CODES: set[str] = set()
for _code in (code for _group, _items in _GDAL_ALGORITHMS for _name, code in _items):
    _DIALOG_FACTORIES[_code] = _make_generic_algo_dialog(_code)
    _GENERIC_ALGO_CODES.add(_code)

#: 工具节点 code → 一次性动作（打开非对话框窗口，无需实例缓存）
_SIMPLE_ACTIONS = {
    "flow:editor": _make_flow_editor,
}

#: 工具节点 code → 一行说明（树节点 tooltip）
_TOOL_TIPS: dict = {
    "native:writevectortiles_xyz": "按 XYZ 模板把矢量图层导出为矢量瓦片",
    "native:buffer": "按指定距离为输入图层生成缓冲区面",
    "native:orientedminimumboundingbox": "为输入图层每个要素生成方向最小化的外包矩形",
    "overlay": "对两个矢量图层做叠加分析",
    "native:shortestpathpointtopoint": "计算两点之间的最短路径",
    "native:reprojectlayer": "把图层重投影到目标坐标系",
    "vector:convert": "把矢量图层转换为 GeoPackage / Shapefile / GeoJSON 等常见格式",
    "native:tilesxyzdirectory": "把当前地图画布切片为 XYZ 瓦片目录",
    "native:tilesxyzmbtiles": "把当前地图画布切片为 MBTiles 文件",
    "flow:editor": "可视化流程节点编排：拖节点建流程、连线、运行与导出",
}
_TOOL_TIPS.update(_GDAL_TIPS)

#: 树分组：(分组名, [(工具名, 算法 code), ...])
_TOOL_GROUPS: list = [
    (
        "矢量分析",
        [
            ("矢量缓冲区", "native:buffer"),
            ("最小外包矩形", "native:orientedminimumboundingbox"),
            ("叠加分析", "overlay"),
            ("最短路径分析", "native:shortestpathpointtopoint"),
            ("重投影（矢量）", "native:reprojectlayer"),
            ("格式转换（矢量）", "vector:convert"),
        ],
    ),
    (
        "切片导出",
        [
            ("生成矢量瓦片（XYZ）", "native:writevectortiles_xyz"),
            ("影像切片（XYZ）", "native:tilesxyzdirectory"),
            ("影像切片（MBTiles）", "native:tilesxyzmbtiles"),
        ],
    ),
    (
        "流程编排",
        [
            ("节点式流程编排", "flow:editor"),
        ],
    ),
]
#: 把 6 个 GDAL 分组追加到尾部（按 GDAL 内部分类连续出现，便于展开后整块阅读）
_TOOL_GROUPS.extend(_GDAL_ALGORITHMS)


class _ToolsTree(QTreeWidget):
    """工具树：只允许「收藏」分组内的条目拖拽排序，其它分组保持只读。

    QTreeWidget 自带的 ``InternalMove`` 会把条目移动到任意分组，而且移动走
    mime 序列化，条目上的 Python 属性（``_code_``）会丢。这里不让视图自己移动：
    只计算落点，把 ``(code, 目标位置)`` 交给 dock 更新收藏顺序并重建树。
    """

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.setDropIndicatorShown(True)
        #: 收藏重排回调 ``(code, 目标插入位置)``；None 时不处理拖拽
        self.on_favorite_reorder = None
        self._drag_code = ""

    def _favorites_group(self):
        """当前树的「收藏」分组（没有收藏功能 / 分组被清空时返回 None）。"""
        if self.topLevelItemCount() == 0:
            return None
        group = self.topLevelItem(0)
        return group if getattr(group, "_favorites_group_", False) else None

    def _favorite_drop_index(self, pos) -> int | None:
        """把鼠标落点换算成「收藏」分组内的插入位置；落在别处返回 None。"""
        group = self._favorites_group()
        if group is None or not self._drag_code:
            return None
        target = self.itemAt(pos)
        if target is None:
            return None
        if target is group:
            return group.childCount()
        if target.parent() is not group:
            return None
        index = group.indexOfChild(target)
        if (
            self.dropIndicatorPosition()
            == QAbstractItemView.DropIndicatorPosition.BelowItem
        ):
            index += 1
        return index

    def startDrag(self, supportedActions) -> None:  # noqa: N802, N803 —— Qt 虚函数覆盖
        self._drag_code = ""
        group = self._favorites_group()
        item = self.currentItem()
        if group is None or item is None or item.parent() is not group:
            return
        code = getattr(item, "_code_", "")
        if not code:
            return
        self._drag_code = code
        super().startDrag(supportedActions)

    def dragMoveEvent(self, event) -> None:
        if self._favorite_drop_index(event.position().toPoint()) is None:
            event.ignore()
            return
        super().dragMoveEvent(event)

    def dropEvent(self, event) -> None:
        index = self._favorite_drop_index(event.position().toPoint())
        code = self._drag_code
        self._drag_code = ""
        if index is None or not code:
            event.ignore()
            return
        event.acceptProposedAction()
        callback = self.on_favorite_reorder
        if callback is not None:
            # 不在 dropEvent 里重建树：等视图处理完这次拖拽再改模型
            QTimer.singleShot(0, lambda: _run_reorder(callback, code, index))


def _run_reorder(callback, code: str, index: int) -> None:
    """延迟执行收藏重排；dock 已销毁时静默忽略（槽内异常会杀进程）。"""
    try:
        callback(code, index)
    except RuntimeError:
        pass
    except Exception:  # noqa: BLE001 —— 定时器回调里未捕获异常会直接杀进程
        logger.exception("重排收藏失败：%s → %s", code, index)


class ToolsDock(QDockWidget):
    """工具树 dock（通用实现）。

    :param parent: 主窗口（点击叶子时作 ``topParent`` 传给对话框工厂）
    :param title: dock 标题，默认「分析工具」
    :param groups: 分组树；``None`` 时用模块级「分析工具」分组 :data:`_TOOL_GROUPS`
    :param simple_actions: code → 一次性动作；``None`` 时用 :data:`_SIMPLE_ACTIONS`
    :param tips: code → 树节点 tooltip；``None`` 时用 :data:`_TOOL_TIPS`
    :param dialog_factories: code → 对话框工厂；``None`` 时用 :data:`_DIALOG_FACTORIES`
    :param destroy_on_close: ``True`` 时对话框关闭（确认 / 取消 / 标题栏关闭）即销毁
        实例，下次点击重新创建；``False``（默认）时缓存实例，关闭后复用
    :param favorites_state: 收藏状态的落盘名（``<config>/state/<名字>.json``）；
        ``None`` 表示本面板不提供收藏。默认给「分析工具」用，质检等专题面板
        传 ``None``，避免与主面板共用同一份收藏
    """

    def __init__(
        self,
        parent=None,
        *,
        title: str = "分析工具",
        groups: list | None = None,
        simple_actions: dict | None = None,
        tips: dict | None = None,
        dialog_factories: dict | None = None,
        destroy_on_close: bool = False,
        favorites_state: str | None = _FAVORITES_STATE,
    ) -> None:
        super().__init__(title, parent)
        # saveState/restoreState 按 objectName 匹配 Dock；空名字会被 Qt 丢弃并告警。
        # 类名 + 标题保证「分析工具 / 质检」等同类面板各自唯一。
        self.setObjectName(f"{type(self).__name__}:{title}")
        self.topParent = parent
        #: 本 dock 的分组树（专题面板可传入自己的分组）
        self._groups: list = _TOOL_GROUPS if groups is None else groups
        #: 一次性动作 / tooltip / 对话框工厂（按引用持有，便于测试替换模块级字典）
        self._simple_actions: dict = _SIMPLE_ACTIONS if simple_actions is None else simple_actions
        self._tips: dict = _TOOL_TIPS if tips is None else tips
        self._dialog_factories: dict = _DIALOG_FACTORIES if dialog_factories is None else dialog_factories
        #: 关闭即销毁（质检面板开）：窗口关闭后不保留实例，下次点击重新创建
        self._destroy_on_close: bool = destroy_on_close
        #: 收藏（按收藏顺序保存；``favorites_state`` 为 None 时不提供收藏）
        self._favorites_state: str | None = favorites_state
        self._favorites: list[str] = (
            _load_favorites(favorites_state) if favorites_state else []
        )
        self._favorites_set: set[str] = set(self._favorites)
        #: code → 工具显示名（收藏分组从原始分组取标题）
        self._tool_titles: dict[str, str] = {}
        self.setFeatures(QDockWidget.DockWidgetFeature.DockWidgetClosable | QDockWidget.DockWidgetFeature.DockWidgetMovable | QDockWidget.DockWidgetFeature.DockWidgetFloatable)
        self.setAllowedAreas(
            Qt.DockWidgetArea.LeftDockWidgetArea | Qt.DockWidgetArea.RightDockWidgetArea | Qt.DockWidgetArea.BottomDockWidgetArea
        )
        #: 算法 code → 当前对话框实例。同一算法只允许一个窗口，已打开时点击置前。
        self._dialogs: dict = {}
        self._build_ui()

    def _build_ui(self) -> None:
        wrapper = QWidget(self)
        layout = QVBoxLayout(wrapper)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        # ---- 顶部工具条：搜索过滤 + 展开/折叠 ----
        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(4)
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("搜索工具…")
        self.search_edit.setClearButtonEnabled(True)
        self.search_edit.textChanged.connect(self._apply_filter)
        header.addWidget(self.search_edit, 1)

        self.btn_expand = QToolButton()
        self.btn_expand.setText("展开")
        self.btn_expand.setToolTip("展开所有分组")
        self.btn_expand.clicked.connect(self._expand_all)
        header.addWidget(self.btn_expand)

        self.btn_collapse = QToolButton()
        self.btn_collapse.setText("折叠")
        self.btn_collapse.setToolTip("折叠所有分组")
        self.btn_collapse.clicked.connect(self._collapse_all)
        header.addWidget(self.btn_collapse)

        layout.addLayout(header)

        # ---- 工具树 ----
        self.tree = _ToolsTree()
        self.tree.on_favorite_reorder = self._on_favorite_reorder
        if self._favorites_state is None:
            # 质检等专题面板不提供收藏，也就不需要拖拽
            self.tree.setDragEnabled(False)
            self.tree.setAcceptDrops(False)
            self.tree.setDragDropMode(QAbstractItemView.DragDropMode.NoDragDrop)
        self.tree.setHeaderHidden(True)
        self.tree.setAnimated(True)
        self.tree.setIndentation(18)
        self.tree.setUniformRowHeights(True)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        # 单击只做选中 / 展开；双击才打开工具
        self.tree.itemDoubleClicked.connect(self._on_item_clicked)
        self.tree.customContextMenuRequested.connect(self._on_context_menu)
        layout.addWidget(self.tree, 1)

        self._build_tree()
        self.setWidget(wrapper)

    def _build_tree(self, expanded: set[str] | None = None) -> None:
        """按 :attr:`_groups` 构建分组树；收藏分组固定在最前。

        只跳过当前 QGIS 环境不存在的 **GDAL** 算法（如 QGIS 3.40 无
        ``gdal:createcog`` / ``gdal:dataset_identify``），否则点开
        ``GenericAlgDialog`` 会因 ``algorithmById`` 为 None 崩溃。

        判定必须限定在 :data:`_GENERIC_ALGO_CODES`（＝通用工厂接管的那批）：
        手写对话框的 code（如 ``overlay``）**不是** Processing 算法 id，
        ``algorithmById()`` 永远返回 None，拿它一起过滤会把整条工具从树里剔除。

        :param expanded: 重建时要保持展开的分组名；``None`` 表示默认全部折叠
            （收藏分组有内容时始终展开）。
        """
        reg = QgsApplication.processingRegistry()
        group_font = self.tree.font()
        group_font.setBold(True)

        self._tool_titles = {}
        for _group_name, tools in self._groups:
            for text, code in tools:
                self._tool_titles.setdefault(code, text)

        if self._favorites_state:
            self._build_favorites_group(group_font, reg)

        for group_name, tools in self._groups:
            group = QTreeWidgetItem(self.tree)
            group.setText(0, group_name)
            group.setFont(0, group_font)
            group.setSizeHint(0, QSize(0, 28))
            # 普通分组只读：不接收拖放，避免把收藏项拖出收藏分组
            group.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
            for text, code in tools:
                if code in _GENERIC_ALGO_CODES and reg.algorithmById(code) is None:
                    # 当前 QGIS 版本无此算法（processing provider 未注册）
                    continue
                item = QTreeWidgetItem(group)
                item.setText(0, self._leaf_text(text, code))
                item._code_ = code
                # 普通工具不可拖拽；只有「收藏」分组里的副本可以拖
                item.setFlags(
                    Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
                )
                tip = self._tips.get(code)
                if tip:
                    item.setToolTip(0, tip)
                item.setSizeHint(0, QSize(0, 28))
            # 默认折叠：打开 dock 时只看到分组名，点开才展开
            group.setExpanded(group_name in expanded if expanded is not None else False)

    def _build_favorites_group(self, group_font, reg) -> None:
        """在最前面建「收藏」分组；没有收藏时放一行提示。"""
        group = QTreeWidgetItem(self.tree)
        group.setText(0, _FAVORITES_GROUP)
        group.setFont(0, group_font)
        group.setSizeHint(0, QSize(0, 28))
        group._favorites_group_ = True
        # 收藏分组接收拖放（用于把条目拖到分组标题上追加到末尾）
        group.setFlags(
            Qt.ItemFlag.ItemIsEnabled
            | Qt.ItemFlag.ItemIsSelectable
            | Qt.ItemFlag.ItemIsDropEnabled
        )

        shown = 0
        for code in self._favorites:
            title = self._tool_titles.get(code)
            if title is None:
                continue
            if code in _GENERIC_ALGO_CODES and reg.algorithmById(code) is None:
                continue
            item = QTreeWidgetItem(group)
            item.setText(0, title)
            item._code_ = code
            # 收藏项可拖拽、可作为落点（落点位置由 _ToolsTree 自行计算）
            item.setFlags(
                Qt.ItemFlag.ItemIsEnabled
                | Qt.ItemFlag.ItemIsSelectable
                | Qt.ItemFlag.ItemIsDragEnabled
                | Qt.ItemFlag.ItemIsDropEnabled
            )
            tip = self._tips.get(code)
            if tip:
                item.setToolTip(0, tip)
            item.setSizeHint(0, QSize(0, 28))
            shown += 1

        if not shown:
            hint = QTreeWidgetItem(group)
            hint.setText(0, _FAVORITES_HINT)
            hint.setFlags(Qt.ItemFlag.NoItemFlags)
            hint.setSizeHint(0, QSize(0, 28))
        group.setExpanded(True)

    def _leaf_text(self, text: str, code: str) -> str:
        """收藏过的工具在原名前加星标，方便在原分组里一眼看到。"""
        return f"{_FAVORITE_MARK}{text}" if code in self._favorites_set else text

    # ------------------------------------------------------------ 交互

    def _expand_all(self) -> None:
        self.tree.expandAll()

    def _collapse_all(self) -> None:
        self.tree.collapseAll()

    def _apply_filter(self, text: str) -> None:
        """按名称实时过滤树：隐藏不匹配叶子与无匹配分组；清空后恢复并展开。

        搜索时隐藏「收藏」分组：收藏项在原分组里也会命中，避免同一工具出现两次。
        """
        text = text.strip().lower()
        for i in range(self.tree.topLevelItemCount()):
            group = self.tree.topLevelItem(i)
            if not text:
                group.setHidden(False)
                for j in range(group.childCount()):
                    group.child(j).setHidden(False)
                group.setExpanded(True)
                continue
            if getattr(group, "_favorites_group_", False):
                group.setHidden(True)
                continue
            group_match = text in group.text(0).lower()
            matched = 0
            for j in range(group.childCount()):
                child = group.child(j)
                show = group_match or text in child.text(0).lower()
                child.setHidden(not show)
                if show:
                    matched += 1
            group.setHidden(matched == 0)
            if group_match:
                group.setExpanded(True)

    def _on_context_menu(self, pos) -> None:
        """右键菜单：叶子 → 打开 / 收藏；分组 → 展开/折叠。"""
        item = self.tree.itemAt(pos)
        if item is None:
            return
        if item.flags() == Qt.ItemFlag.NoItemFlags:
            # 「右键任意工具 → 收藏」提示行等不可操作项
            return
        menu = QMenu(self.tree)
        code = getattr(item, "_code_", "")
        if code in self._dialog_factories or code in self._simple_actions:
            act = menu.addAction("打开工具")
            act.triggered.connect(lambda: self._on_item_clicked(item, 0))
            if self._favorites_state:
                menu.addSeparator()
                is_favorite = code in self._favorites_set
                fav = menu.addAction("取消收藏" if is_favorite else "收藏")
                fav.triggered.connect(
                    lambda _checked=False, c=code: self._toggle_favorite(c)
                )
        else:
            act_text = "折叠" if item.isExpanded() else "展开"
            act = menu.addAction(act_text)
            act.triggered.connect(lambda: item.setExpanded(not item.isExpanded()))
        menu.exec(self.tree.viewport().mapToGlobal(pos))

    def _toggle_favorite(self, code: str) -> None:
        """收藏 / 取消收藏并立即重建树（收藏分组、星标同步刷新）。"""
        state = self._favorites_state
        if state is None:
            return
        if code in self._favorites_set:
            self._favorites = [c for c in self._favorites if c != code]
        else:
            self._favorites.append(code)
        self._favorites_set = set(self._favorites)
        _save_favorites(state, self._favorites)
        self._rebuild_tree()

    def _on_favorite_reorder(self, code: str, target_index: int) -> None:
        """把收藏项移动到「收藏」分组内的 ``target_index`` 位置并落盘。

        ``target_index`` 是按拖拽落点算出的**移动前**插入位置：源项在目标
        之前时要减 1 抵消它自己占的位置。
        """
        state = self._favorites_state
        if state is None or code not in self._favorites_set:
            return
        source_index = self._favorites.index(code)
        reordered = [c for c in self._favorites if c != code]
        insert_at = target_index - 1 if target_index > source_index else target_index
        insert_at = max(0, min(insert_at, len(reordered)))
        reordered.insert(insert_at, code)
        if reordered == self._favorites:
            return
        self._favorites = reordered
        self._favorites_set = set(reordered)
        _save_favorites(state, reordered)
        self._rebuild_tree()
        self._select_favorite(code)

    def _select_favorite(self, code: str) -> None:
        """重建树后把当前项定位到刚拖动的收藏项。"""
        group = self.tree.topLevelItem(0)
        if group is None or not getattr(group, "_favorites_group_", False):
            return
        group.setExpanded(True)
        for i in range(group.childCount()):
            item = group.child(i)
            if getattr(item, "_code_", "") == code:
                self.tree.setCurrentItem(item)
                self.tree.scrollToItem(item)
                return

    def _rebuild_tree(self) -> None:
        """重建工具树，保留分组展开状态 / 搜索词 / 滚动位置。"""
        expanded = {
            self.tree.topLevelItem(i).text(0)
            for i in range(self.tree.topLevelItemCount())
            if self.tree.topLevelItem(i).isExpanded()
        }
        scroll = self.tree.verticalScrollBar().value()
        search = self.search_edit.text()
        self.tree.clear()
        self._build_tree(expanded)
        if search.strip():
            self._apply_filter(search)
        self.tree.verticalScrollBar().setValue(scroll)

    def _on_item_clicked(self, item, column) -> None:
        code = getattr(item, "_code_", "")
        try:
            # 一次性动作（打开非对话框窗口，如流程编辑器）
            simple = self._simple_actions.get(code)
            if simple is not None:
                simple(self.topParent)
                return
            factory = self._dialog_factories.get(code)
            if factory is None:
                return
            dlg = self._dialogs.get(code)
            if dlg is not None:
                try:
                    if dlg.isVisible():
                        # 已有该算法窗口实例：置前，不新建
                        dlg.raise_()
                        dlg.activateWindow()
                        return
                except RuntimeError:
                    # C++ 对象已被销毁（防御性清理，正常流程不会走到）
                    self._dialogs.pop(code, None)
                    dlg = None
            if dlg is None:
                dlg = factory(self.topParent)
                self._dialogs[code] = dlg
                if self._destroy_on_close:
                    self._bind_destroy_on_close(code, dlg)
            dlg.show()
        except Exception as exc:  # noqa: BLE001
            # 不静默吞掉：曾因静默异常导致功能"无反应"难排查（见 CanvasZoomTool 教训）
            logger.error("打开工具对话框失败 code=%s: %s", code, exc, exc_info=True)

    def _bind_destroy_on_close(self, code: str, dlg) -> None:
        """绑定「关闭即销毁」：窗口结束后摘掉缓存引用并 ``deleteLater``，下次点击重建。

        无边框标题栏的「关闭」按钮走 :meth:`QDialog.reject`（不是 ``close``），吃不到
        ``WA_DeleteOnClose``；统一挂在 ``finished`` 上——确认 / 取消 / 关闭按钮 / Esc
        各条路径都会发出该信号。

        回调里只捕获缓存字典与对话框本身（不捕获 ``self``）：即使面板先被销毁，回调
        也不会摸到已析构的 dock。
        """
        dialogs = self._dialogs

        def _release(*_args, code=code, dlg=dlg, dialogs=dialogs):
            dialogs.pop(code, None)
            dlg.deleteLater()

        finished = getattr(dlg, "finished", None)
        if finished is None:
            # 非 QDialog（理论上不会有）：退回 Qt 原生「关闭即销毁」，只摘缓存引用
            dlg.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
            dlg.destroyed.connect(lambda *_a, code=code, dialogs=dialogs: dialogs.pop(code, None))
            return
        finished.connect(_release)
