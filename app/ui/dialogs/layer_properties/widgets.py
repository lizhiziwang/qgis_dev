"""图层属性各页共用的自绘小控件。

- :class:`ColorButton`：「标注」页的文本 / 描边颜色与「渲染」页的填充 / 描边 /
  线 / 点颜色共用同一颗，避免两处各画一份。
- :func:`build_color_dialog` / :func:`pick_color`：全应用统一的「选择颜色」对话框
  （基本颜色 / 自定义颜色 / HSV-RGB-HTML / 不透明度）；符号编辑器里的 QGIS
  颜色按钮也复用这一份，保证各处弹出的取色界面一致。
"""

from __future__ import annotations

from qgis.PyQt.QtCore import QEvent, QObject, Qt, QTimer, pyqtSignal
from qgis.PyQt.QtGui import QColor
from qgis.PyQt.QtWidgets import (
    QColorDialog,
    QDoubleSpinBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSlider,
    QSpinBox,
    QWidget,
)


class _BasicColorAlphaGuard(QObject):
    """基本颜色点击后若透明度仍为 0，自动恢复为不透明；双击基本颜色直接确定。

    先点过「无颜色」后 alpha=0，Qt 的色板点击只改 RGB、不改 alpha，选完颜色
    仍然看不见；这里在颜色格子上拦截鼠标释放，事件处理完再把 alpha 补成 255。
    双击时（``accept_on_double_click``）同样先补 alpha，再接受对话框——用户不用
    再点一次「确定」。
    """

    def __init__(
        self,
        dialog: QColorDialog,
        alpha_spin=None,
        *,
        accept_on_double_click: bool = False,
    ) -> None:
        super().__init__(dialog)
        self._dialog = dialog
        self._alpha_spin = alpha_spin
        self._accept_on_double_click = accept_on_double_click

    def eventFilter(self, obj, event) -> bool:  # noqa: N802 —— Qt 命名
        if event.type() == QEvent.Type.MouseButtonRelease:
            QTimer.singleShot(0, self._restore_alpha)
        elif (
            event.type() == QEvent.Type.MouseButtonDblClick
            and self._accept_on_double_click
        ):
            # 双击序列的第二下在 release 里才真正落色；延后一拍再接受，
            # 保证 currentColor() 已是用户双击的那个颜色。
            QTimer.singleShot(0, self._accept)
        return False

    def _accept(self) -> None:
        try:
            self._restore_alpha()
            self._dialog.accept()
        except RuntimeError:  # 对话框已销毁
            pass

    def _restore_alpha(self) -> None:
        try:
            if self._dialog.currentColor().alpha() != 0:
                return
            if self._alpha_spin is not None:
                # 走 QColorDialog 自己的 alpha 数字框，避免直接 setCurrentColor
                self._alpha_spin.setValue(255)
            else:
                color = self._dialog.currentColor()
                color.setAlpha(255)
                self._dialog.setCurrentColor(color)
        except RuntimeError:  # 对话框已销毁
            pass


