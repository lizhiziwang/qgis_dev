"""图层属性「渲染」页（栅格）。

矢量渲染页（``render.py``）负责符号 / 分类；栅格没有“符号”概念，渲染方式由
:class:`qgis.core.QgsRasterRenderer` 的派生类决定。本页覆盖常用五种：

- **多波段彩色（RGB）**：R/G/B 各绑定一个波段，每个通道独立拉伸；
- **单波段灰度**：波段 + 拉伸 + 反色；
- **单波段伪彩色**：波段 + 色带 + 连续 / 等间距分级（色彩映射）；
- **调色板 / 唯一值**：离散值 → 颜色表，适合土地利用等分类栅格；
- **山体阴影**：方位角 / 高度角 / Z 因子 / 多方向光照。

拉伸（对比度增强）不是独立渲染器，而是灰度 / RGB 渲染器内部的
:class:`qgis.core.QgsContrastEnhancement`；本页把它做成“拉伸方式 + 最小值 /
最大值”的通用控件，三种自动方式（最小–最大、百分比截断、标准差）都由波段
统计计算后写入显式范围，避免依赖 QGIS 对话框的隐式行为。

写回策略与矢量渲染页一致：所有修改先暂存在控件里，点「确定 / 应用」时才在
:meth:`RasterRenderTab.apply` 中构建新渲染器并 ``setRenderer``。用户没动过
本页（``_dirty`` 为假）时什么都不做——外壳会把每一页都 apply 一遍，不能因为
打开过属性对话框就悄悄替换掉图层原有样式。
"""

from __future__ import annotations

import contextlib
import math
import os
import warnings
from collections import Counter

from qgis.core import (
    Qgis,
    QgsColorRampShader,
    QgsContrastEnhancement,
    QgsHillshadeRenderer,
    QgsMultiBandColorRenderer,
    QgsPalettedRasterRenderer,
    QgsRasterLayer,
    QgsRasterRendererUtils,
    QgsRasterShader,
    QgsRasterTransparency,
    QgsRectangle,
    QgsSingleBandGrayRenderer,
    QgsSingleBandPseudoColorRenderer,
    QgsStyle,
)
from qgis.PyQt.QtCore import QSize, Qt
from qgis.PyQt.QtGui import QColor, QIcon, QPainter, QPalette, QPen, QPixmap
from qgis.PyQt.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSpinBox,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.infra.logging import get_logger
from app.ui.common.message_box import MessageBox
from app.ui.dialogs.layer_properties.base import LayerPropertyTab
from app.ui.dialogs.layer_properties.widgets import ColorButton, pick_color

logger = get_logger(__name__)

#: 波段统计采样上限：避免打开属性页时对超大影像做全量扫描。
#: 统计只用于填充默认拉伸范围，采样足够；用户仍可手动改。
_STATS_SAMPLE = 250_000

_RAMP_ICON_SIZE = QSize(220, 16)
#: 调色板颜色列的色块：画成图标而不是单元格底色——表格的选中高亮会盖掉
#: ``setBackground`` 画出的底色，选中行时颜色“消失”（用户报过）。
_SWATCH_SIZE = QSize(44, 18)
_PALETTE_COL_VALUE = 0
_PALETTE_COL_COLOR = 1
_PALETTE_COL_LABEL = 2
_PALETTE_HEADERS = ("值", "颜色", "标签")

#: 连续色带采样点数：只用首尾两色线性插值会丢掉中间色，32 个停靠点足够平滑。
_CONTINUOUS_STOPS = 32

_MODE_MULTIBAND = "multiband"
_MODE_GRAY = "gray"
_MODE_PSEUDO = "pseudocolor"
_MODE_PALETTED = "paletted"
_MODE_HILLSHADE = "hillshade"

#: (下拉框显示文本, 内部值)；顺序同时对应 QStackedWidget 的页顺序。
_MODES = (
    ("多波段彩色（RGB）", _MODE_MULTIBAND),
    ("单波段灰度", _MODE_GRAY),
    ("单波段伪彩色", _MODE_PSEUDO),
    ("调色板 / 唯一值", _MODE_PALETTED),
    ("山体阴影", _MODE_HILLSHADE),
)
_MODE_INDEX = {data: index for index, (_, data) in enumerate(_MODES)}

_STRETCH_MODES = (
    ("不拉伸（按原始值线性映射）", "none"),
    ("最小–最大", "minmax"),
    ("百分比截断（2%–98%）", "percent"),
    ("标准差（2σ）", "stddev"),
    ("手动输入范围", "manual"),
)

_PSEUDO_CONTINUOUS = "continuous"
_PSEUDO_EQUAL = "equal"

_HINTS = {
    _MODE_MULTIBAND: "把三个波段分别映射到红 / 绿 / 蓝通道，可对每个通道单独设置拉伸。",
    _MODE_GRAY: "用单个波段以灰度显示；拉伸方式决定高低值如何映射到黑–白。",
    _MODE_PSEUDO: "按色带把单波段数值映射成颜色，适合 DEM、温度、降雨量等连续数值栅格。",
    _MODE_PALETTED: "为离散值逐项指定颜色（值 → 颜色），适合土地利用、分类编码等整数栅格。",
    _MODE_HILLSHADE: "根据 DEM 高程实时生成山体阴影，常与单波段灰度 / 伪彩色叠加使用。",
}

#: 服务端渲染的在线影像：客户端改渲染器没有意义。
_REMOTE_PROVIDERS = {
    "wms",
    "wmts",
    "arcgismapserver",
    "arcgistiledmapservice",
    "xyz",
}


# ---------------------------------------------------------------- 小工具


class _InputError(ValueError):
    """用户输入问题（调色板为空、范围无效等）。

    这类错误只需提示用户改控件，不是程序异常：``apply`` 捕获后不写 ERROR
    日志 / 堆栈，避免用户在日志里看到一条吓人的 Traceback。
    """


def _data_type_range_map() -> dict[int, tuple[float, float]]:
    """``Qgis.DataType`` → 显示用的兜底范围（拿不到统计时用）。

    枚举名在 QGIS 各版本里基本稳定；用 ``getattr`` 探测，缺哪个就跳过，
    避免因为一个枚举名在旧版本不存在导致整个模块导入失败。
    """
    table = {
        "Byte": (0.0, 255.0),
        "Int8": (-128.0, 127.0),
        "UInt16": (0.0, 65535.0),
        "Int16": (-32768.0, 32767.0),
        "UInt32": (0.0, 4_294_967_295.0),
        "Int32": (-2_147_483_648.0, 2_147_483_647.0),
        "Float32": (0.0, 255.0),
        "Float64": (0.0, 255.0),
    }
    out: dict[int, tuple[float, float]] = {}
    for name, value in table.items():
        enum = getattr(Qgis.DataType, name, None)
        if enum is not None:
            out[int(enum)] = value
    return out


_DATA_TYPE_RANGES = _data_type_range_map()


def _valid_range(lo, hi) -> tuple[float, float] | None:
    """规范化范围：非有限 / 反序 / 相等都返回 ``None`` 或展开后的区间。"""
    try:
        lo = float(lo)
        hi = float(hi)
    except (TypeError, ValueError):
        return None
    if not (math.isfinite(lo) and math.isfinite(hi)):
        return None
    if lo > hi:
        lo, hi = hi, lo
    if lo == hi:
        pad = max(abs(lo) * 0.01, 0.5)
        return lo - pad, hi + pad
    return lo, hi


def _format_value(value: float) -> str:
    """数值 → 图例 / 表单项文本（整数不带小数点，小数保留 6 位有效数字）。"""
    if not math.isfinite(value):
        return "—"
    if value == int(value) and abs(value) < 1e15:
        return str(int(value))
    return f"{value:.6g}"


def _color_close(a: QColor, b: QColor, tolerance: int = 8) -> bool:
    """颜色近似相等：加载时用来把已有色阶匹配回内置色带。"""
    return all(
        abs(getattr(a, channel)() - getattr(b, channel)()) <= tolerance
        for channel in ("red", "green", "blue")
    )


#: 单值透明度列表的“无下限 / 无上限”哨兵。灰度 / 伪彩色把背景色反推成
#: 波段值后，黑色可能覆盖“所有小于最小值的像元”（例如 -9999 填充），用
#: ±1e308 表示开区间，避免只忽略恰好等于最小值的那些像元。
_BACKGROUND_LOW = -1e308
_BACKGROUND_HIGH = 1e308


def _gray_background_ranges(
    lo: float,
    hi: float,
    reverse: bool,
    method: str,
    color: QColor,
) -> list[tuple[float, float]]:
    """灰度渲染：把背景色反推成波段值区间。

    灰度渲染器把 ``[lo, hi]`` 线性映射到 0–255（``none`` 则直接把原始值
    当灰度，超出部分截断）。取背景 RGB 的平均值作为目标灰度，再按反向 /
    拉伸方式算回波段值；越界时返回开区间，覆盖所有会渲染成同一颜色的值。
    """
    red, green, blue, _alpha = color.getRgb()
    intensity = max(0, min(255, int(round((red + green + blue) / 3.0))))
    if method == "none":
        target = 255 - intensity if reverse else intensity
        if target <= 0:
            return [(_BACKGROUND_LOW, 0.0)]
        if target >= 255:
            return [(255.0, _BACKGROUND_HIGH)]
        return [(float(target), float(target))]

    span = hi - lo
    if span <= 0:
        return []
    ratio = intensity / 255.0
    if reverse:
        ratio = 1.0 - ratio
    if ratio <= 0.0:
        return [(_BACKGROUND_LOW, lo)]
    if ratio >= 1.0:
        return [(hi, _BACKGROUND_HIGH)]
    center = lo + ratio * span
    # 灰度按 0–255 量化，同一个灰度可能对应一段波段值
    half = span / 510.0
    return [(max(lo, center - half), min(hi, center + half))]


def _gray_color_for_value(renderer, value: float) -> QColor:
    """灰度渲染：由波段值反推显示灰度（回填背景色用）。"""
    try:
        enhancement = renderer.contrastEnhancement()
        low = float(enhancement.minimumValue())
        high = float(enhancement.maximumValue())
        algorithm = enhancement.contrastEnhancementAlgorithm()
        reverse = (
            renderer.gradient()
            == QgsSingleBandGrayRenderer.Gradient.WhiteToBlack
        )
    except (RuntimeError, AttributeError, TypeError):
        return QColor(0, 0, 0)
    no_enhancement = (
        QgsContrastEnhancement.ContrastEnhancementAlgorithm.NoEnhancement
    )
    if algorithm == no_enhancement:
        intensity = max(0, min(255, int(round(value))))
    elif high > low:
        ratio = max(0.0, min(1.0, (value - low) / (high - low)))
        intensity = int(round(ratio * 255))
    else:
        intensity = 0
    if reverse:
        intensity = 255 - intensity
    return QColor(intensity, intensity, intensity)


