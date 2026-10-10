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

from contextlib import suppress

from qgis.core import (
    QgsApplication,
    QgsLayerTreeModel,
    QgsProject,
    QgsVectorLayer,
)
from qgis.gui import QgsLayerTreeView
from qgis.PyQt.QtCore import QEvent, QPoint, QRectF, QSize, Qt, pyqtSignal
from qgis.PyQt.QtGui import (
    QColor,
    QFont,
    QFontMetrics,
    QIcon,
    QLinearGradient,
    QPainter,
    QPen,
)
from qgis.PyQt.QtWidgets import (
    QDockWidget,
    QHBoxLayout,
    QHeaderView,
    QStyle,
    QStyledItemDelegate,
    QToolButton,
    QToolTip,
    QVBoxLayout,
    QWidget,
)

from app.ui.common.theme import ThemeManager

# QGIS 主题图标名（3.40 / 4.2 两个运行时均已实测可用）
_THEME_ICONS = {
    "style": "mActionOptions.svg",
    "group": "mActionAddGroup.svg",
    "remove": "mActionRemoveLayer.svg",
    "expand": "mActionExpandTree.svg",
    "collapse": "mActionCollapseTree.svg",
    "up": "mActionArrowUp.svg",
    "down": "mActionArrowDown.svg",
}

# (按钮属性名, 图标键, 信号名, 悬停提示)
# 顺序按 QGIS 桌面图层面板：样式 → 添加组 → 删除 → 展开/折叠所有 → 上移/下移
_HEADER_SPECS = (
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

        QSS 的 ``:selected`` 规则排在 ``:hover`` 之后，同时命中时选中胜出，
        这里保持同样的优先级。
        """
        if option.state & QStyle.StateFlag.State_Selected:
            return QColor(theme.toolbar_hover)
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
        """悬停整行都弹完整图层名气泡。

        名字长到被行尾开关盖住、或 Dock 太窄把名字挤没了时，气泡是看全名的兜底
        （另一条路是把 Dock 拉宽，行自然就够长）。组等非图层节点交回默认处理。
        """
        try:
            layer = self._node_layer(index)
            if layer is not None and event is not None and event.type() == QEvent.Type.ToolTip:
                QToolTip.showText(event.globalPos(), layer.name(), view)
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


class LayerDock(QDockWidget):
    """独立的图层树面板。"""

    context_menu_requested = pyqtSignal(object, object)  # (QgsMapLayer, QPoint 全局坐标)
    layer_clicked = pyqtSignal(object)
    label_toggle_requested = pyqtSignal(object, bool)  # (QgsVectorLayer, 期望的标注开关)
    render_tab_requested = pyqtSignal()
    add_group_requested = pyqtSignal()
    remove_requested = pyqtSignal()
    expand_all_requested = pyqtSignal()
    collapse_all_requested = pyqtSignal()
    move_up_requested = pyqtSignal()
    move_down_requested = pyqtSignal()

    def __init__(self, parent=None) -> None:
        super().__init__("图层列表", parent)
        self.setAllowedAreas(Qt.DockWidgetArea.LeftDockWidgetArea | Qt.DockWidgetArea.RightDockWidgetArea)

        self.project: QgsProject = QgsProject.instance()
        self.tree_root = self.project.layerTreeRoot()

        self.model = QgsLayerTreeModel(self.tree_root)
        self.model.setFlags(
            QgsLayerTreeModel.AllowNodeRename
            | QgsLayerTreeModel.AllowNodeReorder
            | QgsLayerTreeModel.AllowNodeChangeVisibility
            # 不显示图例：分类/渐变渲染会挤进 "1"、"2" 等图例项
        )

        self.view = QgsLayerTreeView(self)
        self.view.setModel(self.model)
        # 行尾「标注」开关要一直看得见：列撑满视口 + 关横向滚动条，长图层名交给
        # 默认委托右侧省略（悬停气泡看全名、拉宽 Dock 看更多）。这样 ``option.rect``
        # 的右端就是视口右端，开关不会被长名字顶进横向滚动区里。
        self.view.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.view.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.view.clicked.connect(self.layer_clicked.emit)
        self.view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.view.customContextMenuRequested.connect(self._on_view_context_menu)
        # 行尾「标注」开关：只画、只发信号（读写标注由主窗口实现）
        self.label_delegate = _LabelRadioDelegate(self.view)
        self.view.setItemDelegate(self.label_delegate)
        self.label_delegate.toggle_requested.connect(self.label_toggle_requested.emit)

        self._build_header()

        # 内容 = 头部工具条 + 树视图（保留原生 Dock 标题栏）
        body = QWidget(self)
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.setSpacing(0)
        body_layout.addWidget(self.header_bar)
        body_layout.addWidget(self.view, 1)
        self.setWidget(body)

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
        index = self.view.indexAt(pos)
        if not index.isValid():
            return
        # index2node 对越界/野索引会抛异常；槽内异常未捕获会崩（0xC0000409）
        try:
            node = self.view.index2node(index)
        except Exception:  # noqa: BLE001
            return
        layer_getter = getattr(node, "layer", None)
        layer = layer_getter() if callable(layer_getter) else None
        if layer is None:
            return
        self.context_menu_requested.emit(layer, self.view.viewport().mapToGlobal(pos))

    def refresh_label_state(self) -> None:
        """重绘树视图，让行尾「标注」开关跟上图层实时标注状态。

        开关每帧读 ``layer.labelsEnabled()``，只是本构建的图层标注改动不发任何
        图层信号（见 :class:`_LabelRadioDelegate`），所以**调用方在改完标注后自己
        喊一声**——主窗口的标注开关、图层属性对话框收尾都会调它。
        """
        self.view.viewport().update()

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
            btn.clicked.connect(getattr(self, signal_name).emit)
            header_layout.addWidget(btn)
            setattr(self, attr, btn)
        # QGIS 桌面风格：按钮靠左排列，右侧留白
        header_layout.addStretch(1)
