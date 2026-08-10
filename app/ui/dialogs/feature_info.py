"""点击要素详情 Dialog。"""

from __future__ import annotations

from PyQt6.QtCore import QSize, Qt
from PyQt6.QtGui import QFont, QIcon
from PyQt6.QtWidgets import (
    QApplication,
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
)

from qgis.core import QgsWkbTypes
from qgis.gui import QgsHighlight

from app.core.domain.identify import IdentifyResult
from app.ui.common.icons import Icons
from app.ui.common.theme import ThemeManager


class FeatureInfoDialog(QDialog):
    """点击识别后展示要素详情的 Dialog。"""

    def __init__(self, parent, result: IdentifyResult, highlight: QgsHighlight = None) -> None:
        super().__init__(parent)
        self.setWindowIcon(Icons.app())
        self.layer = result.layer
        self.feature = result.feature
        self.highlight = highlight
        self.setWindowTitle("要素详情")
        self.setMinimumSize(500, 400)
        self.setModal(True)
        self._init_ui()

    def _init_ui(self) -> None:
        t = ThemeManager.instance().current()
        layout = QVBoxLayout()
        title = QHBoxLayout()
        icon_lbl = QLabel()
        icon_lbl.setPixmap(QIcon.fromTheme("layer-vector-point").pixmap(QSize(32, 32)))
        title.addWidget(icon_lbl)
        title.addWidget(QLabel(
            f"<h3 style='margin:0'>{self.layer.name()}</h3>"
            f"<p style='margin:0; color:{t.text_secondary}'>要素 ID: {self.feature.id()}</p>"
        ))
        title.addStretch()
        layout.addLayout(title)

        self.info_text = QTextEdit()
        self.info_text.setReadOnly(True)
        self.info_text.setFont(QFont("Segoe UI", 10))
        self._load_info()
        layout.addWidget(self.info_text, stretch=1)

        btn = QHBoxLayout()
        copy = QPushButton(QIcon.fromTheme("edit-copy"), "复制信息")
        copy.clicked.connect(self._copy)
        close = QPushButton(QIcon.fromTheme("window-close"), "关闭")
        close.clicked.connect(self.close)
        btn.addWidget(copy)
        btn.addWidget(close)
        layout.addLayout(btn)
        self.setLayout(main_layout := layout)

    def _load_info(self) -> None:
        t = ThemeManager.instance().current()
        info = "<div style='line-height:1.6; width:100%; box-sizing:border-box;'>"
        info += f"<p><strong>属性字段（共 {len(self.layer.fields())} 个）：</strong></p>"
        info += """
        <table style='width:100%; border-collapse:collapse; table-layout:auto;'>
            <colgroup><col style='width: 35%;' /><col style='width: 65%;' /></colgroup>
        """
        info += f"""
            <tr><td style='padding:8px; border-bottom:1px solid {t.panel_border};'>几何文本</td>
                <td style='padding:8px; border-bottom:1px solid {t.panel_border};'>{self.feature.geometry().asWkt()}</td></tr>
        """
        for i, (field, value) in enumerate(zip(self.layer.fields(), self.feature.attributes())):
            bg = t.panel_bg if i % 2 == 0 else t.input_bg
            val = str(value) if value is not None else f"<span style='color:{t.text_secondary}'>无</span>"
            info += (
                f"<tr style='background-color:{bg}'>"
                f"<td style='padding:8px; border-bottom:1px solid {t.panel_border};'>{field.name()}</td>"
                f"<td style='padding:8px; border-bottom:1px solid {t.panel_border};'>{val}</td></tr>"
            )
        info += "</table></div>"
        self.info_text.setHtml(info)
        self.info_text.setLineWrapMode(QTextEdit.LineWrapMode.NoWrap)
        self.info_text.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.info_text.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)

    def _copy(self) -> None:
        plain = f"图层: {self.layer.name()}\n要素ID: {self.feature.id()}\n"
        if self.feature.geometry().type() == QgsWkbTypes.PointGeometry:
            p = self.feature.geometry().asPointXY()
            plain += f"  X: {p.x():.6f}\n  Y: {p.y():.6f}\n"
        plain += "\n属性信息:\n"
        for f, v in zip(self.layer.fields(), self.feature.attributes()):
            plain += f"  {f.name()}: {v}\n"
        QApplication.clipboard().setText(plain)

    def closeEvent(self, event) -> None:
        if self.highlight is not None:
            self.highlight.hide()
        super().closeEvent(event)
