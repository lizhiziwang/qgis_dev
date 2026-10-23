"""图层树 Dock。

封装 :class:`QgsLayerTreeView` + :class:`QgsLayerTreeModel`，并发出右键菜单信号。

Dock 内容顶部带一条 QGIS 风格的头部工具条（只发信号，行为由主窗口实现）：

- 打开图层渲染设置（自建图层属性对话框 → 渲染 tab）→ ``render_tab_requested``
- 添加图层组 → ``add_group_requested``
- 删除图层/组 → ``remove_requested``
- 全部展开 / 全部折叠 → ``expand_all_requested`` / ``collapse_all_requested``
- 上移 / 下移 → ``move_up_requested`` / ``move_down_requested``

右键菜单同样只发信号：``context_menu_requested(layer, 全局坐标)``——图层由
``index2node`` 从被点的节点直接解出（**不按图层名回查**，重名图层不会拿错），
菜单本体由主窗口构建，且与工程管理 Dock 共用同一套。

矢量图层行末尾还带一个「标注」开关（见 :class:`_LabelRadioDelegate`）：一个 Radio +
紧跟其后的「标注」二字，选中态 = 图层 ``labelsEnabled()``，点击发
``label_toggle_requested(layer, 期望状态)``，标注的读写同样由主窗口实现。

开关**始终**画在行尾、不参与布局：图层名太长就压在名字上（先用与行底色一致的
胶囊盖掉，左缘渐变淡出），所以 Dock 再窄也不会把开关挤没。想看全名可以拉宽 Dock，
或把鼠标悬在行上——委托会弹完整图层名的气泡（见 :meth:`_LabelRadioDelegate.helpEvent`）。
"""

from __future__ import annotations

import os
from contextlib import suppress

from qgis.core import (
    QgsApplication,
    QgsLayerTree,
    QgsLayerTreeGroup,
    QgsLayerTreeLayer,
    QgsLayerTreeModel,
    QgsProject,
    QgsVectorLayer,
)
from qgis.gui import QgsLayerTreeView
from qgis.PyQt import sip
from qgis.PyQt.QtCore import (
    QEvent,
    QModelIndex,
    QPoint,
    QRectF,
    QSize,
    Qt,
    QTimer,
    pyqtSignal,
)
from qgis.PyQt.QtGui import (
    QColor,
    QCursor,
    QFont,
    QFontMetrics,
    QIcon,
    QLinearGradient,
    QPainter,
    QPen,
    QPixmap,
)
from qgis.PyQt.QtWidgets import (
    QAbstractItemView,
    QButtonGroup,
    QDockWidget,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QStackedWidget,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QToolButton,
    QToolTip,
    QVBoxLayout,
    QWidget,
)

from app.ui.common.theme import DOCK_SELECTION_ALPHA, ThemeManager

# QGIS 主题图标名（3.40 / 4.2 两个运行时均已实测可用）
_THEME_ICONS = {
    "add_layer": "mActionAddLayer.svg",
    "view_order": "mIconLayerTree.svg",
    "view_source": "mActionDataSourceManager.svg",
    "style": "mActionOptions.svg",
    "group": "mActionAddGroup.svg",
    "remove": "mActionRemoveLayer.svg",
    "expand": "mActionExpandTree.svg",
    "collapse": "mActionCollapseTree.svg",
    "up": "mActionArrowUp.svg",
    "down": "mActionArrowDown.svg",
}

# (按钮属性名, 图标键, 信号名, 悬停提示)
# 顺序：先「按图层顺序 / 按数据源」两个展示方式按钮（用户要求放在添加图层
# 前面），再是 QGIS 桌面图层面板那套：添加图层 → 样式 → 添加组 → 删除 →
# 展开/折叠所有 → 上移/下移。展示方式按钮信号名 None = 特殊处理。
_HEADER_SPECS = (
    ("btn_view_order", "view_order", None, "按图层顺序展示"),
    ("btn_view_source", "view_source", None, "按数据源展示"),
    ("btn_add_layer", "add_layer", "add_layer_requested", "添加数据（矢量 / 栅格 / GDB / GeoPackage / SQLite）"),
    ("btn_render_tab", "style", "render_tab_requested", "打开图层渲染设置"),
    ("btn_add_group", "group", "add_group_requested", "添加图层组"),
    ("btn_remove", "remove", "remove_requested", "删除图层/组"),
    ("btn_expand_all", "expand", "expand_all_requested", "全部展开"),
    ("btn_collapse_all", "collapse", "collapse_all_requested", "全部折叠"),
    ("btn_move_up", "up", "move_up_requested", "上移图层/组"),
    ("btn_move_down", "down", "move_down_requested", "下移图层/组"),
)


def _theme_icon(name: str) -> QIcon:
    """取 QGIS 主题图标；不可用时返回空图标（按钮仍可点）。"""
    with suppress(Exception):
        icon = QgsApplication.getThemeIcon(name)
        if icon is not None and not icon.isNull():
            return icon
    return QIcon()


class _SourceTreeModel(QgsLayerTreeModel):
    """「按数据源展示」用的模型：**分组行不显示勾选框**。

    QGIS 的组节点默认带一个可见性勾选框（勾上/取消会级联组内图层）。数据源
    分组只是归类，不是工程里的真实图层组，给它勾选框既多余又容易误导 ——
    只有图层行保留勾选框（控制显示 / 隐藏）。
    """

    def _is_group_index(self, index) -> bool:
        if not index.isValid():
            return False
        try:
            node = self.index2node(index)
        except Exception:  # noqa: BLE001 —— 野索引不能拖垮绘制
            return False
        return isinstance(node, QgsLayerTreeGroup)

    def flags(self, index):
        flags = super().flags(index)
        if self._is_group_index(index):
            flags &= ~Qt.ItemFlag.ItemIsUserCheckable
        return flags

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if (
            role == Qt.ItemDataRole.CheckStateRole
            and self._is_group_index(index)
        ):
            return None
        return super().data(index, role)


#: 「按数据源展示」里非文件类图层的分组（键, 显示名）
_MEMORY_SOURCE = ("~memory", "内存图层")


def _layer_source_group(layer) -> tuple[str, str]:
    """图层 → ``(分组键, 显示名)``（「按数据源展示」用）。

    与 ArcMap 的 List By Source 一致：**同一个工作空间/库的图层归到一组**，
    而不是一个图层一组。Shapefile 按所在文件夹归组；GeoPackage / SQLite /
    FileGDB 按库文件（目录）归组；内存图层单独一组；其余在线服务按数据源分组。
    """
    try:
        provider = str(layer.providerType() or "")
        source = str(layer.source() or "")
    except RuntimeError:  # 图层对象已销毁
        return ("~unknown", "未知数据源")
    if provider in ("memory", "virtual"):
        return _MEMORY_SOURCE
    container = source
    if provider in ("ogr", "gdal", "spatialite"):
        try:
            from app.core.util.local_layer_loader import layer_source_container

            container = str(layer_source_container(layer) or source)
        except Exception:  # noqa: BLE001 —— 解析失败按原始 source 归组
            container = source.split("|", 1)[0].strip() or source
    else:
        # 在线服务 / 数据库连接：source 里就带着主机与库名，整串归组
        return (f"{provider}:{source}", source or provider or "未知数据源")
    if not container:
        return (f"~{provider}", provider or "未知数据源")
    lowered = container.lower()
    if "://" in lowered:
        return (container, container)
    if lowered.endswith((".shp", ".dbf", ".shx", ".tab", ".mif", ".mid")):
        folder = os.path.dirname(container) or container
        return (folder, folder)
    return (container, container)


