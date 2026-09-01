"""点击要素详情 Dialog。"""

from __future__ import annotations

from qgis.PyQt.QtCore import QSize, Qt
from qgis.PyQt.QtGui import QFont, QIcon
from qgis.PyQt.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
)

from qgis.core import QgsWkbTypes
from qgis.gui import QgsHighlight

from app.core.domain.identify import IdentifyResult
from app.ui.common.base_dialog import BaseDialog
from app.ui.common.icons import Icons
from app.ui.common.theme import ThemeManager


class FeatureInfoDialog(BaseDialog):
    """点击识别后展示要素详情的 Dialog。"""

    def __init__(self, parent, result: IdentifyResult, highlight: QgsHighlight = None) -> None:
        super().__init__(parent)
        self.setWindowIcon(Icons.app())
        self.layer = result.layer
        self.feature = result.feature
        self.result = result
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
        if self.result.is_raster:
            subtitle = "栅格像元识别"
        else:
            subtitle = f"要素 ID: {self.feature.id()}"
        title.addWidget(QLabel(
            f"<h3 style='margin:0'>{self.layer.name()}</h3>"
            f"<p style='margin:0; color:{t.text_secondary}'>{subtitle}</p>"
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
        if self.result.is_raster:
            self._load_raster_info()
            return
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

    def _load_raster_info(self) -> None:
        """栅格识别：显示点击坐标与该像元各波段的取值（与 Dock 同款展示）。"""
        t = ThemeManager.instance().current()
        result = self.result
        coord = ""
        if result.feature is not None and not result.feature.geometry().isEmpty():
            try:
                p = result.feature.geometry().centroid().asPoint()  # QGIS4: asPointXY 已移除
                coord = f"X: {p.x():.6f}　Y: {p.y():.6f}"
            except Exception:  # noqa: BLE001
                coord = ""
        rows = list(result.raster_values.items())
        if coord:
            rows.insert(0, ("点击坐标", coord))
        info = "<div style='line-height:1.6; width:100%; box-sizing:border-box;'>"
        info += "<p><strong>波段值（栅格像元）：</strong></p>"
        info += """
        <table style='width:100%; border-collapse:collapse; table-layout:auto;'>
            <colgroup><col style='width: 35%;' /><col style='width: 65%;' /></colgroup>
        """
        if not rows:
            rows = [("（无数据）", "")]
        for i, (name, value) in enumerate(rows):
            bg = t.panel_bg if i % 2 == 0 else t.input_bg
            val = str(value) if value not in (None, "") else f"<span style='color:{t.text_secondary}'>无数据</span>"
            info += (
                f"<tr style='background-color:{bg}'>"
                f"<td style='padding:8px; border-bottom:1px solid {t.panel_border};'>{name}</td>"
                f"<td style='padding:8px; border-bottom:1px solid {t.panel_border};'>{val}</td></tr>"
            )
        info += "</table></div>"
        self.info_text.setHtml(info)
        self.info_text.setLineWrapMode(QTextEdit.LineWrapMode.NoWrap)
        self.info_text.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.info_text.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)

    def _copy(self) -> None:
        result = self.result
        if result.is_raster:
            plain = f"图层: {result.layer.name()}\n"
            for name, value in result.raster_values.items():
                plain += f"  {name}: {value}\n"
            QApplication.clipboard().setText(plain)
            return
        plain = f"图层: {self.layer.name()}\n要素ID: {self.feature.id()}\n"
        if self.feature.geometry().type() == QgsWkbTypes.PointGeometry:
            p = self.feature.geometry().asPoint()  # QGIS4: asPointXY 已移除
            plain += f"  X: {p.x():.6f}\n  Y: {p.y():.6f}\n"
        plain += "\n属性信息:\n"
        for f, v in zip(self.layer.fields(), self.feature.attributes()):
            plain += f"  {f.name()}: {v}\n"
        QApplication.clipboard().setText(plain)

    def closeEvent(self, event) -> None:
        if self.highlight is not None:
            self.highlight.hide()
        super().closeEvent(event)
