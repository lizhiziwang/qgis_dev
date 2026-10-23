"""图层属性「渲染」页（简单符号 + 按字段值分类）。

「渲染方式」两种，可切换：

- **单一符号**：整层一个符号。按几何类型只暴露「简单符号」的常用项——
  面（填充颜色 / 填充样式、描边颜色 / 宽度 / 样式）、线（颜色 / 线宽 / 线型）、
  点（形状 / 颜色 / 大小、描边颜色 / 宽度）。
- **分类（按字段值）**：选字段与色带 → 点「分类」按去重值生成类别；类别列在表格里
  （**符号 / 值 / 图例**）：行首勾选框控制该类是否渲染，点击符号格的**色块**弹出
  本应用的符号选择器，图例列是说明文字可直接编辑（字段挂了字典时默认显示字典名称）。
  表格下方提供新增 / 删除 / 删除全部 / 删除未使用。分类模式**不显示**顶部「符号预览」
  与下面的填充 / 线 / 点参数组（每个类别的符号在各自弹窗里编辑）。

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

import os
from dataclasses import dataclass

from qgis.core import (
    QgsCategorizedSymbolRenderer,
    QgsFillSymbol,
    QgsGraduatedSymbolRenderer,
    QgsInvertedPolygonRenderer,
    QgsLineSymbol,
    QgsLineSymbolLayer,
    QgsMarkerSymbol,
    QgsMapLayer,
    QgsPointDisplacementRenderer,
    QgsProject,
    QgsReadWriteContext,
    QgsRenderContext,
    QgsRendererCategory,
    QgsRuleBasedRenderer,
    QgsSimpleFillSymbolLayer,
    QgsSimpleLineSymbolLayer,
    QgsSimpleMarkerSymbolLayer,
    QgsSingleSymbolRenderer,
    QgsStyle,
    QgsSymbol,
    QgsSymbolLayerUtils,
    QgsUnitTypes,
    QgsVectorLayer,
)
from qgis.PyQt.QtCore import QSize, Qt, QVariant, pyqtSignal
from qgis.PyQt.QtGui import QColor, QIcon, QPainter, QPixmap
from qgis.PyQt.QtXml import QDomDocument
from qgis.PyQt.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.core.util.dict_values import load_field_dict
from app.infra.logging import get_logger
from app.ui.common.field_combo import make_field_combo_searchable
from app.ui.dialogs.layer_properties.base import LayerPropertyTab
from app.ui.dialogs.layer_properties.widgets import ColorButton
from app.ui.tools.feature_attrs import field_alias

logger = get_logger(__name__)

#: 符号预览图尺寸
_PREVIEW_SIZE = QSize(180, 110)

#: 类别表格的列（下标即列号）：符号（勾选 + 符号预览，点击编辑）/ 值 / 图例（说明）
_CAT_COL_SYMBOL = 0
_CAT_COL_VALUE = 1
_CAT_COL_LEGEND = 2
_CAT_HEADERS = ("符号", "值", "图例")

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

#: 「无颜色」：全透明。填充 / 描边按钮用它表示不填充 / 不描边
_NO_COLOR = QColor(0, 0, 0, 0)

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

#: 分类可选的色带（第一项 = 固定分类调色板；其余按 QGIS 样式库里的色带采样）
_COLOR_RAMPS = (
    ("默认分类色", ""),
    ("彩虹", "Spectral"),
    ("红-黄-绿", "RdYlGn"),
    ("红-黄-蓝", "RdYlBu"),
    ("红-蓝", "RdBu"),
    ("红-灰", "RdGy"),
    ("红-紫", "RdPu"),
    ("紫-绿", "PRGn"),
    ("紫-橙", "PuOr"),
    ("粉-绿", "PiYG"),
    ("黄-绿", "YlGn"),
    ("黄-绿-蓝", "YlGnBu"),
    ("黄-橙", "YlOrBr"),
    ("黄-橙-红", "YlOrRd"),
    ("橙-红", "OrRd"),
    ("橙", "Oranges"),
    ("蓝", "Blues"),
    ("蓝-绿", "BuGn"),
    ("蓝-紫", "BuPu"),
    ("绿", "Greens"),
    ("红", "Reds"),
    ("紫", "Purples"),
    ("灰阶", "Greys"),
    ("岩浆", "Magma"),
    ("火焰", "Inferno"),
    ("等离子", "Plasma"),
    ("火箭", "Rocket"),
    ("涡轮", "Turbo"),
    ("Viridis", "Viridis"),
    ("Cividis", "Cividis"),
)

#: 色带预览图标的尺寸（尽量占满下拉框）
_RAMP_ICON_SIZE = QSize(320, 18)

#: 面符号「无填充」时描边用的宽度
_NO_FILL_STROKE_WIDTH = 0.6

#: 类别符号预览图尺寸与按钮尺寸（显示真实符号样式，含斜线等填充图案）
_CAT_SYMBOL_PREVIEW = QSize(44, 26)
_CAT_PREVIEW_BUTTON = QSize(50, 32)

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


def _set_layer_prop(symbol, name, value) -> None:
    """对符号的每个符号层调 setter（颜色 / 大小 / 线宽 / 描边），不支持的层跳过。

    逐层调而不是用 ``QgsSymbol.setColor`` 这类符号级 setter：多符号层里可能混着
    不支持该属性的层（如标记线没有宽度），逐层跳过不会让整条改属性失败。
    """
    try:
        count = symbol.symbolLayerCount()
    except Exception:  # noqa: BLE001
        return
    for index in range(count):
        try:
            layer = symbol.symbolLayer(index)
        except Exception:  # noqa: BLE001
            continue
        _call_setter(layer, name, value)


def _call_setter(obj, name, value) -> None:
    """调 ``obj.name(value)``；没有该方法 / 抛异常就跳过。"""
    fn = getattr(obj, name, None)
    if not callable(fn):
        return
    try:
        fn(value)
    except Exception:  # noqa: BLE001
        pass


def _first_line_layer(symbol):
    """面符号里的第一个线层（轮廓 / 图案边界）；没有返回 ``None``。"""
    try:
        for index in range(symbol.symbolLayerCount()):
            layer = symbol.symbolLayer(index)
            if isinstance(layer, QgsLineSymbolLayer):
                return layer
    except Exception:  # noqa: BLE001
        return None
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

    表里没有的名字（图层用别的途径设的样式，例如 SVG 形状的完整路径）临时追加
    一项显示「其它」——**不要把路径塞进可见文本**；原始名字放在 tooltip 里。
    """
    if not name:
        return
    idx = combo.findData(name)
    if idx < 0:
        combo.addItem("其它", name)
        idx = combo.count() - 1
        combo.setItemData(idx, str(name), Qt.ItemDataRole.ToolTipRole)
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


def _ramp_colors(name: str, count: int) -> list[QColor]:
    """按色带取 ``count`` 个等距颜色；``name`` 为空时用固定分类调色板。"""
    count = max(0, int(count))
    if count == 0:
        return []
    if not name:
        return [_cat_color(index) for index in range(count)]
    try:
        # 色带对象要绑在局部变量上再用：临时对象被回收后 color() 可能崩
        ramp = QgsStyle.defaultStyle().colorRamp(name)
    except Exception:  # noqa: BLE001 —— 读不到就退回默认调色板
        ramp = None
    if ramp is None:
        return [_cat_color(index) for index in range(count)]
    if count == 1:
        steps = [0.5]
    else:
        steps = [index / (count - 1) for index in range(count)]
    try:
        return [QColor(ramp.color(step)) for step in steps]
    except Exception:  # noqa: BLE001
        return [_cat_color(index) for index in range(count)]


