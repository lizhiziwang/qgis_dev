"""OGR 图层里「数据源自己维护的 FID」字段识别。

GeoPackage / OGR 驱动会把 ``fid``（少数驱动是 ``ogc_fid``）当成**普通字段**列在
``layer.fields()`` 里，可它只是数据源的主键，不是业务属性。导入 / 追加时把它当
普通属性复制过去，就会把源图层的主键值写进目标表的主键列 —— 目标里已有同号行时
直接撞 ``UNIQUE constraint failed: xxx.fid``（真实工单：追加 500 个点位到点位5万）。

建新表的导入（:mod:`app.core.domain.import_layer`）早就跳过它了；追加
（:mod:`app.core.domain.append_layer`）漏了同一件事，所以把识别逻辑放这里共用。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from qgis.core import QgsVectorLayer

#: 驱动用来承载 FID 的常见列名（小写）。``objectid`` 不在内 —— 那是 GDB 里
#: 用户能看到、往往也想保留的业务标识。
PROVIDER_ID_NAMES = frozenset({"fid", "ogc_fid"})


def provider_id_field_indices(layer: "QgsVectorLayer") -> set[int]:
    """返回``layer``里数据源自己维护的 FID 字段下标（这些字段不该当属性复制）。

    - 主键元数据可用时：只跳过**主键且列名是 fid/ogc_fid** 的字段；
    - 主键元数据为空时（内存图层 / 部分 OGR 驱动报不出来）：按列名兜底。
    """
    indices: set[int] = set()
    try:
        fields = layer.fields()
        primary_keys = set(layer.primaryKeyAttributes())
    except RuntimeError:  # 图层已销毁
        return indices
    for index in range(fields.count()):
        try:
            name = fields.at(index).name().strip().lower()
        except (IndexError, KeyError):
            continue
        if name not in PROVIDER_ID_NAMES:
            continue
        # 有主键元数据时只认主键列；拿不到时按名字兜底（见 copy_to_layer 同款处理）
        if index in primary_keys or not primary_keys:
            indices.add(index)
    return indices


__all__ = ["PROVIDER_ID_NAMES", "provider_id_field_indices"]