def build_color_dialog(color, parent=None) -> QColorDialog:
    """构造全应用统一的「选择颜色」对话框。

    - 回到早期的 Qt 取色窗（基本颜色 / 自定义颜色 / HSV/RGB/HTML），额外打开透明度；
      强制非原生对话框，否则 Windows 原生取色框会忽略 ShowAlphaChannel。
    - 「基本颜色」下方插入「无颜色」按钮（alpha=0）。
    - 透明度一行换成「不透明度」= 标签 + 滑条 + 百分比输入框（0-100%）。

    渲染 / 选择 / 标注页的 :class:`ColorButton` 与符号编辑器里的 QgsColorButton
    共用这一份，保证各处弹出的取色界面一致。
    """
    dlg = QColorDialog(color, parent)
    dlg.setWindowTitle("选择颜色")
    dlg.setOption(QColorDialog.ColorDialogOption.ShowAlphaChannel, True)
    dlg.setOption(QColorDialog.ColorDialogOption.DontUseNativeDialog, True)

    no_color = QPushButton("无颜色", dlg)
    no_color.setToolTip("设置为无颜色（不填充 / 不描边）")

    # 下面找到 Qt 自带的 alpha 数字框后赋给它；无颜色 / 基本颜色恢复都走它
    alpha_spin = None

    def _choose_none() -> None:
        if alpha_spin is not None:
            alpha_spin.setValue(0)
        else:
            dlg.setCurrentColor(QColor(0, 0, 0, 0))
        dlg.accept()

    no_color.clicked.connect(_choose_none)
    # QColorDialog 没有公开 API 能往基本颜色区加控件：它的左列是一个
    # QVBoxLayout（基本颜色网格 → 拾取屏幕颜色 → …），插在第 2 项即可。
    inserted = False
    try:
        left = dlg.layout().itemAt(0).layout().itemAt(0).layout()
        if left is not None:
            left.insertWidget(2, no_color)
            inserted = True
    except Exception:  # noqa: BLE001 —— 布局结构与 Qt 版本有关
        pass
    if not inserted:
        try:
            dlg.layout().insertWidget(1, no_color)
        except Exception:  # noqa: BLE001
            no_color.setParent(dlg)

    # 透明度：改成「不透明度」一行 = 标签 + 滑条 + 百分比输入框（0-100%）
    try:
        alpha_label = next(
            (
                label
                for label in dlg.findChildren(QLabel)
                if "alpha" in label.text().replace("&", "").lower()
                or "透明度" in label.text()
            ),
            None,
        )
        alpha_spin = alpha_label.buddy() if alpha_label is not None else None
        if isinstance(alpha_spin, QSpinBox) and alpha_spin.maximum() == 255:
            grid = alpha_spin.parentWidget().layout()
            index = grid.indexOf(alpha_spin)
            row = grid.getItemPosition(index)[0]
            initial_alpha = alpha_spin.value()
            # 隐藏 Qt 自带的 Alpha 标签 / 数字框，换成百分比一行
            for widget in (alpha_label, alpha_spin):
                if widget is not None:
                    grid.removeWidget(widget)
                    widget.hide()
            # 打开时以原颜色的 alpha 为准（部分 Qt 版本 show 前
            # currentColor() 的 alpha 还是 255）
            try:
                current = dlg.currentColor()
                if current.alpha() != initial_alpha:
                    current.setAlpha(initial_alpha)
                    dlg.setCurrentColor(current)
            except Exception:  # noqa: BLE001
                pass

            initial_percent = round(initial_alpha / 255 * 100, 1)
            row_widget = QWidget(dlg)
            row_layout = QHBoxLayout(row_widget)
            row_layout.setContentsMargins(0, 0, 0, 0)
            row_layout.setSpacing(6)
            row_label = QLabel("不透明度", row_widget)
            slider = QSlider(Qt.Orientation.Horizontal, row_widget)
            slider.setRange(0, 100)
            slider.setValue(int(round(initial_percent)))
            slider.setToolTip("不透明度 0-100%")
            percent = QDoubleSpinBox(row_widget)
            percent.setRange(0.0, 100.0)
            percent.setDecimals(1)
            percent.setSingleStep(1.0)
            percent.setSuffix(" %")
            percent.setValue(initial_percent)
            # 主题 QSS 给数字框留了右侧按钮 + 内边距，原来的 86px 会把「%」
            # 裁掉；按 sizeHint 给足宽度，"100.0 %" 也能完整显示
            percent.setMinimumWidth(120)
            row_layout.addWidget(row_label)
            row_layout.addWidget(slider, 1)
            row_layout.addWidget(percent)
            grid.addWidget(row_widget, row, 1, 1, 4)

            updating = {"on": False}

            def _apply_percent(value: float) -> None:
                if updating["on"]:
                    return
                updating["on"] = True
                try:
                    pct = max(0.0, min(100.0, float(value)))
                    slider_value = int(round(pct))
                    if slider.value() != slider_value:
                        slider.setValue(slider_value)
                    if abs(percent.value() - pct) > 0.05:
                        percent.setValue(pct)
                    alpha = max(0, min(255, int(round(pct / 100.0 * 255))))
                    # 走 QColorDialog 自己的 alpha 数字框（仍保留信号连接，
                    # 只是隐藏了）：直接 setCurrentColor 在某些 Qt 版本
                    # 会触发内部布局崩溃。
                    if alpha_spin.value() != alpha:
                        alpha_spin.setValue(alpha)
                finally:
                    updating["on"] = False

            def _sync_percent(color: QColor) -> None:
                if updating["on"]:
                    return
                updating["on"] = True
                try:
                    pct = round(color.alpha() / 255 * 100, 1)
                    slider_value = int(round(pct))
                    if slider.value() != slider_value:
                        slider.setValue(slider_value)
                    if abs(percent.value() - pct) > 0.05:
                        percent.setValue(pct)
                finally:
                    updating["on"] = False

            slider.valueChanged.connect(lambda value: _apply_percent(float(value)))
            percent.valueChanged.connect(_apply_percent)
            dlg.currentColorChanged.connect(_sync_percent)
            dlg._alpha_row = row_widget
            dlg._alpha_slider = slider
            dlg._alpha_percent = percent
    except Exception:  # noqa: BLE001 —— 找不到 alpha 控件就保留 Qt 原样
        pass

    # 先点过「无颜色」(alpha=0) 再点基本 / 自定义颜色：Qt 只改 RGB，这里补回不透明。
    # 基本颜色格子额外支持**双击直接确定**；自定义颜色仍走单击选中 + 点确定。
    try:
        left = dlg.layout().itemAt(0).layout().itemAt(0).layout()
        guards = []
        for index, auto_accept in ((1, True), (6, False)):
            try:
                widget = left.itemAt(index).widget()
            except Exception:  # noqa: BLE001
                widget = None
            if widget is None:
                continue
            guard = _BasicColorAlphaGuard(
                dlg, alpha_spin, accept_on_double_click=auto_accept
            )
            widget.installEventFilter(guard)
            if auto_accept:
                widget.setToolTip("双击基本颜色直接确定")
            guards.append(guard)
        if guards:
            dlg._alpha_guards = guards  # 保活（父对象是 dlg）
    except Exception:  # noqa: BLE001
        pass
    return dlg


