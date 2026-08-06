"""点击识别后的要素详情 Dock。

替代原来的弹出 :class:`FeatureInfoDialog`：点击要素后不再弹模态框，
而是把详情显示到停靠在右侧的 Dock 里。内容就地刷新，不重复建 Dock。
"""

from __future__ import annotations

from typing import Optional

from PyQt5.QtCore import Qt
from PyQt5.QtGui import QFont, QIcon
from PyQt5.QtWidgets import (
    QApplication,
    QDockWidget,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from qgis.core import QgsWkbTypes
from qgis.gui import QgsHighlight

from app.core.domain.identify import IdentifyResult


class FeatureInfoDock(QDockWidget):
    """点击识别后展示要素详情的 Dock（默认停靠右侧）。"""

    def __init__(self, parent=None) -> None:
        super().__init__("要素详情", parent)
        self.setFeatures(QDockWidget.AllDockWidgetFeatures)
        self.setAllowedAreas(Qt.LeftDockWidgetArea | Qt.RightDockWidgetArea | Qt.BottomDockWidgetArea)
        self.setMinimumSize(320, 200)
        self._highlight: Optional[QgsHighlight] = None
        self._init_ui()

    def _init_ui(self) -> None:
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        self.title_label = QLabel()
        self.title_label.setWordWrap(True)
        layout.addWidget(self.title_label)

        self.info_text = QTextEdit()
        self.info_text.setReadOnly(True)
        self.info_text.setFont(QFont("Segoe UI", 10))
        layout.addWidget(self.info_text, stretch=1)

        btn = QHBoxLayout()
        copy = QPushButton(QIcon.fromTheme("edit-copy"), "复制信息")
        copy.clicked.connect(self._copy)
        close = QPushButton(QIcon.fromTheme("window-close"), "关闭")
        close.clicked.connect(self.close)
        btn.addWidget(copy)
        btn.addWidget(close)
        btn.addStretch()
        layout.addLayout(btn)

        self.setWidget(widget)

    # ------------------------------------------------------------ 展示

    def show_feature(self, result: IdentifyResult, highlight: Optional[QgsHighlight] = None) -> None:
        """刷新为指定要素的信息并显示 Dock（用户移动过的停靠位置保持不变）。"""
        self.layer = result.layer
        self.feature = result.feature
        if self._highlight is not None:
            self._highlight.hide()
        self._highlight = highlight
        self.title_label.setText(
            f"<h3 style='margin:0'>{self.layer.name()}</h3>"
            f"<p style='margin:0; color:#666'>要素 ID: {self.feature.id()}</p>"
        )
        self._load_info()
        self.show()
        self.raise_()

    def _load_info(self) -> None:
        info = "<div style='line-height:1.6; width:100%; box-sizing:border-box;'>"
        info += f"<p><strong>属性字段（共 {len(self.layer.fields())} 个）：</strong></p>"
        info += """
        <table style='width:100%; border-collapse:collapse; table-layout:auto;'>
            <colgroup><col style='width: 35%;' /><col style='width: 65%;' /></colgroup>
        """
        info += f"""
            <tr><td style='padding:8px; border-bottom:1px solid #eee;'>几何文本</td>
                <td style='padding:8px; border-bottom:1px solid #eee;'>{self.feature.geometry().asWkt()}</td></tr>
        """
        for i, (field, value) in enumerate(zip(self.layer.fields(), self.feature.attributes())):
            bg = "#f8f9fa" if i % 2 == 0 else "#ffffff"
            val = str(value) if value is not None else "<span style='color:#999'>无</span>"
            info += (
                f"<tr style='background-color:{bg}'>"
                f"<td style='padding:8px; border-bottom:1px solid #eee;'>{field.name()}</td>"
                f"<td style='padding:8px; border-bottom:1px solid #eee;'>{val}</td></tr>"
            )
        info += "</table></div>"
        self.info_text.setHtml(info)
        self.info_text.setLineWrapMode(QTextEdit.NoWrap)
        self.info_text.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.info_text.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)

    def _copy(self) -> None:
        if not hasattr(self, "layer"):
            return
        plain = f"图层: {self.layer.name()}\n要素ID: {self.feature.id()}\n"
        if self.feature.geometry().type() == QgsWkbTypes.PointGeometry:
            p = self.feature.geometry().asPointXY()
            plain += f"  X: {p.x():.6f}\n  Y: {p.y():.6f}\n"
        plain += "\n属性信息:\n"
        for f, v in zip(self.layer.fields(), self.feature.attributes()):
            plain += f"  {f.name()}: {v}\n"
        QApplication.clipboard().setText(plain)

    def closeEvent(self, event) -> None:
        if self._highlight is not None:
            self._highlight.hide()
        super().closeEvent(event)
