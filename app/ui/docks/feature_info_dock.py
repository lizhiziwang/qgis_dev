"""点击识别后的要素详情 Dock。

替代原来的弹出 :class:`FeatureInfoDialog`：点击要素后不再弹模态框，
而是把详情显示到停靠在右侧的 Dock 里。内容就地刷新，不重复建 Dock。
"""

from __future__ import annotations

from html import escape as html_escape
from typing import Optional

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QFont, QIcon
from qgis.PyQt.QtWidgets import (
    QApplication,
    QDockWidget,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTextEdit,
    QToolTip,
    QVBoxLayout,
    QWidget,
)

from qgis.core import QgsProject, QgsWkbTypes
from qgis.gui import QgsHighlight

from app.core.domain.identify import IdentifyResult
from app.ui.common.theme import ThemeManager
from app.ui.tools.feature_attrs import field_alias


class _FieldInfoTextEdit(QTextEdit):
    """要素详情只读文本框：字段行支持**即时**悬浮提示。

    富文本自带的 ``QTextCharFormat.setToolTip`` 要等 Qt 默认的 ~700ms 唤醒延时
    才弹，而且只覆盖文字本身（别名很短时很难停准），所以不用它；这里开鼠标跟踪，
    划过哪一行就立刻 :meth:`QToolTip.showText` 弹出该行提示，命中区域覆盖整格。
    """

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setReadOnly(True)
        self.setFont(QFont("Segoe UI", 10))
        self.setMouseTracking(True)
        #: 行号 → 提示文本（空 = 该行无提示），由 :meth:`set_row_tips` 设置
        self._row_tips: list[str] = []
        #: 当前已弹出提示，避免同一行反复 showText 造成闪烁
        self._shown_tip: Optional[str] = None

    def set_row_tips(self, tips) -> None:
        """设置每行的悬浮提示（按表格行号；空文本表示该行不弹）。"""
        self._row_tips = [t or "" for t in tips] if tips else []
        self._shown_tip = None
        QToolTip.hideText()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802 —— Qt 命名
        tip = self._tip_at(event.position().toPoint())
        if tip != self._shown_tip:
            self._shown_tip = tip
            if tip:
                QToolTip.showText(event.globalPosition().toPoint(), tip, self)
            else:
                QToolTip.hideText()
        super().mouseMoveEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802 —— Qt 命名
        self._shown_tip = None
        QToolTip.hideText()
        super().leaveEvent(event)

    def _tip_at(self, pos) -> Optional[str]:
        """取 ``pos``（视口坐标）所在表格行的提示；不在表格内返回 ``None``。"""
        if not self._row_tips:
            return None
        cursor = self.cursorForPosition(pos)
        table = cursor.currentTable()
        if table is None:
            return None
        row = table.cellAt(cursor).row()
        if 0 <= row < len(self._row_tips):
            return self._row_tips[row] or None
        return None


