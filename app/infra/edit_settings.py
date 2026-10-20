"""编辑设置（「编辑设置」弹窗里除快捷键外的那些项）的登记表、持久化与生效值。

这些设置散落在几个互不相干的子系统里（框选弹窗、量算口径、画布滚轮、字段自动
计算），不方便一路从主窗口传下去，所以照 :class:`app.ui.common.theme.ThemeManager`
的样子做成一个进程级单例：各处按需读 :meth:`EditSettingsManager.current`，
弹窗保存时 :meth:`EditSettingsManager.apply` 换掉当前值并落盘。

**读取时永远带默认值兜底**（``yaml.get(key, default)``）：配置文件是"全量写"的，
但以后新增设置项时，老文件里没有这一项 → 自动落回默认，不会因为缺键而炸。

坐标口径（与原型 ``cs端/code01`` 的「编辑设置」一致）::

    sel_filter_on / sel_filter_pos   框选后是否弹「选择集」弹窗 + 弹在哪
    len_method / area_method         默认长度 / 面积计算方法
    auto_calc                        是否自动计算系统长度与面积字段值
    len_unit / area_unit             默认长度 / 面积单位（显示用）
    wheel                            滚轮向前滚动 = 放大还是缩小地图
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

import yaml

from app.infra.logging import get_logger

logger = get_logger(__name__)

# ---- 各选项的取值（同时也是弹窗下拉的选项顺序）----

#: 「选择集筛选框」显示位置
SELECT_FILTER_POSITIONS: tuple[str, ...] = ("顶部居中", "底部居中")
#: 长度计算方法的两个后端（名字即下拉选项）：
#: - **高斯投影长度** —— 转到该几何所在 3° 带的 CGCS2000 高斯-克吕格（横轴墨卡托，
#:   等角）平面上量。**长度量算该用的是等角投影**：它在中央经线上不失真、离得越远
#:   变形越大，但变化是有规律可预期的小量；
#: - **测地线长度** —— 直接在椭球面上量（:class:`QgsDistanceArea`，内部是
#:   GeographicLib / Karney 算法），是**真实地面距离**。
#:
#: 没有「等积投影长度」：等积投影是为**面积**设计的，长度在标准纬线上才不失真、
#: 离得越远越大（实测 27.6°N 附近 0.03%、22°N 0.33%、35°N 1.0%），既不准又不稳定，
#: 不该拿它量长度。
LENGTH_METHODS: tuple[str, ...] = ("高斯投影长度", "测地线长度")
#: 面积计算方法的后端（前两个是平面量算、最后一个是椭球量算）：
#: - **高斯投影面积** —— 高斯-克吕格平面面积。等角投影**不保面积**，会系统性偏大；
#: - **等积投影面积** —— Albers 等积投影平面面积，与真实面积一致（国土/林业口径）；
#: - **椭球面积** —— 椭球面上的真实面积。
AREA_METHODS: tuple[str, ...] = ("高斯投影面积", "等积投影面积", "椭球面积")
#: 方法名 → 量算后端
BACKEND_GAUSS = "gauss"          # 高斯-克吕格平面
BACKEND_ALBERS = "albers"        # 等积（Albers）平面
BACKEND_ELLIPSOID = "ellipsoid"  # 椭球（测地线）
_LENGTH_BACKENDS: dict[str, str] = {
    "高斯投影长度": BACKEND_GAUSS,
    "测地线长度": BACKEND_ELLIPSOID,
}
_AREA_BACKENDS: dict[str, str] = {
    "高斯投影面积": BACKEND_GAUSS,
    "等积投影面积": BACKEND_ALBERS,
    "椭球面积": BACKEND_ELLIPSOID,
}


def length_backend_for(method: str) -> str:
    """长度计算方法名 → 量算后端（认不出就按高斯平面）。"""
    return _LENGTH_BACKENDS.get(str(method or ""), BACKEND_GAUSS)


def area_backend_for(method: str) -> str:
    """面积计算方法名 → 量算后端（认不出就按高斯平面）。"""
    return _AREA_BACKENDS.get(str(method or ""), BACKEND_GAUSS)

#: 老配置里用过的方法名 → 新名字（选项收敛过几轮，避免旧值被判非法而回默认）
_LENGTH_ALIASES = {
    "长度(Karney测地线)": "测地线长度",
    "长度(测地线)": "测地线长度",
    # 「等积投影长度」已下线（等积投影不该用来量长度）→ 并到同为平面量算的高斯投影
    "等积投影长度": "高斯投影长度",
}
_AREA_ALIASES = {
    "面积(Karney测地线)": "椭球面积",
    "国土椭球面积": "椭球面积",
    "面积(测地线)": "椭球面积",
}
#: 长度单位（QGIS 的 ``Qgis.DistanceUnit`` 里有的那几个）
LENGTH_UNITS: tuple[str, ...] = ("米", "千米", "厘米")
#: 面积单位。**「亩」不在 QGIS 的枚举里**（见 :data:`MU_IN_SQUARE_METERS`）
AREA_UNITS: tuple[str, ...] = ("亩", "公顷", "平方千米")
#: 滚轮方向
WHEEL_MODES: tuple[str, ...] = ("放大地图", "缩小地图")

#: 1 亩 = 2000/3 平方米（≈666.6667）
MU_IN_SQUARE_METERS = 2000.0 / 3.0

#: 配置文件顶层键
_SECTION = "edit_settings"


@dataclass(frozen=True)
class EditSettings:
    """「编辑设置」弹窗里那些项的一组取值。"""

    #: 框选后是否弹「选择集」弹窗（关掉＝框选只改选中集，不弹窗）
    sel_filter_on: bool = True
    sel_filter_pos: str = "顶部居中"
    len_method: str = "高斯投影长度"
    area_method: str = "高斯投影面积"
    #: 几何变化后是否**按上面选的算法与单位**重算系统长度 / 面积字段。
    #: 不勾选时按系统原来的方式算（地理坐标系走椭球、投影坐标系走自身 CRS 平面，
    #: 单位固定平方米 / 米）—— **两种都照算**，差别只在"用哪套方法、写什么单位"。
    #: **默认不勾**：开箱行为与本功能引入前完全一致。
    auto_calc: bool = False
    len_unit: str = "米"
    area_unit: str = "亩"
    wheel: str = "放大地图"

    # ------------------------------------------------------------ 派生

    @property
    def reverse_wheel(self) -> bool:
        """滚轮向前滚动是否应当**缩小**地图。"""
        return self.wheel == "缩小地图"

    @property
    def length_backend(self) -> str:
        """长度量算后端：``gauss`` / ``albers`` / ``ellipsoid``。"""
        return length_backend_for(self.len_method)

    @property
    def area_backend(self) -> str:
        """面积量算后端：``gauss`` / ``albers`` / ``ellipsoid``。"""
        return area_backend_for(self.area_method)


DEFAULTS = EditSettings()


def _pick(
    value: Any,
    options: tuple[str, ...],
    fallback: str,
    aliases: dict[str, str] | None = None,
) -> str:
    """``value`` 在 ``options`` 里才认；先按别名迁移老名字，再回退默认。"""
    if isinstance(value, str) and aliases:
        value = aliases.get(value, value)
    return value if isinstance(value, str) and value in options else fallback


def from_mapping(raw: Any) -> EditSettings:
    """把（可能缺键 / 类型不对的）字典读成 :class:`EditSettings`。"""
    data = raw if isinstance(raw, dict) else {}
    d = DEFAULTS
    return EditSettings(
        sel_filter_on=bool(data.get("sel_filter_on", d.sel_filter_on)),
        sel_filter_pos=_pick(data.get("sel_filter_pos"), SELECT_FILTER_POSITIONS, d.sel_filter_pos),
        len_method=_pick(data.get("len_method"), LENGTH_METHODS, d.len_method, _LENGTH_ALIASES),
        area_method=_pick(data.get("area_method"), AREA_METHODS, d.area_method, _AREA_ALIASES),
        auto_calc=bool(data.get("auto_calc", d.auto_calc)),
        len_unit=_pick(data.get("len_unit"), LENGTH_UNITS, d.len_unit),
        area_unit=_pick(data.get("area_unit"), AREA_UNITS, d.area_unit),
        wheel=_pick(data.get("wheel"), WHEEL_MODES, d.wheel),
    )


def load_settings(path: Path) -> EditSettings:
    """读配置文件；文件缺失 / 损坏 → 全部按默认（只记日志，不拦启动）。"""
    try:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        return DEFAULTS
    except Exception:  # noqa: BLE001 —— 配置坏掉不该拦启动
        logger.warning("读取编辑设置失败，全部按默认处理：%s", path, exc_info=True)
        return DEFAULTS
    section = raw.get(_SECTION) if isinstance(raw, dict) else None
    if section is None:
        return DEFAULTS
    return from_mapping(section)


def save_settings(path: Path, settings: EditSettings) -> None:
    """全量写回 ``path``（自动建父目录）。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {_SECTION: asdict(settings)}
    path.write_text(
        "# 编辑设置（「编辑设置」弹窗里的项；删掉本文件即全部回到默认）\n"
        + yaml.safe_dump(data, allow_unicode=True, sort_keys=False, default_flow_style=False),
        encoding="utf-8",
    )


