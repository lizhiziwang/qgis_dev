"""图层属性「选择」页：按几何类型配置**选中要素的高亮样式**。

打开属性页时按当前图层的几何类型（点 / 线 / 面）取系统里对应的高亮样式，预览出来；
点预览图（交互与「渲染 → 单一符号」一致）弹出符号选择器，改颜色 / 描边后只在**本页草稿**
里生效。和其它页一样延迟写回：点图层属性的「确定 / 应用」才写入
``SelectionStyleManager`` 并落盘（覆盖层订阅 ``changed`` 后重画）；点「取消」丢弃草稿，
系统高亮保持原样。

选中高亮是**橡皮筋覆盖层**画的，只吃颜色与线宽，所以写回时只取填充色、描边色与
描边宽度（透明度取颜色自身的 alpha）；形状 / SVG 等其它符号属性不参与高亮绘制。
**透明度不单独设**：颜色按钮打开的取色框里可直接调 alpha（选择 / 渲染页共用）。

因为配置是**系统级、按几何类型分开**的，页面只呈现当前图层那一类，并明确告知
"作用于系统里所有该类图层"——要改点 / 线 / 面里的另一类，开一个那种几何类型的图层属性。
"""

from __future__ import annotations

from dataclasses import replace

from qgis.core import QgsFillSymbol, QgsLineSymbol, QgsMarkerSymbol, QgsSymbolLayerUtils
from qgis.PyQt.QtCore import QSize, Qt, pyqtSignal
from qgis.PyQt.QtGui import QColor
from qgis.PyQt.QtWidgets import (
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from app.infra.logging import get_logger
from app.ui.common.selection_style import (
    HighlightStyle,
    SelectionStyleManager,
    default_style,
)
from app.ui.dialogs.layer_properties.base import LayerPropertyTab
from app.ui.dialogs.layer_properties.render import _PREVIEW_SIZE, _geom_kind

logger = get_logger(__name__)

#: kind → 中文名
_KIND_NAMES = {"point": "点", "line": "线", "polygon": "面"}

#: 预览里点标记的画布尺寸
_PREVIEW_MARKER_SIZE = "6.0"

#: 「恢复默认」用的选中高亮颜色（0,255,255）：填充 / 描边都恢复成它
_DEFAULT_HIGHLIGHT_COLOR = "#00ffff"


class _ClickableLabel(QLabel):
    """可点的 QLabel（点预览图 = 打开符号选择器，与渲染页同一入口）。"""

    clicked = pyqtSignal()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 —— Qt 命名
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mouseReleaseEvent(event)


def _first_layer(symbol):
    """符号首层；没有返回 ``None``。"""
    try:
        if symbol is None or symbol.symbolLayerCount() < 1:
            return None
        return symbol.symbolLayer(0)
    except Exception:  # noqa: BLE001
        return None


def symbol_from_style(kind: str, style: HighlightStyle):
    """按高亮样式造一个示意符号（选择器初始值 / 页面预览共用）。

    "不显示"的部分用全透明表达——橡皮筋没有开关，这是选择页一贯的表达方式。
    """
    stroke = style.stroke_qcolor() if style.stroke_enabled else QColor(0, 0, 0, 0)
    fill = style.fill_qcolor() if style.fill_enabled else QColor(0, 0, 0, 0)
    width = f"{style.stroke_width:.1f}"
    if kind == "point":
        symbol = QgsMarkerSymbol.createSimple(
            {
                "color": fill,
                "outline_color": stroke,
                "size": _PREVIEW_MARKER_SIZE,
                "outline_width": width,
            }
        )
    elif kind == "line":
        symbol = QgsLineSymbol.createSimple(
            {"line_color": stroke, "line_width": width}
        )
    else:
        symbol = QgsFillSymbol.createSimple(
            {"color": fill, "outline_color": stroke, "outline_width": width}
        )
    # 符号先绑到局部变量再用：createSimple 的返回值被 GC 掉后再访问会悬垂
    # （见 render.py 的同类坑）
    return symbol


def _opacity_percent(color: QColor) -> int:
    return round(color.alpha() / 255 * 100)


def style_from_symbol(kind: str, symbol, base: HighlightStyle) -> HighlightStyle:
    """把选择器返回的符号映射回高亮样式（只取颜色 / 描边宽度）。

    橡皮筋只能上色 / 设线宽，形状、SVG、多符号层这些不参与实际高亮；透明度取颜色
    自身的 alpha，alpha=0 视为"不显示"。
    """
    layer = _first_layer(symbol)
    if layer is None:
        return base
    try:
        if kind == "line":
            color = QColor(layer.color())
            return replace(
                base,
                stroke_enabled=color.alpha() > 0,
                stroke_color=color.name(),
                stroke_width=float(layer.width()),
                stroke_opacity=_opacity_percent(color),
            )
        fill = QColor(layer.color())
        stroke = QColor(layer.strokeColor())
        # 「无填充 / 无描边」是样式，不是颜色：橡皮筋只认颜色，这里折成"不显示"
        try:
            props = dict(layer.properties())
        except Exception:  # noqa: BLE001
            props = {}
        fill_off = str(props.get("style") or "").lower() == "no"
        stroke_off = str(props.get("outline_style") or "").lower() == "no"
        return replace(
            base,
            stroke_enabled=stroke.alpha() > 0 and not stroke_off,
            stroke_color=stroke.name(),
            stroke_width=float(layer.strokeWidth()),
            stroke_opacity=_opacity_percent(stroke),
            fill_enabled=fill.alpha() > 0 and not fill_off,
            fill_color=fill.name(),
            fill_opacity=_opacity_percent(fill),
        )
    except (AttributeError, TypeError, RuntimeError) as exc:
        logger.warning("读取符号颜色失败，保持原高亮样式：%s", exc)
        return base


class SelectionTab(LayerPropertyTab):
    """图层属性「选择」页。"""

    def __init__(self, layer=None, parent=None) -> None:
        super().__init__(parent)
        self._layer = layer
        #: 当前图层几何类型（"point"/"line"/"polygon"；None = 不可配置）
        self._kind: str | None = None
        self._layer_name = ""
        #: 本页正在编辑的样式（**草稿**）：点「确定 / 应用」才写回管理器；
        #: 点「取消」直接丢弃，系统高亮保持原样。
        self._style: HighlightStyle | None = None
        #: 草稿是否与已生效的样式不同（没改过就不在「应用」时重复写）
        self._dirty = False
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

        # 预览：与「渲染 → 单一符号」同一套尺寸 / 背景 / 边框，点它 = 打开符号选择器
        form = QFormLayout()
        form.setLabelAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        form.setHorizontalSpacing(8)
        preview_row = QWidget(self)
        row = QHBoxLayout(preview_row)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)
        self.preview_label = _ClickableLabel(self)
        self.preview_label.setFixedSize(_PREVIEW_SIZE)
        self.preview_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview_label.setCursor(Qt.CursorShape.PointingHandCursor)
        self.preview_label.setToolTip("点击打开符号选择器")
        self.preview_label.setStyleSheet(
            "QLabel { border: 1px solid palette(mid); background: palette(base);"
            " border-radius: 3px; }"
        )
        self.preview_label.clicked.connect(self._open_symbol_settings)
        row.addWidget(self.preview_label, 0, Qt.AlignmentFlag.AlignTop)
        self.reset_btn = QPushButton("恢复默认", preview_row)
        self.reset_btn.setToolTip(
            "恢复默认选中高亮样式：填充 / 描边颜色 #00FFFF（0, 255, 255）"
        )
        self.reset_btn.clicked.connect(self._restore_default)
        # 与预览框**顶边**对齐（右上角），不放到预览右侧的垂直中间
        row.addWidget(self.reset_btn, 0, Qt.AlignmentFlag.AlignTop)
        row.addStretch(1)
        form.addRow("符号预览", preview_row)
        root.addLayout(form)

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
        """按图层几何类型取系统里那一类的高亮样式并预览（草稿 = 已生效样式）。"""
        self._kind = _geom_kind(layer)
        self._style = None
        self._dirty = False
        try:
            self._layer_name = layer.name() if layer is not None else ""
        except Exception:  # noqa: BLE001 —— 悬空图层对象
            self._layer_name = ""

        if self._kind is None:
            # 非矢量 / 无几何（属性表）——不可配置，把入口收起来
            self.preview_label.clear()
            self.preview_label.setEnabled(False)
            self.reset_btn.setEnabled(False)
            self.caption_label.setText("")
            self.hint_label.setText("当前图层没有几何类型，无法配置选中高亮。")
            return

        manager = SelectionStyleManager.instance()
        self._style = manager.style(self._kind)
        name = _KIND_NAMES[self._kind]
        self.preview_label.setEnabled(True)
        self.reset_btn.setEnabled(True)
        self.caption_label.setText(f"预览当前选中要素符号样式（{self._layer_name}）")
        self.hint_label.setText(
            f"以上样式作用于系统中**所有「{name}」图层**的选中高亮；"
            f"改完点「确定 / 应用」后生效，点「取消」不修改。"
            f"要改其它几何类型，请打开对应类型图层的属性。"
        )
        self._refresh_preview()

    def apply(self, layer) -> bool:
        """点「确定 / 应用」时才把草稿样式写回管理器并落盘（其它页同款延迟写回）。"""
        if self._kind is None or self._style is None or not self._dirty:
            return True
        manager = SelectionStyleManager.instance()
        manager.set_style(self._kind, self._style)
        # 已应用的样式成为新基线：之后再点应用/确定不会重复写，之后取消也不回退它
        self._dirty = False
        return True

    # ------------------------------------------------------------- 内部

    def _open_symbol_settings(self) -> None:
        """点预览：打开与「渲染 → 单一符号」相同的符号选择器（只改本页草稿）。"""
        if self._kind is None:
            return
        from app.ui.dialogs.layer_properties.symbol_selector import (
            SymbolSelectorDialog,
        )

        base = self._style or SelectionStyleManager.instance().style(self._kind)
        dlg = SymbolSelectorDialog(
            symbol_from_style(self._kind, base), self._kind, self
        )
        try:
            accepted = dlg.exec() == QDialog.DialogCode.Accepted
            chosen = dlg.selected_symbol() if accepted else None
        finally:
            dlg.deleteLater()
        if chosen is not None:
            self._style = style_from_symbol(self._kind, chosen, base)
            self._dirty = True
            self._refresh_preview()

    def _restore_default(self) -> None:
        """恢复默认选中高亮：填充 / 描边 = #00FFFF（0, 255, 255）。

        和本页其它修改一样先进**草稿**，点「确定 / 应用」才写回系统样式。
        """
        if self._kind is None:
            return
        base = default_style(self._kind)
        self._style = replace(
            base,
            stroke_color=_DEFAULT_HIGHLIGHT_COLOR,
            fill_color=_DEFAULT_HIGHLIGHT_COLOR,
        )
        self._dirty = self._style != SelectionStyleManager.instance().style(self._kind)
        self._refresh_preview()

    def _refresh_preview(self) -> None:
        """把当前（草稿）样式画成该类几何的符号预览。"""
        if self._kind is None:
            return
        try:
            style = self._style or SelectionStyleManager.instance().style(self._kind)
            # 符号先绑到局部变量再用：createSimple 返回的对象可能被 GC 掉，
            # 之后 symbolPreviewPixmap 拿到的是悬垂指针（见 render.py 的同类坑）
            symbol = symbol_from_style(self._kind, style)
            self.preview_label.setPixmap(
                QgsSymbolLayerUtils.symbolPreviewPixmap(
                    symbol,
                    QSize(_PREVIEW_SIZE.width() - 20, _PREVIEW_SIZE.height() - 20),
                )
            )
        except Exception:  # noqa: BLE001 —— 预览画不出来不该让页面崩
            logger.warning("生成选中高亮预览失败", exc_info=True)
            self.preview_label.clear()