def _pseudo_background_ranges(
    renderer, color: QColor, samples: int = 512
) -> list[tuple[float, float]]:
    """伪彩色：采样色带，找出渲染成背景色的波段值区间。

    伪彩色没有“波段值 = 颜色”的简单公式，但对色带做等距采样即可反查。
    连续色带通常得到很窄的一段，分级色带会得到该级对应的整段值。
    """
    try:
        low = float(renderer.classificationMin())
        high = float(renderer.classificationMax())
        shader = renderer.shader()
    except (RuntimeError, AttributeError, TypeError):
        return []
    if not (math.isfinite(low) and math.isfinite(high)) or high <= low:
        return []
    step = (high - low) / samples
    matched: list[bool] = []
    for index in range(samples + 1):
        value = low + step * index
        try:
            ok, red, green, blue, _alpha = shader.shade(value)
        except (RuntimeError, TypeError):
            return []
        matched.append(bool(ok) and _color_close(QColor(red, green, blue), color))

    ranges: list[tuple[float, float]] = []
    index = 0
    while index <= samples:
        if not matched[index]:
            index += 1
            continue
        start = index
        while index + 1 <= samples and matched[index + 1]:
            index += 1
        end = index
        # 采样点之间的真实边界取相邻未命中点的中点；贴边时延伸到量程端点
        left = low if start == 0 else low + step * (start - 0.5)
        right = high if end == samples else low + step * (end + 0.5)
        ranges.append((max(low, left), min(high, right)))
        index += 1
    return ranges


def _pseudo_color_for_value(renderer, value: float) -> QColor:
    """伪彩色渲染：由波段值反推显示颜色（回填背景色用）。"""
    try:
        ok, red, green, blue, alpha = renderer.shader().shade(float(value))
    except (RuntimeError, AttributeError, TypeError):
        return QColor(0, 0, 0)
    return QColor(red, green, blue, alpha) if ok else QColor(0, 0, 0)


def _background_tip(mode: str) -> str:
    """「忽略背景色」在不同渲染方式下的说明。"""
    if mode == _MODE_GRAY:
        return (
            "与所选灰度对应的波段值不显示：深色对应低值、浅色对应高值"
            "（勾选「反向灰度」时相反），常用于去掉影像四周的黑 / 白边"
        )
    if mode == _MODE_PSEUDO:
        return (
            "与所选颜色对应的波段值不显示：按色带反查该颜色覆盖的值区间，"
            "常用于去掉色带两端的背景色"
        )
    if mode == _MODE_HILLSHADE:
        return (
            "山体阴影无法按输出颜色反推高程：勾选后自动忽略栅格边缘最常见"
            "的背景高程值，NoData 也会一并透明"
        )
    return (
        "与所选颜色相同的像素不显示，常用于去掉影像四周的黑色 / 白色边；"
        "调色板模式下没有对应色值的像元也会一并透明"
    )




def _ramp_names() -> list[str]:
    try:
        return list(QgsStyle.defaultStyle().colorRampNames())
    except Exception:
        logger.debug("读取内置色带列表失败", exc_info=True)
        return []


#: 内置色带的中文名。名称本身是 QGIS 样式库的键，只换下拉框显示文本；
#: 未收录的（新版 QGIS 可能新增）保留原名，避免下拉框出现空项。
_RAMP_LABELS = {
    "Blues": "蓝",
    "BrBG": "棕-蓝绿",
    "BuGn": "蓝-绿",
    "BuPu": "蓝-紫",
    "Cividis": "深蓝-黄",
    "GnBu": "绿-蓝",
    "Greens": "绿",
    "Greys": "灰阶",
    "Inferno": "火焰",
    "Magma": "岩浆",
    "Mako": "深海",
    "OrRd": "橙-红",
    "Oranges": "橙",
    "PRGn": "紫-绿",
    "PiYG": "粉-绿",
    "Plasma": "等离子",
    "PuBu": "紫-蓝",
    "PuBuGn": "紫-蓝-绿",
    "PuOr": "紫-橙",
    "PuRd": "紫-红",
    "Purples": "紫",
    "RdBu": "红-蓝",
    "RdGy": "红-灰",
    "RdPu": "红-紫",
    "RdYlBu": "红-黄-蓝",
    "RdYlGn": "红-黄-绿",
    "Reds": "红",
    "Rocket": "火箭",
    "Spectral": "彩虹",
    "Turbo": "涡轮",
    "Viridis": "紫-绿-黄",
    "YlGn": "黄-绿",
    "YlGnBu": "黄-绿-蓝",
    "YlOrBr": "黄-橙-棕",
    "YlOrRd": "黄-橙-红",
}


def _ramp_label(name: str) -> str:
    return _RAMP_LABELS.get(name, name)


def _make_ramp(name: str | None):
    """按名称取内置色带；名称缺失时退回第一条可用色带。"""
    style = QgsStyle.defaultStyle()
    ramp = style.colorRamp(name) if name else None
    if ramp is not None:
        return ramp
    names = style.colorRampNames()
    return style.colorRamp(names[0]) if names else None


def _ramp_icon(ramp) -> QIcon:
    """把色带画成下拉框图标（与应用其它色带预览同一观感）。"""
    width = _RAMP_ICON_SIZE.width()
    height = _RAMP_ICON_SIZE.height()
    pixmap = QPixmap(width, height)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    try:
        for x in range(width):
            painter.setPen(QPen(ramp.color(x / max(1, width - 1))))
            painter.drawLine(x, 0, x, height - 1)
        painter.setPen(QPen(QColor(130, 130, 130)))
        painter.drawRect(0, 0, width - 1, height - 1)
    finally:
        painter.end()
    return QIcon(pixmap)


def _swatch_icon(color: QColor) -> QIcon:
    """颜色 → 表格单元格里的色块图标（选中行时不会被高亮盖住）。"""
    width = _SWATCH_SIZE.width()
    height = _SWATCH_SIZE.height()
    pixmap = QPixmap(width, height)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    try:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(QPen(QColor(120, 120, 120, 180), 1))
        painter.setBrush(color)
        painter.drawRoundedRect(0, 0, width - 1, height - 1, 3, 3)
    finally:
        painter.end()
    return QIcon(pixmap)


def _set_palette_color(item: QTableWidgetItem, color: QColor) -> None:
    """把颜色写进「颜色」列条目：色块图标 + UserRole 里存 HexArgb。"""
    if not color.isValid():
        color = QColor(200, 200, 200)
    item.setIcon(_swatch_icon(color))
    item.setData(Qt.ItemDataRole.UserRole, color.name(QColor.NameFormat.HexArgb))
    item.setToolTip(f"双击选择颜色（当前 {color.name()}）")


def _read_band_stats(provider, band: int):
    """读取波段统计（采样）。失败返回 ``None``，调用方走兜底范围。

    ``bandStatistics`` 的 ``stats`` 参数在 QGIS 4.x 已改用
    ``Qgis.RasterBandStatistic``，但绑定层仍会发一条 DeprecationWarning；
    这里只屏蔽这一条调用产生的警告，不影响其它日志。
    """
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            return provider.bandStatistics(
                band,
                Qgis.RasterBandStatistic.All,
                QgsRectangle(),
                _STATS_SAMPLE,
            )
    except Exception:
        logger.debug("读取波段统计失败：band=%s", band, exc_info=True)
        return None


def _read_cumulative_range(provider, band: int):
    """2%–98% 累积截断范围；失败或常量栅格返回 ``None``。"""
    try:
        lo, hi = provider.cumulativeCut(
            band, 0.02, 0.98, QgsRectangle(), _STATS_SAMPLE
        )
        return _valid_range(lo, hi)
    except Exception:
        logger.debug("读取累积截断范围失败：band=%s", band, exc_info=True)
        return None


def _stretch_algorithm(method: str):
    """拉伸方式 → QgsContrastEnhancement 算法。"""
    if method == "none":
        return QgsContrastEnhancement.ContrastEnhancementAlgorithm.NoEnhancement
    return (
        QgsContrastEnhancement.ContrastEnhancementAlgorithm.StretchToMinimumMaximum
    )