# 「标注」开关的几何（相对行矩形右端）
_RADIO_SIZE = 14.0  # 圆点直径
_RADIO_HIT_PAD = 4.0  # 命中判定外扩，行尾点偏一点也能中
_CAPTION_TEXT = "标注"  # 圆点后面跟的两个字
_CAPTION_RIGHT_MARGIN = 8.0  # 文字右端距行右端留白
_CAPTION_GAP = 5.0  # 文字与圆点的间距
# 开关**始终**画在行尾：行再窄也画，宁可压在图层名上（用户要求）。压住字的地方
# 先用一块与行底色一致的胶囊盖掉，长名从胶囊左缘淡出，开关才不至于被名字糊住。
_PILL_PAD_X = 7.0  # 胶囊在开关左侧多盖一点，给淡出留过渡
_PILL_FADE = 12.0  # 胶囊左缘「透明 → 行底色」的渐变宽度（长名在这段里淡出）
# layer item 行高下限：行更舒展，行尾开关的点击区也更稳
_ROW_MIN_HEIGHT = 22


#: 数据源丢失时替换图层图标的红色感叹号（只建一次）
_MISSING_ICON: QIcon | None = None


def _missing_icon() -> QIcon:
    """图层图标位置上的红色感叹号：白描边 + 红色本体，深浅主题都看得清。"""
    global _MISSING_ICON
    if _MISSING_ICON is not None:
        return _MISSING_ICON
    pix = QPixmap(16, 16)
    pix.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pix)
    try:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        font = painter.font()
        font.setBold(True)
        font.setPointSizeF(12.0)
        painter.setFont(font)
        rect = pix.rect()
        painter.setPen(QPen(QColor("#ffffff"), 2.4))
        painter.drawText(rect, int(Qt.AlignmentFlag.AlignCenter), "!")
        painter.setPen(QPen(QColor("#d93025")))
        painter.drawText(rect, int(Qt.AlignmentFlag.AlignCenter), "!")
    finally:
        painter.end()
    _MISSING_ICON = QIcon(pix)
    return _MISSING_ICON


def _blend_over(fg: QColor, alpha: int, bg: QColor) -> QColor:
    """把 ``fg`` 以 ``alpha``（0-255）不透明度叠在 ``bg`` 上，返回**不透明**的结果色。

    用来把 QSS 里的 ``rgba(...)`` 背景换算成委托自绘时用得上的实色。
    """
    a = max(0, min(255, alpha)) / 255.0
    return QColor(
        round(fg.red() * a + bg.red() * (1.0 - a)),
        round(fg.green() * a + bg.green() * (1.0 - a)),
        round(fg.blue() * a + bg.blue() * (1.0 - a)),
    )


