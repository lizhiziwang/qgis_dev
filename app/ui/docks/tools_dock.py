"""工具树 Dock（通用实现）。

迁自 :class:`component.TreeTool.PGISTools`，把"工具树 UI"与"瓦片后台任务"分离。

纯工具树展示：工具按类别分组，顶部提供搜索过滤与展开/折叠控制。
分组默认折叠，避免打开 dock 时铺满整屏；搜索时会自动展开命中的分组。
点击工具节点弹出对应参数对话框（对话框内部是"左参数 + 右算法描述"布局，
描述逻辑见 :mod:`app.ui.common.algo_desc`）；同一算法只维护一个对话框实例，
已打开时点击置前。``destroy_on_close=True`` 的面板（如「质检」，
见 :mod:`app.ui.docks.quality_dock`）则在窗口关闭后立即销毁实例，下次点击
重新创建，不复用上一次的进度 / 结果状态。

本模块只提供通用树 dock 与「分析工具」这一实例；其它专题面板（如「质检」，
见 :mod:`app.ui.docks.quality_dock`）通过 ``title`` / ``groups`` /
``simple_actions`` / ``tips`` / ``dialog_factories`` 参数复用同一套树 UI。
"""

from __future__ import annotations

from qgis.core import QgsApplication
from qgis.PyQt.QtCore import QSize, Qt
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

from app.infra.logging import get_logger
from app.ui.dialogs.buffer import BufferDialog
from app.ui.dialogs.generic_alg import GenericAlgDialog
from app.ui.dialogs.imagery_mbtiles import ImageryMbtilesDialog
from app.ui.dialogs.imagery_tile import ImageryTileDialog
from app.ui.dialogs.oriented_bbox import OrientedBBoxDialog
from app.ui.dialogs.overlay import OverlayDialog
from app.ui.dialogs.reproject import ReprojectDialog
from app.ui.dialogs.shortest_path import ShortestPathDialog
from app.ui.dialogs.vector_tile import VectorTileDialog

logger = get_logger(__name__)


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

    return ReprojectDialog(parent, title="重投影")


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


