"""「关于」对话框。

展示应用名称与版本，由顶部「帮助」页签的「关于」按钮打开。
"""

from __future__ import annotations

from typing import Optional

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QFont
from qgis.PyQt.QtWidgets import (
    QDialogButtonBox,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from app import __version__
from app.ui.common.base_dialog import BaseDialog
from app.ui.common.icons import Icons


class AboutDialog(BaseDialog):
    """应用「关于」对话框。"""

    def __init__(
        self,
        parent: Optional[QWidget] = None,
        app_name: str = "图斑核实平台",
    ) -> None:
        super().__init__(
            parent,
            title="关于",
            show_min_button=False,
            show_max_button=False,
        )
        # BaseDialog 默认 800×600，关于框只放名称和版本，缩成紧凑尺寸
        self.setMinimumSize(420, 220)
        self.resize(480, 260)
        self.setModal(True)
        self._build_ui(app_name)

    def _build_ui(self, app_name: str) -> None:
        layout = QVBoxLayout()
        layout.setContentsMargins(28, 24, 28, 20)
        layout.setSpacing(10)
        layout.setAlignment(Qt.AlignmentFlag.AlignHCenter)

        icon = QLabel()
        icon.setPixmap(Icons.app().pixmap(64, 64))
        icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(icon)

        title = QLabel(app_name)
        title_font = QFont(title.font())
        title_font.setPointSize(15)
        title_font.setBold(True)
        title.setFont(title_font)
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(title)

        version = QLabel(f"版本 {__version__}")
        version.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(version)

        layout.addStretch(1)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok,
            Qt.Orientation.Horizontal,
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("确定")
        buttons.accepted.connect(self.accept)
        layout.addWidget(buttons, 0, Qt.AlignmentFlag.AlignCenter)

        self.setLayout(layout)
