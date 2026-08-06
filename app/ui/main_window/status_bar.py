"""状态栏：比例尺 + 投影选择器。"""

from __future__ import annotations

from PyQt5.QtWidgets import QMainWindow


class StatusBar:
    """状态栏构建器。"""

    def __init__(self, main_window: QMainWindow) -> None:
        self.main_window = main_window
        self.status_bar = main_window.statusBar()

    def build(self) -> None:
        """组装状态栏：左侧消息栏，右侧比例尺 + 投影选择器。"""
        from qgis.gui import QgsScaleWidget, QgsProjectionSelectionWidget

        self.status_bar.showMessage("就绪")

        # 比例尺
        scale_widget = QgsScaleWidget(self.main_window.canvas)
        self.status_bar.addPermanentWidget(scale_widget)

        # 投影选择器
        proj_selector = QgsProjectionSelectionWidget(self.main_window)
        # CRS 由 MainWindow 注入前先隐藏 ProjectCrs 选项
        try:
            proj_selector.setOptionVisible(
                proj_selector.CrsOption.ProjectCrs, False
            )
        except Exception:  # noqa: BLE001
            pass
        self.status_bar.addPermanentWidget(proj_selector)
