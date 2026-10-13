"""系统级的「选中要素高亮样式」：按几何类型（点 / 线 / 面）各存一套样式。

一套样式 = **边界线**（是否显示 + 颜色 + 线宽）+ **填充**（是否显示 + 颜色）。
「图层属性 → 选择」页读它、改它（改走「符号设置」弹窗）；画布上的选中高亮覆盖层
(:mod:`app.ui.common.selection_overlay`) 按它上色。**按几何类型分开**存：改「点」的
样式不影响「线 / 面」，这正是该页的语义。线几何没有填充语义，填充项对它不生效。

持久化走 ``config/selection_style.yaml``（仿 :mod:`app.infra.edit_settings`），
**不用 ``QSettings``**：QSettings 写的是用户真实注册表、验证脚本没法重定向，一调
``set_style`` 就把用户偏好写脏（``theme.py`` 的 ``_RESTORE_SAVED_THEME`` 就是被这坑
逼出来的）；yaml 放在配置目录下，脚本把 ``QGIS_DEV_CONFIG`` 指到临时目录即可完全隔离。

用法::

    mgr = SelectionStyleManager.instance()
    mgr.load(Path(".../selection_style.yaml"))     # 启动时读一次（会发 changed）
    mgr.style("polygon")                           # -> HighlightStyle
    mgr.set_style("polygon", HighlightStyle(...))  # 改 + 落盘 + 发 changed
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

import yaml
from qgis.PyQt.QtCore import QObject, pyqtSignal
from qgis.PyQt.QtGui import QColor

from app.infra.logging import get_logger

logger = get_logger(__name__)

#: 支持的三类几何（与 ``layer_properties.render._geom_kind`` 的取值一一对应）
KINDS = ("point", "line", "polygon")

#: 默认配色 = QGIS 默认选中黄。三类都给这一套：打开页面看不出变化，
#: 用户改了哪类才变哪类（不让人一进来就觉得"选中怎么突然变样了"）。
DEFAULT_STROKE_COLOR = "#ffff00"
DEFAULT_FILL_COLOR = "#ffff00"
DEFAULT_STROKE_WIDTH = 2.0

#: 默认填充透明度（%）——按类给，保持改造前的观感：面半透明看得见底图，
#: 点标记接近实心。边界线一律不透明。
DEFAULT_FILL_OPACITY = {"point": 50, "line": 100, "polygon": 25}

#: 线宽允许范围（px）
MIN_STROKE_WIDTH = 0.1
MAX_STROKE_WIDTH = 50.0

#: 点带子的标记尺寸（橡皮筋默认只有 5px，太小）
POINT_BAND_ICON_SIZE = 8.0

#: 配置文件顶层键
_SECTION = "selection_style"


@dataclass(frozen=True)
class HighlightStyle:
    """一类几何的选中高亮样式：边界线 + 填充（各带透明度）。"""

    stroke_enabled: bool = True
    stroke_color: str = DEFAULT_STROKE_COLOR
    stroke_width: float = DEFAULT_STROKE_WIDTH
    stroke_opacity: int = 100
    fill_enabled: bool = True
    fill_color: str = DEFAULT_FILL_COLOR
    fill_opacity: int = 100

    def stroke_qcolor(self) -> QColor:
        """边界线颜色（已带上透明度）。"""
        color = QColor(self.stroke_color)
        color.setAlphaF(_percent(self.stroke_opacity))
        return color

    def fill_qcolor(self) -> QColor:
        """填充颜色（已带上透明度）。"""
        color = QColor(self.fill_color)
        color.setAlphaF(_percent(self.fill_opacity))
        return color


def default_style(kind: str) -> HighlightStyle:
    """某一类的默认样式（填充透明度按类不同，见 :data:`DEFAULT_FILL_OPACITY`）。"""
    return HighlightStyle(fill_opacity=DEFAULT_FILL_OPACITY.get(kind, 100))


def kind_of_geometry_type(geometry_type: int) -> str | None:
    """``QgsWkbTypes.GeometryType`` → ``"point"/"line"/"polygon"``；认不出返回 ``None``。"""
    try:
        value = int(geometry_type)
    except (TypeError, ValueError):
        return None
    return {0: "point", 1: "line", 2: "polygon"}.get(value)


def style_band(band, kind: str, style: HighlightStyle) -> None:
    """把 ``style`` 应用到一条橡皮筋带子上（``band`` 由调用方按 ``kind`` 建好）。

    **各几何类型吃的属性不一样**（QGIS 4.2 实测）：点带子的 ``setFillColor`` 管的是
    **标记体**、``setColor`` 是**描边** —— 正好对应面的"填充 / 边界线"；线带子
    **忽略** ``setFillColor``，只有描边。"不显示某项"一律用全透明色表达（带子没有开关）。
    """
    try:
        band.setColor(style.stroke_qcolor() if style.stroke_enabled else QColor(0, 0, 0, 0))
        band.setWidth(style.stroke_width)
        if kind == "line":
            return  # 线没有填充语义
        band.setFillColor(style.fill_qcolor() if style.fill_enabled else QColor(0, 0, 0, 0))
        if kind == "point":
            band.setIconSize(POINT_BAND_ICON_SIZE)
    except RuntimeError:
        pass  # 画布 / 带子已销毁


def _percent(value: Any) -> float:
    """0-100 → 0.0-1.0（夹紧）。"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 1.0
    return min(100.0, max(0.0, number)) / 100.0


