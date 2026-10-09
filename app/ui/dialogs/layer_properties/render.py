"""图层属性「渲染」页（简单符号 + 按字段值分类）。

「渲染方式」两种，可切换：

- **单一符号**：整层一个符号。按几何类型只暴露「简单符号」的常用项——
  面（填充颜色 / 填充样式、描边颜色 / 宽度 / 样式）、线（颜色 / 线宽 / 线型）、
  点（形状 / 颜色 / 大小、描边颜色 / 宽度）。
- **分类（按字段值）**：选字段 → 按其去重值分类 → 每一类各配一套上面那些项。
  类别列在表格里（值 / 图例 / 颜色 / 说明）；**选中某一行后，同一套面 / 线 / 点
  控件编辑的就是那一类的符号**（与 QGIS 符号页同一套交互，不复制控件）。

写回策略与其它页一致：改动全部推迟到 :meth:`RenderTab.apply`（点「确定 / 应用」时），
所以「取消」是真取消。单一符号模式两条路：

1. **原地改**（首选）：图层是「单一符号」渲染器、且首符号层就是与几何类型匹配的
   简单符号层（``QgsSimpleFillSymbolLayer`` / ``SimpleLine`` / ``SimpleMarker``）时，
   直接改那几个属性。符号上没暴露的项（角度、偏移、连接 / 端点样式、自定义虚线…）
   原样保留，渲染器也不换，不涉及所有权转移。
2. **替换**：渲染器不是单一符号（分级 / 基于规则…），或首符号层不是简单符号
   （SVG 标记 / 几何生成器…）时，只能新建一个简单符号 ``setRenderer`` 换上去。这条
   路会丢掉原有配置，所以 :meth:`RenderTab.apply` 先弹一次确认，用户点「否」则整页
   中止（对话框保持打开）。

分类模式**每次都整体新建一个 ``QgsCategorizedSymbolRenderer`` 换上去**，
类别符号由本页自己持有（见下），改的是自己那份，apply 时 clone 给渲染器。

注意本 QGIS 构建（4.2）的差异，均已用探针实测：

- ``QgsSymbolLayer.setProperties()`` **没有绑定**（``AttributeError``），所以原地改
  只能逐个走 typed setter（``setBrushStyle`` / ``setPenStyle`` / ``setShape`` …）。
- PyQt6 的枚举不是 int 子类，**不能写 ``int(sl.brushStyle())``**，要取 ``.value``。
- ``createSimple(...).symbolLayer(0)`` 这种一行式会让临时 ``QgsSymbol`` 被 GC，符号层
  变悬垂指针（一访问就原生崩溃）——符号必须先绑到局部变量。**同一个坑在分类渲染器上
  更凶**：``renderer.categories()[0].symbol().symbolLayer(0)`` 也崩，因为
  ``categories()`` 返回的是拷贝、临时拷贝一被回收它下面的符号就是悬垂指针。
  ``categories()`` 的每一项都得先绑到局部变量再往下取。
- ``renderer.categories()`` 返回的拷贝里，``symbol()`` 与渲染器**共享指针**，而且
  **生命周期绑在那个拷贝上**：原地改它不生效；把它交给 ``updateCategorySymbol()``
  则渲染器接手、拷贝还认为自己拥有 → **双重释放、原生崩溃（exit 127）**；把它从函数里
  ``return`` 出去（拷贝已死）再取 ``symbolLayer(0)`` 同样崩。所以本页**全程不调用
  ``updateCategorySymbol``**，读已有类别时也把类别拷贝绑在局部、当场 ``clone()``
  收下自己那份，不让 ``symbol()`` 活过拷贝。
- ``updateColorRamp()`` **不接受无参调用**（``TypeError: not enough arguments``）；
  ``setSourceColorRamp(临时)`` 之后再 ``updateColorRamp(同一个)`` 也崩（所有权）。
  本页用固定调色板，不碰色带。
- ``QgsCategorizedSymbolRenderer.createRenderer(layer)`` **不存在**，``symbolForValue()``
  已废弃且匹配不上 —— 类别只能自己按 ``layer.uniqueValues()`` 造。
- 样式名用 ``symbolLayer.properties()`` 里的字符串原样读写（``style`` / ``line_style``
  / ``outline_style`` / ``name``），不认识的样式名会临时补一项显示出来，不静默改写。
"""

from __future__ import annotations

from dataclasses import dataclass

from qgis.core import (
    QgsCategorizedSymbolRenderer,
    QgsFillSymbol,
    QgsGraduatedSymbolRenderer,
    QgsInvertedPolygonRenderer,
    QgsLineSymbol,
    QgsMarkerSymbol,
    QgsPointDisplacementRenderer,
    QgsRendererCategory,
    QgsRuleBasedRenderer,
    QgsSimpleFillSymbolLayer,
    QgsSimpleLineSymbolLayer,
    QgsSimpleMarkerSymbolLayer,
    QgsSingleSymbolRenderer,
    QgsSymbolLayerUtils,
    QgsUnitTypes,
    QgsVectorLayer,
)
from qgis.PyQt.QtCore import QSize, Qt
from qgis.PyQt.QtGui import QColor
from qgis.PyQt.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.infra.logging import get_logger
from app.ui.dialogs.layer_properties.base import LayerPropertyTab
from app.ui.dialogs.layer_properties.widgets import ColorButton
from app.ui.tools.feature_attrs import field_alias

logger = get_logger(__name__)

#: 符号预览图尺寸
_PREVIEW_SIZE = QSize(80, 56)

#: 类别表格的列（下标即列号）
_CAT_COL_VALUE = 0
_CAT_COL_LEGEND = 1
_CAT_COL_COLOR = 2
_CAT_COL_LABEL = 3
_CAT_HEADERS = ("值", "图例", "颜色", "说明")

#: 类别图例小图尺寸
_LEGEND_SIZE = QSize(46, 24)

#: 分类去重值上限（超过就提示并中止，不让海量类别把界面拖死）
_CLASSIFY_LIMIT = 200

#: 分类自动配色（按类别顺序循环取用；固定调色板 = 结果可预期、可测，也躲开
#: 色带的所有权坑，见模块 docstring）
_CATEGORY_COLORS = (
    QColor(228, 26, 28),
    QColor(55, 126, 184),
    QColor(77, 175, 74),
    QColor(152, 78, 163),
    QColor(255, 127, 0),
    QColor(166, 86, 40),
    QColor(247, 129, 191),
    QColor(153, 153, 153),
    QColor(86, 194, 194),
    QColor(204, 174, 113),
)

