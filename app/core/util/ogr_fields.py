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
    """返回``layer``里不该当普通属性复制的 FID 字段下标。

    判定规则：**列名是 ``fid`` / ``ogc_fid`` 就跳过**，不管它是不是主键 ——
    目标 GeoPackage / SpatiaLite 的 ``fid`` 是保留的整型主键列，同名业务列
    写进去不是撞主键就是挡住目标自己的 FID（GDB 里常见一个非主键的 ``fid``
    列，值往往只是 OBJECTID 的副本）。主键元数据只用于说明来源，不再参与判定。
    """
    indices: set[int] = set()
    try:
        fields = layer.fields()
    except RuntimeError:  # 图层已销毁
        return indices
    for index in range(fields.count()):
        try:
            name = fields.at(index).name().strip().lower()
        except (IndexError, KeyError):
            continue
        if name in PROVIDER_ID_NAMES:
            indices.add(index)
    return indices


__all__ = ["PROVIDER_ID_NAMES", "provider_id_field_indices"]