def _ramp_icon(name: str, width: int = 64, height: int = 14) -> QIcon:
    """色带下拉项的渐变预览（默认分类色画成循环色条）。"""
    pixmap = QPixmap(width, height)
    pixmap.fill(QColor(0, 0, 0, 0))
    painter = QPainter(pixmap)
    try:
        for index, color in enumerate(_ramp_colors(name, width)):
            painter.fillRect(index, 0, 1, height, color)
        painter.setPen(QColor(120, 120, 120))
        painter.drawRect(0, 0, width - 1, height - 1)
    finally:
        painter.end()
    return QIcon(pixmap)


def _value_map_labels(field) -> dict[str, str] | None:
    """QGIS ``ValueMap`` 配置：``{代码: 显示名}``；不是这种字段返回 ``None``。"""
    try:
        setup = field.editorWidgetSetup()
    except Exception:  # noqa: BLE001 —— 老驱动可能没有 widget 配置
        return None
    if setup.type() != "ValueMap":
        return None
    config = setup.config()
    mapping = config.get("map") if isinstance(config, dict) else None
    if not mapping:
        return None
    if isinstance(mapping, dict):
        return {str(code): str(label) for code, label in mapping.items()}
    labels: dict[str, str] = {}
    for pair in mapping:  # PyQGIS 里通常是 [(code, label), ...]
        if isinstance(pair, (list, tuple)) and len(pair) >= 2:
            labels[str(pair[0])] = str(pair[1])
    return labels or None


def _field_value_labels(layer, field_name: str) -> dict[str, str]:
    """字段的「原始值 → 显示名」：``ValueMap`` 优先，其次本地 / 系统字典。"""
    labels: dict[str, str] = {}
    if not isinstance(layer, QgsVectorLayer) or not field_name:
        return labels
    try:
        field = layer.fields().field(field_name)
    except Exception:  # noqa: BLE001
        field = None
    if field is not None:
        labels.update(_value_map_labels(field) or {})
    if not labels:
        try:
            info = load_field_dict(layer, field_name)
        except Exception as exc:  # noqa: BLE001 —— 读不到字典就按原始编码显示
            logger.info("读取字段「%s」字典失败：%s", field_name, exc)
            info = None
        if info is not None:
            labels = {
                str(item.code): str(item.name)
                for item in info.values
                if str(item.code)
            }
    return labels


def default_symbol(kind: str):
    """按几何类型造一个默认简单符号（新建类别 / 单一符号重置用）。"""
    if kind == "polygon":
        return QgsFillSymbol.createSimple(
            {
                "color": _DEFAULT_FILL.name(),
                "outline_color": _DEFAULT_STROKE.name(),
                "outline_width": "0.26",
            }
        )
    if kind == "line":
        return QgsLineSymbol.createSimple(
            {"line_color": _DEFAULT_STROKE.name(), "line_width": "0.26"}
        )
    if kind == "point":
        return QgsMarkerSymbol.createSimple(
            {
                "name": "circle",
                "color": _DEFAULT_MARKER.name(),
                "size": "4.0",
                "outline_color": _DEFAULT_STROKE.name(),
                "outline_width": "0.0",
            }
        )
    return None


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
    #: 该类是否参与渲染（列表第一列的勾选框；对应渲染器的 renderState）
    render: bool = True


class _ClickableLabel(QLabel):
    """可点击的预览框（点击打开符号选择器）。"""

    clicked = pyqtSignal()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 —— Qt 命名
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mouseReleaseEvent(event)