class _LabelRadioDelegate(QStyledItemDelegate):
    """矢量图层行末尾的「标注」开关：一个 Radio + 紧跟的「标注」二字。

    Radio 与文字是**一个控件**：点圆点或点「标注」都算点它（label 跟控件联动是
    常规行为），命中区覆盖整块。

    只画不写：点击发 :attr:`toggle_requested`，标注的读写由主窗口实现（与头部
    工具条、右键菜单一致，Dock 保持"哑视图"）。非矢量图层（栅格 / 组 / 图例）
    一律不画。

    选中态**每帧直接读** ``layer.labelsEnabled()``，不缓存、不订阅图层信号——
    本构建（4.2）实测 ``setLabeling`` / ``setLabelsEnabled`` **不发任何图层信号**
    （``configChanged`` / ``styleChanged`` 都不发），订阅无从谈起；重绘一次即自愈，
    所以外部（图层属性对话框）改过标注后由调用方
    :meth:`LayerDock.refresh_label_state` 触发重绘。

    没配过标注的图层（``layer.labeling() is None``）画成暗色环 + 压暗的文字：
    ``setLabelsEnabled`` 对这类图层是**静默空操作**（实测：调完 ``labelsEnabled()``
    仍是 False），暗一档 + 点击提示比"看着能点却没反应"诚实。
    """

    toggle_requested = pyqtSignal(object, bool)  # (QgsVectorLayer, 期望状态)
    #: 点击名称前的红色感叹号：请主窗口重设该图层数据源
    source_repair_requested = pyqtSignal(object)  # QgsMapLayer

    def __init__(self, view: QgsLayerTreeView, parent=None) -> None:
        super().__init__(parent if parent is not None else view)
        self._view = view
        # 主题色缓存：paint 里不查 ThemeManager 单例（构造它会重设 QApplication
        # 样式表，在绘制中途重入不安全），改由 theme_changed 刷新。
        self._theme = ThemeManager.instance().current()
        ThemeManager.instance().theme_changed.connect(self._on_theme_changed)
        # 「标注」二字的宽度按视图字体量一次：绘制与命中判定共用同一份几何，
        # 两边各量一次就可能量出不同结果（点得到却画歪）。
        self._font = QFont(view.font())
        self._caption_width = 0.0
        self._measure_caption()

    # ------------------------------------------------------------- 内部

    def initStyleOption(self, option, index) -> None:  # noqa: N802 —— Qt 命名
        """选中行不要焦点框；数据源丢失时把图层图标换成红色感叹号。

        图标槽本身就是「勾选框之后、名称之前」，换掉图标后名称由视图正常
        紧贴排布，和 ArcMap 的数据源丢失标记一致。
        """
        super().initStyleOption(option, index)
        option.state &= ~QStyle.StateFlag.State_HasFocus
        try:
            layer = self._node_layer(index)
            if layer is not None and self._source_missing(layer):
                option.icon = _missing_icon()
                option.features |= QStyleOptionViewItem.ViewItemFeature.HasDecoration
        except Exception:  # noqa: BLE001 —— 绘制 / 尺寸计算里不能抛异常
            pass

    def _measure_caption(self) -> None:
        """按当前视图字体重量「标注」二字宽度（绘制与命中判定共用）。"""
        metrics = QFontMetrics(self._font)
        self._caption_width = float(metrics.horizontalAdvance(_CAPTION_TEXT))

    def _node_layer(self, index):
        """index → QgsMapLayer；组 / 图例节点返回 None。

        ``index2node`` 对越界/野索引会抛异常，而它可能在 Qt 虚函数里被调用——
        未捕获异常在本环境是原生崩溃（exit 127，无 traceback），必须吞掉。
        """
        try:
            node = self._view.index2node(index)
        except Exception:  # noqa: BLE001
            return None
        layer_getter = getattr(node, "layer", None)
        return layer_getter() if callable(layer_getter) else None

    @staticmethod
    def _source_missing(layer) -> bool:
        """图层的数据源是不是丢了（文件/库被删/挪走）。"""
        if layer is None:
            return False
        try:
            provider = str(layer.providerType() or "")
            if provider in (
                "memory",
                "virtual",
                "postgres",
                "oracle",
                "mssql",
                "wfs",
                "wms",
                "wmts",
                "arcgisfeatureserver",
                "arcgismapserver",
            ):
                return False
            if not layer.isValid():
                return True
            source = str(layer.source() or "")
        except Exception:  # noqa: BLE001
            return False
        if not source:
            return False
        # 远程服务 / 虚拟数据源不是本地文件，不用判存在性
        lowered = source.lower()
        if "://" in lowered or lowered.startswith(("url=", "type=", "crs=")):
            return False
        if "host=" in lowered or "service=" in lowered:
            return False
        try:
            from app.core.util.local_layer_loader import layer_source_container

            container = str(layer_source_container(layer) or "")
        except Exception:  # noqa: BLE001
            container = source.split("|", 1)[0]
        if not container:
            return False
        if "://" in container.lower() or container.lower().startswith("url="):
            return False
        if not os.path.isabs(container):
            base = str(QgsProject.instance().homePath() or os.getcwd())
            container = os.path.join(base, container)
        return not os.path.exists(container)

    def _badge_rect(self, option, layer=None):
        """红色感叹号的点击区域：可视勾选框之后、名称之前的那块图标槽。"""
        size = 18.0
        left = float(option.rect.left()) + 20.0
        top = float(option.rect.center().y()) - size / 2.0
        return QRectF(left, top, size, size)

    def _toggle_left(self, row_rect) -> float:
        """开关整块的左边界：贴行右端；行太窄时贴住行左端，绝不往左越界。

        行再窄也画（用户要求开关始终可见），所以这里不返回"放不下"，只做下限钳制。
        """
        block_width = _RADIO_SIZE + _CAPTION_GAP + self._caption_width
        left = float(row_rect.right()) - _CAPTION_RIGHT_MARGIN - block_width
        return max(float(row_rect.left()), left)

    def radio_rect(self, row_rect) -> QRectF:
        """行矩形（``option.rect``）→ Radio 的绘制矩形：在「标注」二字左边。

        参数收的是**行矩形**而不是 option，paint / editorEvent / 测试共用同一套几何，
        测试不必手抄偏移量（实现改了偏移测试也不会漂）。
        """
        cy = row_rect.center().y()
        return QRectF(
            self._toggle_left(row_rect),
            cy - _RADIO_SIZE / 2.0,
            _RADIO_SIZE,
            _RADIO_SIZE,
        )

    def caption_rect(self, row_rect) -> QRectF:
        """行矩形 → 「标注」二字的矩形（紧跟圆点右边）。"""
        height = float(QFontMetrics(self._font).height())
        cy = row_rect.center().y()
        return QRectF(
            self._toggle_left(row_rect) + _RADIO_SIZE + _CAPTION_GAP,
            cy - height / 2.0,
            self._caption_width,
            height,
        )

    def trailing_rect(self, row_rect) -> QRectF:
        """整个「标注」开关（Radio + 文字）的外接矩形，用于命中判定。"""
        return self.radio_rect(row_rect).united(self.caption_rect(row_rect))

    def pill_rect(self, row_rect) -> QRectF:
        """覆盖底色胶囊：长图层名从这块下面淡出，开关才不会被名字糊住。

        满行高、贴到行右端；颜色由 :meth:`_row_background` 按行状态取，与 QSS
        画的行底色一致——没和文字重叠时它就是一块"隐形"底色。
        """
        left = max(float(row_rect.left()), self._toggle_left(row_rect) - _PILL_PAD_X)
        return QRectF(
            left,
            float(row_rect.top()),
            float(row_rect.right()) - left,
            float(row_rect.height()),
        )

    def _row_background(self, theme, option) -> QColor:
        """行当前底色（与 theme.py 里 ``QDockWidget QTreeView`` 的 QSS 对齐）。

        - **选中**：QSS 用的是 ``rgba(input_focus, 90)``（半透明），画在视图底色上
          实际看到的是**两者混合后的颜色**。这里必须把混合结果算成不透明色，胶囊
          才会和整行底色严丝合缝 —— 否则选中行上会露出一块颜色不同的补丁
          （用户报「标注下的背景色和图层名下的不一致，看着不连续」）。
        - QSS 的 ``:selected`` 规则排在 ``:hover`` 之后，同时命中时选中胜出，
          这里保持同样的优先级。
        """
        if option.state & QStyle.StateFlag.State_Selected:
            return _blend_over(
                QColor(theme.input_focus), DOCK_SELECTION_ALPHA, QColor(theme.window_bg)
            )
        if option.state & QStyle.StateFlag.State_MouseOver:
            return QColor(theme.menu_hover)
        return QColor(theme.window_bg)

    def _on_theme_changed(self, theme=None) -> None:
        """主题切换 → 换色重绘（QPainter 画的东西不跟 QSS 走）。"""
        try:
            if theme is not None:
                self._theme = theme
            # 主题可能连字体一起换（QSS 里设了 font），量宽跟着重算一次
            self._font = QFont(self._view.font())
            self._measure_caption()
            self._view.viewport().update()
        except RuntimeError:
            pass  # 窗口关闭流程中 C++ 对象已销毁

    # -------------------------------------------------- QStyledItemDelegate

    def paint(self, painter, option, index) -> None:
        """默认画完行内容后，在行尾**叠加**一个「标注」开关（圆点 + 文字）。

        开关不参与布局、不挤图层名：行有多长都画，名字太长就压在名字上——
        先用一块与行底色一致的胶囊盖掉，左缘渐变淡出，再画开关。
        """
        super().paint(painter, option, index)
        # 绘制里的异常不能让整棵树崩：Qt 虚函数里未捕获 = 原生崩溃
        try:
            layer = self._node_layer(index)
            if not isinstance(layer, QgsVectorLayer):
                return
            on = bool(layer.labelsEnabled())
            configured = layer.labeling() is not None
            theme = self._theme
            if on:
                ring = QColor(theme.input_focus)
            elif configured:
                ring = QColor(theme.text_secondary)
            else:
                ring = QColor(theme.border)
            # 没配过标注的图层：文字也压暗，跟暗环一起表示"这块现在点不动"
            caption_color = QColor(theme.menu_text if configured else theme.text_secondary)

            rect = self.radio_rect(option.rect)
            painter.save()
            # 行再窄也不许线条漫出去：把所有绘制裁在行矩形内
            painter.setClipRect(option.rect)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            # 1) 覆盖底色胶囊：盖掉压在开关下面的图层名；左缘透明 → 底色的渐变，
            #    让长名平滑淡出，而不是被硬切一刀。
            pill = self.pill_rect(option.rect)
            if pill.width() > 0.0:
                bg = self._row_background(theme, option)
                fade = QColor(bg)
                fade.setAlpha(0)
                gradient = QLinearGradient(pill.left(), 0.0, pill.left() + _PILL_FADE, 0.0)
                gradient.setColorAt(0.0, fade)
                gradient.setColorAt(1.0, bg)
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(gradient)
                painter.drawRect(pill)
            # 2) Radio：环 + （选中时的）实心内点
            if option.state & QStyle.StateFlag.State_MouseOver:
                tint = QColor(theme.input_focus)
                tint.setAlpha(36)
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(tint)
                painter.drawEllipse(rect.adjusted(-2.0, -2.0, 2.0, 2.0))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(ring, 1.4))
            painter.drawEllipse(rect.adjusted(1.5, 1.5, -1.5, -1.5))
            if on:
                # 选中：环 + 同色实心内点（radio 外观，不依赖行底色）
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(ring)
                painter.drawEllipse(rect.adjusted(5.0, 5.0, -5.0, -5.0))
            # 「标注」二字：用与量宽同一份字体画，画的宽度和预留的宽度才对得上
            painter.setFont(self._font)
            painter.setPen(QPen(caption_color))
            painter.drawText(
                self.caption_rect(option.rect),
                int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter),
                _CAPTION_TEXT,
            )
            painter.restore()
        except Exception:  # noqa: BLE001
            return

    def sizeHint(self, option, index):
        """layer item 行高下限：行更舒展，行尾开关点起来也更稳。"""
        size = super().sizeHint(option, index)
        if size.height() < _ROW_MIN_HEIGHT:
            size.setHeight(_ROW_MIN_HEIGHT)
        return size

    def helpEvent(self, event, view, option, index) -> bool:
        """悬停整行都弹完整名称气泡。

        名字长到被行尾开关盖住、或 Dock 太窄把名字挤没了时，气泡是看全名的兜底
        （另一条路是把 Dock 拉宽，行自然就够长）。**分组行**同样处理：数据源视图
        的分组名是完整路径，窄面板里一定显示不全，没有气泡就完全看不到路径。
        """
        try:
            layer = self._node_layer(index)
            if layer is not None and event is not None and event.type() == QEvent.Type.ToolTip:
                if self._source_missing(layer):
                    QToolTip.showText(
                        event.globalPos(),
                        "数据源不存在。\n点击图层名称前的红色感叹号重设数据源。",
                        view,
                    )
                    return True
                QToolTip.showText(event.globalPos(), layer.name(), view)
                return True
            if event is not None and event.type() == QEvent.Type.ToolTip:
                try:
                    # 视图里的 index 属于内部代理模型（QgsLayerTreeProxyModel）；
                    # 先映射回源模型，再用源模型的 index2node 取节点。直接用
                    # view.index2node 拿分组索引会得到根节点（实测），拿不到分组名。
                    model = self._view.model()
                    src_index = index
                    mapper = getattr(model, "mapToSource", None)
                    if callable(mapper):
                        src_index = mapper(index)
                        model = model.sourceModel()
                    node = model.index2node(src_index) if src_index.isValid() else None
                except Exception:  # noqa: BLE001
                    node = None
                if isinstance(node, QgsLayerTreeGroup):
                    QToolTip.showText(event.globalPos(), node.name(), view)
                    return True
        except Exception:  # noqa: BLE001
            pass
        try:
            return super().helpEvent(event, view, option, index)
        except Exception:  # noqa: BLE001
            return False

    def editorEvent(self, event, model, option, index) -> bool:
        """「标注」开关上的左键 press / release / 双击都吃掉，release 时发切换请求。

        命中区是**整块开关**（圆点 + 「标注」二字）：文字是圆点的 label，点文字
        跟点圆点一个意思。

        吃事件是为了不让它抢走"编辑类"默认行为：从这块上按下不起拖拽，也不触发
        `SelectedClicked` 那条就地重命名；双击同理（否则圆点上会弹出重命名编辑框）。

        注意：``view.clicked`` 仍会照常发出（实测 Qt 在 ``mouseReleaseEvent``
        里先于委托判定当前行），所以点这块会顺带把该行设为当前行——与
        「点击图层行即记录 ``_selected_layer``」的既有行为一致。
        """
        try:
            if event.button() == Qt.MouseButton.LeftButton and event.type() in (
                QEvent.Type.MouseButtonPress,
                QEvent.Type.MouseButtonRelease,
                QEvent.Type.MouseButtonDblClick,
            ):
                layer = self._node_layer(index)
                if layer is not None and self._source_missing(layer):
                    badge_rect = self._badge_rect(option, layer).adjusted(-3, -3, 3, 3)
                    if badge_rect.contains(event.position()):
                        if event.type() == QEvent.Type.MouseButtonRelease:
                            self.source_repair_requested.emit(layer)
                        return True
                if (
                    isinstance(layer, QgsVectorLayer)
                    and self.trailing_rect(option.rect)
                    .adjusted(-_RADIO_HIT_PAD, -_RADIO_HIT_PAD, _RADIO_HIT_PAD, _RADIO_HIT_PAD)
                    .contains(event.position())
                ):
                    if event.type() == QEvent.Type.MouseButtonRelease:
                        self.toggle_requested.emit(layer, not layer.labelsEnabled())
                    return True
        except Exception:  # noqa: BLE001
            return False
        return super().editorEvent(event, model, option, index)


