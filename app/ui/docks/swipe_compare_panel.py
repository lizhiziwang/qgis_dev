"""卷帘对比控制面板（悬浮在地图右上角）。

启动前、启动后都由 MainWindow 贴到地图区域右上角，不占布局、不压缩地图：

- 未开始时先选参数，点「开始卷帘」由宿主创建 SwipeTool；
- 已开始后改选参数即时生效，不必关闭重开；
- 左（上）影像 / 右（下）影像：图层列表按图层树顺序列出工程里的栅格；
- 卷帘方向：垂直（左右）/ 水平（上下）；
- 标题栏可折叠 / 展开设置区；
- 点关闭（×）：已开始则退出卷帘，未开始则只收起面板。

UI 只发信号，真正创建 / 更新 SwipeTool 由 MainWindow 完成。
"""

from __future__ import annotations

from typing import Optional

from qgis.PyQt import sip
from qgis.PyQt.QtCore import QEvent, QPoint, Qt, QTimer, pyqtSignal
from qgis.PyQt.QtWidgets import (
    QButtonGroup,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QRadioButton,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)
from qgis.core import QgsProject

from app.ui.common.theme import ThemeManager
from app.ui.dialogs.swipe_compare import raster_layers_in_tree_order


class SwipeComparePanel(QFrame):
    """卷帘对比设置面板（地图右上角悬浮）。"""

    #: 会话已启动时用户改选了影像或方向
    params_changed = pyqtSignal(object, object, str)
    #: 尚未开始时点「开始卷帘」：(左/上影像, 右/下影像, "vertical"/"horizontal")
    start_requested = pyqtSignal(object, object, str)
    #: 面板被关闭（×）：宿主应退出卷帘会话
    closed = pyqtSignal()

    def __init__(self, canvas, parent=None) -> None:
        host = parent or canvas.parentWidget() or canvas
        super().__init__(host)
        self._canvas = canvas
        self.setObjectName("SwipeComparePanel")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        # 固定宽度、高度随内容；折叠后只留标题行
        self.setFixedWidth(300)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)

        self.project = QgsProject.instance()
        self._syncing = False
        #: 会话是否已启动。未启动时面板只做参数预选，显示「开始卷帘」按钮。
        self._started = False
        self._adjust_pending = False
        self._build_ui()
        self._apply_style()
        self.adjustSize()
        self._place()
        self.show()
        self.raise_()

        ThemeManager.instance().theme_changed.connect(self._on_theme_changed)
        canvas.mapCanvasRefreshed.connect(self._place)
        canvas.extentsChanged.connect(self._place)
        canvas.installEventFilter(self)
        self.project.layersAdded.connect(self._populate_layers)
        self.project.layersRemoved.connect(self._populate_layers)
        try:
            # 图层树里上下移动图层时刷新下拉顺序（面板常驻，不会重新 show）
            self.project.layerTreeRoot().layerOrderChanged.connect(self._populate_layers)
        except (AttributeError, RuntimeError):
            pass

    # ------------------------------------------------------------------ 位置

    def _place(self) -> None:
        """贴到地图区域右上角（随画布位置 / 尺寸变化重算）。"""
        host = self.parentWidget()
        if host is None:
            return
        try:
            origin = self._canvas.mapToGlobal(
                QPoint(0, 0)
            ) - host.mapToGlobal(QPoint(0, 0))
            width = self.width()
        except RuntimeError:
            return
        margin = 8
        x = max(origin.x(), origin.x() + self._canvas.width() - width - margin)
        y = max(origin.y(), origin.y() + margin)
        if self.pos() != QPoint(x, y):
            self.move(x, y)
        self.raise_()

    def eventFilter(self, watched, event) -> bool:  # noqa: N802 —— Qt 命名
        if watched is self._canvas and event.type() == QEvent.Type.Resize:
            self._place()
        return super().eventFilter(watched, event)

    # ------------------------------------------------------------------ UI

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(6)

        header = QHBoxLayout()
        header.setSpacing(4)
        title = QLabel("卷帘对比")
        title.setObjectName("SwipePanelTitle")
        header.addWidget(title)
        header.addStretch(1)

        self.toggle_btn = QToolButton()
        self.toggle_btn.setObjectName("SwipePanelToggle")
        self.toggle_btn.setCheckable(True)
        self.toggle_btn.setChecked(True)
        self.toggle_btn.setToolTip("折叠 / 展开设置")
        self.toggle_btn.setArrowType(Qt.ArrowType.DownArrow)
        self.toggle_btn.clicked.connect(self._on_toggle)
        header.addWidget(self.toggle_btn)

        close_btn = QToolButton()
        close_btn.setObjectName("SwipePanelClose")
        close_btn.setText("×")
        close_btn.setToolTip("关闭并退出卷帘对比")
        close_btn.setAutoRaise(True)
        close_btn.clicked.connect(self.close)
        header.addWidget(close_btn)
        layout.addLayout(header)

        self.body = QWidget(self)
        body = QVBoxLayout(self.body)
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(6)

        self.base_label = QLabel("左影像")
        body.addWidget(self.base_label)
        self.base_combo = QComboBox()
        body.addWidget(self.base_combo)

        self.compare_label = QLabel("右影像")
        body.addWidget(self.compare_label)
        self.compare_combo = QComboBox()
        body.addWidget(self.compare_combo)

        body.addWidget(QLabel("卷帘方向"))
        dir_row = QHBoxLayout()
        self.vertical_radio = QRadioButton("垂直（左右）")
        self.horizontal_radio = QRadioButton("水平（上下）")
        self.vertical_radio.setChecked(True)
        group = QButtonGroup(self)
        group.addButton(self.vertical_radio)
        group.addButton(self.horizontal_radio)
        dir_row.addWidget(self.vertical_radio)
        dir_row.addWidget(self.horizontal_radio)
        dir_row.addStretch(1)
        body.addLayout(dir_row)

        self.start_btn = QPushButton("开始卷帘")
        self.start_btn.clicked.connect(self._on_start_clicked)
        body.addWidget(self.start_btn)

        self.hint = QLabel("")
        self.hint.setWordWrap(True)
        self.hint.setStyleSheet("color: #c0392b;")
        self.hint.hide()  # 无错误时不占高度，面板底部保持紧凑
        body.addWidget(self.hint)
        layout.addWidget(self.body)

        self.base_combo.currentIndexChanged.connect(self._emit_params)
        self.compare_combo.currentIndexChanged.connect(self._emit_params)
        self.vertical_radio.toggled.connect(self._on_orientation_toggled)
        self._populate_layers()

    def _apply_style(self) -> None:
        t = ThemeManager.instance().current()
        self.setStyleSheet(
            f"""
            QFrame#SwipeComparePanel {{
                background-color: {t.panel_bg};
                border: 1px solid {t.input_border};
                border-radius: 6px;
            }}
            QFrame#SwipeComparePanel QLabel#SwipePanelTitle {{
                color: {t.menu_text};
                font-weight: 600;
            }}
            QFrame#SwipeComparePanel QToolButton#SwipePanelClose {{
                color: {t.menu_text};
                border: none;
                background: transparent;
                font-size: 15px;
            }}
            QFrame#SwipeComparePanel QToolButton#SwipePanelClose:hover {{
                color: #ffffff;
                background-color: {t.close_hover};
                border-radius: 3px;
            }}
            """
        )

    def _on_theme_changed(self, _theme=None) -> None:
        self._apply_style()

    def _on_toggle(self, checked: bool) -> None:
        """折叠 / 展开设置区，面板高度随之收缩 / 展开。"""
        self.body.setVisible(checked)
        self.toggle_btn.setArrowType(
            Qt.ArrowType.DownArrow if checked else Qt.ArrowType.RightArrow
        )
        self._adjust_size()

    # -------------------------------------------------------------- 图层列表

    def _populate_layers(self, *_args) -> None:
        """按图层树顺序刷新栅格列表，尽量保留当前选中的两个图层。"""
        prev_syncing = self._syncing
        self._syncing = True
        try:
            base = self.base_combo.currentData()
            compare = self.compare_combo.currentData()
            try:
                base_id = base.id() if base is not None else None
            except RuntimeError:
                base_id = None
            try:
                compare_id = compare.id() if compare is not None else None
            except RuntimeError:
                compare_id = None

            self.base_combo.clear()
            self.compare_combo.clear()
            for layer in raster_layers_in_tree_order(self.project):
                self.base_combo.addItem(layer.name(), layer)
                self.compare_combo.addItem(layer.name(), layer)

            self._restore_combo(self.base_combo, base_id, default_index=0)
            self._restore_combo(self.compare_combo, compare_id, default_index=1)
        finally:
            self._syncing = prev_syncing
        self._update_labels()

    @staticmethod
    def _restore_combo(combo: QComboBox, layer_id: Optional[str], *, default_index: int) -> None:
        if layer_id is not None:
            for index in range(combo.count()):
                layer = combo.itemData(index)
                try:
                    if layer is not None and layer.id() == layer_id:
                        combo.setCurrentIndex(index)
                        return
                except RuntimeError:
                    continue
        if combo.count() > 0:
            combo.setCurrentIndex(min(default_index, combo.count() - 1))

    @staticmethod
    def _select_layer(combo: QComboBox, layer) -> None:
        if layer is None:
            return
        try:
            target_id = layer.id()
        except RuntimeError:
            return
        for index in range(combo.count()):
            item = combo.itemData(index)
            try:
                if item is not None and item.id() == target_id:
                    combo.setCurrentIndex(index)
                    return
            except RuntimeError:
                continue

    def set_params(self, base, compare, orientation: str) -> None:
        """程序化回填当前会话参数（不触发 ``params_changed``）。"""
        self._syncing = True
        try:
            self._populate_layers()
            self._select_layer(self.base_combo, base)
            self._select_layer(self.compare_combo, compare)
            if orientation == "horizontal":
                self.horizontal_radio.setChecked(True)
            else:
                self.vertical_radio.setChecked(True)
        finally:
            self._syncing = False
        self._set_hint("")
        self._update_labels()

    def set_started(self, started: bool) -> None:
        """标记会话已启动：隐藏「开始卷帘」按钮，后续改动即时生效。"""
        self._started = bool(started)
        self.start_btn.setVisible(not self._started)
        self._adjust_size()

    def _adjust_size(self) -> None:
        """隐藏 / 显示控件后重新贴到内容高度（否则固定尺寸策略不会自动收缩）。

        推迟一拍：Qt 的布局尺寸在 hide/show 当拍还没更新，立即 adjustSize
        会沿用旧高度（底部留白）；下一轮事件循环再取 sizeHint 才准确。
        """
        if self._adjust_pending:
            return
        self._adjust_pending = True
        QTimer.singleShot(0, self._do_adjust_size)

    def _do_adjust_size(self) -> None:
        self._adjust_pending = False
        if sip.isdeleted(self):
            return
        try:
            layout = self.layout()
            if layout is not None:
                layout.invalidate()
                layout.activate()
            self.updateGeometry()
            self.adjustSize()
            self._place()
        except RuntimeError:
            pass

    def is_started(self) -> bool:
        return self._started

    # -------------------------------------------------------------- 交互

    def _set_hint(self, text: str) -> None:
        """只有出错时才让提示行占高度，正常状态面板底部保持紧凑。"""
        changed = bool(text) != self.hint.isVisible() or self.hint.text() != text
        self.hint.setText(text)
        self.hint.setVisible(bool(text))
        if changed:
            self._adjust_size()

    def _on_start_clicked(self) -> None:
        base = self.base_combo.currentData()
        compare = self.compare_combo.currentData()
        if base is None or compare is None:
            self._set_hint("请先加载至少两个栅格图层")
            return
        if base is compare:
            self._set_hint("左影像与右影像不能相同")
            return
        self._set_hint("")
        self.start_requested.emit(base, compare, self.orientation())

    def _on_orientation_toggled(self, _checked: bool) -> None:
        self._update_labels()
        self._emit_params()

    def _update_labels(self) -> None:
        if self.vertical_radio.isChecked():
            self.base_label.setText("左影像")
            self.compare_label.setText("右影像")
        else:
            self.base_label.setText("上影像")
            self.compare_label.setText("下影像")

    def orientation(self) -> str:
        return "horizontal" if self.horizontal_radio.isChecked() else "vertical"

    def _emit_params(self, *_args) -> None:
        if self._syncing:
            return
        base = self.base_combo.currentData()
        compare = self.compare_combo.currentData()
        if base is None or compare is None:
            self._set_hint("请先加载至少两个栅格图层")
            return
        if base is compare:
            self._set_hint("左影像与右影像不能相同")
            return
        self._set_hint("")
        if not self._started:
            return
        self.params_changed.emit(base, compare, self.orientation())

    # -------------------------------------------------------------- 关闭

    def closeEvent(self, event) -> None:  # noqa: N802 —— Qt 命名
        try:
            self._canvas.removeEventFilter(self)
        except RuntimeError:
            pass
        self.closed.emit()
        super().closeEvent(event)


__all__ = ["SwipeComparePanel"]
