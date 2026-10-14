"""定位气泡：悬浮在地图画布右上角的「输入坐标 + 定位」面板。

由 :class:`app.ui.main_window.side_toolbar.SideToolBar` 的「定位」按钮
点击唤起；同侧按钮再点一次关闭；面板上的 ``×`` 也能关闭。

坐标系：默认按当前画布 ``destinationCrs()`` 输入，CRS 文本标签显示在面板上
提示用户（默认 ``EPSG:4490`` 即经纬度）。**不**做坐标系换算（QGIS 原生
"Go to coordinate" 同样只接受画布坐标系），避免歧义；如需换算请改打开
坐标系转换面板。

定位行为：保留当前画布缩放级别（extent 的宽 / 高不变），把画布中心平移到
输入坐标；``setExtent`` + ``setCenter`` + ``refresh``（事件栈外调 refresh
详见 :mod:`app.ui.tools.qmap_compare`）。空值 / 非法输入只标记错误边框、
不给画布任何副作用。

视觉：``QFrame`` + 半透明主题底色 + 圆角 + 主题边框 + 投影；输入控件框
``QDoubleSpinBox``，自动跟随主题的 QSS（见 ``app.ui.common.theme``）。
"""

from __future__ import annotations

from typing import Optional

from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsPointXY,
    QgsProject,
    QgsRectangle,
)
from qgis.gui import QgsMapCanvas, QgsVertexMarker
from qgis.PyQt import sip
from qgis.PyQt.QtCore import Qt, QTimer, pyqtSignal
from qgis.PyQt.QtGui import QColor, QFont, QKeyEvent
from qgis.PyQt.QtWidgets import (
    QComboBox,
    QDoubleSpinBox,
    QFrame,
    QGraphicsDropShadowEffect,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from app.infra.logging import get_logger
from app.ui.common.theme import Theme, ThemeManager

logger = get_logger(__name__)

#: 与画布 4 边的间距（逻辑像素）。同 ``_place_side_toolbar`` 用的边距对齐，
#: 两个左上角 / 右上角悬浮元素视觉错位不会太夸张。
_MARGIN = 8
#: 几何坐标系（EPSG:4490 等角度系）保留 6 位小数 ≈ 0.11 m；投影系 3 位 ≈ 1 m。
#: 切换 CRS 自动适配，避免精度漂移或裁掉有效位。
_DECIMALS_GEO = 6
_DECIMALS_PROJ = 3
#: 输入单步 / 上下按钮步进（角度系 0.0001° ≈ 11 m；投影系 1 m）
_SINGLE_STEP_GEO = 0.0001
_SINGLE_STEP_PROJ = 1.0
#: 定位标点颜色。红色 → 与工具条上定位图标（crosshairs）同为强调色，
#: 深底主题也足够亮。
_PIN_COLOR = QColor(220, 40, 40)

#: 视野跨度超过这些阈值就认为"大到看不出在哪"，定位时改用默认窗口（见
#: :meth:`CoordLocateOverlay._window_half_size`）。
_MAX_SPAN_DEG = 10.0          # 角度系：10° 已经跨好几个省
_MAX_SPAN_METERS = 100_000.0  # 投影系：100km
#: 收窄后用的默认窗口（度 / 米）：约 2km 见方，够看清目标点周围
_DEFAULT_SPAN_DEG = 0.02
_DEFAULT_SPAN_METERS = 2000.0

#: 「定位」的**输入坐标系**选项：(下拉里显示的名字, EPSG 码)。
#: 默认第一项是 CGCS2000 经纬度 —— 用户最常直接输经纬度，而这时**必须**按经纬度
#: 解释、再转换到地图坐标系：地图坐标系若是投影系（如 3857／高斯带），把 117/28
#: 当平面米直接塞进去就跑到几内亚湾去了（用户报的"定位到国外"）。
#: 其余四项是国土 / 林业常用的 3° 带高斯投影（`CM_xxxE` 无带号前缀、`Zone_nn` 带）。
_INPUT_CRS_OPTIONS: tuple[tuple[str, str], ...] = (
    ("经纬度（CGCS2000）", "EPSG:4490"),
    ("CGCS2000_3_Degree_GK_CM_114E", "EPSG:4547"),
    ("CGCS2000_3_Degree_GK_CM_117E", "EPSG:4548"),
    ("CGCS2000_3_Degree_GK_Zone_38", "EPSG:4526"),
    ("CGCS2000_3_Degree_GK_Zone_39", "EPSG:4527"),
)


class CoordLocateOverlay(QFrame):
    """定位气泡（X / Y 输入 + 定位按钮 + × 关闭）。"""

    #: 用户主动关闭时（点 × 或外部 toggle 收掉）发出，供宿主同步按钮高亮。
    closed = pyqtSignal()

    def __init__(self, canvas: QgsMapCanvas, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._canvas = canvas
        #: 画布上的定位标点（首次成功定位时创建，气泡关闭时隐藏）。
        #: 用 :class:`QgsVertexMarker` 是项目既定约定——参见
        #: :mod:`app.ui.dialogs.shortest_path` 的起点/终点 marker。
        #: 挂在画布上（QgsMapCanvasItem）→ 画布重绘时随地图一起重画，
        #: 跨地图路径位置 / 缩放都自动跟随。C++ 对象随画布释放。
        self._marker: Optional[QgsVertexMarker] = None
        self._build_ui()
        self._refresh_crs_label()
        self._apply_style()
        # 主题切换：本面板的 QSS（半透明底 + 边框 + 投影色）按主题 token 重生成
        ThemeManager.instance().theme_changed.connect(self._apply_style)
        # CRS 改变（用户改了工程 CRS）：重算小数位与步进 + 刷新标签
        try:
            canvas.mapSettings().destinationCrsChanged.connect(self._on_crs_changed)
        except Exception:  # noqa: BLE001
            # 极少数 QGIS 构建上无该信号，CRS 由 placement 回调里兜底刷新
            pass

    # ===================================================== 装配

    def _build_ui(self) -> None:
        """一行布局：横坐标 input / 纵坐标 input / 定位按钮 / × 关闭。

        标签用简短的 ``X:`` / ``Y:`` —— 因为已用「坐标系 EPSG:4490」+
        SpinBox 的小数位提示用户是经纬度还是投影；中文长标签在 4 个控件一排
        的气泡里会把面板挤到 600px 以上，盖住画布右侧很大区域。简短标签视觉
        更紧凑，与设计原型"短输入 + 一次操作"的轻量风一致。
        """
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.setSpacing(4)

        top = QHBoxLayout()
        top.setSpacing(6)

        # ---- × 关闭按钮 ----
        # 紧贴面板左侧，作为"气泡的嘴"提醒用户这是临时面板而非画布装饰；
        # 同时给用户一个明确的关闭入口，不必再回侧栏点同按钮。
        self._close_btn = QToolButton(self)
        self._close_btn.setText("×")
        self._close_btn.setToolTip("关闭（Esc）")
        self._close_btn.setFixedSize(22, 22)
        self._close_btn.setObjectName("CoordLocateClose")
        self._close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._close_btn.setAutoRaise(True)
        self._close_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)  # 不抢输入框焦点
        self._close_btn.clicked.connect(self._on_close_clicked)
        top.addWidget(self._close_btn)

        # ---- 横坐标 (X) ----
        x_label = QLabel("横坐标 X:")
        x_label.setObjectName("CoordLocateLabel")
        top.addWidget(x_label)
        self._x_input = QDoubleSpinBox()
        self._x_input.setObjectName("CoordLocateX")
        self._x_input.setDecimals(_DECIMALS_GEO)
        self._x_input.setRange(-1e9, 1e9)
        self._x_input.setSingleStep(_SINGLE_STEP_GEO)
        # 紧凑：spinBox 默认 22px 内边距 + 大量上下空白，气泡里太高
        self._x_input.setFixedWidth(120)
        self._x_input.setMinimumHeight(26)
        # 值改变即清错误边框（用户已经在改了就别一直红）
        self._x_input.valueChanged.connect(lambda *_: self._clear_error(self._x_input))
        # 回车触发定位
        self._x_input.lineEdit().returnPressed.connect(self._on_locate_clicked)
        top.addWidget(self._x_input)

        # ---- 纵坐标 (Y) ----
        y_label = QLabel("纵坐标 Y:")
        y_label.setObjectName("CoordLocateLabel")
        top.addWidget(y_label)
        self._y_input = QDoubleSpinBox()
        self._y_input.setObjectName("CoordLocateY")
        self._y_input.setDecimals(_DECIMALS_GEO)
        self._y_input.setRange(-1e9, 1e9)
        self._y_input.setSingleStep(_SINGLE_STEP_GEO)
        self._y_input.setFixedWidth(120)
        self._y_input.setMinimumHeight(26)
        self._y_input.valueChanged.connect(lambda *_: self._clear_error(self._y_input))
        self._y_input.lineEdit().returnPressed.connect(self._on_locate_clicked)
        top.addWidget(self._y_input)

        # ---- 定位按钮（主操作）----
        self._locate_btn = QPushButton("定位")
        self._locate_btn.setObjectName("CoordLocateBtn")
        self._locate_btn.setToolTip("把画布中心移到上述坐标（Enter / 鼠标）")
        self._locate_btn.setMinimumHeight(26)
        self._locate_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._locate_btn.clicked.connect(self._on_locate_clicked)
        top.addWidget(self._locate_btn)

        # ---- 坐标系提示（小灰字），放在最右边 ----
        self._crs_label = QLabel("")
        self._crs_label.setObjectName("CoordLocateCrs")
        font = QFont()
        font.setPointSize(8)
        self._crs_label.setFont(font)
        top.addWidget(self._crs_label)

        layout.addLayout(top)

        # ---- 第二行：输入坐标系选择 ----
        # 输入按**这个**坐标系解释，再转换到地图坐标系（见 _on_locate_clicked）。
        # 用户最常直接输经纬度，而地图坐标系可能是投影系，两者不是一回事。
        crs_row = QHBoxLayout()
        crs_row.setSpacing(6)
        crs_title = QLabel("坐标系:")
        crs_title.setObjectName("CoordLocateLabel")
        crs_row.addWidget(crs_title)
        self._crs_combo = QComboBox(self)
        self._crs_combo.setObjectName("CoordLocateCrsCombo")
        self._crs_combo.setMinimumHeight(26)
        self._crs_combo.setToolTip("输入坐标所属的坐标系；定位时会自动换算到地图坐标系")
        for label, authid in _INPUT_CRS_OPTIONS:
            self._crs_combo.addItem(label, authid)
        self._crs_combo.currentIndexChanged.connect(self._on_input_crs_changed)
        crs_row.addWidget(self._crs_combo, 1)
        layout.addLayout(crs_row)

    def _input_crs(self) -> QgsCoordinateReferenceSystem:
        """当前选中的**输入坐标系**；取不到时退回 CGCS2000 经纬度。"""
        authid = ""
        try:
            authid = self._crs_combo.currentData() or ""
        except RuntimeError:
            pass
        crs = QgsCoordinateReferenceSystem(authid) if authid else QgsCoordinateReferenceSystem()
        if not crs.isValid():
            crs = QgsCoordinateReferenceSystem("EPSG:4490")
        return crs

    def _on_input_crs_changed(self, *_args) -> None:
        """换了输入坐标系：小数位 / 步进跟着换（经纬度 6 位、平面 3 位）。"""
        self._sync_input_precision()
        self._clear_error(self._x_input)
        self._clear_error(self._y_input)

    # ===================================================== 显隐

    def open(self) -> None:
        """显示气泡并把焦点送到横坐标输入框。"""
        if self.isVisible():
            return
        self.show()
        self.raise_()
        # 显隐过程中 adjustSize：作为悬浮子控件，``show()`` 不自动套用
        # sizeHint（与 :class:`CanvasZoomTool` 同样问题，会塌缩成 0 高度）
        self.adjustSize()
        self._clear_error(self._x_input)
        self._clear_error(self._y_input)
        self._x_input.setFocus()
        self._x_input.selectAll()

    def close(self) -> None:
        """关闭气泡。同时隐藏定位标点（气泡关掉则不再表示“该点为此式定位点”）。

        标点引用保留，下次打开重新定位时仍可复用。画布本身被销毁时
        :class:`QgsVertexMarker` C++ 对象会跟着死，下一次复用由
        :meth:`_ensure_marker` 中的 :class:`RuntimeError` 兜底重建。
        """
        self._hide_marker()
        if not self.isVisible():
            # 即使已隐藏也通知宿主清高亮（external toggle 关闭走这里更明确）
            self.closed.emit()
            return
        self.hide()
        self.closed.emit()

    def toggle(self) -> None:
        """已显示则关闭，否则打开。"""
        if self.isVisible():
            self.close()
        else:
            self.open()

    def is_open(self) -> bool:
        """对外查询当前是否处于显示状态（宿主据此同步按钮高亮）。"""
        return self.isVisible()

    # ===================================================== 事件

    def keyPressEvent(self, event: QKeyEvent) -> None:
        """Esc 关闭面板，与 QGIS 桌面浮窗一致（不抢画布其它快捷键）。"""
        if event.key() == Qt.Key.Key_Escape:
            self.close()
            event.accept()
            return
        super().keyPressEvent(event)

    # ===================================================== 内部

    def _on_close_clicked(self) -> None:
        self.close()

    def _on_locate_clicked(self) -> None:
        """把画布居中到 (X, Y)；输入无效仅标红、不动画布。"""
        x = float(self._x_input.value())
        y = float(self._y_input.value())
        # 校验按**输入坐标系**来：角度系才卡经纬度范围，平面系不卡。
        src_crs = self._input_crs()
        if src_crs.isGeographic():
            ok_x = -360.0 <= x <= 360.0
            ok_y = -180.0 <= y <= 180.0
        else:
            ok_x = ok_y = True
        if not ok_x:
            self._mark_error(self._x_input)
            return
        if not ok_y:
            self._mark_error(self._y_input)
            return

        canvas = self._canvas
        dest_crs = canvas.mapSettings().destinationCrs()
        target = QgsPointXY(x, y)
        # **按输入坐标系解释、再换算到地图坐标系**：地图可能是投影系（3857 / 高斯带），
        # 直接把 117/28 当平面米塞进 3857 就落到几内亚湾了（用户报的"定位到国外"）。
        if src_crs != dest_crs:
            try:
                transform = QgsCoordinateTransform(src_crs, dest_crs, QgsProject.instance())
                target = transform.transform(target)
            except Exception as exc:  # noqa: BLE001 —— 越界 / 无法转换
                logger.warning("定位坐标换算失败 (%s, %s): %s", x, y, exc)
                self._mark_error(self._x_input)
                self._mark_error(self._y_input)
                return
        crs = dest_crs
        try:
            # 保留当前缩放级别（extent 大小不变），仅挪中心。
            # setExtent 必不可少：从未 setExtent 时 setCenter / zoomScale 是 no-op
            # （extent 保持 Null），实测 center 变 nan（见 qmap_compare.py 的同
            # 款注释）。直接沿用当前 extent，仅替换中心点。
            cur = canvas.extent()
            if cur is None or cur.isNull() or cur.isEmpty():
                # 全新工程尚未 setExtent：以画布尺寸为默认大小，居中到目标点
                size = canvas.size()
                cur = QgsRectangle(0, 0, max(size.width(), 100), max(size.height(), 100))
            half_w, half_h = self._window_half_size(cur, crs)
            new_extent = QgsRectangle(
                target.x() - half_w,
                target.y() - half_h,
                target.x() + half_w,
                target.y() + half_h,
            )
            canvas.setExtent(new_extent)
            canvas.setCenter(target)
        except Exception as exc:  # noqa: BLE001
            logger.warning("定位失败 (%s, %s): %s", x, y, exc, exc_info=True)
            self._mark_error(self._x_input)
            self._mark_error(self._y_input)
            return
        # 在画布上落下标点（“图钉”效果）：红十字 + red，与工具条上定位图标同调。
        # marker 是 :class:`QgsMapCanvasItem`，setCenter + show 后会被下一拍
        # canvas.refresh() 随地图一起画出。
        self._show_marker(target)
        # 不在信号栈内直接 refresh：setCenter/zoomScale 信号链尚未走完就在
        # 同一栈里 refresh 会重入渲染，QGIS 这边偶发 native 崩溃。推到下一拍。
        QTimer.singleShot(0, canvas.refresh)

    @staticmethod
    def _window_half_size(cur, crs) -> tuple[float, float]:
        """定位窗口的半宽 / 半高：**保留当前缩放级别**，但视野大到看不出在哪时收窄。

        定位的语义是"保留当前比例尺、把画面挪到目标点"（与 QGIS 的「转到坐标」一致）。
        可刚进程序时画布是**全球范围**（默认底图的世界范围，跨度 800° 上下），照搬
        这个跨度挪中心，屏幕上仍是一整张世界地图 —— 用户以为"定位到国外去了"
        （实测：输入 117/28 后中心确实对，但可视范围是 -310..544 度）。

        所以跨度大到没意义时，按比例收到一个能看清的默认窗口（角度系 0.02° ≈ 2km，
        投影系 2000m），宽高比照旧。
        """
        width, height = cur.width(), cur.height()
        span = max(width, height)
        limit, default = (
            (_MAX_SPAN_DEG, _DEFAULT_SPAN_DEG)
            if crs is not None and crs.isGeographic()
            else (_MAX_SPAN_METERS, _DEFAULT_SPAN_METERS)
        )
        if span > limit > 0:
            scale = default / span
            width, height = width * scale, height * scale
        return width / 2.0, height / 2.0

    # ---- 标点管理 ----
    def _ensure_marker(self) -> Optional[QgsVertexMarker]:
        """懒创建画布定位标点（QgsVertexMarker）。C++ 对象死亡时重建。

        :class:`QgsVertexMarker` 继承 :class:`QgsMapCanvasItem`，``setParent`` 到
        画布上后随画布一起释放；父画布被场景重置 / 重建时，本控件缓存的 Python
        引用还在，但 C++ 对象已死。需用 :func:`qgis.PyQt.sip.isdeleted` 检查存活，
        不能随便调一个无返回值的 getter —— 很多 QGIS API 只有 setter 没有对应的 getter，
        例如本类只有 ``setIconSize`` 没有 ``iconSize``，调后者会扔 :class:`AttributeError`
        而不是 C++ 挂掉后的 :class:`RuntimeError`，凭这个判存活就会误判。
        """
        # 1) 复查旧引用：sip.isdeleted 是 Qt 标准 “还活着吗” 检查，返回 True 表示已死
        if self._marker is not None:
            try:
                alive = not sip.isdeleted(self._marker)
            except (RuntimeError, TypeError):
                # sip 本体罕有异常，同样按“已死”处理，避免撞上已损坏的实例
                alive = False
            if alive:
                return self._marker
            self._marker = None
        # 2) 重建
        try:
            marker = QgsVertexMarker(self._canvas)
        except RuntimeError:
            # 画布本身已死（窗口关闭流程）→ 不重建，静默丢弃
            return None
        marker.setIconType(QgsVertexMarker.ICON_CROSS)
        marker.setIconSize(12)
        marker.setPenWidth(2)
        marker.setColor(_PIN_COLOR)
        marker.setFillColor(_PIN_COLOR)
        self._marker = marker
        return marker

    def _show_marker(self, point: QgsPointXY) -> None:
        """在画布的 ``point`` 处显示标点；首次调用后才创建。"""
        marker = self._ensure_marker()
        if marker is None:
            return
        try:
            marker.setCenter(point)
            marker.show()
        except RuntimeError:
            # 画布被析构了，丢弃引用后下次调用重建
            self._marker = None

    def _hide_marker(self) -> None:
        """隐藏标点（引用保留，下次定位复用）。画布死亡时静默失败。"""
        marker = self._marker
        if marker is None:
            return
        try:
            marker.hide()
        except RuntimeError:
            self._marker = None

    def _mark_error(self, box: QDoubleSpinBox) -> None:
        """给输入框临时加 ``Error`` 动态属性，QSS 用 ``[Error="true"]`` 描红。"""
        box.setProperty("Error", True)
        # 改完属性后强制刷新样式表，否则 QSS 选择器不会立刻生效
        box.style().unpolish(box)
        box.style().polish(box)
        box.setFocus()

    def _clear_error(self, box: QDoubleSpinBox) -> None:
        if box.property("Error"):
            box.setProperty("Error", False)
            box.style().unpolish(box)
            box.style().polish(box)

    def _on_crs_changed(self) -> None:
        """画布目标 CRS 改了（用户切换工程 / 改坐标系）：刷新标签 + 步进 / 小数位。"""
        self._refresh_crs_label()

    def _refresh_crs_label(self) -> None:
        """显示当前**地图**坐标系（输入坐标系由下拉选，见 _input_crs）。"""
        try:
            crs = self._canvas.mapSettings().destinationCrs()
        except Exception:  # noqa: BLE001
            self._crs_label.setText("")
            return
        if not crs.isValid():
            self._crs_label.setText("")
            return
        self._crs_label.setText(f"地图 {crs.authid()}")
        self._sync_input_precision()

    def _sync_input_precision(self) -> None:
        """小数位 / 步进跟随**输入坐标系**（角度系 6 位，平面系 3 位）。"""
        is_geo = self._input_crs().isGeographic()
        self._x_input.setDecimals(_DECIMALS_GEO if is_geo else _DECIMALS_PROJ)
        self._y_input.setDecimals(_DECIMALS_GEO if is_geo else _DECIMALS_PROJ)
        self._x_input.setSingleStep(_SINGLE_STEP_GEO if is_geo else _SINGLE_STEP_PROJ)
        self._y_input.setSingleStep(_SINGLE_STEP_GEO if is_geo else _SINGLE_STEP_PROJ)

    # ===================================================== 主题

    def _apply_style(self, *_args) -> None:
        """本面板的 QSS（半透明主题底 + 圆角边框 + 投影 + 输入错误红边）。

        输入 / spinbox 通用样式由全局 ``window_qss`` 接管（含聚焦强调色边框），
        此处只覆盖本面板范围内的样式以避免污染全局。
        """
        theme: Theme = ThemeManager.instance().current()
        border_color = theme.border
        text_color = theme.menu_text
        # 半透明主题底色：与毛玻璃面板一致的"半透磨砂"质感，配合投影形成气泡感；
        # 使用 ``_with_alpha`` 没传（私有），这里手动生成 rgba
        base = QColor(theme.input_bg)
        bg_rgba = f"rgba({base.red()}, {base.green()}, {base.blue()}, 230)"
        self.setStyleSheet(
            f"""
            QFrame {{
                background-color: {bg_rgba};
                border: 1px solid {border_color};
                border-radius: 6px;
                color: {text_color};
            }}
            QLabel#CoordLocateLabel {{
                background: transparent;
                color: {text_color};
                padding: 0px;
            }}
            QLabel#CoordLocateCrs {{
                background: transparent;
                color: {theme.text_secondary};
                padding-left: 4px;
                padding-right: 4px;
            }}
            QPushButton#CoordLocateBtn {{
                background-color: {theme.input_focus};
                color: white;
                border: 1px solid {theme.input_focus};
                border-radius: 4px;
                padding: 4px 14px;
                font-weight: 600;
            }}
            QPushButton#CoordLocateBtn:hover {{
                /* Qt 不支持 CSS filter；用加深边框 + 稍微改背景与主题输入交互 hover 区分 */
                border: 1px solid {theme.menu_text};
            }}
            QPushButton#CoordLocateBtn:pressed {{
                padding-top: 5px;
                padding-bottom: 3px;
            }}
            QToolButton#CoordLocateClose {{
                background: transparent;
                border: none;
                color: {theme.text_secondary};
                font-size: 16pt;
                font-weight: 600;
                padding: 0px;
            }}
            QToolButton#CoordLocateClose:hover {{
                color: {theme.close_hover};
            }}
            /* 输入错误态：红色左边框（与全局 error 样式一致） */
            QDoubleSpinBox#CoordLocateX[Error="true"],
            QDoubleSpinBox#CoordLocateY[Error="true"] {{
                border: 1px solid {theme.text_error};
                border-left: 3px solid {theme.text_error};
            }}
            """
        )
        # 柔和投影：让面板浮在地图上（与 CanvasZoomTool 同款）
        if self.graphicsEffect() is None:
            shadow = QGraphicsDropShadowEffect(self)
            shadow.setBlurRadius(12)
            shadow.setOffset(0, 2)
            shadow.setColor(QColor(0, 0, 0, 80))
            self.setGraphicsEffect(shadow)
