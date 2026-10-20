"""字典管理的数据模型与固定常量（Qt-free）。

- **类别**（字典表）：用户可自行创建，默认预置「政区字典」「资源字典」；
- **字典域**：类别内的一组字典项（Excel 模板里的「字典域」列）；
- **字典项**：域内的代码 / 名称，用 ``parent_code`` 组成多级。

本模块只放数据形状，不碰数据库与界面，便于被 Cython 编译、也便于自检脚本
在没有 QGIS / PyQt 的环境里导入。
"""

from __future__ import annotations

from dataclasses import dataclass, field

#: 首次建库 / 旧库迁移时预置的类别（之后用户可以自己增删改）
DEFAULT_CATEGORIES: tuple[str, ...] = ("政区字典", "资源字典")

#: v1 库里 ``dicts.category`` 存的是代码，迁移到 v2 时换成界面名称
LEGACY_CATEGORY_NAMES: dict[str, str] = {
    "zhengqu": "政区字典",
    "ziyuan": "资源字典",
}


class DictError(RuntimeError):
    """字典操作失败。消息可直接展示给用户（与 ``FieldTemplateError`` 同口径）。"""


# ---------------------------------------------------------------- 数据形状


@dataclass(frozen=True)
class DictCategory:
    """一个类别（字典表）。"""

    id: int
    name: str
    sort_order: int = 0
    domain_count: int = 0


@dataclass(frozen=True)
class DictDomain:
    """一个字典域（``dicts`` 表的一行）。"""

    id: int
    category: str
    domain: str
    description: str = ""
    sort_order: int = 0
    item_count: int = 0


@dataclass(frozen=True)
class DictItem:
    """一个字典项（``dict_items`` 表的一行）。

    ``has_children`` 由查询里的 ``EXISTS`` 现算，供树节点决定是否挂展开箭头；
    ``domain`` / ``category`` 只在跨域查询（搜索）时填充。
    """

    id: int
    dict_id: int
    code: str
    name: str
    parent_code: str = ""
    sort_order: int = 0
    has_children: bool = False
    domain: str = ""
    category: str = ""


@dataclass(frozen=True)
class DictRow:
    """Excel 里解析出来的一行（导入前的中间形态）。"""

    category: str
    domain: str
    code: str
    name: str
    parent_code: str = ""
    #: 源文件行号（1 起，仅用于告警定位）
    row_number: int = 0


@dataclass
class ImportReport:
    """一次 Excel 导入的统计结果。"""

    rows_total: int = 0
    domains_created: int = 0
    items_inserted: int = 0
    items_updated: int = 0
    rows_skipped: int = 0
    #: 前若干条告警（界面只展示这些，避免几万条时卡死）
    warnings: list[str] = field(default_factory=list)
    #: 告警总数（可能大于 ``len(warnings)``）
    warning_total: int = 0

    def warn(self, message: str, *, limit: int = 50) -> None:
        """记一条告警：总数一直累计，正文最多留 ``limit`` 条。"""
        self.warning_total += 1
        if len(self.warnings) < limit:
            self.warnings.append(message)

    @property
    def has_more_warnings(self) -> bool:
        return self.warning_total > len(self.warnings)