class FeatureInfoDock(QDockWidget):
    """点击识别后展示要素详情的 Dock（默认停靠右侧）。"""

    def __init__(self, parent=None) -> None:
        super().__init__("要素详情", parent)
        self.setFeatures(
            QDockWidget.DockWidgetFeature.DockWidgetClosable
            | QDockWidget.DockWidgetFeature.DockWidgetMovable
            | QDockWidget.DockWidgetFeature.DockWidgetFloatable
        )
        self.setAllowedAreas(
            Qt.DockWidgetArea.LeftDockWidgetArea
            | Qt.DockWidgetArea.RightDockWidgetArea
            | Qt.DockWidgetArea.BottomDockWidgetArea
        )
        self.setMinimumSize(320, 200)
        self._highlight: Optional[QgsHighlight] = None
        self.layer = None
        self.feature = None
        self.result = None
        # 图层被删（任何入口）时清空本面板：否则残留的已删图层引用会在「复制信息」
        # 等处抛 RuntimeError: wrapped C/C++ object ... has been deleted。
        QgsProject.instance().layersWillBeRemoved.connect(self._on_layers_will_be_removed)
        self._init_ui()
        # 正文/标题都是**打开时把主题色写进 HTML** 的，切主题必须重画一遍 ——
        # 不接这个信号的话，已经开着的面板要等下次点要素才换色（用户报的
        # "切换深色主题时没有动态更新，关掉再打开才变"）。
        ThemeManager.instance().theme_changed.connect(self._on_theme_changed)

    def _init_ui(self) -> None:
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        self.title_label = QLabel()
        self.title_label.setWordWrap(True)
        layout.addWidget(self.title_label)

        self.info_text = _FieldInfoTextEdit()
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
        self.result = result
        if self._highlight is not None:
            self._highlight.hide()
        self._highlight = highlight
        self._render()
        self.show()
        self.raise_()

    def _on_theme_changed(self, *_args) -> None:
        """切主题：把当前正在展示的内容按新主题色重画一遍。"""
        self._render()

    def _render(self) -> None:
        """按当前数据 + **当前主题**重建标题与正文。

        ``show_feature`` 与主题切换都走这里，两条路径共用同一套渲染，不会出现
        "打开时一个色、切主题后另一个色"。
        """
        result = self.result
        if result is None:
            return
        t = ThemeManager.instance().current()
        show_attrs = bool(result.attributes)
        try:
            layer_name = self.layer.name()
        except RuntimeError:  # 图层已销毁（清空信号可能还没到）
            return
        if result.is_raster and not show_attrs:
            self.title_label.setText(
                f"<h3 style='margin:0'>{layer_name}</h3>"
                f"<p style='margin:0; color:{t.text_secondary}'>栅格像元识别</p>"
            )
            self._load_raster_info()
        else:
            # ArcGIS MapServer 栅格的富化结果带属性，和矢量要素同款展示
            self.title_label.setText(
                f"<h3 style='margin:0'>{layer_name}</h3>"
                f"<p style='margin:0; color:{t.text_secondary}'>要素 ID: {self._display_fid()}</p>"
            )
            self._load_info()

    def clear(self) -> None:
        """清空已显示的数据并断开对图层的引用（图层被移除时调用）。"""
        if self._highlight is not None:
            self._highlight.hide()
            self._highlight = None
        self.layer = None
        self.feature = None
        self.result = None
        self.title_label.clear()
        self.info_text.clear()
        self.info_text.set_row_tips([])

    def _on_layers_will_be_removed(self, layer_ids) -> None:
        """本面板正显示其要素的图层即将被删 → 抢在 C++ 对象销毁前清空数据。"""
        if self.layer is None or not layer_ids:
            return
        try:
            lid = self.layer.id()
        except RuntimeError:
            return  # 已是悬空对象，无需处理
        if lid in set(layer_ids):
            self.clear()

    def _display_fid(self) -> str:
        """标题里的要素号：富化结果取 OBJECTID，矢量结果取 feature.id()。"""
        if self.result.attributes:
            oid = next(
                (v for k, v in self.result.attributes.items() if str(k).lower() == "objectid"),
                None,
            )
            if oid is not None:
                return str(oid)
        if self.feature is not None:
            try:
                return str(self.feature.id())
            except Exception:  # noqa: BLE001
                pass
        return "-"

    def _load_raster_info(self) -> None:
        """栅格识别：显示点击坐标与该像元各波段的取值。"""
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
        self.info_text.set_row_tips([])  # 波段无字段别名，关闭悬浮提示

    def _load_info(self) -> None:
        t = ThemeManager.instance().current()
        result = self.result
        attrs = dict(result.attributes)
        info_map = self._field_info_map()
        if attrs:
            items = list(attrs.items())
            header = f"属性字段（共 {len(items)} 个）："
        else:
            fields = list(self.layer.fields())
            items = [(f.name(), self.feature.attribute(f.name())) for f in fields] if self.feature is not None else []
            header = f"属性字段（共 {len(fields)} 个）："
        info = "<div style='line-height:1.6; width:100%; box-sizing:border-box;'>"
        info += f"<p><strong>{header}</strong></p>"
        info += """
        <table style='width:100%; border-collapse:collapse; table-layout:auto;'>
            <colgroup><col style='width: 35%;' /><col style='width: 65%;' /></colgroup>
        """
        for i, (name, value) in enumerate(items):
            bg = t.panel_bg if i % 2 == 0 else t.input_bg
            val = str(value) if value is not None else f"<span style='color:{t.text_secondary}'>无</span>"
            display, _tip = self._display_and_tip(name, info_map)
            info += (
                f"<tr style='background-color:{bg}'>"
                f"<td style='padding:8px; border-bottom:1px solid {t.panel_border};'>{html_escape(display)}</td>"
                f"<td style='padding:8px; border-bottom:1px solid {t.panel_border};'>{val}</td></tr>"
            )
        info += "</table></div>"
        self.info_text.setHtml(info)
        self.info_text.setLineWrapMode(QTextEdit.LineWrapMode.NoWrap)
        self.info_text.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.info_text.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.info_text.set_row_tips(
            self._display_and_tip(name, info_map)[1] for name, _ in items
        )

    def _field_info_map(self) -> dict:
        """字段名 → ``(别名, 类型名, 字段长度)``；别名无则为空串、长度无则 0。
        再补一份小写键兜底大小写差异。"""
        mapping: dict = {}
        layer = getattr(self, "layer", None)
        if layer is None:
            return mapping
        try:
            fields = layer.fields()
        except Exception:  # noqa: BLE001
            return mapping
        for i in range(fields.count()):
            fld = fields.at(i)
            try:
                type_name = fld.typeName() or ""
            except Exception:  # noqa: BLE001
                type_name = ""
            try:
                length = int(fld.length())
            except Exception:  # noqa: BLE001
                length = 0
            info = (field_alias(fld) or "", type_name, length)
            mapping[fld.name()] = info
            mapping.setdefault(fld.name().lower(), info)
        return mapping

    def _display_and_tip(self, name, info_map: dict) -> tuple:
        """字段显示名与悬浮提示。

        显示名：有别名用别名，否则用真实字段名。悬浮提示：第一行为
        ``别名（真实字段名）``（无别名时即真实字段名），第二行字段类型
        （有长度带在类型后，如 ``String(50)``）。
        """
        real = str(name)
        alias, type_name, length = info_map.get(name) or info_map.get(real.lower()) or ("", "", 0)
        if alias and alias != real:
            display = alias
            tip = f"{alias}（{real}）"
        else:
            display = real
            tip = real
        if type_name:
            tip += f"\n{type_name}"
            if length > 0:
                tip += f"({length})"
        return display, tip

    def _copy(self) -> None:
        if self.layer is None:
            return
        result = self.result
        attrs = dict(result.attributes) if result is not None else {}
        if result is not None and result.is_raster and not attrs:
            plain = f"图层: {result.layer.name()}\n"
            for name, value in result.raster_values.items():
                plain += f"  {name}: {value}\n"
            QApplication.clipboard().setText(plain)
            return
        plain = f"图层: {self.layer.name()}\n要素ID: {self._display_fid()}\n"
        if self.feature is not None and not self.feature.geometry().isEmpty():
            if self.feature.geometry().type() == QgsWkbTypes.PointGeometry:
                p = self.feature.geometry().asPoint()  # QGIS4: asPointXY 已移除
                plain += f"  X: {p.x():.6f}\n  Y: {p.y():.6f}\n"
        plain += "\n属性信息:\n"
        if attrs:
            for name, value in attrs.items():
                plain += f"  {name}: {value}\n"
        elif self.feature is not None:
            for f, v in zip(self.layer.fields(), self.feature.attributes()):
                plain += f"  {f.name()}: {v}\n"
        QApplication.clipboard().setText(plain)

    def closeEvent(self, event) -> None:
        if self._highlight is not None:
            self._highlight.hide()
        super().closeEvent(event)
