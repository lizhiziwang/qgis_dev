"""读取 OGR 数据源自带的字段域（FileGDB / GeoPackage 的 coded value domain）。

GDB 的“属性域”在 QGIS 里不会自动变成字典关联；导入本地库时要把它们读出来，
写成库内字典表 + 字段元数据，属性表才会按域描述显示、编辑时按下拉取值。

Qt-free：只用 osgeo.ogr（QGIS 自带）。
"""

from __future__ import annotations

from dataclasses import dataclass

from app.infra.logging import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True)
class OgrFieldDomain:
    """一个字段挂接的 OGR 属性域（coded value domain）。"""

    field_name: str
    domain_name: str
    #: ``(code, description)``，保持 OGR 给出的顺序
    values: tuple[tuple[str, str], ...]


def layer_field_domains(layer) -> list[OgrFieldDomain]:
    """图层里挂了 OGR 属性域的字段（读不到 / 不是 GDB 时返回空列表）。"""
    if layer is None:
        return []
    try:
        from app.core.util.local_layer_loader import (
            layer_source_container,
            layer_source_table,
        )

        container = str(layer_source_container(layer) or "")
        table = str(layer_source_table(layer) or "")
    except Exception:  # noqa: BLE001
        return []
    if not container or not table:
        return []
    try:
        from osgeo import ogr
    except ImportError:  # pragma: no cover —— QGIS 自带
        return []
    try:
        ds = ogr.Open(container)
    except Exception as exc:  # noqa: BLE001
        logger.info("读取字段域：打不开 %s（%s）", container, exc)
        return []
    if ds is None:
        return []
    try:
        layer_def = ds.GetLayerByName(table)
        if layer_def is None:
            return []
        names = [str(name) for name in (ds.GetFieldDomainNames() or ())]
        if not names:
            return []
        domain_names = set(names)
        out: list[OgrFieldDomain] = []
        defn = layer_def.GetLayerDefn()
        for index in range(defn.GetFieldCount()):
            field_def = defn.GetFieldDefn(index)
            domain_name = str(field_def.GetDomainName() or "")
            if not domain_name or domain_name not in domain_names:
                continue
            try:
                domain = ds.GetFieldDomain(domain_name)
                enum = dict(domain.GetEnumeration() or {})
            except Exception as exc:  # noqa: BLE001 —— 范围域 / 驱动不支持
                logger.info("读取字段域 %s 失败：%s", domain_name, exc)
                continue
            values = tuple((str(code), str(desc)) for code, desc in enum.items())
            if values:
                out.append(
                    OgrFieldDomain(
                        field_name=str(field_def.GetName()),
                        domain_name=domain_name,
                        values=values,
                    )
                )
        return out
    except Exception as exc:  # noqa: BLE001
        logger.info("读取字段域失败 %s/%s：%s", container, table, exc)
        return []
    finally:
        ds = None


__all__ = ["OgrFieldDomain", "layer_field_domains"]