class RasterRenderTab(LayerPropertyTab):
    """图层属性「渲染」页（栅格）。"""

    def __init__(self, layer=None, parent=None) -> None:
        super().__init__(parent)
        self._layer = layer
        self._provider = None
        self._bands = 0
        #: 程序性回填控件期间为 True，防止信号把默认值当成用户改动
        self._loading = True
        #: 用户是否在本页动过东西；没动过时 apply 什么都不做
        self._dirty = False
        #: 各波段统计 / 累积截断缓存（band → 结果），切模式时避免重复扫描
        self._stats_cache: dict[int, object | None] = {}
        self._cumulative_cache: dict[int, tuple[float, float] | None] = {}
        #: 非当前模式的最小/最大值尚未按统计填充（切到该模式时再算）
        self._pending_auto = {
            _MODE_MULTIBAND: True,
            _MODE_GRAY: True,
            _MODE_PSEUDO: True,
        }
        #: 色彩映射导入 / 导出对话框的起始目录（本次会话内记住）
        self._last_color_map_dir = ""
        self._build()
        if layer is not None:
            self.load(layer)
        else:
            self._loading = False
            self._set_unavailable("仅栅格图层支持渲染设置")

    # ------------------------------------------------------------- 构建

    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(10)

        form = QFormLayout()
        form.setLabelAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        form.setHorizontalSpacing(8)
        self.mode_combo = QComboBox(self)
        for label, data in _MODES:
            self.mode_combo.addItem(label, data)
        self.mode_combo.setMinimumWidth(220)
        form.addRow("渲染方式", self.mode_combo)
        root.addLayout(form)

        self.hint_label = QLabel("", self)
        self.hint_label.setObjectName("PropHint")
        self.hint_label.setWordWrap(True)
        root.addWidget(self.hint_label)

        self.stack = QStackedWidget(self)
        #: 背景透明控件放在各渲染方式自己的页里（不再在对话框底部单独一组）：
        #: mode → (容器, 勾选框, 颜色按钮)。每个页一份，勾选 / 颜色互相同步。
        self._background_rows: dict[
            str, tuple[QWidget, QCheckBox, ColorButton]
        ] = {}
        self._pages: dict[str, QWidget] = {
            _MODE_MULTIBAND: self._build_multiband_page(),
            _MODE_GRAY: self._build_gray_page(),
            _MODE_PSEUDO: self._build_pseudo_page(),
            _MODE_PALETTED: self._build_paletted_page(),
            _MODE_HILLSHADE: self._build_hillshade_page(),
        }
        for page in self._pages.values():
            self.stack.addWidget(page)
        root.addWidget(self.stack, 1)

        self.mode_combo.currentIndexChanged.connect(self._on_mode_changed)
        self._sync_mode_page()

    def _new_background_row(self, parent, mode: str) -> QWidget:
        """背景透明控件：把与指定颜色相同的像素设为透明（影像四周的黑 / 白边）。

        控件嵌在每种渲染方式自己的页里，不再单独占对话框最下面一行。山体
        阴影的输出灰度无法反推高程，换成一个不带取色按钮的勾选框：勾选后
        自动忽略栅格边缘最常见的背景高程值（以及 NoData）。
        """
        container = QWidget(parent)
        row = QHBoxLayout(container)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)
        if mode == _MODE_HILLSHADE:
            check = QCheckBox("忽略背景（边缘高程）", container)
            check.setToolTip(_background_tip(mode))
        else:
            check = QCheckBox("忽略背景色", container)
            check.setToolTip(_background_tip(mode))
        check.toggled.connect(
            lambda checked, key=mode: self._on_background_toggled(checked, key)
        )
        color = ColorButton(QColor(0, 0, 0), container)
        color.setToolTip("要忽略的背景颜色（默认黑色）")
        color.color_changed.connect(
            lambda *_args, key=mode: self._on_background_color_changed(key)
        )
        color.setEnabled(False)
        row.addWidget(check)
        if mode == _MODE_HILLSHADE:
            color.setVisible(False)
            hint = QLabel("自动取栅格边缘值", container)
            hint.setObjectName("PropHint")
            row.addWidget(hint)
        else:
            row.addWidget(color)
        row.addStretch(1)
        self._background_rows[mode] = (container, check, color)
        return container

    def _on_background_toggled(self, checked: bool, mode: str) -> None:
        """一个页里勾选 / 取消时，同步其余页面的勾选框和颜色。"""
        source = self._background_rows.get(mode)
        color = source[2].color() if source else QColor(0, 0, 0)
        for key, (_widget, check, color_button) in self._background_rows.items():
            if key == mode:
                continue
            check.blockSignals(True)
            color_button.blockSignals(True)
            try:
                check.setChecked(bool(checked))
                color_button.set_color(color, emit=False)
            finally:
                color_button.blockSignals(False)
                check.blockSignals(False)
        self._mark_dirty()
        self._sync_background_availability()

    def _on_background_color_changed(self, mode: str) -> None:
        """各页面的背景色保持同一个值。"""
        source = self._background_rows.get(mode)
        if source is None:
            return
        color = source[2].color()
        for key, (_widget, _check, color_button) in self._background_rows.items():
            if key != mode:
                color_button.set_color(color, emit=False)
        self._mark_dirty()

    def _new_band_combo(self, parent) -> QComboBox:
        combo = QComboBox(parent)
        combo.setMinimumWidth(180)
        combo.currentIndexChanged.connect(self._on_band_changed)
        return combo

    def _new_range_spin(self, parent) -> QDoubleSpinBox:
        spin = QDoubleSpinBox(parent)
        spin.setRange(-1e12, 1e12)
        spin.setDecimals(6)
        spin.setSingleStep(1.0)
        spin.setMinimumWidth(130)
        spin.setKeyboardTracking(False)
        spin.valueChanged.connect(self._mark_dirty)
        return spin

    def _new_ramp_combo(self, parent) -> QComboBox:
        combo = QComboBox(parent)
        combo.setMinimumWidth(360)
        combo.setIconSize(_RAMP_ICON_SIZE)
        view = combo.view()
        if view is not None:
            view.setIconSize(_RAMP_ICON_SIZE)
        for name in _ramp_names():
            ramp = _make_ramp(name)
            if ramp is None:
                continue
            combo.addItem(_ramp_icon(ramp), _ramp_label(name), name)
        if combo.count() == 0:
            combo.addItem("（没有可用的内置色带）", None)
        combo.currentIndexChanged.connect(self._mark_dirty)
        return combo

    def _build_stretch_group(self, parent, kind: str) -> QGroupBox:
        box = QGroupBox("对比度增强（拉伸）", parent)
        form = QFormLayout(box)
        form.setLabelAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        form.setHorizontalSpacing(8)
        combo = QComboBox(box)
        for label, data in _STRETCH_MODES:
            combo.addItem(label, data)
        # 默认按最小–最大拉伸（QGIS 的常规观感），而不是“不拉伸”直方图全平
        combo.setCurrentIndex(max(0, combo.findData("minmax")))
        form.addRow("拉伸方式", combo)

        row = QHBoxLayout()
        row.setSpacing(6)
        button = QPushButton("按统计重新计算", box)
        button.setToolTip("按当前拉伸方式读取波段统计，重填最小值与最大值")
        row.addWidget(button)
        row.addStretch(1)
        form.addRow("", row)

        combo.currentIndexChanged.connect(
            lambda _index: self._on_stretch_changed(kind)
        )
        button.clicked.connect(
            lambda: self._recompute_ranges(kind, manual_stats=True)
        )
        if kind == "mb":
            self.mb_stretch_combo = combo
            self.mb_stats_btn = button
        else:
            self.gray_stretch_combo = combo
            self.gray_stats_btn = button
        return box

    def _build_multiband_page(self) -> QWidget:
        page = QWidget(self)
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        box = QGroupBox("波段映射", page)
        grid = QGridLayout(box)
        grid.setContentsMargins(10, 12, 10, 10)
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(8)
        for column, text in enumerate(("通道", "波段", "最小值", "最大值")):
            label = QLabel(text, box)
            label.setObjectName("PropHint")
            grid.addWidget(label, 0, column)
        self.mb_bands: list[dict] = []
        for row_index, (name, default_band) in enumerate(
            (("红", 1), ("绿", 2), ("蓝", 3)), start=1
        ):
            channel = QLabel(name, box)
            band_combo = self._new_band_combo(box)
            min_spin = self._new_range_spin(box)
            max_spin = self._new_range_spin(box)
            grid.addWidget(channel, row_index, 0)
            grid.addWidget(band_combo, row_index, 1)
            grid.addWidget(min_spin, row_index, 2)
            grid.addWidget(max_spin, row_index, 3)
            self.mb_bands.append(
                {
                    "label": name,
                    "band": band_combo,
                    "min": min_spin,
                    "max": max_spin,
                    "default": default_band,
                }
            )
        grid.setColumnStretch(1, 1)
        grid.addWidget(self._new_background_row(box, _MODE_MULTIBAND), 4, 0, 1, 4)
        layout.addWidget(box)

        layout.addWidget(self._build_stretch_group(page, "mb"))
        layout.addStretch(1)
        return page

    def _build_gray_page(self) -> QWidget:
        page = QWidget(self)
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        box = QGroupBox("波段", page)
        grid = QGridLayout(box)
        grid.setContentsMargins(10, 12, 10, 10)
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(8)
        self.gray_band_combo = self._new_band_combo(box)
        self.gray_min = self._new_range_spin(box)
        self.gray_max = self._new_range_spin(box)
        grid.addWidget(QLabel("波段", box), 0, 0)
        grid.addWidget(self.gray_band_combo, 0, 1, 1, 3)
        grid.addWidget(QLabel("最小值", box), 1, 0)
        grid.addWidget(self.gray_min, 1, 1)
        grid.addWidget(QLabel("最大值", box), 1, 2)
        grid.addWidget(self.gray_max, 1, 3)
        self.gray_reverse = QCheckBox("反向灰度（白 → 黑）", box)
        self.gray_reverse.toggled.connect(self._mark_dirty)
        grid.addWidget(self.gray_reverse, 2, 0, 1, 4)
        grid.addWidget(self._new_background_row(box, _MODE_GRAY), 3, 0, 1, 4)
        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(3, 1)
        layout.addWidget(box)
        self.gray_row = {
            "label": "灰度",
            "band": self.gray_band_combo,
            "min": self.gray_min,
            "max": self.gray_max,
            "default": 1,
        }

        layout.addWidget(self._build_stretch_group(page, "gray"))
        layout.addStretch(1)
        return page

    def _build_pseudo_page(self) -> QWidget:
        page = QWidget(self)
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        box = QGroupBox("单波段伪彩色", page)
        grid = QGridLayout(box)
        grid.setContentsMargins(10, 12, 10, 10)
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(8)

        self.pseudo_band_combo = self._new_band_combo(box)
        self.pseudo_ramp_combo = self._new_ramp_combo(box)
        self.pseudo_mode_combo = QComboBox(box)
        self.pseudo_mode_combo.addItem("连续（线性插值）", _PSEUDO_CONTINUOUS)
        self.pseudo_mode_combo.addItem("等间距分级", _PSEUDO_EQUAL)
        self.pseudo_mode_combo.currentIndexChanged.connect(
            self._on_pseudo_mode_changed
        )
        self.pseudo_class_spin = QSpinBox(box)
        self.pseudo_class_spin.setRange(2, 256)
        self.pseudo_class_spin.setValue(8)
        self.pseudo_class_spin.setToolTip("等间距分级时的级数（2–256）")
        self.pseudo_class_spin.valueChanged.connect(self._mark_dirty)
        self.pseudo_min = self._new_range_spin(box)
        self.pseudo_max = self._new_range_spin(box)
        self.pseudo_invert = QCheckBox("反转色带", box)
        self.pseudo_invert.toggled.connect(self._mark_dirty)
        stats_btn = QPushButton("按统计重新计算", box)
        stats_btn.clicked.connect(self._fill_pseudo_range)

        grid.addWidget(QLabel("波段", box), 0, 0)
        grid.addWidget(self.pseudo_band_combo, 0, 1, 1, 3)
        grid.addWidget(QLabel("色带", box), 1, 0)
        grid.addWidget(self.pseudo_ramp_combo, 1, 1, 1, 3)
        grid.addWidget(QLabel("插值方式", box), 2, 0)
        grid.addWidget(self.pseudo_mode_combo, 2, 1)
        grid.addWidget(QLabel("分级数", box), 2, 2)
        grid.addWidget(self.pseudo_class_spin, 2, 3)
        grid.addWidget(QLabel("最小值", box), 3, 0)
        grid.addWidget(self.pseudo_min, 3, 1)
        grid.addWidget(QLabel("最大值", box), 3, 2)
        grid.addWidget(self.pseudo_max, 3, 3)
        grid.addWidget(self.pseudo_invert, 4, 0, 1, 2)
        grid.addWidget(stats_btn, 4, 2, 1, 2)
        grid.addWidget(
            self._new_background_row(box, _MODE_PSEUDO), 5, 0, 1, 4
        )
        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(3, 1)
        layout.addWidget(box)
        layout.addStretch(1)
        self._sync_pseudo_mode()
        return page

    def _build_paletted_page(self) -> QWidget:
        page = QWidget(self)
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        top_box = QGroupBox("调色板", page)
        top = QGridLayout(top_box)
        top.setContentsMargins(10, 12, 10, 10)
        top.setHorizontalSpacing(8)
        top.setVerticalSpacing(8)
        self.palette_band_combo = self._new_band_combo(top_box)
        self.palette_ramp_combo = self._new_ramp_combo(top_box)
        self.palette_rat_combo = QComboBox(top_box)
        self.palette_rat_combo.setMinimumWidth(180)
        self.palette_rat_combo.setToolTip(
            "栅格属性表（RAT）里用于取值的字段；没有属性表时只能按像元值渲染"
        )
        self.palette_rat_combo.currentIndexChanged.connect(
            self._on_palette_rat_field_changed
        )
        self.palette_class_spin = QSpinBox(top_box)
        self.palette_class_spin.setRange(2, 256)
        self.palette_class_spin.setValue(8)
        self.palette_class_spin.setMinimumWidth(70)
        self.palette_class_spin.setToolTip(
            "按色带生成时的条目数；默认跟随值字段的不同取值个数"
        )
        # 第 1 列被波段下拉撑开，不给上限的话级数输入框会拉成一整行
        self.palette_class_spin.setMaximumWidth(100)
        self.palette_class_spin.valueChanged.connect(self._mark_dirty)
        self.palette_generate_btn = QPushButton("按色带生成", top_box)
        self.palette_generate_btn.setToolTip(
            "按波段最小 / 最大等间距生成离散值 → 颜色，适合整数编码栅格"
        )
        self.palette_generate_btn.clicked.connect(self._generate_palette)
        top.addWidget(QLabel("波段", top_box), 0, 0)
        top.addWidget(self.palette_band_combo, 0, 1)
        top.addWidget(QLabel("色带", top_box), 0, 2)
        top.addWidget(self.palette_ramp_combo, 0, 3)
        top.addWidget(QLabel("值字段", top_box), 1, 0)
        top.addWidget(self.palette_rat_combo, 1, 1, 1, 3)
        top.addWidget(QLabel("级数", top_box), 2, 0)
        top.addWidget(self.palette_class_spin, 2, 1)
        top.addWidget(self.palette_generate_btn, 2, 2, 1, 2)
        top.addWidget(self._new_background_row(top_box, _MODE_PALETTED), 3, 0, 1, 4)
        top.setColumnStretch(1, 1)
        top.setColumnStretch(3, 1)
        layout.addWidget(top_box)

        self.palette_table = QTableWidget(0, len(_PALETTE_HEADERS), page)
        self.palette_table.setHorizontalHeaderLabels(list(_PALETTE_HEADERS))
        self.palette_table.verticalHeader().setVisible(False)
        self.palette_table.setIconSize(_SWATCH_SIZE)
        self.palette_table.verticalHeader().setDefaultSectionSize(24)
        self.palette_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.palette_table.setSelectionMode(
            QAbstractItemView.SelectionMode.ExtendedSelection
        )
        self.palette_table.setEditTriggers(
            QAbstractItemView.EditTrigger.DoubleClicked
            | QAbstractItemView.EditTrigger.EditKeyPressed
        )
        header = self.palette_table.horizontalHeader()
        header.setSectionResizeMode(0, header.ResizeMode.Interactive)
        header.setSectionResizeMode(1, header.ResizeMode.Interactive)
        header.setSectionResizeMode(2, header.ResizeMode.Stretch)
        self.palette_table.setColumnWidth(_PALETTE_COL_VALUE, 110)
        self.palette_table.setColumnWidth(_PALETTE_COL_COLOR, 90)
        self.palette_table.setMinimumHeight(150)
        self.palette_table.itemChanged.connect(self._on_palette_item_changed)
        self.palette_table.itemDoubleClicked.connect(self._on_palette_double_clicked)
        layout.addWidget(self.palette_table, 1)

        ops = QHBoxLayout()
        ops.setSpacing(6)
        for text, slot, tip in (
            ("新增", self._add_palette_row, "新增一个值 → 颜色条目"),
            ("删除", self._delete_palette_rows, "删除选中的条目"),
            ("修改颜色", self._edit_palette_color, "修改选中条目的颜色"),
        ):
            button = QPushButton(text, page)
            button.setToolTip(tip)
            button.clicked.connect(slot)
            ops.addWidget(button)
        ops.addStretch(1)
        for text, slot, tip in (
            (
                "导入色彩映射…",
                self._import_color_map,
                "从色彩映射模板文件（.clr）导入值 → 颜色条目",
            ),
            (
                "导出色彩映射…",
                self._export_color_map,
                "把当前调色板导出为色彩映射模板文件（.clr）",
            ),
        ):
            button = QPushButton(text, page)
            button.setToolTip(tip)
            button.clicked.connect(slot)
            ops.addWidget(button)
        layout.addLayout(ops)
        return page

    def _build_hillshade_page(self) -> QWidget:
        page = QWidget(self)
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        box = QGroupBox("山体阴影", page)
        form = QFormLayout(box)
        form.setLabelAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        form.setHorizontalSpacing(8)
        self.hs_band_combo = self._new_band_combo(box)
        form.addRow("波段", self.hs_band_combo)
        self.hs_azimuth = QDoubleSpinBox(box)
        self.hs_azimuth.setRange(0.0, 360.0)
        self.hs_azimuth.setDecimals(1)
        self.hs_azimuth.setValue(315.0)
        self.hs_azimuth.valueChanged.connect(self._mark_dirty)
        form.addRow("光源方位角", self.hs_azimuth)
        self.hs_altitude = QDoubleSpinBox(box)
        self.hs_altitude.setRange(0.0, 90.0)
        self.hs_altitude.setDecimals(1)
        self.hs_altitude.setValue(45.0)
        self.hs_altitude.valueChanged.connect(self._mark_dirty)
        form.addRow("光源高度角", self.hs_altitude)
        self.hs_zfactor = QDoubleSpinBox(box)
        self.hs_zfactor.setRange(0.01, 100.0)
        self.hs_zfactor.setDecimals(2)
        self.hs_zfactor.setValue(1.0)
        self.hs_zfactor.valueChanged.connect(self._mark_dirty)
        form.addRow("Z 因子", self.hs_zfactor)
        self.hs_multi = QCheckBox("多方向光照", box)
        self.hs_multi.toggled.connect(self._mark_dirty)
        form.addRow("", self.hs_multi)
        form.addRow(
            "", self._new_background_row(box, _MODE_HILLSHADE)
        )
        layout.addWidget(box)
        layout.addStretch(1)
        return page

    # ------------------------------------------------------------- 读取

    def load(self, layer) -> None:
        """从 ``layer`` 读渲染器与波段信息，填控件。"""
        self._layer = layer
        self._loading = True
        self._dirty = False
        self._provider = None
        self._bands = 0
        self._stats_cache.clear()
        self._cumulative_cache.clear()
        self._pending_auto = {
            _MODE_MULTIBAND: True,
            _MODE_GRAY: True,
            _MODE_PSEUDO: True,
        }
        self.palette_table.setRowCount(0)

        if not isinstance(layer, QgsRasterLayer):
            self._set_unavailable("仅栅格图层支持渲染设置")
            self._loading = False
            return

        try:
            provider = layer.dataProvider()
            bands = int(layer.bandCount())
        except Exception:
            logger.debug("读取栅格图层信息失败", exc_info=True)
            provider = None
            bands = 0
        if provider is None or bands <= 0:
            self._set_unavailable(
                "该图层没有可用的波段（在线影像 / 瓦片图层由服务端渲染）"
            )
            self._loading = False
            return

        try:
            provider_type = str(layer.providerType()).lower()
        except Exception:  # noqa: BLE001
            provider_type = ""
        if provider_type in _REMOTE_PROVIDERS:
            self._set_unavailable(
                "在线影像 / 瓦片图层由服务端渲染，无法在本地调整渲染方式"
            )
            self._loading = False
            return

        self._provider = provider
        self._bands = bands
        self.mode_combo.setEnabled(True)
        self.stack.setEnabled(True)
        self._populate_band_combos()
        self._update_mode_availability()

        try:
            renderer = layer.renderer()
        except Exception:  # noqa: BLE001
            renderer = None

        mode = self._mode_for_renderer(renderer)
        if mode == _MODE_MULTIBAND and bands < 3:
            mode = _MODE_GRAY
        self._select_mode(mode)
        self._apply_renderer_state(renderer, mode)
        if renderer is not None:
            self._load_background_state(renderer)
        self._auto_fill_mode(mode)
        self._sync_mode_page()
        self._dirty = False
        self._loading = False

    def _mode_for_renderer(self, renderer) -> str:
        if isinstance(renderer, QgsMultiBandColorRenderer):
            return _MODE_MULTIBAND
        if isinstance(renderer, QgsSingleBandPseudoColorRenderer):
            return _MODE_PSEUDO
        if isinstance(renderer, QgsPalettedRasterRenderer):
            return _MODE_PALETTED
        if isinstance(renderer, QgsHillshadeRenderer):
            return _MODE_HILLSHADE
        if isinstance(renderer, QgsSingleBandGrayRenderer):
            return _MODE_GRAY
        return _MODE_MULTIBAND if self._bands >= 3 else _MODE_GRAY

    def _apply_renderer_state(self, renderer, mode: str) -> None:
        """把已有渲染器参数回填到对应页；非当前页只填默认值。"""
        try:
            if mode == _MODE_MULTIBAND and isinstance(
                renderer, QgsMultiBandColorRenderer
            ):
                self._load_multiband_state(renderer)
            elif mode == _MODE_GRAY and isinstance(
                renderer, QgsSingleBandGrayRenderer
            ) and not isinstance(renderer, QgsSingleBandPseudoColorRenderer):
                self._load_gray_state(renderer)
            elif mode == _MODE_PSEUDO and isinstance(
                renderer, QgsSingleBandPseudoColorRenderer
            ):
                self._load_pseudo_state(renderer)
            elif mode == _MODE_PALETTED:
                self._load_paletted_state(renderer)
            elif mode == _MODE_HILLSHADE and isinstance(
                renderer, QgsHillshadeRenderer
            ):
                self._load_hillshade_state(renderer)
            else:
                return
            self._pending_auto[mode] = False
        except Exception:
            logger.debug("回填栅格渲染器状态失败", exc_info=True)

    def _load_multiband_state(self, renderer) -> None:
        bands = [renderer.redBand(), renderer.greenBand(), renderer.blueBand()]
        ces = [
            renderer.redContrastEnhancement(),
            renderer.greenContrastEnhancement(),
            renderer.blueContrastEnhancement(),
        ]
        method = "minmax"
        for row, band, ce in zip(self.mb_bands, bands, ces, strict=True):
            if band > 0:
                self._select_band(row["band"], band)
            # 默认渲染器的 CE 是“拉伸到最小–最大”但范围尚未求值（min = max = 0），
            # 这时按统计补默认值；有显式范围才认为是用户/旧样式保存的手动范围。
            explicit = ce is not None and ce.maximumValue() > ce.minimumValue()
            if explicit:
                row["min"].setValue(ce.minimumValue())
                row["max"].setValue(ce.maximumValue())
            else:
                self._fill_row_range(row, "minmax")
            if ce is not None:
                algorithm = ce.contrastEnhancementAlgorithm()
                if algorithm == (
                    QgsContrastEnhancement.ContrastEnhancementAlgorithm.NoEnhancement
                ):
                    method = "none"
                elif explicit and method != "none":
                    method = "manual"
        self._set_combo_data(self.mb_stretch_combo, method)
        self._set_ranges_enabled(self.mb_bands, method == "manual")

    def _load_gray_state(self, renderer) -> None:
        band = renderer.inputBand()
        if band > 0:
            self._select_band(self.gray_band_combo, band)
        ce = renderer.contrastEnhancement()
        method = "minmax"
        explicit = ce is not None and ce.maximumValue() > ce.minimumValue()
        if explicit:
            self.gray_min.setValue(ce.minimumValue())
            self.gray_max.setValue(ce.maximumValue())
        else:
            self._fill_row_range(self.gray_row, "minmax")
        if ce is not None:
            algorithm = ce.contrastEnhancementAlgorithm()
            if algorithm == (
                QgsContrastEnhancement.ContrastEnhancementAlgorithm.NoEnhancement
            ):
                method = "none"
            elif explicit:
                method = "manual"
        self._set_combo_data(self.gray_stretch_combo, method)
        self._set_ranges_enabled([self.gray_row], method == "manual")
        with contextlib.suppress(Exception):
            self.gray_reverse.setChecked(
                renderer.gradient()
                == QgsSingleBandGrayRenderer.Gradient.WhiteToBlack
            )

    def _load_pseudo_state(self, renderer) -> None:
        band = renderer.inputBand()
        if band > 0:
            self._select_band(self.pseudo_band_combo, band)
        lo = renderer.classificationMin()
        hi = renderer.classificationMax()
        shader = renderer.shader()
        function = shader.rasterShaderFunction() if shader is not None else None
        items = []
        if isinstance(function, QgsColorRampShader):
            items = function.colorRampItemList()
            discrete = (
                function.colorRampType()
                == Qgis.ShaderInterpolationMethod.Discrete
            )
            self._set_combo_data(
                self.pseudo_mode_combo,
                _PSEUDO_EQUAL if discrete else _PSEUDO_CONTINUOUS,
            )
            if discrete:
                self.pseudo_class_spin.setValue(max(2, len(items)))
            self._sync_pseudo_mode()
        if items:
            if not (hi > lo):
                lo, hi = items[0].value, items[-1].value
            if hi > lo:
                self.pseudo_min.setValue(lo)
                self.pseudo_max.setValue(hi)
            name, inverted = self._match_ramp(items)
            if name:
                self._set_combo_data(self.pseudo_ramp_combo, name)
                self.pseudo_invert.setChecked(inverted)
        if not (self.pseudo_max.value() > self.pseudo_min.value()):
            self._fill_pseudo_range()

    def _load_paletted_state(self, renderer) -> None:
        band = 0
        classes = []
        if isinstance(renderer, QgsPalettedRasterRenderer):
            band = renderer.inputBand()
            classes = list(renderer.classes())
        if not band:
            band = self._band_with_color_table()
        if not band:
            band = self.palette_band_combo.currentData() or 1
        self._select_band(self.palette_band_combo, band)
        # 值字段下拉要跟着波段走：同一图层不同波段可能只有部分波段带 RAT
        self._refresh_palette_rat_combo()
        if classes:
            self._populate_palette_classes(classes)
            self._refresh_palette_class_default()
            return
        # 当前不是调色板渲染器：数据源自带 color table / RAT 时也先展示出来，
        # 用户切到本页时不用再手动读一次。
        self._populate_palette_from_provider(band)
        self._refresh_palette_class_default()

    def _load_hillshade_state(self, renderer) -> None:
        band = renderer.inputBand()
        if band > 0:
            self._select_band(self.hs_band_combo, band)
        self.hs_azimuth.setValue(renderer.azimuth())
        self.hs_altitude.setValue(renderer.altitude())
        self.hs_zfactor.setValue(renderer.zFactor())
        self.hs_multi.setChecked(renderer.multiDirectional())

    def _populate_band_combos(self) -> None:
        self._fill_band_combo(self.gray_band_combo, 1)
        self._fill_band_combo(self.pseudo_band_combo, 1)
        self._fill_band_combo(self.palette_band_combo, 1)
        self._fill_band_combo(self.hs_band_combo, 1)
        for row in self.mb_bands:
            self._fill_band_combo(
                row["band"], min(int(row["default"]), self._bands)
            )

    def _fill_band_combo(self, combo: QComboBox, select: int) -> None:
        combo.blockSignals(True)
        try:
            combo.clear()
            for band in range(1, self._bands + 1):
                combo.addItem(self._band_name(band), band)
            index = combo.findData(select)
            combo.setCurrentIndex(index if index >= 0 else 0)
        finally:
            combo.blockSignals(False)

    def _band_name(self, band: int) -> str:
        try:
            name = str(self._layer.bandName(band) or "").strip()
        except Exception:  # noqa: BLE001
            name = ""
        if name and name.lower() not in (f"band {band}", f"波段 {band}"):
            return f"波段 {band}（{name}）"
        return f"波段 {band}"

    def _select_band(self, combo: QComboBox, band: int) -> None:
        index = combo.findData(int(band))
        if index < 0:
            return
        combo.blockSignals(True)
        try:
            combo.setCurrentIndex(index)
        finally:
            combo.blockSignals(False)

    def _update_mode_availability(self) -> None:
        """少于 3 个波段时禁用 RGB；当前模式被禁用则退回灰度。"""
        # QSS 的 ``QComboBox QAbstractItemView::item:disabled`` 在本构建的下拉弹窗里
        # 不生效（实测禁用项仍是正常文字色）。直接在模型里给禁用项设置前景色，
        # 保证用户一眼看出「多波段彩色」选不了。
        disabled_color = self.palette().color(
            QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text
        )
        model = self.mode_combo.model()
        for index in range(self.mode_combo.count()):
            data = self.mode_combo.itemData(index)
            enabled = not (
                data == _MODE_MULTIBAND and self._bands < 3
            )
            item = model.item(index) if hasattr(model, "item") else None
            if item is None:
                continue
            flags = item.flags()
            if enabled:
                item.setFlags(flags | Qt.ItemFlag.ItemIsEnabled)
                item.setData(None, Qt.ItemDataRole.ForegroundRole)
            else:
                item.setFlags(flags & ~Qt.ItemFlag.ItemIsEnabled)
                item.setData(disabled_color, Qt.ItemDataRole.ForegroundRole)
        if (
            self.mode_combo.currentData() == _MODE_MULTIBAND
            and self._bands < 3
        ):
            self._select_mode(_MODE_GRAY)

    def _select_mode(self, mode: str) -> None:
        index = _MODE_INDEX.get(mode, 0)
        self.mode_combo.blockSignals(True)
        try:
            self.mode_combo.setCurrentIndex(index)
        finally:
            self.mode_combo.blockSignals(False)
        self._sync_mode_page()

    def _sync_mode_page(self) -> None:
        mode = self.mode_combo.currentData()
        page = self._pages.get(mode)
        if page is not None:
            self.stack.setCurrentWidget(page)
        self.hint_label.setText(_HINTS.get(mode, ""))
        self.hint_label.setVisible(True)
        self._sync_background_availability()

    # --------------------------------------------------------- 背景透明

    def _background_supported(self, mode: str | None = None) -> bool:
        """五种渲染方式都支持忽略背景。

        多波段按输出 RGB 匹配；调色板按条目颜色匹配；灰度 / 伪彩色把颜色
        反推成波段值；山体阴影则忽略栅格边缘的背景高程。
        """
        if mode is None:
            mode = self.mode_combo.currentData()
        return mode in _MODE_INDEX

    def _sync_background_availability(self) -> None:
        mode = self.mode_combo.currentData()
        supported = self._background_supported(mode)
        for key, (_widget, check, color_button) in self._background_rows.items():
            check.setEnabled(supported)
            check.setToolTip(_background_tip(key))
            color_button.setEnabled(supported and check.isChecked())
        # 兼容外部按 ``background_check`` 查询当前页可用性的写法：始终指向
        # 当前渲染方式的控件。
        row = self._background_rows.get(mode)
        if row is None:
            row = next(iter(self._background_rows.values()), None)
        if row is not None:
            self.background_check = row[1]
            self.background_color = row[2]

    def _load_background_state(self, renderer) -> None:
        """从渲染器回填「忽略背景色」：三值 / 单值透明，或调色板的透明 nodata。"""
        checked = False
        color = QColor(0, 0, 0)
        try:
            transparency = renderer.rasterTransparency()
        except (RuntimeError, AttributeError):
            transparency = None
        if transparency is not None:
            try:
                three = transparency.transparentThreeValuePixelList()
            except Exception:  # noqa: BLE001
                three = []
            try:
                single = transparency.transparentSingleValuePixelList()
            except Exception:  # noqa: BLE001
                single = []
            if three:
                pixel = three[0]
                color = QColor(
                    int(pixel.red), int(pixel.green), int(pixel.blue)
                )
                checked = color.isValid()
            elif single:
                value = float(single[0].min)
                checked = True
                if isinstance(renderer, QgsPalettedRasterRenderer):
                    # 调色板：值 → 条目颜色；值不在表里时让下面的 nodata
                    # 分支继续判断
                    checked = False
                    for cls in renderer.classes():
                        with contextlib.suppress(Exception):
                            if math.isclose(
                                float(cls.value),
                                value,
                                rel_tol=0.0,
                                abs_tol=1e-9,
                            ):
                                color = QColor(cls.color)
                                checked = color.isValid()
                                break
                elif isinstance(renderer, QgsSingleBandGrayRenderer):
                    color = _gray_color_for_value(renderer, value)
                elif isinstance(renderer, QgsSingleBandPseudoColorRenderer):
                    color = _pseudo_color_for_value(renderer, value)
                # 山体阴影的透明值是背景高程，没有对应颜色可回填
        if (
            not checked
            and isinstance(renderer, QgsPalettedRasterRenderer)
            and hasattr(renderer, "nodataColor")
        ):
            # 调色板模式勾选后会把无类别像元的 nodata 色设成透明；回填时
            # 用它记住勾选状态（RGB 值仍在，只是 alpha 为 0）。
            nodata = QColor(renderer.nodataColor())
            if nodata.isValid() and nodata.alpha() == 0:
                nodata.setAlpha(255)
                color = nodata
                checked = True
        for _widget, check, color_button in self._background_rows.values():
            check.blockSignals(True)
            color_button.blockSignals(True)
            try:
                check.setChecked(checked)
                if checked:
                    color_button.set_color(color, emit=False)
            finally:
                color_button.blockSignals(False)
                check.blockSignals(False)
        self._sync_background_availability()

    def _background_palette_values(self, color: QColor) -> list[float]:
        """调色板里与背景色相同的类值。

        用默认容差（±8）而不是严格相等：色带生成的调色板最浅 / 最深色常常
        是 ``#f7fbff`` 这类“看起来是白色”的颜色，严格匹配会让用户勾了
        「忽略背景色」却仍有白边。
        """
        values = []
        try:
            classes = self._palette_classes_from_table()
        except ValueError:
            return values
        for cls in classes:
            with contextlib.suppress(Exception):
                if _color_close(QColor(cls.color), color):
                    values.append(float(cls.value))
        return values

    def _apply_background_transparency(self, renderer) -> None:
        """按「忽略背景色」给新渲染器挂透明像素；未勾选时清掉旧设置。"""
        mode = self.mode_combo.currentData()
        row = self._background_rows.get(mode)
        if row is None:
            return
        check, color_button = row[1], row[2]
        transparency = QgsRasterTransparency()
        if check.isChecked():
            color = color_button.color()
            if isinstance(renderer, QgsMultiBandColorRenderer):
                # RGB：只有三个通道同时等于背景色才透明，避免把暗部单独抠掉
                pixel = QgsRasterTransparency.TransparentThreeValuePixel(
                    color.red(), color.green(), color.blue()
                )
                transparency.setTransparentThreeValuePixelList([pixel])
            else:
                if mode == _MODE_GRAY:
                    lo, hi = self._read_range(self.gray_row, "灰度")
                    ranges = _gray_background_ranges(
                        lo,
                        hi,
                        self.gray_reverse.isChecked(),
                        self.gray_stretch_combo.currentData(),
                        color,
                    )
                elif mode == _MODE_PSEUDO:
                    ranges = _pseudo_background_ranges(renderer, color)
                elif mode == _MODE_HILLSHADE:
                    ranges = [
                        (value, value)
                        for value in self._hillshade_background_values()
                    ]
                else:
                    ranges = [
                        (value, value)
                        for value in self._background_palette_values(color)
                    ]
                pixels = []
                for low, high in ranges:
                    pixel = QgsRasterTransparency.TransparentSingleValuePixel()
                    pixel.min = float(low)
                    pixel.max = float(high)
                    pixel.percentTransparent = 100.0
                    pixels.append(pixel)
                if pixels:
                    transparency.setTransparentSingleValuePixelList(pixels)
                if isinstance(renderer, QgsPalettedRasterRenderer):
                    # 调色板没有同色条目时不要弹窗：用色带生成的调色板几乎
                    # 永远匹配不到纯黑 / 纯白。无类别像元（背景值常常不在
                    # 调色板里）本来按 nodata 色渲染成黑边，勾选后一并透明。
                    nodata = QColor(color.red(), color.green(), color.blue(), 0)
                    renderer.setNodataColor(nodata)
        renderer.setRasterTransparency(transparency)

    def _hillshade_background_values(self) -> list[float]:
        """山体阴影的背景高程：波段 NoData + 栅格边缘最常见的值。

        山体阴影的输出灰度由坡度和光照决定，不能像灰度 / 伪彩色那样从
        颜色反推高程，因此改为识别真正位于影像四周的背景高程值。
        """
        band = self.hs_band_combo.currentData()
        provider = self._provider
        if not band or provider is None:
            return []
        values: list[float] = []
        with contextlib.suppress(Exception):
            nodata = float(provider.sourceNoDataValue(int(band)))
            if math.isfinite(nodata):
                values.append(nodata)
        edge = self._edge_value(int(band))
        if edge is not None and edge not in values:
            values.append(edge)
        return values

    def _edge_value(self, band: int) -> float | None:
        """降采样读取栅格边缘，返回出现最多的非 NoData 值。"""
        layer = self._layer
        provider = self._provider
        if layer is None or provider is None:
            return None
        size = 64
        try:
            block = provider.block(band, layer.extent(), size, size)
        except Exception:  # noqa: BLE001
            logger.debug("读取栅格边缘失败", exc_info=True)
            return None
        if block is None or not block.isValid():
            return None
        values: list[float] = []
        for index in range(size):
            for row, column in (
                (0, index),
                (size - 1, index),
                (index, 0),
                (index, size - 1),
            ):
                try:
                    if block.isNoData(row, column):
                        continue
                    value = float(block.value(row, column))
                except (RuntimeError, TypeError, ValueError):
                    continue
                if math.isfinite(value):
                    values.append(value)
        if not values:
            return None
        value, count = Counter(values).most_common(1)[0]
        # 边缘只有零星同值的多数情况不算背景（例如整幅都是数据的 DEM）
        if count < max(4, len(values) // 4):
            return None
        return value

    def _set_unavailable(self, message: str) -> None:
        self.mode_combo.setEnabled(False)
        self.stack.setEnabled(False)
        self.hint_label.setText(message)
        self.hint_label.setVisible(True)

    # ------------------------------------------------------------- 交互

    def _mark_dirty(self, *_args) -> None:
        if not self._loading:
            self._dirty = True

    def _on_mode_changed(self, *_args) -> None:
        if self._loading:
            self._sync_mode_page()
            return
        self._dirty = True
        self._sync_mode_page()
        mode = self.mode_combo.currentData()
        if mode == _MODE_PALETTED:
            self._refresh_palette_rat_combo()
            if self.palette_table.rowCount() == 0:
                band = self.palette_band_combo.currentData()
                if band:
                    self._populate_palette_from_provider(band)
            self._refresh_palette_class_default()
        self._auto_fill_mode(mode)

    def _on_band_changed(self, *_args) -> None:
        if self._loading:
            return
        self._dirty = True
        combo = self.sender()
        if combo is None:
            return
        for row in self.mb_bands:
            if combo is row["band"]:
                method = self.mb_stretch_combo.currentData()
                if method != "manual":
                    self._fill_row_range(row, method)
                return
        if combo is self.gray_band_combo:
            method = self.gray_stretch_combo.currentData()
            if method != "manual":
                self._fill_row_range(self.gray_row, method)
            return
        if combo is self.pseudo_band_combo:
            self._fill_pseudo_range()
            return
        if combo is self.palette_band_combo:
            # 换波段后 RAT 字段列表可能完全不同（有的波段没有属性表）
            self._refresh_palette_rat_combo()
            self._refresh_palette_class_default()

    def _auto_fill_mode(self, mode: str) -> None:
        if not self._pending_auto.get(mode, False):
            return
        if mode == _MODE_MULTIBAND:
            method = self.mb_stretch_combo.currentData()
            for row in self.mb_bands:
                self._fill_row_range(row, method)
            self._set_ranges_enabled(self.mb_bands, method == "manual")
        elif mode == _MODE_GRAY:
            method = self.gray_stretch_combo.currentData()
            self._fill_row_range(self.gray_row, method)
            self._set_ranges_enabled([self.gray_row], method == "manual")
        elif mode == _MODE_PSEUDO:
            self._fill_pseudo_range()
        self._pending_auto[mode] = False

    def _on_stretch_changed(self, kind: str) -> None:
        if self._loading:
            return
        self._dirty = True
        rows = self.mb_bands if kind == "mb" else [self.gray_row]
        combo = (
            self.mb_stretch_combo if kind == "mb" else self.gray_stretch_combo
        )
        method = combo.currentData()
        self._set_ranges_enabled(rows, method == "manual")
        if method != "manual":
            for row in rows:
                self._fill_row_range(row, method)

    def _recompute_ranges(self, kind: str, manual_stats: bool = False) -> None:
        rows = self.mb_bands if kind == "mb" else [self.gray_row]
        combo = (
            self.mb_stretch_combo if kind == "mb" else self.gray_stretch_combo
        )
        method = combo.currentData()
        if manual_stats and method in ("manual", "none"):
            # “按统计重新计算”在手动 / 不拉伸下没有可用的自动范围，退回最小–最大
            method = "minmax"
        for row in rows:
            self._fill_row_range(row, method)
        if not self._loading:
            self._dirty = True

    def _fill_row_range(self, row: dict, method: str) -> None:
        band = row["band"].currentData()
        if not band:
            return
        fallback = self._data_type_range(band)
        lo, hi = self._stretch_range(band, method, fallback)
        row["min"].setValue(lo)
        row["max"].setValue(hi)

    def _fill_pseudo_range(self) -> None:
        band = self.pseudo_band_combo.currentData()
        if not band:
            return
        lo, hi = self._stretch_range(
            band, "minmax", self._data_type_range(band)
        )
        self.pseudo_min.setValue(lo)
        self.pseudo_max.setValue(hi)
        if not self._loading:
            self._dirty = True

    def _on_pseudo_mode_changed(self, *_args) -> None:
        self._sync_pseudo_mode()
        self._mark_dirty()

    def _sync_pseudo_mode(self) -> None:
        equal = self.pseudo_mode_combo.currentData() == _PSEUDO_EQUAL
        self.pseudo_class_spin.setEnabled(equal)

    def _set_ranges_enabled(self, rows: list[dict], enabled: bool) -> None:
        for row in rows:
            row["min"].setEnabled(enabled)
            row["max"].setEnabled(enabled)

    # ------------------------------------------------------- 范围 / 统计

    def _data_type_range(self, band: int) -> tuple[float, float]:
        try:
            data_type = int(self._provider.dataType(band))
        except Exception:  # noqa: BLE001
            data_type = 0
        return _DATA_TYPE_RANGES.get(data_type, (0.0, 255.0))

    def _band_stats(self, band: int):
        if band in self._stats_cache:
            return self._stats_cache[band]
        stats = _read_band_stats(self._provider, band)
        self._stats_cache[band] = stats
        return stats

    def _cumulative_range(self, band: int):
        if band in self._cumulative_cache:
            return self._cumulative_cache[band]
        result = _read_cumulative_range(self._provider, band)
        self._cumulative_cache[band] = result
        return result

    def _stretch_range(
        self, band: int, method: str, fallback: tuple[float, float]
    ) -> tuple[float, float]:
        if method in ("none", "manual"):
            return fallback
        if method == "percent":
            result = self._cumulative_range(band)
            if result is not None:
                return result
            method = "minmax"
        stats = self._band_stats(band)
        if stats is not None:
            if method == "stddev":
                try:
                    lo = stats.mean - 2.0 * stats.stdDev
                    hi = stats.mean + 2.0 * stats.stdDev
                except Exception:  # noqa: BLE001
                    lo = hi = None
                result = _valid_range(lo, hi)
                if result is not None:
                    return result
            try:
                result = _valid_range(stats.minimumValue, stats.maximumValue)
            except Exception:  # noqa: BLE001
                result = None
            if result is not None:
                return result
        return fallback

    def _band_with_color_table(self) -> int:
        for band in range(1, self._bands + 1):
            try:
                if self._provider.colorTable(band):
                    return band
            except Exception:  # noqa: BLE001
                continue
        return 0

    def _populate_palette_from_provider(self, band: int) -> None:
        try:
            table = list(self._provider.colorTable(band))
        except Exception:  # noqa: BLE001
            table = []
        if not table:
            # 没有 color table 时退回 RAT：分类栅格常用 RAT 存值 / 名称 / 颜色
            self._populate_palette_from_rat()
            return
        classes = [
            QgsPalettedRasterRenderer.Class(
                float(item.value), QColor(item.color), str(item.label or "")
            )
            for item in table
        ]
        self._populate_palette_classes(classes)

    # --------------------------------------------------------- RAT 值字段

    @staticmethod
    def _usage(name: str) -> int:
        """``Qgis.RasterAttributeTableFieldUsage`` 成员的整数形式。"""
        usage = getattr(Qgis.RasterAttributeTableFieldUsage, name, None)
        return int(usage) if usage is not None else -1

    def _attribute_table(self, band):
        """取波段的 RAT；不存在 / 无效时返回 ``None``。"""
        if self._layer is None:
            return None
        try:
            band = int(band)
        except (TypeError, ValueError):
            return None
        if band <= 0:
            return None
        try:
            if int(self._layer.attributeTableCount()) <= 0:
                return None
            table = self._layer.attributeTable(band)
            if table is None:
                return None
            valid = table.isValid()
            if isinstance(valid, tuple):
                valid = bool(valid[0])
            return table if valid else None
        except Exception:  # 数据源不支持 RAT 时按没有处理
            logger.debug("读取栅格属性表失败：band=%s", band, exc_info=True)
            return None

    def _rat_value_fields(self, table) -> list[tuple[int, object]]:
        """可用作取值来源的字段：排除名称 / 计数 / 颜色等非数值字段。"""
        skip = {
            self._usage(name)
            for name in (
                "Name",
                "PixelCount",
                "Red",
                "Green",
                "Blue",
                "Alpha",
                "RedMin",
                "RedMax",
                "GreenMin",
                "GreenMax",
                "BlueMin",
                "BlueMax",
                "AlphaMin",
                "AlphaMax",
            )
        }
        fields = []
        try:
            for index, field in enumerate(table.fields()):
                if int(field.usage) in skip:
                    continue
                with contextlib.suppress(Exception):
                    if field.isColor() or field.isRamp():
                        continue
                fields.append((index, field))
        except Exception:
            logger.debug("枚举栅格属性表字段失败", exc_info=True)
        return fields

    def _default_rat_column(self, fields: list[tuple[int, object]]) -> int:
        """默认值字段：优先 MinMax（QGIS 的 Value），其次名为 Value，最后第一个。"""
        minmax = self._usage("MinMax")
        for index, field in fields:
            with contextlib.suppress(Exception):
                if int(field.usage) == minmax:
                    return index
        for index, field in fields:
            with contextlib.suppress(Exception):
                if str(field.name).strip().lower() == "value":
                    return index
        return fields[0][0]

    def _refresh_palette_rat_combo(self) -> None:
        """按当前波段重建「值字段」下拉（没有 RAT 时禁用并说明原因）。"""
        table = self._attribute_table(self.palette_band_combo.currentData())
        combo = self.palette_rat_combo
        combo.blockSignals(True)
        try:
            combo.clear()
            if table is None:
                combo.addItem("（无属性表，按像元值）", None)
                combo.setEnabled(False)
                return
            fields = self._rat_value_fields(table)
            # 再按实际数据筛一遍：字符串字段不可能是取值来源
            try:
                rows = table.data()
            except Exception:  # noqa: BLE001
                rows = []
            numeric = []
            for index, field in fields:
                if not rows or self._rat_field_is_numeric(table, index, rows):
                    numeric.append((index, field))
            fields = numeric
            if not fields:
                combo.addItem("（属性表没有可用的数值字段）", None)
                combo.setEnabled(False)
                return
            for index, field in fields:
                combo.addItem(str(field.name), index)
            default = self._default_rat_column(fields)
            position = combo.findData(default)
            combo.setCurrentIndex(position if position >= 0 else 0)
            combo.setEnabled(True)
        finally:
            combo.blockSignals(False)

    @staticmethod
    def _rat_field_is_numeric(table, column: int, rows) -> bool:
        """字段是否至少有一行能转成数值（RAT 字段类型在不同驱动下不一致）。"""
        for row in range(len(rows)):
            try:
                float(table.value(row, column))
            except (TypeError, ValueError):
                continue
            return True
        return False

    def _on_palette_rat_field_changed(self, *_args) -> None:
        if self._loading:
            return
        if self.palette_rat_combo.currentData() is None:
            return
        if self._populate_palette_from_rat():
            self._refresh_palette_class_default()
            self._dirty = True

    def _populate_palette_from_rat(self) -> bool:
        """按当前「值字段」从 RAT 生成调色板条目；没有 RAT 时返回 False。"""
        column = self.palette_rat_combo.currentData()
        if column is None:
            return False
        table = self._attribute_table(self.palette_band_combo.currentData())
        if table is None:
            return False
        try:
            rows = table.data()
        except Exception:
            logger.debug("读取栅格属性表数据失败", exc_info=True)
            return False
        if not rows:
            return False
        name_column = None
        try:
            for index, field in enumerate(table.fields()):
                if int(field.usage) == self._usage("Name"):
                    name_column = index
                    break
        except Exception:  # noqa: BLE001
            name_column = None
        try:
            has_color = bool(table.hasColor())
        except Exception:  # noqa: BLE001
            has_color = False
        ramp = _make_ramp(self.palette_ramp_combo.currentData())
        classes = []
        for row in range(len(rows)):
            try:
                value = float(table.value(row, column))
            except (TypeError, ValueError):
                continue
            label = ""
            if name_column is not None:
                with contextlib.suppress(Exception):
                    label = str(table.value(row, name_column) or "")
            color = None
            if has_color:
                with contextlib.suppress(Exception):
                    candidate = QColor(table.color(row))
                    if candidate.isValid():
                        color = candidate
            if color is None:
                ratio = row / (len(rows) - 1) if len(rows) > 1 else 0.0
                color = ramp.color(ratio) if ramp is not None else QColor(120, 120, 120)
            classes.append(
                QgsPalettedRasterRenderer.Class(
                    value, color, label or _format_value(value)
                )
            )
        if not classes:
            return False
        self._populate_palette_classes(classes)
        return True

    def _palette_rat_values(self) -> list[float] | None:
        """当前「值字段」里出现过的不同数值（升序）；没有 RAT 时返回 ``None``。"""
        column = self.palette_rat_combo.currentData()
        if column is None:
            return None
        table = self._attribute_table(self.palette_band_combo.currentData())
        if table is None:
            return None
        try:
            rows = table.data()
        except Exception:  # noqa: BLE001
            return None
        values = set()
        for row in range(len(rows)):
            try:
                values.add(round(float(table.value(row, column)), 9))
            except (TypeError, ValueError):
                continue
        return sorted(values) or None

    def _palette_value_count(self) -> int | None:
        """值字段（没有 RAT 时按像元范围）的不同取值个数，用于默认「级数」。

        默认 8 对分类编码栅格常常不够：按色带生成时会跳过中间的编码值，
        图例与数据对不上。整型栅格能直接数出范围里有多少个整数。
        """
        values = self._palette_rat_values()
        if values:
            return len(values)
        band = self.palette_band_combo.currentData()
        if not band or self._is_float_band(band):
            return None
        lo, hi = self._stretch_range(
            band, "minmax", self._data_type_range(band)
        )
        try:
            span = math.floor(hi) - math.ceil(lo) + 1
        except (TypeError, ValueError, OverflowError):
            return None
        return span if span >= 2 else None

    def _refresh_palette_class_default(self) -> None:
        """按值字段回填「级数」，不把回填当成用户改动。"""
        count = self._palette_value_count()
        if count is None:
            return
        spin = self.palette_class_spin
        value = max(2, min(int(spin.maximum()), int(count)))
        if spin.value() == value:
            return
        spin.blockSignals(True)
        try:
            spin.setValue(value)
        finally:
            spin.blockSignals(False)

    # ----------------------------------------------------------- 调色板表

    def _populate_palette_classes(self, classes) -> None:
        self.palette_table.blockSignals(True)
        try:
            ordered = sorted(classes, key=lambda cls: float(cls.value))
            self.palette_table.setRowCount(len(ordered))
            for row, cls in enumerate(ordered):
                color = QColor(cls.color)
                value_item = QTableWidgetItem(_format_value(float(cls.value)))
                color_item = QTableWidgetItem()
                _set_palette_color(color_item, color)
                color_item.setFlags(
                    color_item.flags() & ~Qt.ItemFlag.ItemIsEditable
                )
                label_item = QTableWidgetItem(str(cls.label or ""))
                self.palette_table.setItem(row, _PALETTE_COL_VALUE, value_item)
                self.palette_table.setItem(row, _PALETTE_COL_COLOR, color_item)
                self.palette_table.setItem(row, _PALETTE_COL_LABEL, label_item)
        finally:
            self.palette_table.blockSignals(False)

    def _palette_classes_from_table(self) -> list:
        classes = []
        for row in range(self.palette_table.rowCount()):
            value_item = self.palette_table.item(row, _PALETTE_COL_VALUE)
            color_item = self.palette_table.item(row, _PALETTE_COL_COLOR)
            label_item = self.palette_table.item(row, _PALETTE_COL_LABEL)
            if value_item is None or color_item is None:
                continue
            text = value_item.text().strip()
            if not text:
                continue
            try:
                value = float(text)
            except ValueError:
                raise _InputError(
                    f"第 {row + 1} 行的值不是数字：{text}"
                ) from None
            color = QColor(
                str(color_item.data(Qt.ItemDataRole.UserRole) or "")
            )
            if not color.isValid():
                color = color_item.background().color()
            if not color.isValid():
                color = QColor(200, 200, 200)
            label = label_item.text().strip() if label_item is not None else ""
            classes.append(
                QgsPalettedRasterRenderer.Class(value, color, label)
            )
        unique = {float(cls.value): cls for cls in classes}
        return [unique[key] for key in sorted(unique)]

    def _on_palette_item_changed(self, *_args) -> None:
        self._mark_dirty()

    def _on_palette_double_clicked(self, item) -> None:
        if item.column() != _PALETTE_COL_COLOR:
            return
        color = QColor(str(item.data(Qt.ItemDataRole.UserRole) or ""))
        if not color.isValid():
            color = item.background().color()
        chosen = pick_color(self, color)
        if chosen is None:
            return
        _set_palette_color(item, chosen)
        self._dirty = True

    def _add_palette_row(self) -> None:
        row = self.palette_table.rowCount()
        value = row
        try:
            values = [
                float(
                    self.palette_table.item(r, _PALETTE_COL_VALUE).text()
                )
                for r in range(row)
                if self.palette_table.item(r, _PALETTE_COL_VALUE) is not None
            ]
            if values:
                value = max(values) + 1
        except (TypeError, ValueError):
            pass
        ramp = _make_ramp(self.palette_ramp_combo.currentData())
        color = ramp.color(0.5) if ramp is not None else QColor(200, 200, 200)
        self.palette_table.blockSignals(True)
        try:
            self.palette_table.insertRow(row)
            self.palette_table.setItem(
                row, _PALETTE_COL_VALUE, QTableWidgetItem(_format_value(value))
            )
            color_item = QTableWidgetItem()
            _set_palette_color(color_item, color)
            color_item.setFlags(
                color_item.flags() & ~Qt.ItemFlag.ItemIsEditable
            )
            self.palette_table.setItem(row, _PALETTE_COL_COLOR, color_item)
            self.palette_table.setItem(
                row, _PALETTE_COL_LABEL, QTableWidgetItem("")
            )
        finally:
            self.palette_table.blockSignals(False)
        self._dirty = True

    def _delete_palette_rows(self) -> None:
        rows = sorted(
            {index.row() for index in self.palette_table.selectedIndexes()},
            reverse=True,
        )
        if not rows:
            return
        self.palette_table.blockSignals(True)
        try:
            for row in rows:
                self.palette_table.removeRow(row)
        finally:
            self.palette_table.blockSignals(False)
        self._dirty = True

    def _edit_palette_color(self) -> None:
        rows = sorted(
            {index.row() for index in self.palette_table.selectedIndexes()}
        )
        if not rows:
            return
        item = self.palette_table.item(rows[0], _PALETTE_COL_COLOR)
        if item is None:
            return
        color = QColor(str(item.data(Qt.ItemDataRole.UserRole) or ""))
        if not color.isValid():
            color = item.background().color()
        chosen = pick_color(self, color)
        if chosen is None:
            return
        self.palette_table.blockSignals(True)
        try:
            for row in rows:
                target = self.palette_table.item(row, _PALETTE_COL_COLOR)
                if target is None:
                    continue
                _set_palette_color(target, chosen)
        finally:
            self.palette_table.blockSignals(False)
        self._dirty = True

    # --------------------------------------------------- 色彩映射导入导出

    def _import_color_map(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "导入色彩映射",
            self._last_color_map_dir,
            "色彩映射文件 (*.clr *.txt);;所有文件 (*)",
        )
        if not path:
            return
        self._last_color_map_dir = os.path.dirname(path)
        self._import_color_map_from(path)

    def _import_color_map_from(self, path: str) -> bool:
        """从色彩映射模板文件（``.clr``）读取值 → 颜色并填入表格。"""
        try:
            result = QgsRasterRendererUtils.parseColorMapFile(path)
        except Exception as exc:
            logger.warning("导入色彩映射失败：%s", exc, exc_info=True)
            MessageBox.warning(
                self, "图层属性", f"导入色彩映射失败：{exc}"
            )
            return False
        # 返回 (ok, items, interpolation, errors)；防御不同版本少一个字段
        ok = bool(result[0]) if result else False
        items = list(result[1]) if len(result) > 1 else []
        errors = list(result[-1]) if len(result) > 2 else []
        if not ok:
            detail = "；".join(str(error) for error in errors if error) or "文件格式无法识别"
            MessageBox.warning(self, "图层属性", f"导入色彩映射失败：{detail}")
            return False
        classes = [
            QgsPalettedRasterRenderer.Class(
                float(item.value), QColor(item.color), str(item.label or "")
            )
            for item in items
        ]
        if not classes:
            MessageBox.warning(self, "图层属性", "色彩映射文件里没有可用的颜色条目。")
            return False
        self._populate_palette_classes(classes)
        self._dirty = True
        return True

    def _export_color_map(self) -> None:
        try:
            classes = self._palette_classes_from_table()
        except ValueError as exc:
            MessageBox.warning(self, "图层属性", str(exc))
            return
        if not classes:
            MessageBox.warning(self, "图层属性", "调色板为空，没有可导出的颜色条目。")
            return
        default_name = os.path.join(self._last_color_map_dir, "color_map.clr")
        path, _ = QFileDialog.getSaveFileName(
            self,
            "导出色彩映射",
            default_name,
            "色彩映射文件 (*.clr);;所有文件 (*)",
        )
        if not path:
            return
        if not os.path.splitext(path)[1]:
            path += ".clr"
        self._last_color_map_dir = os.path.dirname(path)
        self._export_color_map_to(path, classes)

    def _export_color_map_to(self, path: str, classes=None) -> bool:
        """把当前调色板写成色彩映射模板文件（``.clr``）。"""
        if classes is None:
            try:
                classes = self._palette_classes_from_table()
            except ValueError as exc:
                MessageBox.warning(self, "图层属性", str(exc))
                return False
        if not classes:
            MessageBox.warning(self, "图层属性", "调色板为空，没有可导出的颜色条目。")
            return False
        items = [
            QgsColorRampShader.ColorRampItem(
                float(cls.value), QColor(cls.color), str(cls.label or "")
            )
            for cls in classes
        ]
        try:
            saved = QgsRasterRendererUtils.saveColorMapFile(
                path, items, Qgis.ShaderInterpolationMethod.Discrete
            )
        except Exception as exc:
            logger.warning("导出色彩映射失败：%s", exc, exc_info=True)
            MessageBox.warning(self, "图层属性", f"导出色彩映射失败：{exc}")
            return False
        if not saved:
            MessageBox.warning(
                self, "图层属性", "导出色彩映射失败：无法写入目标文件。"
            )
            return False
        return True

    def _palette_labels(self) -> dict[float, str]:
        """当前表格里「值 → 标签」，生成调色板时保留原有名称。"""
        labels = {}
        for row in range(self.palette_table.rowCount()):
            value_item = self.palette_table.item(row, _PALETTE_COL_VALUE)
            label_item = self.palette_table.item(row, _PALETTE_COL_LABEL)
            if value_item is None:
                continue
            try:
                value = round(float(value_item.text().strip()), 9)
            except ValueError:
                continue
            label = label_item.text().strip() if label_item is not None else ""
            if label:
                labels[value] = label
        return labels

    def _generate_palette(self) -> None:
        band = self.palette_band_combo.currentData()
        ramp_name = self.palette_ramp_combo.currentData()
        if not band:
            return
        ramp = _make_ramp(ramp_name)
        if ramp is None:
            MessageBox.warning(self, "图层属性", "没有可用的色带，无法生成调色板。")
            return
        count = max(2, int(self.palette_class_spin.value()))
        labels = self._palette_labels()
        rat_values = self._palette_rat_values()
        if rat_values:
            # 有「值字段」时按字段里实际存在的值生成：编码 10 / 20 / 30 不能被
            # 换成像元范围算出的 0 / 1 / 2。
            lo, hi = rat_values[0], rat_values[-1]
            if len(rat_values) <= count:
                values = list(rat_values)
            else:
                values = sorted(
                    {
                        rat_values[
                            round(index * (len(rat_values) - 1) / (count - 1))
                        ]
                        for index in range(count)
                    }
                )
        else:
            lo, hi = self._stretch_range(
                band, "minmax", self._data_type_range(band)
            )
            if self._is_float_band(band):
                values = [
                    lo + (hi - lo) * (index / (count - 1))
                    for index in range(count)
                ]
            else:
                # 整数栅格按整数取值：范围里不够 count 个值时就用全部（不虚构不存在的值）
                first, last = math.ceil(lo), math.floor(hi)
                span = last - first
                if span + 1 <= count:
                    values = [float(first + step) for step in range(span + 1)]
                else:
                    values = [
                        float(first + round(span * index / (count - 1)))
                        for index in range(count)
                    ]
        classes = []
        seen: set[float] = set()
        for value in values:
            if value in seen:
                continue
            seen.add(value)
            ratio = (value - lo) / (hi - lo) if hi > lo else 0.5
            classes.append(
                QgsPalettedRasterRenderer.Class(
                    value,
                    ramp.color(ratio),
                    labels.get(round(value, 9)) or _format_value(value),
                )
            )
        if not classes:
            return
        self._populate_palette_classes(classes)
        self._dirty = True

    def _is_float_band(self, band: int) -> bool:
        try:
            data_type = int(self._provider.dataType(band))
        except Exception:  # noqa: BLE001
            return False
        float_types = {
            int(getattr(Qgis.DataType, name))
            for name in ("Float32", "Float64")
            if getattr(Qgis.DataType, name, None) is not None
        }
        return data_type in float_types

    # ------------------------------------------------------------- 写回

    def apply(self, layer) -> bool:
        """把控件值写回图层；用户没动过本页时什么都不做。"""
        if not isinstance(layer, QgsRasterLayer):
            return True
        if not self._dirty:
            return True
        try:
            provider = layer.dataProvider()
            bands = int(layer.bandCount())
        except Exception:  # noqa: BLE001
            return True
        if provider is None or bands <= 0:
            return True
        self._provider = provider
        self._bands = bands
        mode = self.mode_combo.currentData()
        try:
            if mode == _MODE_MULTIBAND:
                renderer = self._build_multiband_renderer(provider)
            elif mode == _MODE_GRAY:
                renderer = self._build_gray_renderer(provider)
            elif mode == _MODE_PSEUDO:
                renderer = self._build_pseudo_renderer(provider)
            elif mode == _MODE_PALETTED:
                renderer = self._build_paletted_renderer(provider)
            elif mode == _MODE_HILLSHADE:
                renderer = self._build_hillshade_renderer(provider)
            else:
                return True
            self._apply_background_transparency(renderer)
            layer.setRenderer(renderer)
            # 不在信号栈里同步重绘：外壳 apply 完会用 QTimer.singleShot(0) 统一刷新
            self._dirty = False
            return True
        except _InputError as exc:
            # 调色板为空 / 范围无效等是用户可以直接改的输入问题：只提示，
            # 不记 ERROR 堆栈（用户报过“应用栅格渲染设置失败 + Traceback”）。
            logger.debug("栅格渲染设置未通过校验：%s", exc)
            MessageBox.warning(self, "图层属性", str(exc))
            return False
        except Exception as exc:
            logger.error("应用栅格渲染设置失败: %s", exc, exc_info=True)
            MessageBox.warning(self, "图层属性", f"应用栅格渲染设置失败：{exc}")
            return False

    def _read_range(self, row: dict, label: str) -> tuple[float, float]:
        lo = row["min"].value()
        hi = row["max"].value()
        if not (math.isfinite(lo) and math.isfinite(hi)):
            raise _InputError(f"{label}的最小值 / 最大值无效")
        if lo >= hi:
            raise _InputError(f"{label}的最小值必须小于最大值")
        return lo, hi

    def _make_ce(
        self,
        provider,
        band: int,
        lo: float,
        hi: float,
        method: str,
    ) -> QgsContrastEnhancement:
        try:
            data_type = provider.dataType(band)
        except Exception:  # noqa: BLE001
            data_type = Qgis.DataType.Byte
        enhancement = QgsContrastEnhancement(data_type)
        enhancement.setMinimumValue(float(lo))
        enhancement.setMaximumValue(float(hi))
        enhancement.setContrastEnhancementAlgorithm(_stretch_algorithm(method))
        return enhancement

    def _build_multiband_renderer(self, provider) -> QgsMultiBandColorRenderer:
        values = []
        for row in self.mb_bands:
            band = row["band"].currentData()
            if not band:
                raise _InputError("请选择红 / 绿 / 蓝通道对应的波段")
            lo, hi = self._read_range(row, f"{row['label']}通道")
            values.append((int(band), lo, hi))
        method = self.mb_stretch_combo.currentData()
        renderer = QgsMultiBandColorRenderer(
            provider, values[0][0], values[1][0], values[2][0]
        )
        setters = (
            renderer.setRedContrastEnhancement,
            renderer.setGreenContrastEnhancement,
            renderer.setBlueContrastEnhancement,
        )
        for setter, (band, lo, hi) in zip(setters, values, strict=True):
            setter(self._make_ce(provider, band, lo, hi, method))
        return renderer

    def _build_gray_renderer(self, provider) -> QgsSingleBandGrayRenderer:
        band = self.gray_band_combo.currentData()
        if not band:
            raise _InputError("请选择波段")
        lo, hi = self._read_range(self.gray_row, "灰度")
        renderer = QgsSingleBandGrayRenderer(provider, int(band))
        renderer.setContrastEnhancement(
            self._make_ce(
                provider,
                int(band),
                lo,
                hi,
                self.gray_stretch_combo.currentData(),
            )
        )
        renderer.setGradient(
            QgsSingleBandGrayRenderer.Gradient.WhiteToBlack
            if self.gray_reverse.isChecked()
            else QgsSingleBandGrayRenderer.Gradient.BlackToWhite
        )
        return renderer

    def _build_pseudo_renderer(
        self, provider
    ) -> QgsSingleBandPseudoColorRenderer:
        band = self.pseudo_band_combo.currentData()
        if not band:
            raise _InputError("请选择波段")
        lo, hi = self._read_range(
            {"min": self.pseudo_min, "max": self.pseudo_max}, "伪彩色"
        )
        ramp_name = self.pseudo_ramp_combo.currentData()
        if not ramp_name:
            raise _InputError("没有可用的色带，无法构建伪彩色渲染")
        shader = self._build_color_shader(lo, hi, ramp_name)
        renderer = QgsSingleBandPseudoColorRenderer(provider, int(band), shader)
        renderer.setClassificationMin(lo)
        renderer.setClassificationMax(hi)
        return renderer

    def _build_color_shader(
        self, lo: float, hi: float, ramp_name: str
    ) -> QgsRasterShader:
        ramp = _make_ramp(ramp_name)
        if ramp is None:
            raise _InputError(f"找不到色带：{ramp_name}")
        invert = self.pseudo_invert.isChecked()
        equal = self.pseudo_mode_combo.currentData() == _PSEUDO_EQUAL
        count = (
            max(2, int(self.pseudo_class_spin.value()))
            if equal
            else _CONTINUOUS_STOPS
        )
        items = []
        for index in range(count):
            ratio = index / (count - 1)
            value = lo + (hi - lo) * ratio
            color = ramp.color(1.0 - ratio if invert else ratio)
            items.append(
                QgsColorRampShader.ColorRampItem(
                    value, color, _format_value(value)
                )
            )
        function = QgsColorRampShader(
            lo,
            hi,
            ramp,
            (
                Qgis.ShaderInterpolationMethod.Discrete
                if equal
                else Qgis.ShaderInterpolationMethod.Linear
            ),
            (
                Qgis.ShaderClassificationMethod.EqualInterval
                if equal
                else Qgis.ShaderClassificationMethod.Continuous
            ),
        )
        function.setColorRampItemList(items)
        shader = QgsRasterShader()
        shader.setRasterShaderFunction(function)
        return shader

    def _match_ramp(self, items) -> tuple[str | None, bool]:
        """已有色阶首尾颜色 → 内置色带名（含是否反向），匹配不到返回 None。"""
        if len(items) < 2:
            return None, False
        first = QColor(items[0].color)
        last = QColor(items[-1].color)
        for name in _ramp_names():
            ramp = _make_ramp(name)
            if ramp is None:
                continue
            start = ramp.color(0.0)
            end = ramp.color(1.0)
            if _color_close(start, first) and _color_close(end, last):
                return name, False
            if _color_close(start, last) and _color_close(end, first):
                return name, True
        return None, False

    def _build_paletted_renderer(self, provider) -> QgsPalettedRasterRenderer:
        band = self.palette_band_combo.currentData()
        if not band:
            raise _InputError("请选择波段")
        classes = self._palette_classes_from_table()
        if not classes:
            raise _InputError("调色板为空，请先添加条目或点「按色带生成」")
        return QgsPalettedRasterRenderer(provider, int(band), classes)

    def _build_hillshade_renderer(self, provider) -> QgsHillshadeRenderer:
        band = self.hs_band_combo.currentData()
        if not band:
            raise _InputError("请选择波段")
        renderer = QgsHillshadeRenderer(
            provider,
            int(band),
            float(self.hs_azimuth.value()),
            float(self.hs_altitude.value()),
        )
        renderer.setZFactor(float(self.hs_zfactor.value()))
        renderer.setMultiDirectional(self.hs_multi.isChecked())
        return renderer

    def _set_combo_data(self, combo: QComboBox, data) -> None:
        index = combo.findData(data)
        if index < 0:
            return
        combo.blockSignals(True)
        try:
            combo.setCurrentIndex(index)
        finally:
            combo.blockSignals(False)
