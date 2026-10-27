"""自绘消息框（替代 ``QMessageBox``）。

沿用 :class:`~app.ui.common.base_dialog.BaseDialog` 的无边框 chrome 与主题
配色，提供和 ``QMessageBox`` 基本一致的静态入口
（:meth:`information` / :meth:`warning` / :meth:`critical` / :meth:`question`）
与常用实例 API（``setText / setIcon / setStandardButtons / addButton /
setDefaultButton / exec / clickedButton``），调用侧可以按原样迁移。

与 ``QMessageBox`` 的差异：

- 图标由本模块按主题配色自绘（信息 / 问号 / 感叹号 / 警告三角），不依赖
  平台原生样式；
- **主按钮**（默认按钮）套 ``#DialogPrimary`` 主色样式，破坏性按钮
  （``DestructiveRole``）文字用 ``text_error`` 标红；
- 标准按钮文案固定为中文，不依赖 Qt 翻译是否加载成功；
- 返回值仍是 ``QMessageBox.StandardButton``，原有
  ``ret == QMessageBox.StandardButton.Yes`` 这类判断无需改写。

线程 / 生命周期约定：静态方法内部创建、``exec()``、随即销毁，和
``QMessageBox`` 一样是阻塞式调用。
"""

from __future__ import annotations

from qgis.PyQt.QtCore import QPointF, QRectF, Qt
from qgis.PyQt.QtGui import (
    QColor,
    QFont,
    QGuiApplication,
    QPainter,
    QPen,
    QPixmap,
    QPolygonF,
)
from qgis.PyQt.QtWidgets import (
    QAbstractButton,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QToolButton,
    QVBoxLayout,
)

from app.ui.common.base_dialog import BaseDialog
from app.ui.common.theme import ThemeManager

#: 自绘图标边长（px）。
ICON_SIZE = 33
#: 正文 / 补充说明的最大文本宽度（px），超出自动换行。
MAX_TEXT_WIDTH = 375
#: 消息框宽度上下限（px）。
MIN_WIDTH = 315
MAX_WIDTH = 480
#: 标准按钮最小宽度（px），保证「确定 / 取消」等按钮不挤成一团。
BUTTON_MIN_WIDTH = 63
#: 内容区左右边距 / 图标与文字的间距（px），尺寸计算与布局共用。
CONTENT_H_MARGIN = 21
ICON_TEXT_SPACING = 12

#: 标准按钮的展示顺序（同时决定从左到右的排列）。
#: 保存 / 放弃 / 取消 的顺序与 Windows 一致。
_STANDARD_BUTTON_ORDER = (
    QMessageBox.StandardButton.Yes,
    QMessageBox.StandardButton.YesToAll,
    QMessageBox.StandardButton.No,
    QMessageBox.StandardButton.NoToAll,
    QMessageBox.StandardButton.Ok,
    QMessageBox.StandardButton.Save,
    QMessageBox.StandardButton.SaveAll,
    QMessageBox.StandardButton.Open,
    QMessageBox.StandardButton.Apply,
    QMessageBox.StandardButton.Reset,
    QMessageBox.StandardButton.RestoreDefaults,
    QMessageBox.StandardButton.Retry,
    QMessageBox.StandardButton.Ignore,
    QMessageBox.StandardButton.Abort,
    QMessageBox.StandardButton.Discard,
    QMessageBox.StandardButton.Cancel,
    QMessageBox.StandardButton.Close,
    QMessageBox.StandardButton.Help,
)

