"""图层属性「选择」页：按几何类型配置**选中要素的高亮样式**。

打开属性页时按当前图层的几何类型（点 / 线 / 面）取系统里对应的高亮样式，大图预览出来；
点「符号设置」（或直接点预览图）弹小窗改**边界线（是否显示 / 颜色 / 线宽）**与
**填充（是否显示 / 颜色）**。线几何没有填充语义，小窗里对线不显示填充项。

因为配置是**系统级、按几何类型分开**的，页面只呈现当前图层那一类，并明确告知
"作用于系统里所有该类图层"——要改点 / 线 / 面里的另一类，开一个那种几何类型的图层属性。

改样式在「符号设置」小窗里点**确定**即写入 ``SelectionStyleManager`` 并落盘，
画布随之立刻刷新（覆盖层订阅了 ``changed``）；小窗里点**取消**则完全丢弃。
"""

from __future__ import annotations

from qgis.core import QgsFillSymbol, QgsLineSymbol, QgsMarkerSymbol, QgsSymbolLayerUtils
from qgis.PyQt.QtCore import QSize, Qt, pyqtSignal
from qgis.PyQt.QtGui import QColor
from qgis.PyQt.QtWidgets import (
    QCheckBox,
    QDialog,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from app.infra.logging import get_logger
from app.ui.common.base_dialog import BaseDialog
from app.ui.common.selection_style import (
    MAX_STROKE_WIDTH,
    MIN_STROKE_WIDTH,
    HighlightStyle,
    SelectionStyleManager,
)
from app.ui.dialogs.layer_properties.base import LayerPropertyTab
from app.ui.dialogs.layer_properties.render import _geom_kind
from app.ui.dialogs.layer_properties.widgets import ColorButton

logger = get_logger(__name__)

#: 预览图尺寸
_PREVIEW_SIZE = QSize(240, 170)

#: kind → 中文名
_KIND_NAMES = {"point": "点", "line": "线", "polygon": "面"}

#: kind → 描边那组控件的中文叫法（点说"轮廓"更顺口）
_STROKE_TITLES = {"point": "轮廓", "line": "线条", "polygon": "边界线"}

#: 预览框底色（产品指定，不用 palette——白色底看着像"空白"）
_PREVIEW_BG = "#e2eaf1"

#: 预览里点标记的画布尺寸
_PREVIEW_MARKER_SIZE = "6.0"


def _opacity_spin(parent, value: int) -> QSpinBox:
    """透明度输入框（0-100 %）。"""
    spin = QSpinBox(parent)
    spin.setRange(0, 100)
    spin.setSingleStep(5)
    spin.setSuffix(" %")
    spin.setValue(int(value))
    return spin


class _ClickableLabel(QLabel):
    """可点的 QLabel（点预览图 = 打开「符号设置」，与按钮同一入口）。"""

    clicked = pyqtSignal()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 —— Qt 命名
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mouseReleaseEvent(event)


class _SymbolSettingsDialog(BaseDialog):
    """「符号设置」小窗：编辑一类的边界线与填充。"""

    def __init__(self, kind: str, style: HighlightStyle, parent=None) -> None:
        super().__init__(parent, f"符号设置 - {_KIND_NAMES.get(kind, kind)}图层选中高亮",
                         show_min_button=False, show_max_button=False)
        self._kind = kind
        # 基类默认 800x600 对这个表单太大
        self.setMinimumSize(0, 0)
        self._build(style)

    def _build(self, style: HighlightStyle) -> None:
        layout = QVBoxLayout()
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(12)

        stroke_title = _STROKE_TITLES.get(self._kind, "边界线")
        stroke_box = QGroupBox(stroke_title, self)
        stroke_form = QFormLayout(stroke_box)
        stroke_form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        stroke_form.setHorizontalSpacing(8)
        self.stroke_check = QCheckBox(f"显示{stroke_title}", stroke_box)
        self.stroke_check.setChecked(style.stroke_enabled)
        stroke_form.addRow("", self.stroke_check)
        self.stroke_color_btn = ColorButton(style.stroke_qcolor(), stroke_box)
        stroke_form.addRow("颜色", self.stroke_color_btn)
        self.width_spin = QDoubleSpinBox(stroke_box)
        self.width_spin.setRange(MIN_STROKE_WIDTH, MAX_STROKE_WIDTH)
        self.width_spin.setSingleStep(0.5)
        self.width_spin.setDecimals(1)
        self.width_spin.setSuffix(" px")
        self.width_spin.setValue(style.stroke_width)
        stroke_form.addRow("线宽", self.width_spin)
        self.stroke_opacity_spin = _opacity_spin(stroke_box, style.stroke_opacity)
        stroke_form.addRow("透明度", self.stroke_opacity_spin)
        layout.addWidget(stroke_box)

        # 线几何没有填充，整组不显示
        self.fill_check = None
        self.fill_color_btn = None
        self.fill_opacity_spin = None
        if self._kind != "line":
            fill_box = QGroupBox("填充", self)
            fill_form = QFormLayout(fill_box)
            fill_form.setLabelAlignment(
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            fill_form.setHorizontalSpacing(8)
            self.fill_check = QCheckBox("显示填充", fill_box)
            self.fill_check.setChecked(style.fill_enabled)
            fill_form.addRow("", self.fill_check)
            self.fill_color_btn = ColorButton(style.fill_qcolor(), fill_box)
            fill_form.addRow("颜色", self.fill_color_btn)
            self.fill_opacity_spin = _opacity_spin(fill_box, style.fill_opacity)
            fill_form.addRow("透明度", self.fill_opacity_spin)
            layout.addWidget(fill_box)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        ok_btn = QPushButton("确定", self)
        ok_btn.setDefault(True)
        ok_btn.clicked.connect(self.accept)
        buttons.addWidget(ok_btn)
        cancel_btn = QPushButton("取消", self)
        cancel_btn.clicked.connect(self.reject)
        buttons.addWidget(cancel_btn)
        layout.addLayout(buttons)

        self.setLayout(layout)

        self.stroke_check.toggled.connect(self._sync_enabled)
        self._sync_enabled()

    def _sync_enabled(self) -> None:
        """没勾"显示"时把对应的颜色 / 线宽 / 透明度置灰。"""
        on = self.stroke_check.isChecked()
        self.stroke_color_btn.setEnabled(on)
        self.width_spin.setEnabled(on)
        self.stroke_opacity_spin.setEnabled(on)
        if self.fill_check is not None:
            fill_on = self.fill_check.isChecked()
            self.fill_color_btn.setEnabled(fill_on)
            self.fill_opacity_spin.setEnabled(fill_on)

    def result_style(self) -> HighlightStyle:
        """按控件当前的取值拼出样式。"""
        return HighlightStyle(
            stroke_enabled=self.stroke_check.isChecked(),
            stroke_color=self.stroke_color_btn.color().name(),
            stroke_width=float(self.width_spin.value()),
            stroke_opacity=self.stroke_opacity_spin.value(),
            fill_enabled=bool(self.fill_check is not None and self.fill_check.isChecked()),
            fill_color=(self.fill_color_btn.color().name()
                        if self.fill_color_btn is not None
                        else HighlightStyle().fill_color),
            fill_opacity=(self.fill_opacity_spin.value()
                          if self.fill_opacity_spin is not None
                          else HighlightStyle().fill_opacity),
        )


class SelectionTab(LayerPropertyTab):
    """图层属性「选择」页。"""

    def __init__(self, layer=None, parent=None) -> None:
        super().__init__(parent)
        self._layer = layer
        #: 当前图层几何类型（"point"/"line"/"polygon"；None = 不可配置）
        self._kind: str | None = None
        self._layer_name = ""
        self._build()
        # 无条件 load：这一页的 load 本身就能处理 None / 非矢量（走降级分支、禁用控件），
        # 所以不必像兄弟页那样先判 `if layer is not None`——否则 layer 为空时控件
        # 会停在"可点但点了没用"的状态。
        self.load(layer)

    # ------------------------------------------------------------- 构建

    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(10)

        top = QHBoxLayout()
        top.setSpacing(12)

        # 预览：带边框的大图，点它 = 打开「符号设置」
        self.preview_label = _ClickableLabel(self)
        self.preview_label.setObjectName("SelHighlightPreview")
        self.preview_label.setFixedSize(_PREVIEW_SIZE)
        self.preview_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview_label.setCursor(Qt.CursorShape.PointingHandCursor)
        self.preview_label.setToolTip("点击修改高亮样式")
        self.preview_label.setStyleSheet(
            "QLabel#SelHighlightPreview {"
            f" border: 1px solid palette(mid); background-color: {_PREVIEW_BG};"
            " border-radius: 3px; }"
        )
        self.preview_label.clicked.connect(self._open_symbol_settings)
        top.addWidget(self.preview_label)

        side = QVBoxLayout()
        side.setSpacing(6)
        self.symbol_btn = QPushButton("符号设置", self)
        self.symbol_btn.clicked.connect(self._open_symbol_settings)
        side.addWidget(self.symbol_btn)
        side.addStretch(1)
        top.addLayout(side)
        top.addStretch(1)
        root.addLayout(top)

        self.caption_label = QLabel("", self)
        self.caption_label.setObjectName("PropHint")
        root.addWidget(self.caption_label)

        self.hint_label = QLabel("", self)
        self.hint_label.setObjectName("PropHint")
        self.hint_label.setWordWrap(True)
        root.addWidget(self.hint_label)
        root.addStretch(1)

    # ------------------------------------------------------------- 页协议

    def load(self, layer) -> None:
        """按图层几何类型取系统里那一类的高亮样式并预览。"""
        self._kind = _geom_kind(layer)
        try:
            self._layer_name = layer.name() if layer is not None else ""
        except Exception:  # noqa: BLE001 —— 悬空图层对象
            self._layer_name = ""

        if self._kind is None:
            # 非矢量 / 无几何（属性表）——不可配置，把入口收起来
            self.preview_label.clear()
            self.symbol_btn.setEnabled(False)
            self.caption_label.setText("")
            self.hint_label.setText("当前图层没有几何类型，无法配置选中高亮。")
            return

        name = _KIND_NAMES[self._kind]
        self.symbol_btn.setEnabled(True)
        self.caption_label.setText(f"预览当前选中要素符号样式（{self._layer_name}）")
        self.hint_label.setText(
            f"以上样式作用于系统中**所有「{name}」图层**的选中高亮；"
            f"点「符号设置」修改，立即生效。要改其它几何类型，请打开对应类型图层的属性。"
        )
        self._refresh_preview()

    def apply(self, layer) -> bool:
        """样式在「符号设置」小窗点确定时就已写入并落盘，这里无事可做。"""
        return True

    # ------------------------------------------------------------- 内部

    def _open_symbol_settings(self) -> None:
        if self._kind is None:
            return
        manager = SelectionStyleManager.instance()
        dlg = _SymbolSettingsDialog(self._kind, manager.style(self._kind), self)
        try:
            accepted = dlg.exec() == QDialog.DialogCode.Accepted
        finally:
            dlg.deleteLater()
        if accepted:
            manager.set_style(self._kind, dlg.result_style())
            self._refresh_preview()

    def _refresh_preview(self) -> None:
        """把当前样式画成该类几何的符号预览。"""
        if self._kind is None:
            return
        try:
            style = SelectionStyleManager.instance().style(self._kind)
            # 符号先绑到局部变量再用：createSimple 返回的对象可能被 GC 掉，
            # 之后 symbolPreviewPixmap 拿到的是悬空指针（见 render.py 的同类坑）
            symbol = self._build_symbol(self._kind, style)
            self.preview_label.setPixmap(
                QgsSymbolLayerUtils.symbolPreviewPixmap(symbol, _PREVIEW_SIZE)
            )
        except Exception:  # noqa: BLE001 —— 预览画不出来不该让页面崩
            logger.warning("生成选中高亮预览失败", exc_info=True)
            self.preview_label.clear()

    @staticmethod
    def _build_symbol(kind: str, style: HighlightStyle):
        """按几何类型建一个示意符号；"不显示"的部分用全透明表达。

        颜色由 ``stroke_qcolor() / fill_qcolor()`` 给出，**已带上用户设的透明度**，
        所以预览与画布所见一致。
        """
        stroke = style.stroke_qcolor() if style.stroke_enabled else QColor(0, 0, 0, 0)
        fill = style.fill_qcolor() if style.fill_enabled else QColor(0, 0, 0, 0)
        width = f"{style.stroke_width:.1f}"
        if kind == "point":
            return QgsMarkerSymbol.createSimple({
                "color": fill, "outline_color": stroke,
                "size": _PREVIEW_MARKER_SIZE, "outline_width": width,
            })
        if kind == "line":
            return QgsLineSymbol.createSimple({"color": stroke, "width": width})
        return QgsFillSymbol.createSimple({
            "color": fill, "outline_color": stroke, "outline_width": width,
        })