#: ``geometryType()`` → 本页内部的种类名（与 general.py 的 _GEOM_NAMES 同源）
_GEOM_KINDS = {0: "point", 1: "line", 2: "polygon"}

#: 默认颜色（只在「替换为简单符号」那条路上用到；原地改读的是图层真值）
_DEFAULT_FILL = QColor(200, 215, 235)
_DEFAULT_STROKE = QColor(35, 35, 35)
_DEFAULT_MARKER = QColor(70, 110, 160)

#: 符号输出单位 → 中文名（跟在宽度 / 大小输入框后面当后缀）
_UNIT_NAMES = {
    QgsUnitTypes.RenderUnit.Millimeters: "毫米",
    QgsUnitTypes.RenderUnit.Points: "点",
    QgsUnitTypes.RenderUnit.Pixels: "像素",
    QgsUnitTypes.RenderUnit.Inches: "英寸",
    QgsUnitTypes.RenderUnit.MapUnits: "地图单位",
    QgsUnitTypes.RenderUnit.Percentage: "百分比",
}

#: (显示文本, QGIS 样式名, Qt 枚举)。名字直接按 ``properties()['style']`` 原样存取，
#: 故只能用探针实测过能 round-trip 的值（``hourglass`` / ``zigzag`` / ``regular_star``
#: / ``x`` 会被 QGIS 归一成别的名字，故意不收）。
_FILL_STYLES = (
    ("实线", "solid", Qt.BrushStyle.SolidPattern),
    ("无填充", "no", Qt.BrushStyle.NoBrush),
    ("横线", "horizontal", Qt.BrushStyle.HorPattern),
    ("竖线", "vertical", Qt.BrushStyle.VerPattern),
    ("网格", "cross", Qt.BrushStyle.CrossPattern),
    ("斜线 ╲", "b_diagonal", Qt.BrushStyle.BDiagPattern),
    ("斜线 ╱", "f_diagonal", Qt.BrushStyle.FDiagPattern),
    ("斜网格", "diagonal_x", Qt.BrushStyle.DiagCrossPattern),
    ("密点 1", "dense1", Qt.BrushStyle.Dense1Pattern),
    ("密点 4", "dense4", Qt.BrushStyle.Dense4Pattern),
    ("密点 7", "dense7", Qt.BrushStyle.Dense7Pattern),
)

_LINE_STYLES = (
    ("实线", "solid", Qt.PenStyle.SolidLine),
    ("虚线", "dash", Qt.PenStyle.DashLine),
    ("点线", "dot", Qt.PenStyle.DotLine),
    ("点划线", "dash dot", Qt.PenStyle.DashDotLine),
    ("双点划线", "dash dot dot", Qt.PenStyle.DashDotDotLine),
)

#: 面的描边样式：比线样式多一个「无描边」
_OUTLINE_STYLES = (
    ("无描边", "no", Qt.PenStyle.NoPen),
    *_LINE_STYLES,
)

_MARKER_SHAPES = (
    ("圆形", "circle", QgsSimpleMarkerSymbolLayer.Shape.Circle),
    ("方形", "square", QgsSimpleMarkerSymbolLayer.Shape.Square),
    ("菱形", "diamond", QgsSimpleMarkerSymbolLayer.Shape.Diamond),
    ("三角形", "triangle", QgsSimpleMarkerSymbolLayer.Shape.Triangle),
    ("等边三角形", "equilateral_triangle", QgsSimpleMarkerSymbolLayer.Shape.EquilateralTriangle),
    ("五边形", "pentagon", QgsSimpleMarkerSymbolLayer.Shape.Pentagon),
    ("六边形", "hexagon", QgsSimpleMarkerSymbolLayer.Shape.Hexagon),
    ("星形", "star", QgsSimpleMarkerSymbolLayer.Shape.Star),
    ("箭头", "arrow", QgsSimpleMarkerSymbolLayer.Shape.Arrow),
    ("十字", "cross", QgsSimpleMarkerSymbolLayer.Shape.Cross),
    ("实心十字", "cross_fill", QgsSimpleMarkerSymbolLayer.Shape.CrossFill),
    ("半圆", "semi_circle", QgsSimpleMarkerSymbolLayer.Shape.SemiCircle),
    ("扇形", "quarter_circle", QgsSimpleMarkerSymbolLayer.Shape.QuarterCircle),
)

#: 几何种类 → 能在本页原地编辑的简单符号层类
_SIMPLE_LAYER_CLASSES = {
    "polygon": QgsSimpleFillSymbolLayer,
    "line": QgsSimpleLineSymbolLayer,
    "point": QgsSimpleMarkerSymbolLayer,
}

#: 渲染器 → 中文名（提示语里用；认不出的按类名显示）
_RENDERER_NAMES = (
    (QgsSingleSymbolRenderer, "单一符号"),
    (QgsCategorizedSymbolRenderer, "分类"),
    (QgsGraduatedSymbolRenderer, "分级"),
    (QgsRuleBasedRenderer, "基于规则"),
    (QgsInvertedPolygonRenderer, "反转多边形"),
    (QgsPointDisplacementRenderer, "点位移"),
)


# --------------------------------------------------------------------- 工具函数


def _geom_kind(layer) -> str | None:
    """点 / 线 / 面；非矢量或没有几何类型返回 ``None``。"""
    if not isinstance(layer, QgsVectorLayer):
        return None
    try:
        # QGIS 的 sip 枚举是 int 子类，int() 取值安全（见仓库备忘录）
        return _GEOM_KINDS.get(int(layer.geometryType()))
    except Exception:  # noqa: BLE001 —— 无效图层读几何类型会抛
        return None


def _call(obj, name):
    """取 ``obj`` 的无参方法返回值；没有该方法 / 调用失败 → ``None``。"""
    fn = getattr(obj, name, None)
    if not callable(fn):
        return None
    try:
        return fn()
    except Exception:  # noqa: BLE001
        return None


def _props(symbol_layer) -> dict:
    """符号层属性字典；取不到返回空 dict。"""
    if symbol_layer is None:
        return {}
    fn = getattr(symbol_layer, "properties", None)
    if not callable(fn):
        return {}
    try:
        return dict(fn())
    except Exception:  # noqa: BLE001
        return {}


def _simple_layer(symbol, kind):
    """``symbol`` 首层是与 ``kind`` 匹配的简单符号层时返回它，否则 ``None``。"""
    cls = _SIMPLE_LAYER_CLASSES.get(kind)
    if symbol is None or cls is None:
        return None
    try:
        if symbol.symbolLayerCount() < 1:
            return None
        layer = symbol.symbolLayer(0)
    except Exception:  # noqa: BLE001
        return None
    return layer if isinstance(layer, cls) else None