#: 标准按钮 → 中文文案。
_STANDARD_BUTTON_TEXT = {
    QMessageBox.StandardButton.Ok: "确定",
    QMessageBox.StandardButton.Save: "保存",
    QMessageBox.StandardButton.SaveAll: "全部保存",
    QMessageBox.StandardButton.Open: "打开",
    QMessageBox.StandardButton.Yes: "是",
    QMessageBox.StandardButton.YesToAll: "全是",
    QMessageBox.StandardButton.No: "否",
    QMessageBox.StandardButton.NoToAll: "全否",
    QMessageBox.StandardButton.Abort: "中止",
    QMessageBox.StandardButton.Retry: "重试",
    QMessageBox.StandardButton.Ignore: "忽略",
    QMessageBox.StandardButton.Close: "关闭",
    QMessageBox.StandardButton.Cancel: "取消",
    QMessageBox.StandardButton.Discard: "放弃",
    QMessageBox.StandardButton.Help: "帮助",
    QMessageBox.StandardButton.Apply: "应用",
    QMessageBox.StandardButton.Reset: "重置",
    QMessageBox.StandardButton.RestoreDefaults: "恢复默认",
}

#: 标准按钮 → 按钮角色（决定默认按钮的挑选与配色）。
_STANDARD_BUTTON_ROLE = {
    QMessageBox.StandardButton.Ok: QMessageBox.ButtonRole.AcceptRole,
    QMessageBox.StandardButton.Save: QMessageBox.ButtonRole.AcceptRole,
    QMessageBox.StandardButton.SaveAll: QMessageBox.ButtonRole.AcceptRole,
    QMessageBox.StandardButton.Open: QMessageBox.ButtonRole.AcceptRole,
    QMessageBox.StandardButton.Retry: QMessageBox.ButtonRole.AcceptRole,
    QMessageBox.StandardButton.Ignore: QMessageBox.ButtonRole.AcceptRole,
    QMessageBox.StandardButton.Yes: QMessageBox.ButtonRole.YesRole,
    QMessageBox.StandardButton.YesToAll: QMessageBox.ButtonRole.YesRole,
    QMessageBox.StandardButton.No: QMessageBox.ButtonRole.NoRole,
    QMessageBox.StandardButton.NoToAll: QMessageBox.ButtonRole.NoRole,
    QMessageBox.StandardButton.Abort: QMessageBox.ButtonRole.RejectRole,
    QMessageBox.StandardButton.Close: QMessageBox.ButtonRole.RejectRole,
    QMessageBox.StandardButton.Cancel: QMessageBox.ButtonRole.RejectRole,
    QMessageBox.StandardButton.Discard: QMessageBox.ButtonRole.DestructiveRole,
    QMessageBox.StandardButton.Help: QMessageBox.ButtonRole.HelpRole,
    QMessageBox.StandardButton.Apply: QMessageBox.ButtonRole.ApplyRole,
    QMessageBox.StandardButton.Reset: QMessageBox.ButtonRole.ResetRole,
    QMessageBox.StandardButton.RestoreDefaults: QMessageBox.ButtonRole.ResetRole,
}

#: 回车默认选中的角色（没有显式 ``setDefaultButton`` 时按顺序取第一个）。
_ACCEPT_ROLES = frozenset(
    {
        QMessageBox.ButtonRole.AcceptRole,
        QMessageBox.ButtonRole.YesRole,
        QMessageBox.ButtonRole.ApplyRole,
    }
)


