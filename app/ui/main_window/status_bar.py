"""状态栏：鼠标坐标 + 比例尺 + 投影选择器。"""

from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QLabel, QMainWindow


class StatusBar:
    """状态栏构建器。"""

    def __init__(self, main_window: QMainWindow) -> None:
        self.main_window = main_window
        self.status_bar = main_window.statusBar()

    def build(self) -> None:
        """组装状态栏：左侧消息栏，右侧 鼠标坐标 + 比例尺 + 投影选择器。"""
        from qgis.gui import QgsProjectionSelectionWidget, QgsScaleWidget

        self.status_bar.showMessage("就绪")

        # 鼠标坐标（最左侧永久部件，随鼠标移动实时更新）
        self.coord_label = self._build_coordinate_label()
        self.status_bar.addPermanentWidget(self.coord_label)

        # 比例尺：本构建里 QgsScaleWidget(canvas) 的构造参数不生效（没有 setCanvas），
        # 只显示默认 1:1,000,000，且永远不随缩放更新 —— "比例尺未生效" 的根因。
        # 必须显式 setMapCanvas + 连接 scaleChanged，否则缩放后比例尺不变。
        self.scale_widget = QgsScaleWidget()
        self.scale_widget.setMapCanvas(self.main_window.canvas)
        self.scale_widget.updateScales()
        self.scale_widget.setMinimumWidth(120)
        self.main_window.canvas.scaleChanged.connect(self._on_canvas_scale_changed)
        self._update_scale()
        self.status_bar.addPermanentWidget(self.scale_widget)

        # 投影选择器：构造后 crs() 默认无效（authid 空），需显式 setCrs 到当前工程
        # 才能显示正确的 CRS；否则控件永久停留在"无效投影"状态。
        # 同时把 ProjectCrs 选项隐藏（占用空间大、不需要），并绑定 crsChanged 信号
        # 在工程 CRS 变更时同步。
        from qgis.core import QgsProject

        proj_selector = QgsProjectionSelectionWidget(self.main_window)
        # 限制下拉最大宽度，避免占用过多状态栏空间
        proj_selector.setMaximumWidth(200)
        # CRS 由 MainWindow 注入前先隐藏 ProjectCrs 选项
        try:
            proj_selector.setOptionVisible(
                proj_selector.CrsOption.ProjectCrs, False
            )
        except Exception:  # noqa: BLE001
            pass
        # 拿当前工程 CRS：MainWindow 装配顺序在 _build_status_bar 之前已
        # 把 ``self.project = QgsProject.instance()`` 绑好。若宿主未挂
        # ``project`` 属性（裸 QMainWindow 测试），退回到全局单例。
        project = getattr(self.main_window, "project", None) or QgsProject.instance()
        proj_selector.setCrs(project.crs())

        # crsChanged 是无参信号，setCrs 需要 CRS 参数 → 包一层 lambda。
        # 控件被销毁后单例 project 仍可能发信号 → 闭包访问已死的 sip 对象会
        # 抛 RuntimeError，所以再 try 一层（见 qgis-ui-integration-gotchas）。
        def _sync_proj_selector() -> None:
            try:
                proj_selector.setCrs(project.crs())
            except RuntimeError:
                pass

        project.crsChanged.connect(_sync_proj_selector)
        self.status_bar.addPermanentWidget(proj_selector)

    def _build_coordinate_label(self) -> QLabel:
        """鼠标位置坐标标签：跟随画布 xyCoordinates 信号更新。"""
        label = QLabel("—, —")
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        label.setMinimumWidth(150)
        label.setToolTip("当前鼠标位置坐标")
        canvas = self.main_window.canvas
        canvas.xyCoordinates.connect(self._on_xy_coordinates)
        # 切换投影后坐标显示格式可能变化（地理 / 投影），重置占位
        canvas.destinationCrsChanged.connect(self._on_destination_crs_changed)
        return label

    # ---- 比例尺 ----

    def _on_canvas_scale_changed(self, *args) -> None:
        self._update_scale()

    def _update_scale(self) -> None:
        """把画布当前比例同步到比例尺控件。

        canvas.scale() 在尚未设置范围时为 1.0（哨兵值），此时保留控件默认
        1:1,000,000，避免显示怪异的 1:1。
        """
        if self.main_window.canvas.scale() > 1.0:
            self.scale_widget.setScaleFromCanvas()

    # ---- 鼠标坐标 ----

    def _on_destination_crs_changed(self) -> None:
        self._on_xy_coordinates(None)

    def _on_xy_coordinates(self, point) -> None:
        """鼠标移动时更新坐标标签；离开画布（空点）时显示占位。"""
        if point is None or point.isEmpty():
            self.coord_label.setText("—, —")
            return
        x, y = point.x(), point.y()
        crs = self.main_window.canvas.mapSettings().destinationCrs()
        if crs is not None and crs.isValid() and crs.isGeographic():
            text = f"{x:.5f}°, {y:.5f}°"
        else:
            text = f"{x:.1f}, {y:.1f}"
        self.coord_label.setText(text)