def _first_layer(symbol):
    """符号首层（不管是不是简单符号）；没有返回 ``None``。"""
    try:
        if symbol is None or symbol.symbolLayerCount() < 1:
            return None
        return symbol.symbolLayer(0)
    except Exception:  # noqa: BLE001
        return None


def _renderer_name(renderer) -> str:
    """渲染器的中文名。"""
    for cls, name in _RENDERER_NAMES:
        if isinstance(renderer, cls):
            return name
    return type(renderer).__name__ if renderer is not None else "无"


def _fill_combo(combo: QComboBox, table) -> None:
    """把 ``(文本, 样式名, 枚举)`` 表灌进 combo，itemData 存样式名（字符串）。"""
    combo.clear()
    for text, name, _enum in table:
        combo.addItem(text, name)


def _select_name(combo: QComboBox, name) -> None:
    """按样式名选中 combo 项。

    表里没有的名字（图层用别的途径设的样式）临时追加一项原样显示，避免回显时
    被顶到「实线」而在 apply 时静默改写。
    """
    if not name:
        return
    idx = combo.findData(name)
    if idx < 0:
        combo.addItem(f"其它（{name}）", name)
        idx = combo.count() - 1
    combo.setCurrentIndex(idx)


def _style_enum(table, name):
    """样式名 → Qt 枚举；表里没有的返回 ``None``。

    返回 ``None`` 时调用方**不要动**那个属性：表外的名字（图层在别处设的样式）
    本页没有对应的枚举值，写回会被压成默认样式，属于静默改写。``createSimple``
    那条路不受影响——它收的是样式名本身，表外的名字原样传下去仍是合法的。
    """
    for _text, qgis_name, enum in table:
        if qgis_name == name:
            return enum
    return None


def _color_string(color: QColor) -> str:
    """QColor → QGIS 的 ``"r,g,b,a"`` 颜色串。"""
    return f"{color.red()},{color.green()},{color.blue()},{color.alpha()}"


def _unique_values(layer, field: str) -> list | None:
    """字段去重值（**原始类型**，排序稳定）；读不了返回 ``None``。

    与 ``labeling._field_values`` 的区别：那个是拿去显示的、只留字符串；这里要拿去
    建 :class:`QgsRendererCategory`，**必须保留原始类型**（整型字段的类别值要是
    ``int``，否则匹配不上图层里的值）。

    排序要稳定（多次「分类」之间行序与配色一致）：整型 / 浮点按数值序，其余按显示
    文本（短值优先，照 ``labeling`` 的惯例）。

    超过 :data:`_CLASSIFY_LIMIT` 个去重值抛 :class:`ValueError`，由调用方提示用户，
    不静默截断。
    """
    if not isinstance(layer, QgsVectorLayer):
        return None
    idx = layer.fields().indexOf(field)
    if idx < 0:
        return None
    try:
        # 多要一个用来判断「是不是超了」，避免为计数再跑一次
        values = layer.uniqueValues(idx, _CLASSIFY_LIMIT + 1)
    except Exception as exc:  # noqa: BLE001
        logger.warning("读取字段「%s」去重值失败: %s", field, exc)
        return None
    values = [v for v in values if v is not None]
    if len(values) > _CLASSIFY_LIMIT:
        raise ValueError(f"字段「{field}」的去重值超过 {_CLASSIFY_LIMIT} 个，不适合分类渲染")
    numbers = [v for v in values if isinstance(v, int | float) and not isinstance(v, bool)]
    if len(numbers) == len(values):
        return sorted(values)
    return sorted(values, key=lambda v: (len(str(v)), str(v)))


def _cat_color(index: int) -> QColor:
    """类别自动配色：按序号循环取固定调色板。"""
    return QColor(_CATEGORY_COLORS[index % len(_CATEGORY_COLORS)])


@dataclass
class _Category:
    """一个类别：字段值 + 图例文本 + **本页自己持有的**符号。

    符号是本页 clone 出来的，改的就是这一份；apply 时再 clone 一份交给渲染器。
    刻意不持有 :class:`QgsRendererCategory` —— 它拥有符号，而 ``categories()`` 返回
    的是拷贝、其符号又与渲染器共享指针，两处都当拥有者会双重释放（见模块 docstring）。
    """

    value: object
    label: str
    symbol: object


