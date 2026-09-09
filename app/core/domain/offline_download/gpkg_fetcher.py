"""GPKG 产物抓取器：矢量与 ArcGIS 影像都直写**同一个 .gpkg**。

全部复用 ``app/core/util``（与数据管理/图层加载完全同源）：
- 矢量：``arcgis_server_util.ArcGISServer`` 拉要素 → ``local_db_util``
  ``create_vector_table`` + ``batch_add_vector_data``（feature 表 + GPKG 规范登记）；
- 影像（ArcGIS ImageServer/MapServer）：``arcgis_iamge_server_util.ArcGISImageServer``
  ``save_tiles_to_db`` 把瓦片金字塔写入同一库（瓦片表 + ``gpkg_tile_matrix``）。

产物不再有 .db / 影像文件夹 / zip——任务结束时把 .gpkg 留在下载位置即可。
"""

from __future__ import annotations

import math
from typing import Optional

from .model import FetchItem

#: ArcGIS 字段类型 → sqlite 列类型
_ESRI_TO_SQL = {
    "esriFieldTypeOID": "INTEGER",
    "esriFieldTypeInteger": "INTEGER",
    "esriFieldTypeSmallInteger": "INTEGER",
    "esriFieldTypeDouble": "REAL",
    "esriFieldTypeSingle": "REAL",
    "esriFieldTypeDate": "TEXT",
    "esriFieldTypeString": "TEXT",
    "esriFieldTypeGUID": "TEXT",
    "esriFieldTypeGlobalID": "TEXT",
}

#: 度单位地理 wkid（与 4326 基准差异可忽略）
_LL_WKIDS = (4326, 4490, 4610, 4214, 4258, 4269, 4267, 4283, 4230)
_MERC_WKIDS = (102100, 102113, 3857, 900913)


def _normalize_wkid(wkid: Optional[int]) -> Optional[int]:
    if wkid in (102100, 102113, 900913):
        return 3857
    return wkid


def fetch_vector(item: FetchItem, gpkg_path: str, scope_wgs84, ctx) -> dict:
    """矢量服务 → .gpkg feature 表（表名=服务名，见 planner）。"""
    from app.core.util.arcgis_server_util import ArcGISServer
    from app.core.util.local_db_util import local_db_util
    from .task_info import arcgis_sublayer_url
    from .fetcher import _scope_query_params

    url = arcgis_sublayer_url(item.service_url or "", item.layer_id)
    client = ArcGISServer(timeout=max(10.0, float(ctx.timeout or 30)))
    meta = client.layer_metadata(url)
    src_sr = meta.get("sourceSpatialReference") or meta.get("spatialReference") or {}
    wkid = int(src_sr.get("latestWkid") or src_sr.get("wkid") or 4326)
    wkid = _normalize_wkid(wkid) or 4326

    geometry_type = "MULTIPOLYGON" if "polygon" in str(meta.get("geometryType", "")).lower() else (
        "MULTILINESTRING" if "polyline" in str(meta.get("geometryType", "")).lower() else (
            "MULTIPOINT" if "multipoint" in str(meta.get("geometryType", "")).lower() else
            "POINT" if "point" in str(meta.get("geometryType", "")).lower() else "GEOMETRY"
        )
    )

    columns = []
    for f in meta.get("fields") or []:
        name = str(f.get("name") or "")
        if not name or name.lower() in ("shape", "geom", "geometry"):
            continue
        ftype = str(f.get("type") or "")
        if ftype == "esriFieldTypeGeometry":
            continue
        sql = _ESRI_TO_SQL.get(ftype, "TEXT")
        columns.append((name, sql))

    db = local_db_util(gpkg_path)  # 新文件自动 init_gpkg_db
    try:
        db.create_vector_table(
            item.target,
            columns=columns,
            geometry_column="shape",
            geometry_type=geometry_type,
            srid=wkid,
            overwrite=True,
        )
        scope_extra = _scope_query_params(scope_wgs84, wkid)
        done = [0]

        def _on_progress(fetched, total):
            ctx.report((fetched - done[0]) * 96)
            done[0] = fetched

        features = client.query_all_features(
            url,
            where="1=1",
            out_fields="*",
            out_sr=wkid,
            extra=scope_extra,
            progress=_on_progress,
        )
        ctx.check()
        total = 0
        for start in range(0, len(features), 1000):
            total += db.batch_add_vector_data(
                item.target, features[start : start + 1000]
            )
            ctx.check()
        db.conn.commit()
    finally:
        try:
            db.conn.close()
        except Exception:  # noqa: BLE001
            pass
    ctx.log(f"「{item.label}」写入 gpkg 矢量表 {item.target} 完成：{total} 要素")
    return {"feature_count": total, "crs": f"EPSG:{wkid}", "table": item.target}