class EditSettingsManager:
    """当前生效的编辑设置（进程级单例，用法同 ``ThemeManager.instance()``）。"""

    _instance: "EditSettingsManager | None" = None

    def __init__(self) -> None:
        self._current = DEFAULTS

    @classmethod
    def instance(cls) -> "EditSettingsManager":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def current(self) -> EditSettings:
        return self._current

    def apply(self, settings: EditSettings) -> None:
        """换掉当前值（由「编辑设置」弹窗保存时调用）。"""
        self._current = settings

    def update(self, **kwargs: Any) -> EditSettings:
        """只改几个字段（测试 / 局部调整用）。"""
        self._current = replace(self._current, **kwargs)
        return self._current

    def reset(self) -> None:
        """回到默认（测试用）。"""
        self._current = DEFAULTS


__all__ = [
    "BACKEND_ALBERS",
    "BACKEND_ELLIPSOID",
    "BACKEND_GAUSS",
    "AREA_METHODS",
    "AREA_UNITS",
    "DEFAULTS",
    "EditSettings",
    "EditSettingsManager",
    "LENGTH_METHODS",
    "LENGTH_UNITS",
    "MU_IN_SQUARE_METERS",
    "SELECT_FILTER_POSITIONS",
    "WHEEL_MODES",
    "from_mapping",
    "length_backend_for",
    "load_settings",
    "save_settings",
    "area_backend_for",
]