# ---------------------------------------------------------------- 读写

def _color(value: Any, fallback: str) -> str:
    """``value`` 是合法颜色就取其 hex，否则回退 ``fallback``。"""
    color = QColor(value) if isinstance(value, str) else QColor()
    return color.name() if color.isValid() else fallback


def _width(value: Any, fallback: float) -> float:
    try:
        width = float(value)
    except (TypeError, ValueError):
        return fallback
    return min(MAX_STROKE_WIDTH, max(MIN_STROKE_WIDTH, width))


def from_mapping(raw: Any, fallback: HighlightStyle | None = None) -> HighlightStyle:
    """把（可能缺键 / 类型不对 / 是旧版单色的）值读成 :class:`HighlightStyle`。

    **兼容旧格式**：早期版本每类只存一个颜色 hex 字符串，这里按"边界与填充都用它"迁移。
    """
    d = fallback if fallback is not None else HighlightStyle()
    if isinstance(raw, str):  # 旧格式：'#ffff00'
        color = _color(raw, d.stroke_color)
        return replace(d, stroke_color=color, fill_color=color)
    data = raw if isinstance(raw, dict) else {}
    return HighlightStyle(
        stroke_enabled=bool(data.get("stroke_enabled", d.stroke_enabled)),
        stroke_color=_color(data.get("stroke_color"), d.stroke_color),
        stroke_width=_width(data.get("stroke_width"), d.stroke_width),
        stroke_opacity=int(round(_percent(data.get("stroke_opacity", d.stroke_opacity)) * 100)),
        fill_enabled=bool(data.get("fill_enabled", d.fill_enabled)),
        fill_color=_color(data.get("fill_color"), d.fill_color),
        fill_opacity=int(round(_percent(data.get("fill_opacity", d.fill_opacity)) * 100)),
    )


def load_styles(path: str | Path) -> dict[str, HighlightStyle]:
    """读配置文件；文件缺失 / 损坏 → 全部按默认（只记日志，不拦启动）。"""
    defs = {kind: default_style(kind) for kind in KINDS}
    try:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        return defs
    except Exception:  # noqa: BLE001 —— 配置坏掉不该拦启动
        logger.warning("读取选中高亮样式失败，全部按默认处理：%s", path, exc_info=True)
        return defs
    section = raw.get(_SECTION) if isinstance(raw, dict) else None
    data = section if isinstance(section, dict) else {}
    return {kind: from_mapping(data.get(kind), defs[kind]) for kind in KINDS}


def save_styles(path: str | Path, styles: dict[str, HighlightStyle]) -> None:
    """全量写回 ``path``（自动建父目录）。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        _SECTION: {
            kind: asdict(styles.get(kind) or default_style(kind)) for kind in KINDS
        }
    }
    path.write_text(
        "# 选中要素高亮样式（「图层属性 → 选择」页按几何类型配置；删掉本文件即全部回到默认）\n"
        + yaml.safe_dump(data, allow_unicode=True, sort_keys=False, default_flow_style=False),
        encoding="utf-8",
    )


class SelectionStyleManager(QObject):
    """当前生效的选中高亮样式（进程级单例，用法同 ``ThemeManager.instance()``）。

    状态就一份 ``{kind: HighlightStyle}``；:attr:`changed` 一发，覆盖层换样式重画。
    """

    #: 样式集合被换掉（改样式 / load / reset）
    changed = pyqtSignal()

    _instance: "SelectionStyleManager | None" = None

    def __init__(self) -> None:
        super().__init__()
        self._styles: dict[str, HighlightStyle] = {
            kind: default_style(kind) for kind in KINDS
        }
        #: 落盘路径；``None`` = 还没 load 过（测试 / 独立用），此时不写盘
        self._path: Path | None = None

    @classmethod
    def instance(cls) -> "SelectionStyleManager":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    # ------------------------------------------------------------- 读

    def style(self, kind: str) -> HighlightStyle:
        return self._styles.get(kind) or default_style(kind)

    # ------------------------------------------------------------- 写

    def set_style(self, kind: str, style: HighlightStyle, persist: bool = True) -> None:
        """换掉某一类的样式并广播。

        :param persist: 是否落盘。验证脚本传 ``False``（或干脆不 ``load``）以免写脏
            用户配置。
        """
        if kind not in KINDS or style is None:
            return
        if style == self.style(kind):
            return
        self._styles[kind] = style
        if persist and self._path is not None:
            try:
                save_styles(self._path, self._styles)
            except Exception:  # noqa: BLE001 —— 写盘失败不该让改样式崩掉
                logger.warning("保存选中高亮样式失败：%s", self._path, exc_info=True)
        self.changed.emit()

    def load(self, path: str | Path) -> None:
        """从 ``path`` 读入并记住它（之后 :meth:`set_style` 会写回这里）。

        读完**必须发** ``changed``：启动时覆盖层可能已经用默认样式建好了，
        不发一次它不会知道自己该换样式（见 ``chrome._init_shortcuts`` 的调用次序）。
        """
        self._path = Path(path)
        self._styles = load_styles(self._path)
        self.changed.emit()

    def reset(self) -> None:
        """回到默认，不落盘（测试用）。"""
        self._styles = {kind: default_style(kind) for kind in KINDS}
        self.changed.emit()