def fetch_imagery(item: FetchItem, gpkg_path: str, ctx) -> dict:
    """ArcGIS 影像瓦片金字塔 → 同一个 .gpkg 瓦片表（util ArcGISImageServer）。"""
    from app.core.util.arcgis_iamge_server_util import ArcGISImageServer
    from app.core.util.local_db_util import local_db_util
    from .fetcher import FetchError

    url = (item.service_url or "").split("?")[0]
    server = ArcGISImageServer(timeout=max(10.0, float(ctx.timeout or 30)))
    meta = server.metadata(url)
    sr = meta.get("spatialReference") or {}
    wkid = _normalize_wkid(int(sr.get("latestWkid") or sr.get("wkid") or 4326))
    db = local_db_util(gpkg_path)

    extent = None
    extent_sr = None
    if ctx.scope_wgs84 is not None:
        try:
            rect = _scope_to_sr(ctx.scope_wgs84, wkid)
            if rect is not None:
                extent = rect
                extent_sr = wkid
        except Exception:  # noqa: BLE001
            extent = None
    try:
        cancel_cb = None
        if ctx.cancel is not None:
            cancel_cb = lambda: bool(ctx.cancel.is_set())
        result = server.save_tiles_to_db(
            url,
            db_util=db,
            extent=extent,
            extent_sr=extent_sr,
            min_zoom=item.zmin,
            max_zoom=item.zmax,
            workers=4,
            overwrite=True,
            table_name=item.target,
            register=True,
            progress=lambda done, total: ctx.log(
                f"「{item.label}」影像瓦片 {done}/{total or '?'}"
            ),
            cancel=cancel_cb,
        )
    finally:
        try:
            db.conn.close()
        except Exception:  # noqa: BLE001
            pass
    if result.get("failed", 0):
        ctx.log(f"「{item.label}」有 {result['failed']} 张瓦片失败：{result.get('errors') or ''}")
    ctx.log(
        f"「{item.label}」写入 gpkg 影像表 {result.get('table') or item.target} 完成："
        f"{result.get('downloaded', 0)} 张"
    )
    return {
        "tile_count": result.get("downloaded", 0),
        "failed": result.get("failed", 0),
        "table": result.get("table") or item.target,
    }


def fetch_item(item: FetchItem, root: str, scope_wgs84, ctx) -> dict:
    """分发：矢量/ArcGIS 影像 → .gpkg；文件类保持普通文件下载。"""
    from .fetcher import FetchError, fetch_files

    if item.kind == "vector":
        return fetch_vector(item, ctx.gpkg_path, scope_wgs84, ctx)
    if item.kind == "imagery":
        if item.source_type != "arcgis-image":
            raise FetchError(
                f"「{item.label}」暂只支持 ArcGIS 影像（ImageServer/MapServer）写入 .gpkg，"
                "WMS/WMTS/XYZ 请先改用 ArcGIS 影像源。"
            )
        return fetch_imagery(item, ctx.gpkg_path, ctx)
    if item.kind == "files" or item.source_type == "file":
        return fetch_files(item, root, ctx)
    raise FetchError(f"暂不支持的数据类型：{item.source_type}（{item.label}）")


# ------------------------------------------------------------------ 内部

def _scope_to_sr(scope_wgs84, wkid: int):
    """WGS84 外包 → 服务坐标系外包；非 ll/merc 返回 None（表示用服务全幅）。"""
    x1, y1, x2, y2 = scope_wgs84
    if wkid in _LL_WKIDS:
        return (x1, y1, x2, y2)
    if wkid == 3857:
        r = 20037508.342789244

        def _mx(lon: float) -> float:
            return lon / 180.0 * r

        def _my(lat: float) -> float:
            lat = max(-89.9, min(89.9, lat))
            return math.log(math.tan(math.pi / 4 + math.radians(lat) / 2)) * 6378137.0

        return (_mx(x1), _my(y1), _mx(x2), _my(y2))
    return None