def pick_color(parent, color) -> QColor | None:
    """打开统一取色框：确定返回颜色，取消返回 ``None``。"""
    dlg = build_color_dialog(color, parent)
    try:
        if not dlg.exec():
            return None
        chosen = dlg.currentColor()
        return QColor(chosen) if chosen.isValid() else None
    finally:
        dlg.deleteLater()


class ColorButton(QPushButton):
    """自绘颜色选择按钮（跟随本应用主题，不用 QgsColorButton 的原生 chrome）。

    颜色变化发 :attr:`color_changed`：``clicked`` 在用户选完色**之前**就发了，
    拿它当变化通知会读到旧颜色，所以需要刷预览的页面接这个信号。
    """

    #: 颜色被改变（程序设置也算）
    color_changed = pyqtSignal(QColor)

    def __init__(self, color=None, parent=None) -> None:
        super().__init__(parent)
        self._color = QColor(color) if color is not None else QColor(0, 0, 0)
        self.setObjectName("PropColorBtn")
        self.setFixedSize(34, 22)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip("点击选择颜色")
        self.clicked.connect(self._pick)
        self._sync()

    def color(self) -> QColor:
        return QColor(self._color)

    def set_color(self, color, emit: bool = True) -> None:
        if color is None:
            return
        c = QColor(color)
        if not c.isValid():
            return
        self._color = c
        self._sync()
        if emit:
            self.color_changed.emit(QColor(c))

    def open_picker(self) -> None:
        """打开取色框（给"点预览图也能取色"这类外部入口用，等价于点自己）。"""
        self._pick()

    def _sync(self) -> None:
        # 内联样式只作用于这一颗按钮：色块背景 + 中性描边。
        # 透明色（alpha=0）= 无颜色，用「无」字 + 浅底表示，和实际"不填充/不描边"一致。
        c = self._color
        if c.alpha() == 0:
            self.setText("无")
            self.setStyleSheet(
                "QPushButton#PropColorBtn {"
                " background-color: palette(base);"
                " color: palette(mid);"
                " border: 1px solid palette(mid); border-radius: 3px; }"
                "QPushButton#PropColorBtn:hover { border-color: palette(highlight); }"
            )
            self.setToolTip("无颜色（点击选择颜色）")
            return
        self.setText("")
        bg = f"rgba({c.red()}, {c.green()}, {c.blue()}, {c.alpha()})"
        self.setStyleSheet(
            "QPushButton#PropColorBtn {"
            f" background-color: {bg};"
            " border: 1px solid palette(mid); border-radius: 3px; }"
            "QPushButton#PropColorBtn:hover { border-color: palette(highlight); }"
        )
        alpha = round(c.alpha() / 255 * 100)
        self.setToolTip(f"点击选择颜色（透明度 {alpha}%）")

    def _pick(self) -> None:
        chosen = pick_color(self, self._color)
        if chosen is not None:
            self.set_color(chosen)