def _icon_pixmap(icon, size: int = ICON_SIZE) -> QPixmap:
    """按当前主题把消息图标画成 ``QPixmap``（抗锯齿）。

    信息 / 问号用主色圆，严重错误用 ``text_error`` 红圆 + 白叉，警告用琥珀色
    圆角三角；颜色实时读 :class:`ThemeManager`，主题切换后重新 ``setIcon`` 即可刷新。
    """
    theme = ThemeManager.instance().current()
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)

    if icon == QMessageBox.Icon.Warning:
        _draw_warning_triangle(painter, QColor("#E6A23C"), size)
        glyph = "!"
    else:
        if icon == QMessageBox.Icon.Critical:
            color = QColor(theme.text_error)
            glyph = ""
        elif icon == QMessageBox.Icon.Question:
            color = QColor(theme.input_focus)
            glyph = "?"
        else:  # Information
            color = QColor(theme.input_focus)
            glyph = "i"
        painter.setBrush(color)
        painter.drawEllipse(
            QPointF(size / 2, size / 2), size / 2 - 1, size / 2 - 1
        )
        if not glyph:
            # 严重错误：手绘白叉，不依赖字体里「×」的字形大小
            pen = QPen(QColor("#ffffff"))
            pen.setWidthF(max(2.0, size * 0.09))
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            painter.setPen(pen)
            pad = size * 0.33
            painter.drawLine(QPointF(pad, pad), QPointF(size - pad, size - pad))
            painter.drawLine(
                QPointF(size - pad, pad), QPointF(pad, size - pad)
            )
            painter.end()
            return pixmap

    font = QFont("Microsoft YaHei")
    font.setBold(True)
    font.setPixelSize(int(size * 0.58))
    painter.setFont(font)
    painter.setPen(QColor("#ffffff"))
    rect = QRectF(0, 0, size, size)
    if icon == QMessageBox.Icon.Warning:
        # 三角形视觉重心偏下，字也往下挪一点
        rect = QRectF(0, size * 0.08, size, size * 0.92)
    painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, glyph)
    painter.end()
    return pixmap


def _draw_warning_triangle(painter: QPainter, color: QColor, size: int) -> None:
    """圆角警告三角：同色粗描边（圆角连接）叠在填充三角上。"""
    painter.setBrush(color)
    pen = QPen(color)
    pen.setWidthF(size * 0.14)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    inset = size * 0.10
    painter.drawPolygon(
        QPolygonF(
            [
                QPointF(size * 0.5, inset + size * 0.02),
                QPointF(size - inset, size - inset),
                QPointF(inset, size - inset),
            ]
        )
    )
    painter.setPen(Qt.PenStyle.NoPen)