class RenderTab(LayerPropertyTab):
    """图层属性「渲染」页（单一符号 / 按字段值分类）。"""

    def __init__(self, layer=None, parent=None) -> None:
        super().__init__(parent)
        self._layer = layer
        self._geom: str | None = None
        #: 符号输出单位：读图层现有符号，写回时保持（默认毫米）
        self._unit = QgsUnitTypes.RenderUnit.Millimeters
        #: 程序性回填控件期间为 True。**凡是回填都要包住**（load / 切行 / 切模式 /
        #: 行色块同步分组），否则信号会把上一个目标的值写进新目标（见 _commit_target）
        self._loading = False
        #: "single" | "categorized"
        self._mode = "single"
        #: 分类模式的类别（本页自己持有的符号，见 _Category）
        self._cats: list[_Category] = []
        #: 分类渲染器的兜底符号（不属于任何类别的值用它画）；None = 不画
        self._source_symbol = None
        #: 当前选中的类别下标；-1 = 无（单一符号模式恒为 -1）
        self._selected = -1
        self._build()
        if layer is not None:
            self.load(layer)

    # ------------------------------------------------------------- 构建

    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(10)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        form.setHorizontalSpacing(8)
        self.renderer_combo = QComboBox(self)
        self.renderer_combo.addItem("单一符号", "single")
        self.renderer_combo.addItem("分类（按字段值）", "categorized")
        self.renderer_combo.setMinimumWidth(180)
        form.addRow("渲染方式", self.renderer_combo)

        preview_row = QWidget(self)
        pr = QHBoxLayout(preview_row)
        pr.setContentsMargins(0, 0, 0, 0)
        pr.setSpacing(6)
        self.preview_label = QLabel(self)
        self.preview_label.setFixedSize(_PREVIEW_SIZE)
        self.preview_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview_label.setToolTip("当前设置的符号预览")
        pr.addWidget(self.preview_label)
        pr.addStretch(1)
        form.addRow("符号预览", preview_row)
        root.addLayout(form)

        self.hint_label = QLabel("", self)
        self.hint_label.setObjectName("PropHint")
        self.hint_label.setWordWrap(True)
        self.hint_label.setVisible(False)
        root.addWidget(self.hint_label)

        self.categorize_group = self._build_categorize_group()
        root.addWidget(self.categorize_group)

        self.fill_group = self._build_fill_group()
        self.line_group = self._build_line_group()
        self.marker_group = self._build_marker_group()
        for box in (self.fill_group, self.line_group, self.marker_group):
            root.addWidget(box)
        root.addStretch(1)

        self.renderer_combo.currentIndexChanged.connect(self._on_mode_changed)
        # 任何一项变化都写回当前目标并重画预览。注意 ColorButton 的 color_changed 才是
        # 「选完色」的时机（clicked 在选完之前就发了，读到的是旧颜色）
        for btn in (
            self.fill_color_btn,
            self.stroke_color_btn,
            self.line_color_btn,
            self.marker_color_btn,
            self.marker_stroke_color_btn,
        ):
            btn.color_changed.connect(self._commit_target)
        for combo in (
            self.fill_style_combo,
            self.stroke_style_combo,
            self.line_style_combo,
            self.marker_shape_combo,
        ):
            combo.currentIndexChanged.connect(self._commit_target)
        for spin in (
            self.stroke_width_spin,
            self.line_width_spin,
            self.marker_size_spin,
            self.marker_stroke_width_spin,
        ):
            spin.valueChanged.connect(self._commit_target)

    def _build_categorize_group(self) -> QGroupBox:
        """「按字段值分类」：字段选择 + 「分类」按钮 + 类别表。"""
        box = QGroupBox("按字段值分类", self)
        lay = QVBoxLayout(box)
        lay.setContentsMargins(10, 8, 10, 10)
        lay.setSpacing(8)

        top = QHBoxLayout()
        top.setSpacing(8)
        field_label = QLabel("字段", box)
        field_label.setObjectName("PropHint")
        top.addWidget(field_label)
        self.field_combo = QComboBox(box)
        self.field_combo.setMinimumWidth(200)
        top.addWidget(self.field_combo, 1)
        self.classify_btn = QPushButton("分类", box)
        self.classify_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.classify_btn.setToolTip("按该字段的去重值生成类别")
        self.classify_btn.clicked.connect(self._on_classify)
        top.addWidget(self.classify_btn)
        lay.addLayout(top)

        self.cat_table = QTableWidget(0, len(_CAT_HEADERS), box)
        self.cat_table.setHorizontalHeaderLabels(list(_CAT_HEADERS))
        self.cat_table.verticalHeader().setVisible(False)
        self.cat_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.cat_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        # 类别值 / 说明都不允许就地编辑（本页只改样式），整表关掉编辑触发
        self.cat_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        # 不开排序：类别下标 ↔ 行号就是映射，行一漂就写错行
        self.cat_table.setSortingEnabled(False)
        header = self.cat_table.horizontalHeader()
        # 固定列 Interactive + 明确宽度，「说明」列 Stretch 吃掉剩余宽度；
        # 不用 ResizeToContents（逐行 setText 都重算整列，是 O(n²)）
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setMinimumSectionSize(48)
        header.setStretchLastSection(False)
        for col, width in (
            (_CAT_COL_VALUE, 110),
            (_CAT_COL_LEGEND, 60),
            (_CAT_COL_COLOR, 56),
        ):
            self.cat_table.setColumnWidth(col, width)
        header.setSectionResizeMode(_CAT_COL_LABEL, QHeaderView.ResizeMode.Stretch)
        header.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.cat_table.setMinimumHeight(110)
        self.cat_table.itemSelectionChanged.connect(self._on_category_selected)
        lay.addWidget(self.cat_table, 1)
        return box

    def _new_form(self, parent) -> QFormLayout:
        form = QFormLayout(parent)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        form.setHorizontalSpacing(8)
        form.setVerticalSpacing(8)
        return form

    def _new_size_spin(self, parent, high: float, step: float, value: float) -> QDoubleSpinBox:
        spin = QDoubleSpinBox(parent)
        spin.setRange(0.0, high)
        spin.setDecimals(2)
        spin.setSingleStep(step)
        spin.setValue(value)
        return spin

    def _build_fill_group(self) -> QGroupBox:
        box = QGroupBox("面符号（填充 / 描边）", self)
        form = self._new_form(box)

        self.fill_color_btn = ColorButton(_DEFAULT_FILL, box)
        form.addRow("填充颜色", self.fill_color_btn)

        self.fill_style_combo = QComboBox(box)
        _fill_combo(self.fill_style_combo, _FILL_STYLES)
        form.addRow("填充样式", self.fill_style_combo)

        self.stroke_color_btn = ColorButton(_DEFAULT_STROKE, box)
        form.addRow("描边颜色", self.stroke_color_btn)

        self.stroke_width_spin = self._new_size_spin(box, 100.0, 0.1, 0.26)
        form.addRow("描边宽度", self.stroke_width_spin)

        self.stroke_style_combo = QComboBox(box)
        _fill_combo(self.stroke_style_combo, _OUTLINE_STYLES)
        form.addRow("描边样式", self.stroke_style_combo)
        return box

    def _build_line_group(self) -> QGroupBox:
        box = QGroupBox("线符号", self)
        form = self._new_form(box)

        self.line_color_btn = ColorButton(_DEFAULT_STROKE, box)
        form.addRow("线颜色", self.line_color_btn)

        self.line_width_spin = self._new_size_spin(box, 1000.0, 0.1, 0.26)
        form.addRow("线宽", self.line_width_spin)

        self.line_style_combo = QComboBox(box)
        _fill_combo(self.line_style_combo, _LINE_STYLES)
        form.addRow("线样式", self.line_style_combo)
        return box

    def _build_marker_group(self) -> QGroupBox:
        box = QGroupBox("点符号", self)
        form = self._new_form(box)

        self.marker_shape_combo = QComboBox(box)
        _fill_combo(self.marker_shape_combo, _MARKER_SHAPES)
        form.addRow("形状", self.marker_shape_combo)

        self.marker_color_btn = ColorButton(_DEFAULT_MARKER, box)
        form.addRow("颜色", self.marker_color_btn)

        self.marker_size_spin = self._new_size_spin(box, 5000.0, 0.5, 2.0)
        form.addRow("大小", self.marker_size_spin)

        self.marker_stroke_color_btn = ColorButton(_DEFAULT_STROKE, box)
        form.addRow("描边颜色", self.marker_stroke_color_btn)

        self.marker_stroke_width_spin = self._new_size_spin(box, 100.0, 0.1, 0.0)
        form.addRow("描边宽度", self.marker_stroke_width_spin)
        return box

    # ------------------------------------------------------------- 读取

    def load(self, layer) -> None:
        """从 ``layer`` 读值填控件。"""
        self._layer = layer
        self._geom = _geom_kind(layer)
        self._cats = []
        self._source_symbol = None
        self._selected = -1
        if self._geom is None:
            # 非矢量 / 没有几何类型：没有符号可配，只给提示
            self.preview_label.clear()
            self._sync_visibility()
            self._set_hint(
                "仅矢量图层支持符号渲染"
                if not isinstance(layer, QgsVectorLayer)
                else "该图层没有几何类型，无法配置符号"
            )
            return

        renderer = layer.renderer()
        if isinstance(renderer, QgsCategorizedSymbolRenderer):
            self._mode = "categorized"
            self._load_categorized(renderer)
        else:
            self._mode = "single"
            self._load_single()

        self._sync_visibility()
        self._activate_target()
        self._refresh_hint()

    def _load_single(self) -> None:
        """单一符号（或别的渲染器）→ 回填交给 :meth:`_activate_target`。

        图层不是「单一符号」渲染器时没有符号可读，控件保持现值（首次 load 即默认值）；
        点「应用」时会用控件值替换掉原渲染配置。
        """
        self._set_combo_mode("single")
        self._populate_fields(None)

    def _load_categorized(self, renderer) -> None:
        """已有的分类渲染器 → 把字段与各类别读进来（保留原有颜色 / 未暴露属性）。"""
        self._set_combo_mode("categorized")
        self._populate_fields(renderer.classAttribute())
        cats: list[_Category] = []
        try:
            # categories() 给的是拷贝，且其 symbol 与渲染器共享指针 —— 每一项都必须
            # 先绑到局部变量再往下取，否则临时拷贝一被 GC 符号就是悬垂指针（崩）
            for i in range(len(renderer.categories())):
                cat = renderer.categories()[i]
                symbol = cat.symbol()
                if symbol is None:
                    continue
                cats.append(
                    _Category(
                        value=cat.value(),
                        label=str(cat.label()),
                        symbol=symbol.clone(),
                    )
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("读取分类渲染器类别失败: %s", exc)
            cats = []
        self._cats = cats
        source = renderer.sourceSymbol()
        self._source_symbol = source.clone() if source is not None else None
        self._selected = 0 if cats else -1
        self._rebuild_cat_table()
        self._select_row(self._selected)

    def _set_combo_mode(self, mode: str) -> None:
        """程序性设置「渲染方式」（不触发 _on_mode_changed 的副作用）。"""
        idx = self.renderer_combo.findData(mode)
        if idx < 0 or idx == self.renderer_combo.currentIndex():
            return
        self._loading = True
        try:
            self.renderer_combo.setCurrentIndex(idx)
        finally:
            self._loading = False

    def _populate_fields(self, current) -> None:
        """字段下拉：``名字 · 别名``，itemData 存原始字段名。"""
        self.field_combo.clear()
        layer = self._layer
        if not isinstance(layer, QgsVectorLayer):
            return
        fields = layer.fields()
        for i in range(fields.size()):
            fld = fields.at(i)
            name = fld.name()
            alias = field_alias(fld) or name
            self.field_combo.addItem(f"{name} · {alias}", name)
        if current:
            idx = self.field_combo.findData(current)
            if idx >= 0:
                self.field_combo.setCurrentIndex(idx)

    def _refresh_hint(self) -> None:
        """按模式 / 图层渲染器 / 类别状态给提示；不需要提示时清空。"""
        if self._geom is None:
            return
        if self._mode == "categorized":
            if not self._cats:
                self._set_hint("选择分类字段后点「分类」生成类别。")
                return
            if self._has_odd_symbol():
                self._set_hint("部分类别的符号不是简单符号（如 SVG 标记 / 几何生成器），本页只能编辑简单符号。")
                return
            self._set_hint("")
            return
        renderer = self._layer.renderer() if self._layer is not None else None
        if not isinstance(renderer, QgsSingleSymbolRenderer):
            self._set_hint(
                f"当前渲染方式为「{_renderer_name(renderer)}」，本页可编辑单一符号或按字段值分类；"
                "点「应用」会替换为所选方式，原有渲染配置将丢失。"
            )
            return
        symbol = renderer.symbol()
        if symbol is not None and _simple_layer(symbol, self._geom) is None:
            self._set_hint("当前符号不是简单符号（如 SVG 标记 / 几何生成器）；点「应用」会替换为简单符号。")
            return
        self._set_hint("")

    def _has_odd_symbol(self) -> bool:
        """分类里是否有「不是匹配几何类型的简单符号」的类别。"""
        return any(_simple_layer(c.symbol, self._geom) is None for c in self._cats)

    def _set_hint(self, text: str) -> None:
        self.hint_label.setText(text)
        self.hint_label.setVisible(bool(text))

    def _reset_combos(self) -> None:
        """丢掉上次 load 因「其它（…）」临时补的项，回到表里的固定项。"""
        for combo, table in (
            (self.fill_style_combo, _FILL_STYLES),
            (self.stroke_style_combo, _OUTLINE_STYLES),
            (self.line_style_combo, _LINE_STYLES),
            (self.marker_shape_combo, _MARKER_SHAPES),
        ):
            if combo.count() != len(table):
                _fill_combo(combo, table)

    def _apply_symbol_values(self, symbol_layer) -> None:
        """把现有符号层的值回填到控件（读不到的项保持默认）。"""
        props = _props(symbol_layer)
        if self._geom == "polygon":
            self._set_color(self.fill_color_btn, _call(symbol_layer, "color"))
            _select_name(self.fill_style_combo, props.get("style"))
            self._set_color(self.stroke_color_btn, _call(symbol_layer, "strokeColor"))
            self._set_value(self.stroke_width_spin, _call(symbol_layer, "strokeWidth"))
            _select_name(self.stroke_style_combo, props.get("outline_style"))
        elif self._geom == "line":
            self._set_color(self.line_color_btn, _call(symbol_layer, "color"))
            self._set_value(self.line_width_spin, _call(symbol_layer, "width"))
            _select_name(self.line_style_combo, props.get("line_style"))
        elif self._geom == "point":
            _select_name(self.marker_shape_combo, props.get("name"))
            self._set_color(self.marker_color_btn, _call(symbol_layer, "color"))
            self._set_value(self.marker_size_spin, _call(symbol_layer, "size"))
            self._set_color(self.marker_stroke_color_btn, _call(symbol_layer, "strokeColor"))
            self._set_value(
                self.marker_stroke_width_spin, _call(symbol_layer, "strokeWidth")
            )

    @staticmethod
    def _set_color(button: ColorButton, color) -> None:
        if color is None:
            return
        button.set_color(color, emit=False)  # 回填不刷预览，load 末尾统一刷一次

    @staticmethod
    def _set_value(spin: QDoubleSpinBox, value) -> None:
        if value is None:
            return
        try:
            spin.setValue(float(value))
        except (TypeError, ValueError):
            logger.warning("符号属性值无法回填：%r", value)

    def _sync_units(self) -> None:
        """把单位名写进各宽度 / 大小输入框的后缀（图层符号用的什么单位就写什么）。"""
        suffix = f" {_UNIT_NAMES.get(self._unit, '毫米')}"
        for spin in (
            self.stroke_width_spin,
            self.line_width_spin,
            self.marker_size_spin,
            self.marker_stroke_width_spin,
        ):
            spin.setSuffix(suffix)

    def _sync_visibility(self) -> None:
        """按模式 / 几何类型显示隐藏各分组；分类模式没有选中类别时禁用样式分组。"""
        kind = self._geom
        has_target = self._has_target()
        for box, box_kind in (
            (self.fill_group, "polygon"),
            (self.line_group, "line"),
            (self.marker_group, "point"),
        ):
            visible = box_kind == kind
            box.setVisible(visible)
            box.setEnabled(visible and has_target)
        # 非矢量 / 没有几何类型时连渲染方式与分类 UI 一起收起来，别在栅格页露出
        self.renderer_combo.setEnabled(kind is not None)
        self.categorize_group.setVisible(kind is not None and self._mode == "categorized")

    def _has_target(self) -> bool:
        """当前是否有可编辑的目标符号（分类模式没选中类别时为 ``False``）。"""
        if self._mode == "categorized":
            return 0 <= self._selected < len(self._cats)
        return True

    # ------------------------------------------------------------- 目标 / 写回

    def _target_symbol(self):
        """控件当前编辑的符号；``None`` = 控件自己是真值来源（单一符号模式）。

        单一符号模式**不**在这里返回图层符号：改动要推迟到 :meth:`apply`，提前拿图层
        的符号来改等于当场生效，「取消」就不是取消了。
        """
        if self._mode != "categorized" or not self._has_target():
            return None
        return self._cats[self._selected].symbol

    def _display_symbol(self):
        """回填控件时该读哪个符号（单一符号模式读图层的现有符号）。"""
        if self._mode == "categorized":
            return self._target_symbol()
        renderer = self._layer.renderer() if self._layer is not None else None
        if isinstance(renderer, QgsSingleSymbolRenderer):
            return renderer.symbol()
        return None

    def _activate_target(self) -> None:
        """**唯一的控件写入口**：按当前目标刷新控件，最后刷预览。

        不变量：``_loading`` 为假时，控件值 == 目标符号的可编辑属性；控件一变就写回
        同一个目标。凡程序性回填（load / 切行 / 切模式 / 行色块同步分组）都必须走这里，
        且 ``_loading`` 必须包住整段回填，否则信号会把上一个目标的值写进新目标。
        """
        self._loading = True
        try:
            self._reset_combos()
            symbol = self._display_symbol()
            if symbol is not None:
                unit = _call(symbol, "outputUnit")
                if unit is not None:
                    self._unit = unit
                # 拿不到「匹配几何类型的简单符号层」时退而求其次读首层（可能是 SVG /
                # 几何生成器 / 标记线），能读多少回显多少，读不到的保持现值
                self._apply_symbol_values(
                    _simple_layer(symbol, self._geom) or _first_layer(symbol)
                )
            self._sync_units()
        except Exception as exc:  # noqa: BLE001
            logger.warning("回填符号控件失败: %s", exc)
        finally:
            self._loading = False
        self._refresh_preview()

    def _commit_target(self, *_) -> None:
        """**唯一的控件变化槽**：把控件值写回当前目标，再刷预览与那一行。

        单一符号模式没有目标可写（控件即真值，:meth:`apply` 时才落图层），只刷预览。
        跑在信号槽里，异常必须自己兜住（未捕获会原生崩溃）。
        """
        if self._loading or self._geom is None:
            return
        try:
            cat = self._cats[self._selected] if self._has_target() else None
            if cat is not None:
                cat.symbol = self._write_symbol(cat.symbol, self._geom)
                self._refresh_cat_row(self._selected)
            self._refresh_preview()
        except Exception as exc:  # noqa: BLE001
            logger.warning("写回符号设置失败: %s", exc)

    def _write_symbol(self, symbol, kind: str):
        """把控件值写进 ``symbol``，返回（可能换了对象的）符号。

        首层是与 ``kind`` 匹配的简单符号层时就地改（未暴露的属性原样保留）；否则这是
        个简单符号层用不了的符号（SVG 标记 / 几何生成器…），整体换成按控件值新建的简单
        符号 —— 传进来的符号是本页自己持有的，换了不涉及 QGIS 所有权。
        """
        simple = _simple_layer(symbol, kind)
        if simple is not None:
            self._write_in_place(simple, kind)
            return symbol
        return self._build_symbol(kind) or symbol

    def _symbol_with_color(self, symbol, kind: str, color: QColor):
        """把颜色写进 ``symbol``（不是简单符号就整体换掉再上色），返回符号。"""
        simple = _simple_layer(symbol, kind)
        if simple is None:
            symbol = self._build_symbol(kind) or symbol
            simple = _simple_layer(symbol, kind)
        if simple is not None:
            simple.setColor(color)
        return symbol

    def _kind_color_btn(self, kind: str) -> ColorButton | None:
        """几何种类 → 分组里的「主颜色」控件。"""
        return {
            "polygon": self.fill_color_btn,
            "line": self.line_color_btn,
            "point": self.marker_color_btn,
        }.get(kind)

    def _row_of(self, cat: _Category) -> int:
        """类别在表格里的行号；用**对象身份**查（行号漂移不会写错行）。"""
        for row, item in enumerate(self._cats):
            if item is cat:
                return row
        return -1

    def _legend_pixmap(self, symbol):
        """按符号画类别图例小图；画不出来返回 ``None``。"""
        try:
            return QgsSymbolLayerUtils.symbolPreviewPixmap(symbol, _LEGEND_SIZE)
        except Exception:  # noqa: BLE001
            return None

    # ------------------------------------------------------------- 分类交互

    def _on_mode_changed(self, *_) -> None:
        """「渲染方式」切换。**不**重建 ``_cats``（只有「分类」按钮重建）。"""
        if self._loading or self._geom is None:
            return
        self._mode = self.renderer_combo.currentData() or "single"
        if self._mode == "categorized" and self._cats:
            if not self._has_target():
                self._select_row(0)
        else:
            self._selected = -1
            self._loading = True
            try:
                self.cat_table.clearSelection()
            finally:
                self._loading = False
        self._sync_visibility()
        self._activate_target()
        self._refresh_hint()

    def _on_category_selected(self) -> None:
        """类别表选中行变化 → 换成编辑那一类。"""
        if self._loading or self._geom is None:
            return
        rows = self.cat_table.selectionModel().selectedRows()
        row = rows[0].row() if rows else -1
        if row == self._selected:
            return
        self._selected = row if 0 <= row < len(self._cats) else -1
        self._sync_visibility()
        self._activate_target()
        self._refresh_hint()

    def _on_row_color(self, cat: _Category, color: QColor) -> None:
        """某一行的色块改了颜色：写进**那一行**的符号。

        只有改的正好是选中行时才同步分组控件 —— 否则会把非选中行的颜色写进选中行。
        """
        if self._loading or self._geom is None:
            return
        try:
            cat.symbol = self._symbol_with_color(cat.symbol, self._geom, color)
            row = self._row_of(cat)
            if row >= 0:
                self._refresh_cat_row(row)
            if row >= 0 and row == self._selected:
                btn = self._kind_color_btn(self._geom)
                if btn is not None:
                    self._loading = True
                    try:
                        btn.set_color(color, emit=False)
                    finally:
                        self._loading = False
            self._refresh_preview()
        except Exception as exc:  # noqa: BLE001
            logger.warning("写回类别颜色失败: %s", exc)

    def _on_classify(self) -> None:
        """按所选字段的去重值重建类别（已有类别时先确认会丢弃样式）。"""
        if self._geom is None:
            return
        field = self.field_combo.currentData()
        if not field:
            QMessageBox.information(self, "图层属性", "该图层没有可用的字段。")
            return
        if self._cats and not self._confirm_discard():
            return
        try:
            values = _unique_values(self._layer, field)
        except ValueError as exc:
            QMessageBox.warning(self, "图层属性", str(exc))
            return
        if values is None:
            QMessageBox.warning(self, "图层属性", f"读不到字段「{field}」的去重值。")
            return

        template = self._build_symbol(self._geom)
        cats: list[_Category] = []
        for index, value in enumerate(values):
            symbol = template.clone() if template is not None else self._build_symbol(self._geom)
            simple = _simple_layer(symbol, self._geom)
            if simple is not None:
                simple.setColor(_cat_color(index))
            cats.append(_Category(value=value, label=str(value), symbol=symbol))

        self._cats = cats
        self._source_symbol = template.clone() if template is not None else None
        self._mode = "categorized"
        self._set_combo_mode("categorized")
        self._selected = 0 if cats else -1
        self._rebuild_cat_table()
        self._select_row(self._selected)
        self._sync_visibility()
        self._activate_target()
        self._refresh_hint()
        if not cats:
            # 区分「还没点分类」与「点了但字段没值」——这两种情况的下一步动作不一样
            self._set_hint(f"字段「{field}」没有可用的值，未生成类别。")

    def _confirm_discard(self) -> bool:
        """重新分类前的确认（已设的各类样式会没）。"""
        answer = QMessageBox.question(
            self,
            "图层属性",
            "重新分类会丢弃已设置的各类样式，确定继续吗？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return answer == QMessageBox.StandardButton.Yes

    def _rebuild_cat_table(self) -> None:
        """按 ``_cats`` 重建类别表（程序性，全程 ``_loading`` 包住）。"""
        self._loading = True
        try:
            table = self.cat_table
            table.setRowCount(0)  # 连带清掉旧的单元格控件
            table.setRowCount(len(self._cats))
            for row, cat in enumerate(self._cats):
                value_item = QTableWidgetItem(str(cat.value))
                value_item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
                table.setItem(row, _CAT_COL_VALUE, value_item)

                legend = QLabel(table)
                legend.setAlignment(Qt.AlignmentFlag.AlignCenter)
                pixmap = self._legend_pixmap(cat.symbol)
                if pixmap is not None:
                    legend.setPixmap(pixmap)
                table.setCellWidget(row, _CAT_COL_LEGEND, legend)

                # 色块直接当单元格控件（它自己已固定尺寸，套容器反而会被顶层对齐裁掉）
                color = _call(cat.symbol, "color")
                btn = ColorButton(color if color is not None else _cat_color(row), table)
                btn.color_changed.connect(
                    lambda new_color, item=cat: self._on_row_color(item, new_color)
                )
                table.setCellWidget(row, _CAT_COL_COLOR, btn)

                label_item = QTableWidgetItem(cat.label)
                label_item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
                table.setItem(row, _CAT_COL_LABEL, label_item)
        except Exception as exc:  # noqa: BLE001
            logger.warning("重建类别表失败: %s", exc)
        finally:
            self._loading = False

    def _refresh_cat_row(self, row: int) -> None:
        """刷新某一行的图例与色块（写回后调用）；行数与 ``_cats`` 不同步则整表重建。"""
        if self.cat_table.rowCount() != len(self._cats):
            self._rebuild_cat_table()
            return
        if not (0 <= row < len(self._cats)):
            return
        cat = self._cats[row]
        legend = self.cat_table.cellWidget(row, _CAT_COL_LEGEND)
        if isinstance(legend, QLabel):
            legend.clear()
            pixmap = self._legend_pixmap(cat.symbol)
            if pixmap is not None:
                legend.setPixmap(pixmap)
        btn = self.cat_table.cellWidget(row, _CAT_COL_COLOR)
        color = _call(cat.symbol, "color")
        if isinstance(btn, ColorButton) and color is not None:
            btn.set_color(color, emit=False)

    def _select_row(self, row: int) -> None:
        """程序性选中某行（不触发 :meth:`_on_category_selected` 的副作用）。"""
        if row < 0:
            return
        self._loading = True
        try:
            self.cat_table.selectRow(row)
        finally:
            self._loading = False

    # ------------------------------------------------------------- 预览

    def _refresh_preview(self, *_) -> None:
        """按控件值重画符号预览。跑在信号槽里，异常必须自己兜住（未捕获会原生崩溃）。"""
        if self._loading or self._geom is None:
            return
        try:
            symbol = self._build_symbol(self._geom)
            if symbol is None:
                self.preview_label.clear()
                return
            self.preview_label.setPixmap(
                QgsSymbolLayerUtils.symbolPreviewPixmap(symbol, _PREVIEW_SIZE)
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("绘制符号预览失败: %s", exc)

    # ------------------------------------------------------------- 写回

    def _fill_props(self) -> dict:
        return {
            "color": _color_string(self.fill_color_btn.color()),
            "style": self.fill_style_combo.currentData() or "solid",
            "outline_color": _color_string(self.stroke_color_btn.color()),
            "outline_width": f"{self.stroke_width_spin.value():g}",
            "outline_style": self.stroke_style_combo.currentData() or "solid",
        }

    def _line_props(self) -> dict:
        return {
            "line_color": _color_string(self.line_color_btn.color()),
            "line_width": f"{self.line_width_spin.value():g}",
            "line_style": self.line_style_combo.currentData() or "solid",
        }

    def _marker_props(self) -> dict:
        return {
            "name": self.marker_shape_combo.currentData() or "circle",
            "color": _color_string(self.marker_color_btn.color()),
            "size": f"{self.marker_size_spin.value():g}",
            "outline_color": _color_string(self.marker_stroke_color_btn.color()),
            "outline_width": f"{self.marker_stroke_width_spin.value():g}",
        }

    def _build_symbol(self, kind):
        """按控件值造一个简单符号；符号必须先绑住再往下用（见模块 docstring）。"""
        if kind == "polygon":
            symbol = QgsFillSymbol.createSimple(self._fill_props())
        elif kind == "line":
            symbol = QgsLineSymbol.createSimple(self._line_props())
        elif kind == "point":
            symbol = QgsMarkerSymbol.createSimple(self._marker_props())
        else:
            return None
        symbol.setOutputUnit(self._unit)
        return symbol

    def _write_in_place(self, symbol_layer, kind: str) -> None:
        """把控件值写进已有的简单符号层（未暴露的属性原样保留）。

        样式项只在表里查得到时才写（见 :func:`_style_enum`）；颜色 / 宽度是数值，
        直接写不受影响。
        """
        if kind == "polygon":
            symbol_layer.setColor(self.fill_color_btn.color())
            fill_style = _style_enum(_FILL_STYLES, self.fill_style_combo.currentData())
            if fill_style is not None:
                symbol_layer.setBrushStyle(fill_style)
            symbol_layer.setStrokeColor(self.stroke_color_btn.color())
            symbol_layer.setStrokeWidth(self.stroke_width_spin.value())
            stroke_style = _style_enum(
                _OUTLINE_STYLES, self.stroke_style_combo.currentData()
            )
            if stroke_style is not None:
                symbol_layer.setStrokeStyle(stroke_style)
        elif kind == "line":
            symbol_layer.setColor(self.line_color_btn.color())
            symbol_layer.setWidth(self.line_width_spin.value())
            line_style = _style_enum(_LINE_STYLES, self.line_style_combo.currentData())
            if line_style is not None:
                symbol_layer.setPenStyle(line_style)
        elif kind == "point":
            shape = _style_enum(_MARKER_SHAPES, self.marker_shape_combo.currentData())
            if shape is not None:
                symbol_layer.setShape(shape)
            symbol_layer.setColor(self.marker_color_btn.color())
            symbol_layer.setSize(self.marker_size_spin.value())
            symbol_layer.setStrokeColor(self.marker_stroke_color_btn.color())
            symbol_layer.setStrokeWidth(self.marker_stroke_width_spin.value())

    def _confirm_replace(self, renderer, target: str = "single") -> bool:
        """替换现有渲染配置前的确认；用户点「否」则整页中止。

        ``target`` 是替换成什么（``"single"`` / ``"categorized"``），文案按它变。
        """
        name = "分类" if target == "categorized" else "单一符号"
        if isinstance(renderer, QgsSingleSymbolRenderer):
            text = (
                "该图层当前的符号不是简单符号（如 SVG 标记 / 几何生成器）。\n"
                f"本页只能编辑简单符号，确定替换为{name}吗？"
            )
        else:
            text = (
                f"该图层当前的渲染方式为「{_renderer_name(renderer)}」，"
                f"确定替换为{name}吗？原有渲染配置会丢失。"
            )
        answer = QMessageBox.question(
            self,
            "图层属性",
            text,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return answer == QMessageBox.StandardButton.Yes

    def apply(self, layer) -> bool:
        """把控件值写回图层。两条路见模块 docstring。"""
        kind = _geom_kind(layer)
        if kind is None:
            return True
        if (self.renderer_combo.currentData() or "single") == "categorized":
            return self._apply_categorized(layer, kind)
        return self._apply_single(layer, kind)

    def _apply_single(self, layer, kind: str) -> bool:
        """单一符号：原地改优先，否则确认后换成简单符号（今天的行为，未变）。"""
        renderer = layer.renderer()
        symbol = renderer.symbol() if isinstance(renderer, QgsSingleSymbolRenderer) else None
        simple = _simple_layer(symbol, kind)
        if simple is None and not self._confirm_replace(renderer):
            return False
        try:
            if simple is not None:
                self._write_in_place(simple, kind)
            else:
                layer.setRenderer(QgsSingleSymbolRenderer(self._build_symbol(kind)))
        except Exception as exc:  # noqa: BLE001
            logger.error("应用渲染设置失败: %s", exc, exc_info=True)
            QMessageBox.warning(self, "图层属性", f"应用渲染设置失败：{exc}")
            return False
        self._geom = kind
        self._refresh_preview()
        return True

    def _apply_categorized(self, layer, kind: str) -> bool:
        """分类：整体新建一个渲染器换上去（校验必须先于任何写操作）。"""
        field = self.field_combo.currentData()
        if not field or not self._cats:
            # 外壳在前一页可能已改过图层，本页返回 False 时不能再留下半成品，
            # 所以这里在任何写操作之前就把住
            QMessageBox.warning(self, "图层属性", "还没有类别，请先选择字段并点「分类」。")
            return False
        renderer = layer.renderer()
        if not isinstance(
            renderer, (QgsSingleSymbolRenderer, QgsCategorizedSymbolRenderer)
        ) and not self._confirm_replace(renderer, target="categorized"):
            return False
        try:
            # 逐类 clone：渲染器与我方 _cats 各自拥有自己的符号，避免双重释放
            cats = [
                QgsRendererCategory(cat.value, cat.symbol.clone(), cat.label)
                for cat in self._cats
            ]
            new_renderer = QgsCategorizedSymbolRenderer(field, cats)
            source = self._source_symbol or self._build_symbol(kind)
            if source is not None:
                # **必须在 setRenderer 之前**：兜底符号丢了，不在任何类别里的字段值
                # 会从画布上静默消失（setRenderer 之后拿到的可能已不是同一个对象）
                new_renderer.setSourceSymbol(source.clone())
            layer.setRenderer(new_renderer)
        except Exception as exc:  # noqa: BLE001
            logger.error("应用分类渲染设置失败: %s", exc, exc_info=True)
            QMessageBox.warning(self, "图层属性", f"应用分类渲染设置失败：{exc}")
            return False
        self._geom = kind
        self._refresh_preview()
        return True