#: GDAL processing 算法清单（QGIS 4.2 注册的 57 个 gdal:*）。
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
            ("矢栅转换", "gdal:rasterize"),
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
            ("重切片", "gdal:retile"),
            ("gdal2tiles 切片", "gdal:gdal2tiles"),
            ("栅格信息", "gdal:gdalinfo"),
            ("Grid 移动平均", "gdal:gridaverage"),
            ("Grid 数据度量", "gdal:griddatametrics"),
            ("Grid IDW（幂）", "gdal:gridinversedistance"),
            ("Grid IDW（最近邻搜索）", "gdal:gridinversedistancenearestneighbor"),
            ("Grid 线性插值", "gdal:gridlinear"),
            ("Grid 最近邻", "gdal:gridnearestneighbor"),
            ("格式转换", "gdal:convertformat"),
        ],
    ),
    (
        "GDAL 栅格元数据",
        [
            ("指派投影", "gdal:assignprojection"),
            ("提取投影", "gdal:extractprojection"),
            ("数据集识别", "gdal:dataset_identify"),
        ],
    ),
    (
        "GDAL 矢量分析",
        [
            ("融合", "gdal:dissolve"),
            ("矢量缓冲区", "gdal:buffervectors"),
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
    (
        "GDAL 数据导出",
        [
            ("PostgreSQL 导出（已有连接）", "gdal:importvectorintopostgisdatabaseavailableconnections"),
            ("PostgreSQL 导出（新连接）", "gdal:importvectorintopostgisdatabasenewconnection"),
            ("构建虚拟矢量", "gdal:buildvirtualvector"),
            ("gdal2xyz", "gdal:gdal2xyz"),
        ],
    ),
]


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
            ("重投影", "native:reprojectlayer"),
        ],
    ),
    (
        "数据导出",
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
    ) -> None:
        super().__init__(title, parent)
        self.topParent = parent
        #: 本 dock 的分组树（专题面板可传入自己的分组）
        self._groups: list = _TOOL_GROUPS if groups is None else groups
        #: 一次性动作 / tooltip / 对话框工厂（按引用持有，便于测试替换模块级字典）
        self._simple_actions: dict = _SIMPLE_ACTIONS if simple_actions is None else simple_actions
        self._tips: dict = _TOOL_TIPS if tips is None else tips
        self._dialog_factories: dict = _DIALOG_FACTORIES if dialog_factories is None else dialog_factories
        #: 关闭即销毁（质检面板开）：窗口关闭后不保留实例，下次点击重新创建
        self._destroy_on_close: bool = destroy_on_close
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
        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.setAnimated(True)
        self.tree.setIndentation(18)
        self.tree.setUniformRowHeights(True)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.itemClicked.connect(self._on_item_clicked)
        # 双击 / 回车同样打开（对话框复用逻辑天然幂等）
        self.tree.itemActivated.connect(self._on_item_clicked)
        self.tree.customContextMenuRequested.connect(self._on_context_menu)
        layout.addWidget(self.tree, 1)

        self._build_tree()
        self.setWidget(wrapper)

    def _build_tree(self) -> None:
        """按 :attr:`_groups` 构建分组树；分组加粗，叶子加 tooltip。

        只跳过当前 QGIS 环境不存在的 **GDAL** 算法（如 QGIS 3.40 无
        ``gdal:createcog`` / ``gdal:dataset_identify``），否则点开
        ``GenericAlgDialog`` 会因 ``algorithmById`` 为 None 崩溃。

        判定必须限定在 :data:`_GENERIC_ALGO_CODES`（＝通用工厂接管的那批）：
        手写对话框的 code（如 ``overlay``）**不是** Processing 算法 id，
        ``algorithmById()`` 永远返回 None，拿它一起过滤会把整条工具从树里剔除。
        """
        reg = QgsApplication.processingRegistry()
        group_font = self.tree.font()
        group_font.setBold(True)
        for group_name, tools in self._groups:
            group = QTreeWidgetItem(self.tree)
            group.setText(0, group_name)
            group.setFont(0, group_font)
            group.setSizeHint(0, QSize(0, 28))
            for text, code in tools:
                if code in _GENERIC_ALGO_CODES and reg.algorithmById(code) is None:
                    # 当前 QGIS 版本无此算法（processing provider 未注册）
                    continue
                item = QTreeWidgetItem(group)
                item.setText(0, text)
                item._code_ = code
                tip = self._tips.get(code)
                if tip:
                    item.setToolTip(0, tip)
                item.setSizeHint(0, QSize(0, 28))
            # 默认折叠：打开 dock 时只看到分组名，点开才展开
            group.setExpanded(False)

    # ------------------------------------------------------------ 交互

    def _expand_all(self) -> None:
        self.tree.expandAll()

    def _collapse_all(self) -> None:
        self.tree.collapseAll()

    def _apply_filter(self, text: str) -> None:
        """按名称实时过滤树：隐藏不匹配叶子与无匹配分组；清空后恢复并展开。"""
        text = text.strip().lower()
        for i in range(self.tree.topLevelItemCount()):
            group = self.tree.topLevelItem(i)
            if not text:
                group.setHidden(False)
                for j in range(group.childCount()):
                    group.child(j).setHidden(False)
                group.setExpanded(True)
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
        """右键菜单：叶子 → 打开工具；分组 → 展开/折叠。"""
        item = self.tree.itemAt(pos)
        if item is None:
            return
        menu = QMenu(self.tree)
        code = getattr(item, "_code_", "")
        if code in self._dialog_factories or code in self._simple_actions:
            act = menu.addAction("打开工具")
            act.triggered.connect(lambda: self._on_item_clicked(item, 0))
        else:
            act_text = "折叠" if item.isExpanded() else "展开"
            act = menu.addAction(act_text)
            act.triggered.connect(lambda: item.setExpanded(not item.isExpanded()))
        menu.exec(self.tree.viewport().mapToGlobal(pos))

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