class MessageBox(BaseDialog):
    """无边框 + 主题配色的消息框（用法与 ``QMessageBox`` 基本一致）。"""

    #: 复用 QMessageBox 的枚举，调用侧 ``MessageBox.StandardButton.Yes`` 照常可用。
    StandardButton = QMessageBox.StandardButton
    ButtonRole = QMessageBox.ButtonRole
    Icon = QMessageBox.Icon

    def __init__(
        self,
        parent=None,
        *,
        title: str = "",
        text: str = "",
        icon=QMessageBox.Icon.NoIcon,
        buttons=QMessageBox.StandardButton.NoButton,
        default_button=QMessageBox.StandardButton.NoButton,
    ) -> None:
        super().__init__(
            parent,
            title=title,
            show_min_button=False,
            show_max_button=False,
        )
        # BaseDialog 给普通对话框留的 800×600 下限对消息框太大
        self.setMinimumSize(MIN_WIDTH, 0)
        self.setMaximumWidth(MAX_WIDTH if MAX_WIDTH >= MIN_WIDTH else MIN_WIDTH)
        self.setObjectName("MessageBox")

        self._icon = icon
        self._buttons: list[QPushButton] = []
        self._button_roles: dict[QPushButton, object] = {}
        self._standard_of: dict[QPushButton, object] = {}
        self._button_layout: QHBoxLayout | None = None
        self._default_button: QPushButton | None = None
        self._escape_button: QPushButton | None = None
        self._clicked_button: QPushButton | None = None
        self._standard_result = self.StandardButton.NoButton

        self._build_ui()
        if icon != QMessageBox.Icon.NoIcon:
            self.setIcon(icon)
        if text:
            self.setText(text)
        if buttons:
            self.setStandardButtons(buttons)
        if default_button != QMessageBox.StandardButton.NoButton:
            self.setDefaultButton(default_button)

    # ---------------------------------------------------------------- 构建 UI

    def _build_ui(self) -> None:
        self._icon_label = QLabel()
        self._icon_label.setFixedSize(ICON_SIZE, ICON_SIZE)
        self._icon_label.setAlignment(
            Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignHCenter
        )
        self._icon_label.setObjectName("MessageBoxIcon")

        self._text_label = QLabel()
        self._text_label.setWordWrap(True)
        self._text_label.setTextFormat(Qt.TextFormat.AutoText)
        self._text_label.setMaximumWidth(MAX_TEXT_WIDTH)
        self._text_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self._text_label.setObjectName("MessageBoxText")

        self._informative_label = QLabel()
        self._informative_label.setWordWrap(True)
        self._informative_label.setTextFormat(Qt.TextFormat.AutoText)
        self._informative_label.setMaximumWidth(MAX_TEXT_WIDTH)
        self._informative_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self._informative_label.setObjectName("MessageBoxInfo")
        self._informative_label.hide()

        text_column = QVBoxLayout()
        text_column.setContentsMargins(0, 0, 0, 0)
        text_column.setSpacing(8)
        text_column.addWidget(self._text_label)
        text_column.addWidget(self._informative_label)

        text_row = QHBoxLayout()
        text_row.setContentsMargins(0, 0, 0, 0)
        text_row.setSpacing(ICON_TEXT_SPACING)
        text_row.addWidget(self._icon_label, 0, Qt.AlignmentFlag.AlignTop)
        text_row.addLayout(text_column, 1)

        self._details_button = QToolButton()
        self._details_button.setText("详细信息")
        self._details_button.setObjectName("MessageBoxDetails")
        self._details_button.setCheckable(True)
        self._details_button.setAutoRaise(True)
        self._details_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self._details_button.setArrowType(Qt.ArrowType.RightArrow)
        self._details_button.setToolButtonStyle(
            Qt.ToolButtonStyle.ToolButtonTextBesideIcon
        )
        self._details_button.hide()
        self._details_button.toggled.connect(self._toggle_details)

        self._details_view = QPlainTextEdit()
        self._details_view.setObjectName("MessageBoxDetailsView")
        self._details_view.setReadOnly(True)
        self._details_view.setMaximumHeight(90)
        self._details_view.hide()

        self._button_layout = QHBoxLayout()
        self._button_layout.setContentsMargins(0, 0, 0, 0)
        self._button_layout.setSpacing(8)
        self._button_layout.addStretch(1)

        layout = QVBoxLayout()
        layout.setContentsMargins(CONTENT_H_MARGIN, 17, CONTENT_H_MARGIN, 15)
        layout.setSpacing(12)
        layout.addLayout(text_row)
        layout.addWidget(self._details_button, 0, Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(self._details_view)
        layout.addLayout(self._button_layout)
        self.setLayout(layout)

    # ------------------------------------------------------------ 文本 / 图标

    def setText(self, text: str) -> None:  # noqa: N802 —— 对齐 QMessageBox API
        self._text_label.setText(text)

    def text(self) -> str:
        return self._text_label.text()

    def setInformativeText(self, text: str) -> None:  # noqa: N802 —— 对齐 API
        self._informative_label.setText(text)
        self._informative_label.setVisible(bool(text))

    def informativeText(self) -> str:  # noqa: N802 —— 对齐 API
        return self._informative_label.text()

    def setDetailedText(self, text: str) -> None:  # noqa: N802 —— 对齐 API
        self._details_view.setPlainText(text)
        self._details_button.setVisible(bool(text))
        if not text:
            self._details_view.hide()
            self._details_button.setChecked(False)

    def detailedText(self) -> str:  # noqa: N802 —— 对齐 API
        return self._details_view.toPlainText()

    def setTextFormat(self, text_format) -> None:  # noqa: N802 —— 对齐 API
        self._text_label.setTextFormat(text_format)
        self._informative_label.setTextFormat(text_format)

    def setIcon(self, icon) -> None:  # noqa: N802 —— 对齐 API
        self._icon = icon
        if icon == QMessageBox.Icon.NoIcon:
            self._icon_label.hide()
            return
        self._icon_label.setPixmap(_icon_pixmap(icon))
        self._icon_label.show()

    def icon(self):
        return self._icon

    def _toggle_details(self, checked: bool) -> None:
        self._details_view.setVisible(checked)
        self._details_button.setArrowType(
            Qt.ArrowType.DownArrow if checked else Qt.ArrowType.RightArrow
        )
        self._fit_size()

    # ---------------------------------------------------------------- 按钮

    def setStandardButtons(self, buttons) -> None:  # noqa: N802 —— 对齐 API
        """按 :data:`_STANDARD_BUTTON_ORDER` 重建标准按钮（自定义按钮保留）。"""
        for button in list(self._standard_of):
            self._remove_button(button)
        if not buttons:
            self._apply_button_styles()
            return
        for standard in _STANDARD_BUTTON_ORDER:
            if buttons & standard:
                self._add_standard_button(standard)
        self._apply_button_styles()

    def addButton(self, button, role=QMessageBox.ButtonRole.InvalidRole):  # noqa: N802
        """添加标准按钮 / 文本按钮 / 现成的 ``QAbstractButton``，返回按钮控件。"""
        if isinstance(button, QAbstractButton):
            widget = button
            if widget.parent() is None:
                widget.setParent(self)
            if role != QMessageBox.ButtonRole.InvalidRole:
                self._button_roles[widget] = role
            self._button_layout.addWidget(widget)
            self._buttons.append(widget)
            widget.clicked.connect(lambda _checked=False, b=widget: self._activate(b))
        elif isinstance(button, str):
            widget = self._create_button(button, role)
            self._standard_of[widget] = self.StandardButton.NoButton
        else:
            standard = self.StandardButton(int(button))
            widget = self._add_standard_button(standard)
        self._apply_button_styles()
        return widget

    def _add_standard_button(self, standard):
        role = _STANDARD_BUTTON_ROLE.get(standard, QMessageBox.ButtonRole.InvalidRole)
        widget = self._create_button(_STANDARD_BUTTON_TEXT.get(standard, ""), role)
        self._standard_of[widget] = standard
        return widget

    def _create_button(self, text: str, role) -> QPushButton:
        widget = QPushButton(text)
        widget.setMinimumWidth(BUTTON_MIN_WIDTH)
        widget.setAutoDefault(False)
        widget.setDefault(False)
        widget.clicked.connect(lambda _checked=False, b=widget: self._activate(b))
        if self._button_layout is not None:
            # stretch 在最前，按钮追加到其后 → 整排右对齐
            self._button_layout.addWidget(widget)
        self._buttons.append(widget)
        self._button_roles[widget] = role
        return widget

    def _remove_button(self, widget: QPushButton) -> None:
        if self._button_layout is not None:
            self._button_layout.removeWidget(widget)
        if widget in self._buttons:
            self._buttons.remove(widget)
        self._button_roles.pop(widget, None)
        self._standard_of.pop(widget, None)
        if widget is self._default_button:
            self._default_button = None
        if widget is self._escape_button:
            self._escape_button = None
        widget.hide()
        widget.deleteLater()

    def button(self, which):  # noqa: N802 —— 对齐 API
        for widget in self._buttons:
            if self._standard_of.get(widget) == which:
                return widget
        return None

    def setDefaultButton(self, button) -> None:  # noqa: N802 —— 对齐 API
        if isinstance(button, QAbstractButton):
            self._default_button = button
        else:
            self._default_button = self.button(button)
        self._apply_button_styles()

    def setEscapeButton(self, button) -> None:  # noqa: N802 —— 对齐 API
        if isinstance(button, QAbstractButton):
            self._escape_button = button
        else:
            self._escape_button = self.button(button)

    def clickedButton(self):  # noqa: N802 —— 对齐 API
        return self._clicked_button

    def standardButton(self, button):  # noqa: N802 —— 对齐 API
        return self._standard_of.get(button, self.StandardButton.NoButton)

    def _auto_default(self):
        for widget in self._buttons:
            if self._button_roles.get(widget) in _ACCEPT_ROLES:
                return widget
        return self._buttons[0] if self._buttons else None

    def _apply_button_styles(self) -> None:
        """默认按钮套主色样式，破坏性按钮标红；其余保持普通按钮。"""
        if self._default_button in self._buttons:
            default = self._default_button
        else:
            default = self._auto_default()
        for widget in self._buttons:
            is_default = widget is default
            widget.setAutoDefault(False)
            widget.setDefault(is_default)
            if is_default:
                name = "DialogPrimary"
            elif self._button_roles.get(widget) == QMessageBox.ButtonRole.DestructiveRole:
                name = "DialogDanger"
            else:
                name = ""
            if widget.objectName() != name:
                widget.setObjectName(name)
                widget.style().unpolish(widget)
                widget.style().polish(widget)

    # -------------------------------------------------------------- 执行 / 关闭

    def _activate(self, widget: QAbstractButton) -> None:
        self._clicked_button = widget  # type: ignore[assignment]
        standard = self._standard_of.get(widget)  # type: ignore[arg-type]
        if standard is None:
            standard = self.StandardButton.NoButton
        self._standard_result = standard
        self.done(int(standard))

    def reject(self) -> None:  # noqa: N802 —— 对齐 QDialog API
        """Esc / 标题栏关闭：有 escape 按钮就走它，否则按「无结果」关闭。"""
        escape = self._escape_button
        if escape is not None and escape.isVisible():
            self._activate(escape)
            return
        self._standard_result = self.StandardButton.NoButton
        super().reject()

    def exec(self):  # noqa: N802 —— 对齐 QDialog / QMessageBox API
        self._clicked_button = None
        self._standard_result = self.StandardButton.NoButton
        self._fit_size()
        self._position_on_parent()
        super().exec()
        return self._standard_result

    # ---------------------------------------------------------------- 尺寸 / 定位

    def _fit_size(self) -> None:
        """按内容自适应宽度；长文本先放宽再算换行后的高度。"""
        layout = self._content.layout()
        if layout is None:
            return
        layout.activate()
        text_width = 0
        for label in (self._text_label, self._informative_label):
            content = label.text()
            if not content:
                continue
            if "<" in content and ">" in content:
                # 富文本：按标签 sizeHint（已按当前宽度换行）算不准自然宽度，
                # 退回最小宽度即可
                continue
            text_width = max(
                text_width, label.fontMetrics().horizontalAdvance(content)
            )
        if text_width:
            # 文本自然宽度 + 图标列 + 左右边距（上限由 MAX_WIDTH 兜住）
            text_width = min(text_width, MAX_TEXT_WIDTH)
            preferred = text_width + ICON_SIZE + ICON_TEXT_SPACING + 2 * CONTENT_H_MARGIN
        else:
            preferred = 0
        width = min(max(self.sizeHint().width(), MIN_WIDTH, preferred), MAX_WIDTH)
        self._fit_details_height(width)
        if layout.hasHeightForWidth():
            content_height = layout.heightForWidth(width)
        else:
            content_height = layout.sizeHint().height()
        title_height = max(
            self.title_bar.height(), self.title_bar.sizeHint().height()
        )
        self.resize(width, content_height + title_height)

    def _fit_details_height(self, width: int) -> None:
        """按换行后的行数给详细信息区定高，避免短内容也留一大块空白。"""
        view = self._details_view
        if view.isHidden():
            return
        metrics = view.fontMetrics()
        frame = 2 * view.frameWidth()
        usable = max(60, width - 2 * CONTENT_H_MARGIN - frame - 15)
        lines = 0
        for paragraph in view.toPlainText().split("\n"):
            advance = metrics.horizontalAdvance(paragraph)
            lines += max(1, -(-advance // usable))
        height = lines * metrics.lineSpacing() + frame + 10
        # 多留一行：中英混排的实际折行常比 horizontalAdvance 估算更碎，
        # 留一行余量避免短内容也冒出竖向滚动条
        height += metrics.lineSpacing()
        view.setFixedHeight(min(90, max(39, height)))

    def _position_on_parent(self) -> None:
        """优先居中于父窗口，没有父窗口时居中于当前屏幕（并夹在工作区内）。"""
        parent = self.parentWidget()
        center = None
        if parent is not None:
            window = parent.window()
            if window is not None and window.isVisible():
                center = window.frameGeometry().center()
        if center is None:
            screen = QGuiApplication.primaryScreen()
            if screen is None:
                return
            center = screen.availableGeometry().center()

        geometry = self.frameGeometry()
        geometry.moveCenter(center)
        screen = QGuiApplication.screenAt(center) or QGuiApplication.primaryScreen()
        if screen is not None:
            available = screen.availableGeometry()
            geometry.moveLeft(
                min(max(geometry.left(), available.left()), available.right() - geometry.width())
            )
            geometry.moveTop(
                min(max(geometry.top(), available.top()), available.bottom() - geometry.height())
            )
        self.move(geometry.topLeft())

    # ---------------------------------------------------------------- 静态入口

    @classmethod
    def _show(
        cls,
        icon,
        parent,
        title: str,
        text: str,
        buttons,
        default_button,
        *,
        informative_text: str = "",
        detailed_text: str = "",
    ):
        box = cls(parent, icon=icon, buttons=buttons, default_button=default_button)
        box.setWindowTitle(title)
        box.setText(text)
        if informative_text:
            box.setInformativeText(informative_text)
        if detailed_text:
            box.setDetailedText(detailed_text)
        return box.exec()

    @classmethod
    def information(
        cls,
        parent,
        title: str,
        text: str,
        buttons=QMessageBox.StandardButton.Ok,
        default_button=QMessageBox.StandardButton.NoButton,
        *,
        informative_text: str = "",
        detailed_text: str = "",
    ):
        return cls._show(
            QMessageBox.Icon.Information,
            parent,
            title,
            text,
            buttons,
            default_button,
            informative_text=informative_text,
            detailed_text=detailed_text,
        )

    @classmethod
    def warning(
        cls,
        parent,
        title: str,
        text: str,
        buttons=QMessageBox.StandardButton.Ok,
        default_button=QMessageBox.StandardButton.NoButton,
        *,
        informative_text: str = "",
        detailed_text: str = "",
    ):
        return cls._show(
            QMessageBox.Icon.Warning,
            parent,
            title,
            text,
            buttons,
            default_button,
            informative_text=informative_text,
            detailed_text=detailed_text,
        )

    @classmethod
    def critical(
        cls,
        parent,
        title: str,
        text: str,
        buttons=QMessageBox.StandardButton.Ok,
        default_button=QMessageBox.StandardButton.NoButton,
        *,
        informative_text: str = "",
        detailed_text: str = "",
    ):
        return cls._show(
            QMessageBox.Icon.Critical,
            parent,
            title,
            text,
            buttons,
            default_button,
            informative_text=informative_text,
            detailed_text=detailed_text,
        )

    @classmethod
    def question(
        cls,
        parent,
        title: str,
        text: str,
        buttons=(
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        ),
        default_button=QMessageBox.StandardButton.NoButton,
        *,
        informative_text: str = "",
        detailed_text: str = "",
    ):
        return cls._show(
            QMessageBox.Icon.Question,
            parent,
            title,
            text,
            buttons,
            default_button,
            informative_text=informative_text,
            detailed_text=detailed_text,
        )

    @classmethod
    def about(cls, parent, title: str, text: str) -> None:
        cls.information(parent, title, text)


__all__ = ["ICON_SIZE", "MAX_TEXT_WIDTH", "MIN_WIDTH", "MAX_WIDTH", "MessageBox"]