class _SymbolPreviewButton(QPushButton):
    """类别符号预览：显示真实符号样式（含斜线等填充图案），无下拉三角。

    点击打开符号选择器编辑该类别的符号。用 ``symbolPreviewPixmap`` 而不是
    单纯涂一个颜色 —— 颜色块看不出斜线 / 图案填充，和实际渲染不一致。
    """

    def __init__(self, symbol, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("CategorySymbolPreview")
        self.setFixedSize(_CAT_PREVIEW_BUTTON)
        self.setIconSize(_CAT_SYMBOL_PREVIEW)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip("点击编辑该类别的符号")
        self.setStyleSheet(
            "QPushButton#CategorySymbolPreview {"
            " border: 1px solid palette(mid); background: palette(base);"
            " border-radius: 3px; }"
            "QPushButton#CategorySymbolPreview:hover {"
            " border-color: palette(highlight); }"
        )
        self.set_symbol(symbol)

    def set_symbol(self, symbol) -> None:
        """重画预览（符号为空时清空图标）。"""
        try:
            pixmap = QgsSymbolLayerUtils.symbolPreviewPixmap(
                symbol, _CAT_SYMBOL_PREVIEW
            )
            self.setIcon(QIcon(pixmap))
        except Exception:  # noqa: BLE001 —— 预览画不出来不能拖垮表格
            self.setIcon(QIcon())


class SymbolParamsWidget(QWidget):
    """面 / 线 / 点符号的常用参数控件（符号选择器右侧用）。

    只暴露**通用项**（颜色 / 大小 / 线宽）：形状 / 填充样式 / 多符号层 / SVG 等由左侧
    符号库或「编辑符号…」决定。改参数时在 :attr:`_template`（选中符号的拷贝）上原地
    改，而不是重新 ``createSimple`` —— 否则 SVG 标记、多符号层会被悄悄换成默认简单
    标记（选中的图标整个变样）。

    [`set_symbol`] 回填控件并记下模板，[`build_symbol`] 按控件值更新模板；任何控件
    变化发 :attr:`changed`，由宿主实时刷新预览。
    """

    changed = pyqtSignal()

    def __init__(self, kind: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._kind = kind
        self._unit = QgsUnitTypes.RenderUnit.Millimeters
        self._loading = False
        #: 当前符号的模板：右侧控件只改颜色 / 大小 / 线宽，其它属性原样保留
        self._template = None
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(8)
        if kind == "polygon":
            root.addWidget(self._build_fill_group())
        elif kind == "line":
            root.addWidget(self._build_line_group())
        elif kind == "point":
            root.addWidget(self._build_marker_group())
        root.addStretch(1)

    def _new_form(self, parent) -> QFormLayout:
        form = QFormLayout(parent)
        form.setLabelAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        form.setHorizontalSpacing(8)
        form.setVerticalSpacing(8)
        return form

    def _new_size_spin(self, parent, high: float, step: float, value: float):
        spin = QDoubleSpinBox(parent)
        spin.setRange(0.0, high)
        spin.setDecimals(2)
        spin.setSingleStep(step)
        spin.setValue(value)
        spin.valueChanged.connect(self._on_changed)
        return spin

    def _build_fill_group(self) -> QGroupBox:
        box = QGroupBox("面符号（填充 / 描边）", self)
        form = self._new_form(box)

        self.fill_color_btn = ColorButton(_DEFAULT_FILL, box)
        self.fill_color_btn.color_changed.connect(self._on_changed)
        form.addRow("填充颜色", self.fill_color_btn)

        self.stroke_color_btn = ColorButton(_DEFAULT_STROKE, box)
        self.stroke_color_btn.color_changed.connect(self._on_changed)
        form.addRow("描边颜色", self.stroke_color_btn)

        self.stroke_width_spin = self._new_size_spin(box, 100.0, 0.1, 0.26)
        form.addRow("描边宽度", self.stroke_width_spin)
        return box

    def _build_line_group(self) -> QGroupBox:
        box = QGroupBox("线符号", self)
        form = self._new_form(box)

        self.line_color_btn = ColorButton(_DEFAULT_STROKE, box)
        self.line_color_btn.color_changed.connect(self._on_changed)
        form.addRow("线颜色", self.line_color_btn)

        self.line_width_spin = self._new_size_spin(box, 1000.0, 0.1, 0.26)
        form.addRow("线宽", self.line_width_spin)
        return box

    def _build_marker_group(self) -> QGroupBox:
        box = QGroupBox("点符号", self)
        form = self._new_form(box)

        self.marker_color_btn = ColorButton(_DEFAULT_MARKER, box)
        self.marker_color_btn.color_changed.connect(self._on_changed)
        form.addRow("颜色", self.marker_color_btn)

        self.marker_size_spin = self._new_size_spin(box, 5000.0, 0.5, 4.0)
        form.addRow("大小", self.marker_size_spin)

        self.marker_stroke_color_btn = ColorButton(_DEFAULT_STROKE, box)
        self.marker_stroke_color_btn.color_changed.connect(self._on_changed)
        form.addRow("描边颜色", self.marker_stroke_color_btn)

        self.marker_stroke_width_spin = self._new_size_spin(box, 100.0, 0.1, 0.0)
        form.addRow("描边宽度", self.marker_stroke_width_spin)
        return box

    def _on_changed(self, *_) -> None:
        if not self._loading:
            self.changed.emit()

    def set_symbol(self, symbol) -> None:
        """把符号回填到控件，并记下模板（之后改参数时在它上面原地改）。"""
        if symbol is None:
            return
        # 模板必须持有完整符号：SVG / 多符号层的其它属性靠它保留
        self._template = symbol.clone()
        self._loading = True
        try:
            unit = _call(symbol, "outputUnit")
            if unit is not None:
                self._unit = unit
            self._apply_symbol_values(symbol)
            self._sync_units()
        finally:
            self._loading = False

    def _apply_symbol_values(self, symbol) -> None:
        """把符号（匹配几何的简单层 / 首层）的值回填到控件（读不到的项保持默认）。"""
        symbol_layer = _simple_layer(symbol, self._kind) or _first_layer(symbol)
        if self._kind == "polygon":
            # 面符号的"描边"可能是填充层的 strokeColor，也可能是一条独立的线层
            # （outline-only / 图案填充的边界）——后者用 color / width。
            if not isinstance(symbol_layer, QgsLineSymbolLayer):
                brush = _call(symbol_layer, "brushStyle")
                self._set_color_or_none(
                    self.fill_color_btn,
                    _call(symbol_layer, "color"),
                    brush,
                    Qt.BrushStyle.NoBrush,
                )
            else:
                # 只有轮廓线、没有填充
                self._set_color(self.fill_color_btn, _NO_COLOR)
            border = _first_line_layer(symbol)
            if border is not None:
                self._set_color_or_none(
                    self.stroke_color_btn,
                    _call(border, "color"),
                    _call(border, "penStyle"),
                    Qt.PenStyle.NoPen,
                )
                self._set_value(self.stroke_width_spin, _call(border, "width"))
            else:
                self._set_color_or_none(
                    self.stroke_color_btn,
                    _call(symbol_layer, "strokeColor"),
                    _call(symbol_layer, "strokeStyle"),
                    Qt.PenStyle.NoPen,
                )
                self._set_value(
                    self.stroke_width_spin, _call(symbol_layer, "strokeWidth")
                )
        elif self._kind == "line":
            self._set_color_or_none(
                self.line_color_btn,
                _call(symbol_layer, "color"),
                _call(symbol_layer, "penStyle"),
                Qt.PenStyle.NoPen,
            )
            self._set_value(self.line_width_spin, _call(symbol_layer, "width"))
        elif self._kind == "point":
            self._set_color_or_none(
                self.marker_color_btn, _call(symbol_layer, "color")
            )
            self._set_value(self.marker_size_spin, _call(symbol_layer, "size"))
            self._set_color_or_none(
                self.marker_stroke_color_btn,
                _call(symbol_layer, "strokeColor"),
            )
            self._set_value(
                self.marker_stroke_width_spin, _call(symbol_layer, "strokeWidth")
            )

    @staticmethod
    def _set_color(button: ColorButton, color) -> None:
        if color is not None:
            button.set_color(color, emit=False)

    @classmethod
    def _set_color_or_none(
        cls, button: ColorButton, color, style=None, no_style=None
    ) -> None:
        """回填颜色按钮；透明色或 NoBrush / NoPen 都显示为「无颜色」。"""
        if color is None:
            return
        value = QColor(color)
        if value.alpha() == 0 or (
            style is not None and no_style is not None and style == no_style
        ):
            button.set_color(_NO_COLOR, emit=False)
        else:
            button.set_color(value, emit=False)

    @staticmethod
    def _set_value(spin: QDoubleSpinBox, value) -> None:
        if value is None:
            return
        try:
            spin.setValue(float(value))
        except (TypeError, ValueError):
            pass

    def _sync_units(self) -> None:
        suffix = f" {_UNIT_NAMES.get(self._unit, '毫米')}"
        for name in (
            "stroke_width_spin",
            "line_width_spin",
            "marker_size_spin",
            "marker_stroke_width_spin",
        ):
            spin = getattr(self, name, None)
            if spin is not None:
                spin.setSuffix(suffix)

    def build_symbol(self):
        """按控件值更新当前符号（保留符号类型 / 多符号层 / SVG 等其它属性）。"""
        symbol = (
            self._template.clone()
            if self._template is not None
            else default_symbol(self._kind)
        )
        if symbol is None:
            return None
        if self._kind == "polygon":
            self._update_fill(symbol)
        elif self._kind == "line":
            self._update_line(symbol)
        elif self._kind == "point":
            self._update_marker(symbol)
        else:
            return None
        symbol.setOutputUnit(self._unit)
        # 把结果留作下一次的模板：否则"调宽度时恢复的实线描边"下一次重建又丢了
        # （模板每次都是从 set_symbol 时的原符号 clone 出来的）
        self._template = symbol.clone()
        return symbol

    def _update_fill(self, symbol) -> None:
        """填充色 / 描边逐层改（不支持的层跳过，其它样式 / 层原样保留）。

        透明色（颜色框选「无颜色」）= 无填充 / 无描边：同时把画刷 / 画笔切成
        NoBrush / NoPen，下次回填颜色按钮才会显示为「无颜色」；再选回普通颜色
        时恢复实线，颜色才真正生效。
        """
        fill = self.fill_color_btn.color()
        stroke = self.stroke_color_btn.color()
        width = self.stroke_width_spin.value()
        try:
            count = symbol.symbolLayerCount()
        except Exception:  # noqa: BLE001
            return
        for index in range(count):
            try:
                layer = symbol.symbolLayer(index)
            except Exception:  # noqa: BLE001
                continue
            if isinstance(layer, QgsLineSymbolLayer):
                if stroke.alpha() == 0:
                    _call_setter(layer, "setPenStyle", Qt.PenStyle.NoPen)
                elif _call(layer, "penStyle") == Qt.PenStyle.NoPen:
                    _call_setter(layer, "setPenStyle", Qt.PenStyle.SolidLine)
                _call_setter(layer, "setColor", stroke)
                _call_setter(layer, "setWidth", width)
                continue
            # 填充层
            if fill.alpha() == 0:
                _call_setter(layer, "setBrushStyle", Qt.BrushStyle.NoBrush)
            elif _call(layer, "brushStyle") == Qt.BrushStyle.NoBrush:
                _call_setter(layer, "setBrushStyle", Qt.BrushStyle.SolidPattern)
            _call_setter(layer, "setColor", fill)
            if stroke.alpha() == 0:
                _call_setter(layer, "setStrokeStyle", Qt.PenStyle.NoPen)
            elif _call(layer, "strokeStyle") == Qt.PenStyle.NoPen:
                _call_setter(layer, "setStrokeStyle", Qt.PenStyle.SolidLine)
            _call_setter(layer, "setStrokeColor", stroke)
            _call_setter(layer, "setStrokeWidth", width)

    def _update_line(self, symbol) -> None:
        color = self.line_color_btn.color()
        if color.alpha() == 0:
            _set_layer_prop(symbol, "setPenStyle", Qt.PenStyle.NoPen)
        else:
            try:
                for index in range(symbol.symbolLayerCount()):
                    layer = symbol.symbolLayer(index)
                    if _call(layer, "penStyle") == Qt.PenStyle.NoPen:
                        _call_setter(layer, "setPenStyle", Qt.PenStyle.SolidLine)
            except Exception:  # noqa: BLE001
                pass
        _set_layer_prop(symbol, "setColor", color)
        _set_layer_prop(symbol, "setWidth", self.line_width_spin.value())

    def _update_marker(self, symbol) -> None:
        """颜色 / 大小对 SVG、字体标记等同样有效；不支持的符号层静默跳过。"""
        _set_layer_prop(symbol, "setColor", self.marker_color_btn.color())
        _set_layer_prop(symbol, "setSize", self.marker_size_spin.value())
        _set_layer_prop(symbol, "setStrokeColor", self.marker_stroke_color_btn.color())
        _set_layer_prop(
            symbol, "setStrokeWidth", self.marker_stroke_width_spin.value()
        )


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
        #: 单一符号模式当前选中的符号（在符号选择器里改；apply 时落图层）
        self._single_symbol = None
        #: 分类渲染器的兜底符号（不属于任何类别的值用它画）；None = 不画
        self._source_symbol = None
        #: 当前选中的类别下标；-1 = 无（单一符号模式恒为 -1）
        self._selected = -1
        #: 用户是否在本页动过东西。外壳点「确定 / 应用」会把**每一页**都 apply 一遍，
        #: 本页在"当前符号不是简单符号"时会弹「是否替换为单一符号」——用户只是改了别的
        #: 页也会被这条提示拦住，答"否"还会让整个对话框关不掉。所以没改动就什么都不做。
        self._dirty = False
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
        renderer_row = QWidget(self)
        renderer_layout = QHBoxLayout(renderer_row)
        renderer_layout.setContentsMargins(0, 0, 0, 0)
        renderer_layout.setSpacing(6)
        self.renderer_combo = QComboBox(renderer_row)
        self.renderer_combo.addItem("单一符号", "single")
        self.renderer_combo.addItem("分类（按字段值）", "categorized")
        self.renderer_combo.setMinimumWidth(180)
        renderer_layout.addWidget(self.renderer_combo, 1)
        self.import_style_btn = QPushButton("导入样式…", renderer_row)
        self.import_style_btn.setToolTip(
            "从工程内同几何类型的图层或外部样式文件导入渲染方式"
        )
        self.import_style_btn.clicked.connect(self._on_import_style)
        renderer_layout.addWidget(self.import_style_btn)
        form.addRow("渲染方式", renderer_row)

        preview_row = QWidget(self)
        pr = QHBoxLayout(preview_row)
        pr.setContentsMargins(0, 0, 0, 0)
        pr.setSpacing(6)
        self.preview_label = _ClickableLabel(self)
        self.preview_label.setFixedSize(_PREVIEW_SIZE)
        self.preview_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview_label.setCursor(Qt.CursorShape.PointingHandCursor)
        self.preview_label.setToolTip("点击打开符号选择器")
        # 与符号选择器里的「当前符号」预览同一套背景 / 边框
        self.preview_label.setStyleSheet(
            "QLabel { border: 1px solid palette(mid); background: palette(base);"
            " border-radius: 3px; }"
        )
        self.preview_label.clicked.connect(self._pick_single_symbol)
        pr.addWidget(self.preview_label)
        pr.addStretch(1)
        # 分类模式不需要这块预览（各类别的颜色在表格里直接看 / 点），
        # _sync_visibility 里按模式把整行（标签 + 预览）一起收起
        self.preview_caption = QLabel("符号预览", self)
        self.preview_row = preview_row
        form.addRow(self.preview_caption, preview_row)
        root.addLayout(form)

        self.hint_label = QLabel("", self)
        self.hint_label.setObjectName("PropHint")
        self.hint_label.setWordWrap(True)
        self.hint_label.setVisible(False)
        root.addWidget(self.hint_label)

        self.categorize_group = self._build_categorize_group()
        root.addWidget(self.categorize_group, 1)
        root.addStretch(0)

        self.renderer_combo.currentIndexChanged.connect(self._on_mode_changed)

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
        self.field_combo.setMinimumWidth(150)
        # 字段名不需要一直撑满整行：把空间让给色带，否则色带挤得看不清
        self.field_combo.setMaximumWidth(220)
        make_field_combo_searchable(self.field_combo)
        top.addWidget(self.field_combo, 0)
        ramp_label = QLabel("色带", box)
        ramp_label.setObjectName("PropHint")
        top.addWidget(ramp_label)
        self.ramp_combo = QComboBox(box)
        self.ramp_combo.setMinimumWidth(420)
        self.ramp_combo.setIconSize(_RAMP_ICON_SIZE)
        # 下拉弹出列表用 view 自己的 iconSize，不设的话色带还是被缩成小方块
        ramp_view = self.ramp_combo.view()
        if ramp_view is not None:
            ramp_view.setIconSize(_RAMP_ICON_SIZE)
        self.ramp_combo.setToolTip("点「分类」时按所选色带给各类别配色（分类后可再逐个改）")
        for ramp_label_text, ramp_name in _COLOR_RAMPS:
            self.ramp_combo.addItem(
                _ramp_icon(
                    ramp_name, _RAMP_ICON_SIZE.width(), _RAMP_ICON_SIZE.height()
                ),
                ramp_label_text,
                ramp_name,
            )
        top.addWidget(self.ramp_combo, 1)
        # 面符号可选「无填充」：色带颜色只用于描边
        self.no_fill_check = QCheckBox("无填充", box)
        self.no_fill_check.setToolTip("面符号只按色带颜色描边，不填充")
        self.no_fill_check.toggled.connect(self._on_no_fill_toggled)
        top.addWidget(self.no_fill_check)
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
        self.cat_table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        # 符号列是勾选框 + 色块（点色块弹本应用的符号选择器）；「图例」列可编辑
        # （说明文字），其余列只读。
        self.cat_table.setEditTriggers(
            QAbstractItemView.EditTrigger.DoubleClicked
            | QAbstractItemView.EditTrigger.EditKeyPressed
        )
        # 不开排序：类别下标 ↔ 行号就是映射，行一漂就写错行
        self.cat_table.setSortingEnabled(False)
        header = self.cat_table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setMinimumSectionSize(48)
        header.setStretchLastSection(False)
        self.cat_table.setColumnWidth(_CAT_COL_SYMBOL, 84)
        self.cat_table.setColumnWidth(_CAT_COL_VALUE, 110)
        header.setSectionResizeMode(_CAT_COL_LEGEND, QHeaderView.ResizeMode.Stretch)
        header.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.cat_table.setMinimumHeight(110)
        self.cat_table.itemSelectionChanged.connect(self._on_category_selected)
        self.cat_table.itemChanged.connect(self._on_cat_item_changed)
        lay.addWidget(self.cat_table, 1)

        # 列表操作（参照 QGIS 分类列表下方的 + / - / 全删 / 删除未使用）
        ops = QHBoxLayout()
        ops.setSpacing(6)
        for text, slot, tip in (
            ("新增", self._add_category, "新增一个类别"),
            ("删除", self._delete_categories, "删除选中的类别"),
            ("删除全部", self._delete_all_categories, "删除全部类别"),
            ("删除未使用", self._delete_unused_categories,
             "删除图层数据里已经不存在的类别值"),
        ):
            btn = QPushButton(text, box)
            btn.setToolTip(tip)
            btn.clicked.connect(slot)
            ops.addWidget(btn)
        ops.addStretch(1)
        lay.addLayout(ops)
        return box

    # ------------------------------------------------------------- 读取

    def load(self, layer) -> None:
        """从 ``layer`` 读值填控件。"""
        self._layer = layer
        self._geom = _geom_kind(layer)
        self._cats = []
        self._single_symbol = None
        self._source_symbol = None
        self._selected = -1
        # 刚读进来 = 用户还没动过；回填过程本身不算"改动"
        self._dirty = False
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
        """单一符号（或别的渲染器）→ 记下当前符号，供预览与符号选择器使用。"""
        self._set_combo_mode("single")
        self._populate_fields(None)
        self._single_symbol = None
        renderer = self._layer.renderer() if self._layer is not None else None
        if isinstance(renderer, QgsSingleSymbolRenderer):
            symbol = renderer.symbol()
            if symbol is not None:
                self._single_symbol = symbol.clone()
        if self._single_symbol is None:
            self._single_symbol = default_symbol(self._geom)

    def _load_categorized(self, renderer) -> None:
        """已有的分类渲染器 → 把字段与各类别读进来（保留原有颜色 / 未暴露属性）。"""
        self._set_combo_mode("categorized")
        # 切回「单一符号」时要有东西可画：先准备一个默认符号
        if self._single_symbol is None and self._geom is not None:
            self._single_symbol = default_symbol(self._geom)
        field = renderer.classAttribute()
        self._populate_fields(field)
        # 字段挂了字典时，图例列默认显示字典名称而不是原始编码；只替换那些
        # 「图例文本 == 原始值」的默认标签，用户自己改过的说明保持不动。
        labels = _field_value_labels(self._layer, field)
        cats: list[_Category] = []
        try:
            # categories() 给的是拷贝，且其 symbol 与渲染器共享指针 —— 每一项都必须
            # 先绑到局部变量再往下取，否则临时拷贝一被 GC 符号就是悬垂指针（崩）
            for i in range(len(renderer.categories())):
                cat = renderer.categories()[i]
                symbol = cat.symbol()
                if symbol is None:
                    continue
                value = cat.value()
                label = str(cat.label())
                if label == str(value):
                    label = labels.get(str(value), label)
                cats.append(
                    _Category(
                        value=value,
                        label=label,
                        symbol=symbol.clone(),
                        render=bool(cat.renderState()),
                    )
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("读取分类渲染器类别失败: %s", exc)
            cats = []
        self._cats = cats
        if self._geom == "polygon":
            brush = (
                _call(_simple_layer(cats[0].symbol, "polygon"), "brushStyle")
                if cats
                else None
            )
            self.no_fill_check.blockSignals(True)
            self.no_fill_check.setChecked(brush == Qt.BrushStyle.NoBrush)
            self.no_fill_check.blockSignals(False)
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
            # 分类模式不显示提示语（用户要求去掉「选择分类字段后点分类…」那行）
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
        if symbol is None:
            self._set_hint("")
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
        """按模式 / 几何类型显示隐藏分类列表；参数在符号选择器里编辑。"""
        kind = self._geom
        self.renderer_combo.setEnabled(kind is not None)
        categorized = kind is not None and self._mode == "categorized"
        self.categorize_group.setVisible(categorized)
        # 「无填充」只对面试用
        self.no_fill_check.setVisible(kind == "polygon")
        # 分类模式的符号在表格里逐个改，顶部「符号预览」不再需要
        show_preview = kind is not None and not categorized
        self.preview_caption.setVisible(show_preview)
        self.preview_row.setVisible(show_preview)

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
        """刷新当前预览（单一符号 / 分类模式指向不同的符号来源）。"""
        self._refresh_preview()

    def _commit_target(self, *_) -> None:
        """**唯一的控件变化槽**：把控件值写回当前目标，再刷预览与那一行。

        单一符号模式没有目标可写（控件即真值，:meth:`apply` 时才落图层），只刷预览。
        跑在信号槽里，异常必须自己兜住（未捕获会原生崩溃）。
        """
        if self._loading or self._geom is None:
            return
        self._dirty = True  # 用户改了样式控件
        try:
            # **只有分类模式才有“当前类别”**：单一符号模式下 _cats 为空，
            # 访问 self._cats[self._selected] 会 IndexError，预览永远不刷新。
            cat = (
                self._cats[self._selected]
                if self._mode == "categorized" and self._has_target()
                else None
            )
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

    def _row_of(self, cat: _Category) -> int:
        """类别在表格里的行号；用**对象身份**查（行号漂移不会写错行）。"""
        for row, item in enumerate(self._cats):
            if item is cat:
                return row
        return -1

    # ------------------------------------------------------------- 分类交互

    def _on_mode_changed(self, *_) -> None:
        """「渲染方式」切换。**不**重建 ``_cats``（只有「分类」按钮重建）。"""
        if self._loading or self._geom is None:
            return
        self._dirty = True  # 用户切了渲染方式
        self._mode = self.renderer_combo.currentData() or "single"
        if self._mode == "single" and self._single_symbol is None and self._geom:
            # 图层原本是分类渲染、切回单一符号时没有符号可显示：给一个默认符号
            self._single_symbol = default_symbol(self._geom)
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

    def _colorize_category_symbol(self, symbol, color: QColor) -> None:
        """按色带颜色给类别符号上色；面 + 勾选「无填充」时只上描边。"""
        simple = _simple_layer(symbol, self._geom)
        if simple is None:
            return
        if self._geom == "polygon" and self.no_fill_check.isChecked():
            simple.setBrushStyle(Qt.BrushStyle.NoBrush)
            simple.setStrokeColor(color)
            simple.setStrokeWidth(_NO_FILL_STROKE_WIDTH)
            return
        if self._geom == "polygon":
            # 从「无填充」切回来时恢复实心填充
            simple.setBrushStyle(Qt.BrushStyle.SolidPattern)
        simple.setColor(color)

    def _on_no_fill_toggled(self, _checked: bool) -> None:
        """「无填充」勾选变化：按当前色带重刷已有类别（面符号）。"""
        if self._geom != "polygon" or not self._cats or self._loading:
            return
        colors = _ramp_colors(
            self.ramp_combo.currentData() or "", len(self._cats)
        )
        for category, color in zip(self._cats, colors):
            self._colorize_category_symbol(category.symbol, color)
        self._dirty = True
        self._rebuild_cat_table()
        self._select_row(self._selected)
        self._refresh_preview()

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

        template = default_symbol(self._geom)
        labels = _field_value_labels(self._layer, field)
        colors = _ramp_colors(self.ramp_combo.currentData() or "", len(values))
        cats: list[_Category] = []
        for index, value in enumerate(values):
            symbol = template.clone() if template is not None else default_symbol(self._geom)
            if symbol is not None:
                self._colorize_category_symbol(symbol, colors[index])
            cats.append(
                _Category(
                    value=value,
                    # 字段挂了字典 → 图例默认显示字典名称，而不是原始编码
                    label=labels.get(str(value), str(value)),
                    symbol=symbol,
                )
            )

        self._cats = cats
        self._dirty = True  # 用户点了「分类」，类别是按本页控件新建的
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
        table = self.cat_table
        table.blockSignals(True)
        try:
            table.setRowCount(0)  # 连带清掉旧的单元格控件
            table.setRowCount(len(self._cats))
            for row, cat in enumerate(self._cats):
                table.setCellWidget(row, _CAT_COL_SYMBOL, self._symbol_cell(cat))

                value_item = QTableWidgetItem(str(cat.value))
                value_item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
                table.setItem(row, _CAT_COL_VALUE, value_item)

                # 「图例」= 说明文字，可直接编辑
                label_item = QTableWidgetItem(cat.label)
                label_item.setFlags(
                    Qt.ItemFlag.ItemIsEnabled
                    | Qt.ItemFlag.ItemIsSelectable
                    | Qt.ItemFlag.ItemIsEditable
                )
                table.setItem(row, _CAT_COL_LEGEND, label_item)
        except Exception as exc:  # noqa: BLE001
            logger.warning("重建类别表失败: %s", exc)
        finally:
            table.blockSignals(False)
            self._loading = False

    def _symbol_cell(self, cat: _Category) -> QWidget:
        """符号列：勾选框 + 色块（点击弹本应用的符号选择器改符号）。"""
        cell = QWidget(self.cat_table)
        row = QHBoxLayout(cell)
        row.setContentsMargins(2, 0, 2, 0)
        row.setSpacing(4)

        check = QCheckBox(cell)
        check.setChecked(bool(cat.render))
        check.setToolTip("是否渲染该类别")
        check.toggled.connect(
            lambda checked, c=cat: self._on_category_render_toggled(c, checked)
        )
        row.addWidget(check)

        swatch = _SymbolPreviewButton(cat.symbol, cell)
        swatch.clicked.connect(
            lambda _checked=False, c=cat: self._edit_category_symbol(c)
        )
        row.addWidget(swatch, 0, Qt.AlignmentFlag.AlignLeft)
        return cell

    def _refresh_cat_row(self, row: int) -> None:
        """刷新某一行的勾选 / 符号色块 / 值 / 图例；行数与 ``_cats`` 不同步则整表重建。"""
        if self.cat_table.rowCount() != len(self._cats):
            self._rebuild_cat_table()
            return
        if not (0 <= row < len(self._cats)):
            return
        cat = self._cats[row]
        self._loading = True
        self.cat_table.blockSignals(True)
        try:
            cell = self.cat_table.cellWidget(row, _CAT_COL_SYMBOL)
            if isinstance(cell, QWidget):
                check = cell.findChild(QCheckBox)
                if check is not None and check.isChecked() != bool(cat.render):
                    check.blockSignals(True)
                    check.setChecked(bool(cat.render))
                    check.blockSignals(False)
                swatch = cell.findChild(_SymbolPreviewButton)
                if swatch is not None:
                    swatch.set_symbol(cat.symbol)
            value_item = self.cat_table.item(row, _CAT_COL_VALUE)
            if value_item is not None:
                value_item.setText(str(cat.value))
            label_item = self.cat_table.item(row, _CAT_COL_LEGEND)
            if label_item is not None and label_item.text() != cat.label:
                label_item.setText(cat.label)
        finally:
            self.cat_table.blockSignals(False)
            self._loading = False

    def _on_cat_item_changed(self, item: QTableWidgetItem) -> None:
        """「图例」列编辑 → 更新该类别的说明文字。"""
        if self._loading or item.column() != _CAT_COL_LEGEND:
            return
        row = item.row()
        if 0 <= row < len(self._cats):
            self._cats[row].label = item.text()
            self._dirty = True

    def _edit_category_symbol(self, cat: _Category) -> None:
        """点色块：用与「单一符号」相同的符号选择器修改该类别的符号。"""
        if self._loading or self._geom is None:
            return
        from app.ui.dialogs.layer_properties.symbol_selector import (
            SymbolSelectorDialog,
        )

        dlg = SymbolSelectorDialog(cat.symbol, self._geom, self)
        try:
            accepted = dlg.exec() == QDialog.DialogCode.Accepted
            chosen = dlg.selected_symbol() if accepted else None
        finally:
            dlg.deleteLater()
        if chosen is None:
            return
        cat.symbol = chosen
        self._dirty = True
        self._refresh_cat_row(self._row_of(cat))
        self._refresh_preview()

    def _on_category_render_toggled(self, cat: _Category, checked: bool) -> None:
        """行首勾选框：该类别是否参与渲染（写回渲染器的 renderState）。"""
        if self._loading:
            return
        cat.render = bool(checked)
        self._dirty = True
        self._refresh_preview()

    # ---------------------------------------------------- 类别列表操作

    def _selected_cat_rows(self) -> list[int]:
        rows = self.cat_table.selectionModel()
        if rows is None:
            return []
        return sorted({index.row() for index in rows.selectedRows()})

    def _add_category(self) -> None:
        """新增一个类别（值手动输入；符号先按当前模板/调色板建）。"""
        if self._geom is None:
            return
        text, ok = QInputDialog.getText(self, "新增类别", "类别值：")
        if not ok or not text.strip():
            return
        value = self._coerce_category_value(text.strip())
        if any(str(cat.value) == str(value) for cat in self._cats):
            QMessageBox.information(self, "新增类别", "该类别值已存在。")
            return
        symbol = default_symbol(self._geom)
        if symbol is None:
            return
        colors = _ramp_colors(
            self.ramp_combo.currentData() or "", len(self._cats) + 1
        )
        self._colorize_category_symbol(symbol, colors[-1])
        labels = _field_value_labels(self._layer, self.field_combo.currentData() or "")
        self._cats.append(
            _Category(
                value=value,
                label=labels.get(str(value), text.strip()),
                symbol=symbol,
                render=True,
            )
        )
        self._dirty = True
        self._selected = len(self._cats) - 1
        self._rebuild_cat_table()
        self._select_row(self._selected)
        self._sync_visibility()
        self._refresh_preview()
        self._refresh_hint()

    def _delete_categories(self) -> None:
        """删除选中的类别（没选中则删当前行）。"""
        rows = self._selected_cat_rows()
        if not rows and 0 <= self._selected < len(self._cats):
            rows = [self._selected]
        if not rows:
            QMessageBox.information(self, "删除类别", "请先选中要删除的类别。")
            return
        for row in reversed(rows):
            if 0 <= row < len(self._cats):
                del self._cats[row]
        self._dirty = True
        self._selected = 0 if self._cats else -1
        self._rebuild_cat_table()
        if self._selected >= 0:
            self._select_row(self._selected)
        self._sync_visibility()
        self._refresh_preview()
        self._refresh_hint()

    def _delete_all_categories(self) -> None:
        """删除全部类别（需确认）。"""
        if not self._cats:
            return
        answer = QMessageBox.question(
            self,
            "删除全部类别",
            "确定删除全部类别吗？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._cats = []
        self._dirty = True
        self._selected = -1
        self._rebuild_cat_table()
        self._sync_visibility()
        self._refresh_preview()
        self._refresh_hint()

    def _delete_unused_categories(self) -> None:
        """删除图层数据里已不存在的类别值。"""
        field = self.field_combo.currentData()
        if not field:
            QMessageBox.information(self, "删除未使用类别", "请先选择分类字段。")
            return
        try:
            values = _unique_values(self._layer, field)
        except ValueError as exc:
            QMessageBox.warning(self, "删除未使用类别", str(exc))
            return
        if values is None:
            QMessageBox.warning(
                self, "删除未使用类别", f"读不到字段「{field}」的去重值。"
            )
            return
        used = {str(value) for value in values}
        before = len(self._cats)
        self._cats = [cat for cat in self._cats if str(cat.value) in used]
        removed = before - len(self._cats)
        if not removed:
            QMessageBox.information(self, "删除未使用类别", "没有未使用的类别。")
            return
        self._dirty = True
        self._selected = 0 if self._cats else -1
        self._rebuild_cat_table()
        if self._selected >= 0:
            self._select_row(self._selected)
        self._sync_visibility()
        self._refresh_preview()
        self._refresh_hint()
        QMessageBox.information(
            self, "删除未使用类别", f"已删除 {removed} 个未使用的类别。"
        )

    def _coerce_category_value(self, text: str):
        """输入文本 → 与字段类型匹配的类别值（否则渲染器匹配不上）。"""
        field = self.field_combo.currentData()
        layer = self._layer
        if not field or not isinstance(layer, QgsVectorLayer):
            return text
        index = layer.fields().indexOf(field)
        if index < 0:
            return text
        fld = layer.fields().at(index)
        if fld.type() in (
            QVariant.Int,
            QVariant.LongLong,
            QVariant.UInt,
            QVariant.ULongLong,
        ):
            try:
                return int(text)
            except ValueError:
                return text
        if fld.type() == QVariant.Double:
            try:
                return float(text)
            except ValueError:
                return text
        return text

    def _select_row(self, row: int) -> None:
        """程序性选中某行（不触发 :meth:`_on_category_selected` 的副作用）。"""
        if row < 0:
            return
        self._loading = True
        try:
            self.cat_table.selectRow(row)
        finally:
            self._loading = False

    # ------------------------------------------------------------- 导入样式

    def _symbol_type(self):
        return {
            "point": QgsSymbol.SymbolType.Marker,
            "line": QgsSymbol.SymbolType.Line,
            "polygon": QgsSymbol.SymbolType.Fill,
        }.get(self._geom)

    def _same_geometry_layers(self) -> list:
        """工程里与当前图层同几何类型、且有渲染器的其它矢量图层。"""
        result = []
        for layer in QgsProject.instance().mapLayers().values():
            if not isinstance(layer, QgsVectorLayer) or layer is self._layer:
                continue
            if _geom_kind(layer) != self._geom:
                continue
            try:
                if layer.renderer() is None:
                    continue
            except RuntimeError:
                continue
            result.append(layer)
        return result

    def _on_import_style(self) -> None:
        """导入样式：从其它图层 / 外部 QGIS 样式文件带一份渲染方式过来。"""
        if self._geom is None:
            return
        from app.ui.dialogs.layer_properties.import_style import ImportStyleDialog

        dlg = ImportStyleDialog(self._same_geometry_layers(), self._geom, self)
        try:
            if dlg.exec() != QDialog.DialogCode.Accepted:
                return
            mode, source = dlg.source()
        finally:
            dlg.deleteLater()
        if mode == "layer":
            try:
                source_renderer = source.renderer() if source is not None else None
                renderer = (
                    source_renderer.clone() if source_renderer is not None else None
                )
            except (RuntimeError, AttributeError):
                renderer = None
        else:
            renderer = self._renderer_from_file(str(source or ""))
        if renderer is not None:
            self._apply_imported_renderer(renderer)

    def _renderer_from_file(self, path: str):
        """读取外部 QGIS 样式文件（图层 QML/XML，或单个符号 XML/QML）。"""
        if not path or not os.path.isfile(path):
            QMessageBox.warning(self, "导入样式", "样式文件不存在。")
            return None
        message = ""
        renderer = None
        # 先看文件里的渲染器符号类型，避免把面样式加载到点 / 线图层上
        file_match = self._file_renderer_symbols_match(path)
        if file_match is False:
            QMessageBox.warning(
                self, "导入样式", "样式文件中的符号类型与当前图层几何类型不一致。"
            )
            return None
        if file_match is True:
            uri = {
                "point": "Point",
                "line": "LineString",
                "polygon": "Polygon",
            }.get(self._geom)
            if uri:
                # 用临时内存图层走 QGIS 自己的样式解析（QML / XML 图层样式）
                temp = QgsVectorLayer(
                    f"{uri}?crs=EPSG:4490", "import_style_tmp", "memory"
                )
                try:
                    # 只读渲染（符号）分类：导入样式不需要把标注 / 文字格式等
                    # 一并加载进来（那些部分可能带像素字号，QGIS 内部处理时
                    # 会触发 QFont::setPointSize(-1) 警告）
                    try:
                        result = temp.loadNamedStyle(
                            str(path), QgsMapLayer.StyleCategory.Symbology
                        )
                    except TypeError:  # 老构建没有 categories 参数
                        result = temp.loadNamedStyle(str(path))
                    if isinstance(result, tuple):
                        message = str(result[0] or "")
                        loaded = bool(result[-1])
                    else:
                        loaded = bool(result)
                    if loaded:
                        renderer = temp.renderer()
                except Exception as exc:  # noqa: BLE001
                    message = str(exc)
                if renderer is not None and self._renderer_matches_geometry(renderer):
                    return renderer.clone()
        else:
            # 没有 renderer-v2：可能是单个符号文件
            symbol = self._symbol_from_file(path)
            if symbol is not None and symbol.type() == self._symbol_type():
                return QgsSingleSymbolRenderer(symbol.clone())
        QMessageBox.warning(
            self,
            "导入样式",
            "无法从该文件读取与当前图层几何类型一致的样式。"
            + (f"\n{message}" if message else ""),
        )
        return None

    def _file_renderer_symbols_match(self, path: str):
        """文件里的 ``renderer-v2`` 符号类型是否匹配当前几何。

        返回 ``True`` / ``False``；文件里没有渲染器（纯符号文件）返回 ``None``。
        """
        expected = {"point": "marker", "line": "line", "polygon": "fill"}.get(
            self._geom
        )
        if expected is None:
            return None
        try:
            with open(path, encoding="utf-8") as handle:
                content = handle.read()
            doc = QDomDocument()
            if not doc.setContent(content):
                return None
        except (OSError, ValueError):
            return None
        nodes = doc.elementsByTagName("renderer-v2")
        if nodes.count() == 0:
            return None
        renderer_element = nodes.at(0).toElement()
        symbols = renderer_element.elementsByTagName("symbol")
        if symbols.count() == 0:
            return None
        try:
            return all(
                symbols.at(index).toElement().attribute("type") == expected
                for index in range(symbols.count())
            )
        except Exception:  # noqa: BLE001
            return None

    def _renderer_matches_geometry(self, renderer) -> bool:
        """渲染器里的符号是否都是当前几何类型（防止点图层导入面样式）。"""
        expected = self._symbol_type()
        if expected is None:
            return False
        try:
            symbols = renderer.symbols(QgsRenderContext())
        except Exception:  # noqa: BLE001
            return False
        if not symbols:
            return False
        return all(symbol.type() == expected for symbol in symbols)

    @staticmethod
    def _symbol_from_file(path: str):
        """从 XML/QML 文件里取第一个可读的 ``<symbol>``。"""
        try:
            with open(path, encoding="utf-8") as handle:
                content = handle.read()
            doc = QDomDocument()
            if not doc.setContent(content):
                return None
        except (OSError, ValueError):
            return None
        nodes = doc.elementsByTagName("symbol")
        for index in range(nodes.count()):
            element = nodes.at(index).toElement()
            # 跳过 elevation / 渲染器内部的符号（它们不是文件的样式主体）
            parent = element.parentNode()
            skip = False
            while not parent.isNull() and parent.isElement():
                if parent.toElement().tagName() in ("elevation", "renderer-v2"):
                    skip = True
                    break
                parent = parent.parentNode()
            if skip:
                continue
            try:
                symbol = QgsSymbolLayerUtils.loadSymbol(
                    element, QgsReadWriteContext()
                )
            except Exception:  # noqa: BLE001
                continue
            if symbol is not None:
                return symbol
        return None

    def _apply_imported_renderer(self, renderer) -> None:
        """把导入的渲染器变成渲染页的当前编辑状态（确定 / 应用时才写回图层）。"""
        if isinstance(renderer, QgsSingleSymbolRenderer):
            symbol = renderer.symbol()
            if symbol is None:
                return
            self._single_symbol = symbol.clone()
            self._mode = "single"
            self._set_combo_mode("single")
            self._populate_fields(None)
            self._dirty = True
            self._sync_visibility()
            self._activate_target()
            self._refresh_hint()
            return
        if isinstance(renderer, QgsCategorizedSymbolRenderer):
            field = self._ask_import_field(renderer.classAttribute())
            if field is None:
                return
            categories = []
            try:
                copies = renderer.categories()
                for index in range(len(copies)):
                    category = copies[index]
                    symbol = category.symbol()
                    if symbol is None:
                        continue
                    categories.append(
                        _Category(
                            value=category.value(),
                            label=str(category.label()),
                            symbol=symbol.clone(),
                            render=bool(category.renderState()),
                        )
                    )
            except Exception as exc:  # noqa: BLE001
                logger.warning("读取导入样式类别失败：%s", exc)
                return
            self._cats = categories
            source = renderer.sourceSymbol()
            self._source_symbol = source.clone() if source is not None else None
            if self._geom == "polygon" and categories:
                brush = _call(
                    _simple_layer(categories[0].symbol, "polygon"), "brushStyle"
                )
                self.no_fill_check.blockSignals(True)
                self.no_fill_check.setChecked(brush == Qt.BrushStyle.NoBrush)
                self.no_fill_check.blockSignals(False)
            self._mode = "categorized"
            self._populate_fields(field)
            self._set_combo_mode("categorized")
            self._selected = 0 if categories else -1
            self._dirty = True
            self._rebuild_cat_table()
            self._select_row(self._selected)
            self._sync_visibility()
            self._activate_target()
            self._refresh_hint()
            return
        QMessageBox.information(
            self,
            "导入样式",
            f"暂不支持导入「{_renderer_name(renderer)}」样式，"
            "目前支持单一符号和分类（唯一值）样式。",
        )

    def _ask_import_field(self, suggested: str) -> str | None:
        """多值样式导入：选择当前图层里与类别对应的字段。"""
        layer = self._layer
        if not isinstance(layer, QgsVectorLayer):
            return None
        fields = layer.fields()
        names = [fields.at(i).name() for i in range(fields.size())]
        if not names:
            QMessageBox.warning(self, "导入样式", "当前图层没有可用于分类的字段。")
            return None
        choices = []
        for index in range(fields.size()):
            field = fields.at(index)
            alias = field_alias(field) or field.name()
            choices.append(f"{field.name()} · {alias}")
        default = names.index(suggested) if suggested in names else 0
        choice, ok = QInputDialog.getItem(
            self,
            "导入样式",
            "选择与导入类别匹配的字段：",
            choices,
            default,
            False,
        )
        if not ok or not choice:
            return None
        return choice.split(" · ", 1)[0]

    # ------------------------------------------------------------- 预览

    def _pick_single_symbol(self) -> None:
        """点符号预览：打开 ArcMap 式「符号选择器」。"""
        if self._geom is None:
            return
        from app.ui.dialogs.layer_properties.symbol_selector import (
            SymbolSelectorDialog,
        )

        dlg = SymbolSelectorDialog(
            self._single_symbol or default_symbol(self._geom),
            self._geom,
            self,
        )
        if dlg.exec() == QDialog.DialogCode.Accepted:
            chosen = dlg.selected_symbol()
            if chosen is not None:
                self._single_symbol = chosen
                self._dirty = True
                self._refresh_preview()
                self._refresh_hint()
        dlg.deleteLater()

    def _refresh_preview(self, *_) -> None:
        """按控件值重画符号预览。跑在信号槽里，异常必须自己兜住（未捕获会原生崩溃）。"""
        if self._loading or self._geom is None:
            return
        try:
            if self._mode == "categorized":
                symbol = (
                    self._cats[self._selected].symbol
                    if self._has_target()
                    else None
                )
            else:
                symbol = self._single_symbol
            if symbol is None:
                self.preview_label.clear()
                return
            self.preview_label.setPixmap(
                QgsSymbolLayerUtils.symbolPreviewPixmap(
                    symbol,
                    QSize(_PREVIEW_SIZE.width() - 20, _PREVIEW_SIZE.height() - 20),
                )
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
        # **用户没碰过本页就别动图层**：外壳「确定 / 应用」会把每一页都 apply 一遍，
        # 本页在"当前符号不是简单符号"时会弹「是否替换为单一符号」，用户只是改了「选择」
        # 之类的别的页也会被这条提示拦住（答"否"还会让对话框关不掉）。没改动即无操作。
        if not self._dirty:
            return True
        if (self.renderer_combo.currentData() or "single") == "categorized":
            return self._apply_categorized(layer, kind)
        return self._apply_single(layer, kind)

    def _apply_single(self, layer, kind: str) -> bool:
        """单一符号：把符号选择器里选中的符号写给图层。

        图层本来就是单一符号渲染器 → 只换符号（保留渲染器与其它配置）；
        其它渲染器 → 确认后整体换成 ``QgsSingleSymbolRenderer``。
        """
        renderer = layer.renderer()
        symbol = self._single_symbol or default_symbol(kind)
        if not isinstance(renderer, QgsSingleSymbolRenderer):
            if not self._confirm_replace(renderer):
                return False
        try:
            if isinstance(renderer, QgsSingleSymbolRenderer):
                renderer.setSymbol(symbol.clone())
            else:
                layer.setRenderer(QgsSingleSymbolRenderer(symbol.clone()))
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
                QgsRendererCategory(
                    cat.value, cat.symbol.clone(), cat.label, cat.render
                )
                for cat in self._cats
            ]
            new_renderer = QgsCategorizedSymbolRenderer(field, cats)
            source = self._source_symbol or default_symbol(kind)
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