class _InfoPanel(QWidget):
    """图层面板底部的「图层信息」块（双击 → 打开属性表）。"""

    double_clicked = pyqtSignal()

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802 —— Qt 命名
        self.double_clicked.emit()
        super().mouseDoubleClickEvent(event)


class LayerDock(QDockWidget):
    """独立的图层树面板。"""

    context_menu_requested = pyqtSignal(object, object)  # (QgsMapLayer, QPoint 全局坐标)
    #: 右键落在**图层组**节点上（组没有 layer()）：(QgsLayerTreeGroup, QPoint 全局坐标)
    group_context_menu_requested = pyqtSignal(object, object)
    #: 右键落在**树的空白处**（没有节点）：(QPoint 全局坐标)
    blank_context_menu_requested = pyqtSignal(object)
    #: 双击底部「图层信息」块 → 打开该图层的属性表（只对矢量图层发）
    attribute_table_requested = pyqtSignal(object)
    #: 双击图层行的符号图标 → 编辑整层符号（单一符号时弹符号选择器）
    layer_symbol_edit_requested = pyqtSignal(object)  # QgsMapLayer
    #: 双击分类图例子项 → 编辑该类别符号：(图层, 类别行号)
    legend_symbol_edit_requested = pyqtSignal(object, int)
    layer_clicked = pyqtSignal(object)
    label_toggle_requested = pyqtSignal(object, bool)  # (QgsVectorLayer, 期望的标注开关)
    #: 图层数据源丢失，用户点了名称前的红色感叹号：请宿主重设数据源
    source_repair_requested = pyqtSignal(object)  # QgsMapLayer
    render_tab_requested = pyqtSignal()
    add_layer_requested = pyqtSignal()
    add_group_requested = pyqtSignal()
    remove_requested = pyqtSignal()
    expand_all_requested = pyqtSignal()
    collapse_all_requested = pyqtSignal()
    move_up_requested = pyqtSignal()
    move_down_requested = pyqtSignal()

    def __init__(self, parent=None) -> None:
        super().__init__("图层列表", parent)
        # saveState/restoreState 按 objectName 匹配 Dock；空名字会被 Qt 丢弃并告警，
        # 布局自愈重套时就会错位（见 WindowChromeMixin.heal_stalled_layout）。
        self.setObjectName("LayerDock")
        self.setAllowedAreas(Qt.DockWidgetArea.LeftDockWidgetArea | Qt.DockWidgetArea.RightDockWidgetArea)

        self.project: QgsProject = QgsProject.instance()
        self.tree_root = self.project.layerTreeRoot()
        #: 当前展示方式：``order`` = 按图层顺序（真实工程图层树），
        #: ``source`` = 按数据源（分组用的是一棵**独立**的树，不碰工程图层顺序）
        self._view_mode = "order"
        # 多值符号图例的展开状态（见 _collapse_new_legends）：
        # initialized = 已经按默认折叠过的图层；user_expanded = 用户手动展开的图层
        self._legend_initialized: dict[str, set[str]] = {
            "order": set(),
            "source": set(),
        }
        self._legend_user_expanded: dict[str, set[str]] = {
            "order": set(),
            "source": set(),
        }
        self._legend_collapse_pending = False

        self.model = QgsLayerTreeModel(self.tree_root)
        self.model.setFlags(
            QgsLayerTreeModel.AllowNodeRename
            | QgsLayerTreeModel.AllowNodeReorder
            | QgsLayerTreeModel.AllowNodeChangeVisibility
            # 图层行前显示符号 / 图例：单一符号直接显示色块；分类渲染显示可展开的
            # 类别子项（带下拉箭头），与 QGIS 图层树一致
            | QgsLayerTreeModel.ShowLegend
            | QgsLayerTreeModel.AllowLegendChangeState
        )

        self._order_view = self._make_view(self.model, order=True)
        # 「按数据源展示」：独立的根节点 + 模型，图层节点按数据源分组。
        # 不复用工程树是为了**不改绘制顺序**（ArcMap 的 List By Source 也是
        # 纯视图）；图层对象仍是同一个，勾选可见性 / 标注开关照样生效。
        self.source_root = QgsLayerTree()
        self.source_model = _SourceTreeModel(self.source_root)
        self.source_model.setFlags(
            QgsLayerTreeModel.AllowNodeChangeVisibility
            | QgsLayerTreeModel.ShowLegend
            | QgsLayerTreeModel.AllowLegendChangeState
        )
        self.source_view = self._make_view(self.source_model, order=False)
        # 图例子项也是"插入行"：插完再安排一次折叠对账。只有 singleShot(0)
        # 时可能早于模型插入图例，首次切换到数据源视图就会默认展开（用户报过）
        for tree_model in (self.model, self.source_model):
            tree_model.rowsInserted.connect(self._on_tree_rows_inserted)

        self.view_stack = QStackedWidget(self)
        self.view_stack.addWidget(self._order_view)
        self.view_stack.addWidget(self.source_view)

        self._build_header()

        # 内容 = 头部工具条 + 树视图（保留原生 Dock 标题栏）
        body = QWidget(self)
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.setSpacing(0)
        body_layout.addWidget(self.header_bar)
        body_layout.addWidget(self.view_stack, 1)
        body_layout.addWidget(self._build_info_panel())
        self.setWidget(body)

        self._view_mode = "order"
        self.view_stack.setCurrentWidget(self._order_view)
        # 工程增删图层 → 数据源视图跟着重建（只在数据源模式下做，省得白跑）。
        # 以**图层树**的增删信号为准：``layersAdded`` 在节点挂进树之前就发了，
        # 那时 findLayers() 还看不到新图层。嵌套组里的增删只有该组会发信号，
        # 所以递归给每个组节点都挂上。
        self._structure_hooked: set[int] = set()
        self._hook_tree_nodes()
        # 可见性在 QGIS 4 里挂在**图层树节点**上（QgsMapLayer 已没有 isVisible）：
        # 数据源视图是另一棵树，勾选框必须和工程节点双向同步，否则点了没反应
        self._visibility_hooked: set[str] = set()
        self._hook_project_visibility()
        # 图层符号变化 → 行首色块重画（只挂一次）
        self._style_hooked: set[str] = set()
        self._hook_layer_styles()
        self._schedule_collapse_legends()

    def _make_view(self, model: QgsLayerTreeModel, *, order: bool) -> QgsLayerTreeView:
        """建一个图层树视图（两个展示方式共用同一套交互 / 委托）。"""
        view = QgsLayerTreeView(self)
        view.setModel(model)
        # 行尾「标注」开关要一直看得见：列撑满视口 + 关横向滚动条，长图层名交给
        # 默认委托右侧省略（悬停气泡看全名、拉宽 Dock 看更多）。这样 ``option.rect``
        # 的右端就是视口右端，开关不会被长名字顶进横向滚动区里。
        view.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        view.setTextElideMode(Qt.TextElideMode.ElideRight)
        # clicked 载荷是 index：主窗口按「当前视图」映射节点，再记录选中图层
        view.clicked.connect(self.layer_clicked.emit)
        view.clicked.connect(self._on_view_clicked)
        # 双击图层项 → 打开属性表；就地重命名改用编辑键（F2），别一次双击干两件事
        view.setEditTriggers(QAbstractItemView.EditTrigger.EditKeyPressed)
        view.doubleClicked.connect(self._on_view_double_clicked)
        view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        view.customContextMenuRequested.connect(self._on_view_context_menu)
        # 记录用户手动展开 / 折叠的多值图例（刷新时恢复，不被自动折叠）
        view_key = "order" if order else "source"
        view.expanded.connect(
            lambda index, v=view, k=view_key: self._on_legend_expanded(k, v, index)
        )
        view.collapsed.connect(
            lambda index, v=view, k=view_key: self._on_legend_collapsed(k, v, index)
        )
        # 行尾「标注」开关：只画、只发信号（读写标注由主窗口实现）
        delegate = _LabelRadioDelegate(view)
        view.setItemDelegate(delegate)
        delegate.toggle_requested.connect(self.label_toggle_requested.emit)
        delegate.source_repair_requested.connect(self.source_repair_requested.emit)
        if order:
            self.label_delegate = delegate
        else:
            self.source_delegate = delegate
        return view

    @property
    def view(self) -> QgsLayerTreeView:
        """当前**可见**的图层树视图（主窗口的所有操作都作用在它上面）。"""
        if self._view_mode == "source":
            return self.source_view
        return self._order_view

    # ------------------------------------------------------- 按数据源展示

    def _hook_tree_nodes(self) -> None:
        """给工程树根及所有组挂增删 / 勾选信号（嵌套组的增删不会冒泡到根）。"""
        try:
            nodes = [self.tree_root, *self.tree_root.findGroups()]
        except RuntimeError:
            return
        for node in nodes:
            # 用底层 C++ 指针做键：Python 包装对象被回收后 id() 可能复用
            try:
                key = int(sip.unwrapinstance(node))
            except (TypeError, RuntimeError):
                continue
            if key in self._structure_hooked:
                continue
            try:
                node.addedChildren.connect(self._on_layers_changed)
                node.removedChildren.connect(self._on_layers_changed)
                # 组 / 根节点的勾选**不会**给子图层发 visibilityChanged（实测），
                # 所以整组开关要单独接一个「全量对账」的槽
                node.visibilityChanged.connect(
                    self._on_project_group_visibility_changed
                )
            except (RuntimeError, AttributeError):
                continue
            self._structure_hooked.add(key)

    def _on_layers_changed(self, *_args) -> None:
        """工程里的图层增删了：数据源视图需要重建（顺序视图由 Qt 模型自己跟）。"""
        # 新加的组也要挂信号，否则组内再增删就收不到
        self._hook_tree_nodes()
        self._hook_project_visibility()
        self._hook_layer_styles()
        # 图层增删：新图层的图例默认折叠（已有图层保持用户的展开状态）
        self._schedule_collapse_legends()
        if self._view_mode == "source":
            self._rebuild_source_view()

    # ------------------------------------------------------- 行首符号色块

    def _hook_layer_styles(self) -> None:
        """图层符号变化（样式 / 渲染器）→ 行首色块缓存失效并重画。"""
        try:
            nodes = self.tree_root.findLayers()
        except RuntimeError:
            return
        for node in nodes:
            try:
                layer = node.layer()
            except RuntimeError:
                continue
            if not isinstance(layer, QgsVectorLayer):
                continue
            try:
                layer_id = str(layer.id())
            except RuntimeError:
                continue
            if layer_id in self._style_hooked:
                continue
            for name in ("styleChanged", "rendererChanged"):
                signal = getattr(layer, name, None)
                if signal is None:
                    continue
                try:
                    signal.connect(self._on_layer_style_changed)
                except (TypeError, RuntimeError):
                    pass
            self._style_hooked.add(layer_id)

    def _on_layer_style_changed(self, *_args) -> None:
        self.refresh_symbol_icons()

    def refresh_symbol_icons(self) -> None:
        """重建两棵树的图例（行首色块 / 分类子项）。

        图层属性的「确定 / 应用」里，单一符号的 ``renderer.setSymbol`` 不一定发
        ``styleChanged``（本构建实测），所以对话框关闭时由主窗口显式调一次。
        """
        for model, root in (
            (self.model, self.tree_root),
            (self.source_model, self.source_root),
        ):
            try:
                nodes = root.findLayers()
            except RuntimeError:
                continue
            for node in nodes:
                try:
                    model.refreshLayerLegend(node)
                except (RuntimeError, AttributeError):
                    continue
        for view in (self._order_view, self.source_view):
            try:
                view.viewport().update()
            except RuntimeError:
                pass
        self._schedule_collapse_legends()

    # ------------------------------------------------------- 图例展开状态

    def _schedule_collapse_legends(self) -> None:
        """等模型插完图例子项后再折叠（插入发生在信号 / 事件栈里）。"""
        if self._legend_collapse_pending:
            return
        self._legend_collapse_pending = True
        QTimer.singleShot(0, self._collapse_new_legends)

    def _on_tree_rows_inserted(self, *_args) -> None:
        """模型插入行（图例子项也是行）→ 安排一次图例折叠对账。"""
        self._schedule_collapse_legends()

    @staticmethod
    def _layer_id_of_index(view, index):
        """视图索引 → 图层 id；组 / 图例子项返回 ``None``。"""
        try:
            node = view.index2node(index)
        except Exception:  # noqa: BLE001
            return None
        if not isinstance(node, QgsLayerTreeLayer):
            return None
        try:
            return str(node.layerId())
        except RuntimeError:
            return None

    def _on_legend_expanded(self, key: str, view, index) -> None:
        """用户手动展开图例 → 记下来，刷新图例后恢复。"""
        layer_id = self._layer_id_of_index(view, index)
        if layer_id is None:
            return
        # 图例刚插入时 QGIS 会自动展开一次；这不算用户操作
        if layer_id not in self._legend_initialized[key]:
            return
        self._legend_user_expanded[key].add(layer_id)

    def _on_legend_collapsed(self, key: str, view, index) -> None:
        layer_id = self._layer_id_of_index(view, index)
        if layer_id is not None:
            self._legend_user_expanded[key].discard(layer_id)

    def _collapse_new_legends(self) -> None:
        """图例默认折叠；用户手动展开过的图层保持展开。

        分类 / 分级图层的图例子项会被 QgsLayerTreeView **自动展开**。这里每次
        对账都按 ``_legend_user_expanded`` 处理：没手动展开就折叠，手动展开过
        就保留。

        以前只在「第一次看到某个图层」时折叠（``_legend_initialized`` 命中就
        跳过）—— 数据源视图重建后是**新的节点**，模型会再次自动展开图例，而
        缓存里已经有这个图层，于是不再折叠，看起来就是"默认展开了符号图例"
        （用户报过）。改成每次对账都强制到目标状态。
        """
        self._legend_collapse_pending = False
        pruned_any = False
        for key, view in (
            ("order", self._order_view),
            ("source", self.source_view),
        ):
            proxy = view.model()
            if proxy is None:
                continue
            # QgsLayerTreeView 会把模型包一层 QgsLayerTreeProxyModel：视图操作
            # （isExpanded/index2node）用 proxy 索引，removeLegendFromLayer 这类
            # 模型方法必须落到 sourceModel 上（proxy 没有这个方法）
            source_model = proxy
            source_getter = getattr(proxy, "sourceModel", None)
            if callable(source_getter):
                try:
                    source = source_getter()
                except RuntimeError:
                    source = None
                if source is not None:
                    source_model = source
            initialized = self._legend_initialized[key]
            user_expanded = self._legend_user_expanded[key]
            pruned = False

            def walk(parent) -> None:
                nonlocal pruned
                for row in range(proxy.rowCount(parent)):
                    index = proxy.index(row, 0, parent)
                    try:
                        node = view.index2node(index)
                    except Exception:  # noqa: BLE001 —— 野索引跳过
                        continue
                    if isinstance(node, QgsLayerTreeLayer):
                        try:
                            layer_id = str(node.layerId())
                            rows = proxy.rowCount(index)
                        except RuntimeError:
                            continue
                        source_index = index
                        mapper = getattr(proxy, "mapToSource", None)
                        if source_model is not proxy and callable(mapper):
                            try:
                                source_index = mapper(index)
                            except Exception:  # noqa: BLE001
                                source_index = index
                        if (
                            rows == 1
                            and source_index.isValid()
                            and self._is_empty_legend_child(source_model, source_index)
                        ):
                            # 空图例（XYZ 瓦片等）：去掉唯一的空子项，否则视图会
                            # 画出展开箭头，点开却什么都没有（用户报过天地图）
                            try:
                                source_model.removeLegendFromLayer(node)
                                pruned = True
                            except (RuntimeError, AttributeError):
                                pass
                            continue
                        if rows <= 0:
                            # 图例子项还没插入；rowsInserted 会再安排一轮对账
                            continue
                        initialized.add(layer_id)
                        if layer_id in user_expanded:
                            if not view.isExpanded(index):
                                view.expand(index)
                        elif view.isExpanded(index):
                            view.collapse(index)
                    elif isinstance(node, QgsLayerTreeGroup):
                        walk(index)

            try:
                walk(QModelIndex())
            except RuntimeError:  # 视图 / 模型正在销毁
                continue
            if pruned:
                # QgsLayerTreeProxyModel 不会把 sourceModel 的图例删除同步到
                # 自己的行映射：sourceModel.rowCount 已经是 0，proxy.rowCount
                # 仍是 1，视图照样画展开箭头（实测）。必须让代理重建一次。
                self._invalidate_proxy(proxy)
                pruned_any = True
        if pruned_any:
            # 代理重建可能把展开状态一并重置，再对账一次恢复用户手动展开
            self._schedule_collapse_legends()

    @staticmethod
    def _invalidate_proxy(proxy) -> None:
        """让代理模型重建行映射（source 的图例增删不一定转发 rowsRemoved）。"""
        for name in ("invalidate", "invalidateRowsFilter", "invalidateFilter"):
            method = getattr(proxy, name, None)
            if not callable(method):
                continue
            try:
                method()
                return
            except Exception:  # noqa: BLE001
                continue

    @staticmethod
    def _is_empty_legend_child(model, layer_index) -> bool:
        """图例只有一个子项且文字 / 图标都为空 → 视为"没有图例"。

        正常的单一符号矢量图例是嵌在父行里的（``rowCount == 0``）；分类 /
        分级图例的子项有文字或色块；XYZ 瓦片这类图层则会建一个**空**子项，
        视图因此多出一个展开箭头。
        """
        try:
            child = model.index(0, 0, layer_index)
            text = model.data(child, Qt.ItemDataRole.DisplayRole)
            icon = model.data(child, Qt.ItemDataRole.DecorationRole)
        except RuntimeError:
            return False
        # 有的实现返回"空 QIcon / 空 QPixmap"而不是 None
        if icon is not None and hasattr(icon, "isNull") and icon.isNull():
            icon = None
        return not text and icon is None

    def _hook_project_visibility(self) -> None:
        """给工程图层节点挂可见性同步（每个图层只挂一次）。"""
        try:
            nodes = self.tree_root.findLayers()
        except RuntimeError:
            return
        for node in nodes:
            try:
                layer_id = str(node.layerId())
                if layer_id in self._visibility_hooked:
                    continue
                node.visibilityChanged.connect(self._on_project_visibility_changed)
                self._visibility_hooked.add(layer_id)
            except RuntimeError:
                continue

    def _on_project_visibility_changed(self, node) -> None:
        """工程节点勾选变了 → 数据源视图里同一图层的勾选跟着变。"""
        if self._view_mode != "source":
            return
        layer = self._node_layer_of(node)
        if layer is None:
            return
        source_node = self.source_root.findLayer(layer.id())
        if source_node is None:
            return
        # 用**实际可见性**（含所在组的勾选）：工程里组关了但图层自己仍勾着，
        # 画布上就是不显示，数据源视图里也该显示为不勾选
        checked = node.isVisible()
        if source_node.itemVisibilityChecked() != checked:
            source_node.setItemVisibilityChecked(checked)

    def _on_project_group_visibility_changed(self, _node) -> None:
        """工程里的组 / 根勾选变了 → 全量对账（子图层不会逐个发信号）。"""
        if self._view_mode != "source":
            return
        try:
            nodes = self.tree_root.findLayers()
        except RuntimeError:
            return
        for node in nodes:
            layer = self._node_layer_of(node)
            if layer is None:
                continue
            source_node = self.source_root.findLayer(layer.id())
            if source_node is None:
                continue
            checked = node.isVisible()
            if source_node.itemVisibilityChecked() != checked:
                source_node.setItemVisibilityChecked(checked)

    def _on_source_visibility_changed(self, node) -> None:
        """数据源视图勾选变了 → 写回工程节点（canvas 才会真的显示 / 隐藏）。

        组节点的勾选是**级联到组内每个图层**的（QGIS 的组节点就是这个语义），
        所以按组里的图层逐个写回，而不是只同步组节点自己。
        """
        try:
            if isinstance(node, QgsLayerTreeGroup):
                targets = list(node.findLayers())
                checked = node.itemVisibilityChecked()
            else:
                targets = [node]
                checked = node.itemVisibilityChecked()
        except RuntimeError:
            return
        for target in targets:
            layer = self._node_layer_of(target)
            if layer is None:
                continue
            self._set_project_layer_visible(layer, checked)

    def _set_project_layer_visible(self, layer, checked: bool) -> None:
        """把数据源视图的勾选写回工程图层节点（必要时连带打开祖先组）。

        数据源视图的分组和工程分组不是一回事：某图层在工程里可能躺在被关掉的
        组里。用户在数据源视图里把它勾上，是希望画布上出现它 —— 只改图层节点
        自己的勾选还不够，祖先组也得打开。
        """
        try:
            node = self.tree_root.findLayer(layer.id())
        except RuntimeError:
            return
        if node is None:
            return
        if checked:
            try:
                parent = node.parent()
                while isinstance(parent, QgsLayerTreeGroup) and parent is not self.tree_root:
                    if not parent.itemVisibilityChecked():
                        parent.setItemVisibilityChecked(True)
                    parent = parent.parent()
            except RuntimeError:
                return
        if node.itemVisibilityChecked() != checked:
            node.setItemVisibilityChecked(checked)

    @staticmethod
    def _node_layer_of(node):
        """节点 → ``QgsMapLayer``（组 / 已销毁节点返回 ``None``）。"""
        try:
            getter = getattr(node, "layer", None)
            return getter() if callable(getter) else None
        except RuntimeError:
            return None

    def _rebuild_source_view(self) -> None:
        """按「数据源」重组独立树：同一工作空间 / 库的图层归到一个组里。"""
        view = self.source_view
        # 重建前先清掉选中 / 当前项：节点随后会被删，别让视图留着持久索引
        with suppress(Exception):
            view.selectionModel().clear()
            view.setCurrentIndex(QModelIndex())
        self.source_root.removeAllChildren()
        grouped: dict[str, tuple[str, list]] = {}
        # findLayers() 就是树里从上到下（绘制顺序从上到下）的图层顺序
        order = [
            node.layer() for node in self.tree_root.findLayers() if node.layer() is not None
        ]
        for layer in order:
            key, label = _layer_source_group(layer)
            grouped.setdefault(key, (label, []))[1].append(layer)
        for key in sorted(grouped, key=lambda item: grouped[item][0].casefold()):
            label, layers = grouped[key]
            group = self.source_root.addGroup(label)
            group.setCustomProperty("data_source_key", key)
            group.visibilityChanged.connect(self._on_source_visibility_changed)
            for layer in layers:
                node = group.addLayer(layer)
                # 初值取工程图层的**实际可见性**；之后两边靠 visibilityChanged 双向同步
                try:
                    project_node = self.tree_root.findLayer(layer.id())
                    if project_node is not None:
                        node.setItemVisibilityChecked(project_node.isVisible())
                except RuntimeError:
                    pass
                node.visibilityChanged.connect(self._on_source_visibility_changed)
            group.setExpanded(True)

    def set_view_mode(self, mode: str) -> None:
        """切换展示方式：``order``（按图层顺序）/ ``source``（按数据源）。"""
        mode = "source" if mode == "source" else "order"
        if mode == "source":
            self._rebuild_source_view()
        self._view_mode = mode
        self.view_stack.setCurrentWidget(self.view)
        if hasattr(self, "btn_view_order"):
            self.btn_view_order.blockSignals(True)
            self.btn_view_source.blockSignals(True)
            self.btn_view_order.setChecked(mode == "order")
            self.btn_view_source.setChecked(mode == "source")
            self.btn_view_order.blockSignals(False)
            self.btn_view_source.blockSignals(False)
        self.set_current_layer(self._info_layer_obj)
        self._schedule_collapse_legends()

    def _source_tree_has(self, node) -> bool:
        """节点是不是「按数据源」那棵独立树里的（含其虚拟分组）。"""
        try:
            cursor = node
            while cursor is not None:
                if cursor is self.source_root:
                    return True
                cursor = cursor.parent()
        except RuntimeError:  # 节点已销毁
            pass
        return False

    def project_node(self, node):
        """数据源视图里的节点 → 工程图层树里的真实节点。

        主窗口的删除 / 上移下移 / 分组等操作都按节点干；数据源视图里的节点是
        **副本**（不在工程树里），必须映射回工程节点，否则删的只是视图。
        虚拟的「数据源」分组节点没有对应工程节点，返回 ``None``（调用方跳过）。
        """
        if node is None or not self._source_tree_has(node):
            return node
        getter = getattr(node, "layer", None)
        layer = getter() if callable(getter) else None
        if layer is None:
            return None
        with suppress(RuntimeError):
            return self.tree_root.findLayer(layer.id())
        return None

    # ------------------------------------------------------------- 图层信息块

    def _build_info_panel(self) -> QWidget:
        """底部「图层信息」块：当前图层名 + 要素数量，双击打开属性表。"""
        panel = _InfoPanel(self)
        panel.setObjectName("LayerDockInfo")
        box = QVBoxLayout(panel)
        box.setContentsMargins(8, 6, 8, 6)
        box.setSpacing(2)
        # 标题初始为空：没选中图层时连「图层信息」四个字都不显示（见 set_current_layer）
        title = QLabel("")
        title.setObjectName("LayerDockInfoTitle")
        self._info_title = title
        box.addWidget(title)
        self._info_layer = QLabel("")
        self._info_layer.setObjectName("LayerDockInfoLine")
        box.addWidget(self._info_layer)
        self._info_count = QLabel("")
        self._info_count.setObjectName("LayerDockInfoLine")
        box.addWidget(self._info_count)
        # 当前展示的图层（供别处查询；双击打开属性表走**图层树项**，见 view.doubleClicked）
        self._info_layer_obj = None
        return panel

    def _sender_view(self) -> QgsLayerTreeView:
        """发出信号的视图（两个展示方式共用槽；直接调用时按当前视图）。"""
        sender = self.sender()
        if sender is self.source_view:
            return self.source_view
        if sender is self._order_view:
            return self._order_view
        return self.view

    def _double_click_zone(self, view: QgsLayerTreeView, index) -> str:
        """双击落点 → ``"symbol"`` / ``"text"`` / ``"other"``。

        用到委托的 ``initStyleOption`` + 样式的子元素矩形，位置与绘制一致；
        计算失败时按 ``"other"`` 处理（不误开属性表 / 符号选择器）。
        """
        try:
            pos = view.viewport().mapFromGlobal(QCursor.pos())
            rect = view.visualRect(index)
            if not rect.isValid() or rect.isEmpty():
                return "other"
            option = QStyleOptionViewItem()
            option.initFrom(view)
            option.widget = view
            option.rect = rect
            delegate = view.itemDelegate()
            if delegate is not None:
                delegate.initStyleOption(option, index)
            # QTreeView 绘制时会预置 decorationSize；手动构造选项时没有，
            # 图标矩形的宽度会算成 -1（图层行图标就点不中）
            if option.decorationSize.width() <= 0:
                side = view.style().pixelMetric(
                    QStyle.PixelMetric.PM_SmallIconSize, None, view
                )
                option.decorationSize = QSize(side, side)
            style = view.style()
            symbol_rect = style.subElementRect(
                QStyle.SubElement.SE_ItemViewItemDecoration, option, view
            )
            text_rect = style.subElementRect(
                QStyle.SubElement.SE_ItemViewItemText, option, view
            )
            if symbol_rect.isValid() and symbol_rect.contains(pos):
                return "symbol"
            if text_rect.isValid() and text_rect.contains(pos):
                return "text"
        except Exception:  # noqa: BLE001 —— 计算落点失败不能拖垮双击
            return "other"
        return "other"

    def _on_view_double_clicked(self, index) -> None:
        """双击：符号 → 符号选择器；图层文字 → 属性表；分类子项 → 该类符号。

        注意：双击不再用于就地重命名（那条改成 F2 / 编辑键，见 ``view`` 的
        ``EditKeyPressed``），否则一次双击既开属性表又开改名框。
        """
        from qgis.core import QgsVectorLayer

        view = self._sender_view()
        # 分类 / 分级的图例子项：双击符号编辑那个类别的符号
        try:
            legend = view.index2legendNode(index)
        except Exception:  # noqa: BLE001
            legend = None
        if legend is not None and index.parent().isValid():
            if self._double_click_zone(view, index) != "symbol":
                return
            try:
                layer = legend.layerNode().layer()
            except Exception:  # noqa: BLE001
                return
            if isinstance(layer, QgsVectorLayer):
                self.legend_symbol_edit_requested.emit(layer, index.row())
            return

        # 图层行：图标 → 编辑符号；文字 → 属性表；勾选框 / 箭头等不处理
        try:
            node = view.index2node(index)
            getter = getattr(node, "layer", None)
            layer = getter() if callable(getter) else None
        except Exception:  # noqa: BLE001 —— 野索引不该把双击拖垮
            return
        if not isinstance(layer, QgsVectorLayer):
            return
        zone = self._double_click_zone(view, index)
        if zone == "symbol":
            self.layer_symbol_edit_requested.emit(layer)
        elif zone == "text":
            self.attribute_table_requested.emit(layer)

    def set_current_layer(self, layer) -> None:
        """刷新「图层信息」；**没选中图层就把整块清空**（不留占位文字）。

        数量取 ``featureCount()``：数据源给不出（返回 -1）时显示「未知」而不是 -1。
        """
        self._info_layer_obj = None
        try:
            name = layer.name() if layer is not None else ""
            count = int(layer.featureCount()) if layer is not None else -1
        except RuntimeError:  # 已是悬空对象：当作没选中
            name, count = "", -1
        except Exception:  # noqa: BLE001 —— 数据源不认 featureCount 就当未知
            name, count = (layer.name() if layer is not None else ""), -1
        if not name:
            # 没选中（点到组 / 空白）：三行全清，不显示"当前图层：—"这类占位
            self._info_title.setText("")
            self._info_layer.setText("")
            self._info_count.setText("")
            return
        self._info_layer_obj = layer
        self._info_title.setText("图层信息")
        self._info_layer.setText(f"当前图层：{name}")
        text = "未知" if count < 0 else f"{count:,}"
        self._info_count.setText(f"要素数量：{text} · 双击图层项打开属性表")

    def _on_info_double_clicked(self) -> None:
        """（保留）信息块自身不再触发打开属性表 —— 改由双击**图层项**实现。"""

    def _on_view_clicked(self, index) -> None:
        """点中节点：刷新底部「图层信息」（点到组 / 空白显示占位）。"""
        layer = None
        try:
            node = self._sender_view().index2node(index)
            getter = getattr(node, "layer", None)
            layer = getter() if callable(getter) else None
        except Exception:  # noqa: BLE001 —— 野索引不该把点击拖垮
            pass
        self.set_current_layer(layer)

    # ------------------------------------------------------------- 事件

    def _on_view_context_menu(self, pos: QPoint) -> None:
        """右键图层节点 → 发 ``(layer, 全局坐标)``，菜单本体由主窗口构建。

        图层用 ``index2node`` 从被点的节点解出，而不是拿显示名去
        ``QgsProject.mapLayersByName`` 回查——**同名图层会回查到错的那个**
        （树里看是 A，菜单作用的却是 B）。

        ``customContextMenuRequested`` 的 pos 是 **viewport 坐标**：``indexAt``
        与 ``mapToGlobal`` 都按 viewport 解释，与工程管理 Dock 的写法一致。

        组节点 / 图例节点没有 ``layer()``（返回 None），无效位置也直接返回，
        一律不弹菜单。
        """
        view = self._sender_view()
        index = view.indexAt(pos)
        if not index.isValid():
            # **空白处右键**：树本身不弹菜单，发信号给宿主（添加组 / 展开折叠 /
            # 粘贴…）。以前这里直接返回，空白处右键什么都不出来。
            self.blank_context_menu_requested.emit(view.viewport().mapToGlobal(pos))
            return
        # index2node 对越界/野索引会抛异常；槽内异常未捕获会崩（0xC0000409）
        try:
            node = view.index2node(index)
        except Exception:  # noqa: BLE001
            return
        layer_getter = getattr(node, "layer", None)
        layer = layer_getter() if callable(layer_getter) else None
        if layer is None:
            # 组节点没有 layer()：走另一条信号，交给组右键菜单（重命名 / 移除 /
            # 复制粘贴…）。以前这里直接 return，于是"右键图层组没有任何内容"。
            from qgis.core import QgsLayerTreeGroup

            if isinstance(node, QgsLayerTreeGroup):
                # 数据源视图里的分组是纯视图节点：工程里没有对应组，不弹组菜单
                if self._source_tree_has(node):
                    return
                self.group_context_menu_requested.emit(
                    node, view.viewport().mapToGlobal(pos)
                )
            return
        self.context_menu_requested.emit(layer, view.viewport().mapToGlobal(pos))

    def refresh_label_state(self) -> None:
        """重绘树视图，让行尾「标注」开关跟上图层实时标注状态。

        开关每帧读 ``layer.labelsEnabled()``，只是本构建的图层标注改动不发任何
        图层信号（见 :class:`_LabelRadioDelegate`），所以**调用方在改完标注后自己
        喊一声**——主窗口的标注开关、图层属性对话框收尾都会调它。
        """
        for _view in (self._order_view, self.source_view):
            _view.viewport().update()

    # ------------------------------------------------------- 头部工具条

    def _build_header(self) -> None:
        """构造 QGIS 式头部工具条：左对齐的小图标按钮。

        样式集中到 :func:`app.ui.common.theme.window_qss` 的
        ``QWidget#LayerDockHeader`` 规则（背景色 + 底部分隔线 + 按钮 hover/press），
        主题切换自动跟随；这里只负责布局（紧凑：边距 / 间距 / 按钮 / 图标尺寸）
        和 ``objectName`` 绑定。
        """
        self.header_bar = QWidget(self)
        self.header_bar.setObjectName("LayerDockHeader")
        header_layout = QHBoxLayout(self.header_bar)
        # 紧凑：上下贴边，左右留 4px，按钮间距 1px
        header_layout.setContentsMargins(4, 0, 4, 0)
        header_layout.setSpacing(2)
        for attr, icon_key, signal_name, tip in _HEADER_SPECS:
            btn = QToolButton(self.header_bar)
            btn.setAutoRaise(True)
            btn.setIcon(_theme_icon(_THEME_ICONS[icon_key]))
            btn.setToolTip(tip)
            btn.setIconSize(QSize(14, 14))
            btn.setFixedSize(QSize(20, 20))
            if signal_name:
                btn.clicked.connect(getattr(self, signal_name).emit)
            header_layout.addWidget(btn)
            setattr(self, attr, btn)
        # 两个展示方式按钮：互斥的勾选态（ArcMap 的按钮组），默认「按图层顺序」
        self.view_mode_group = QButtonGroup(self)
        self.view_mode_group.setExclusive(True)
        for btn in (self.btn_view_order, self.btn_view_source):
            btn.setCheckable(True)
            self.view_mode_group.addButton(btn)
        self.btn_view_order.setChecked(True)
        self.btn_view_order.clicked.connect(lambda: self.set_view_mode("order"))
        self.btn_view_source.clicked.connect(lambda: self.set_view_mode("source"))
        # QGIS 桌面风格：按钮靠左排列，右侧留白
        header_layout.addStretch(1)
