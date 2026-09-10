"""数据抓取器：按 FetchItem.source_type 抓取并写入 staging（方案 §6.2/6.4）。

运行环境约束：本模块只被 :class:`~app.core.domain.offline_download.task.OfflineDownloadTask`
在**后台 worker 线程**调用，故可直接做阻塞 HTTP 请求；qgis/gdal 一律方法内按需
import（顶层模块不 import，见方案 §6.6）。

source_type 路由：
- ``file``                → 直链文件流式下载（断点续传 `.part`，见 :func:`fetch_files`）；
- ``arcgis-feature``      → FeatureServer/MapServer 分页 query → 写 SpatiaLite .sqlite（见 :func:`fetch_feature_sqlite`）；
- ``arcgis-image``        → ImageServer/MapServer exportimage 按 XYZ 网格抓瓦片金字塔；
- ``wms``                 → GetMap 按 XYZ 网格抓瓦片金字塔；
- ``xyz``/``wmts``(模板)  → 按 ``{z}/{x}/{y}`` 模板直抓瓦片金字塔。
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Callable, Optional

from app.core.util.arcgis_server_util import ArcGISServer
from app.core.util.local_db_util import local_db_util

from . import srs
from .disk import format_bytes
from .layout import SQLITE_CONTAINER, safe_name
from .model import FetchItem
from .task_info import arcgis_sublayer_url

#: 单张 PNG 瓦片平均字节（估算用，方案 §7.2）
_AVG_TILE_BYTES = 200_000

#: 服务元数据缓存（同一次下载内避免重复 GET ?f=json；TTL 120s）
_META_CACHE: dict[str, tuple[float, dict]] = {}


def _meta_cached(key: str, loader) -> dict:
    now = time.time()
    hit = _META_CACHE.get(key)
    if hit and now - hit[0] < 120.0:
        return hit[1]
    meta = loader()
    _META_CACHE[key] = (now, meta)
    return meta


class DownloadCanceled(Exception):
    """下载被用户取消（内部信号，task 捕获后走 taskTerminated）。"""


class FetchError(RuntimeError):
    """抓取失败（带中文提示，task 记日志并标该数据集失败）。"""


@dataclass
class FetchContext:
    """抓取期共享上下文（由 task 构造，跨线程安全）。"""

    #: 取消事件（``threading.Event``）；None 表示不可取消
    cancel: object = None
    #: 已写盘字节增量上报（驱动总进度条）
    report: Callable[[int], None] = lambda _n: None
    #: 阶段日志（如“抓图斑矢量第 3/12 页”）
    log: Callable[[str], None] = lambda _m: None
    #: 附加请求头（如认证 token）
    headers: dict = field(default_factory=dict)
    #: 单次 HTTP 超时秒
    timeout: float = 30.0
    #: 单 gpkg 产物路径（矢量/影像落库；task 注入，见 gpkg_fetcher）
    gpkg_path: Optional[str] = None
    #: 下载范围 WGS84 (xmin,ymin,xmax,ymax)（供影像 extent 换算，task 注入）
    scope_wgs84: tuple = None

    def check(self) -> None:
        if self.cancel is not None and self.cancel.is_set():
            raise DownloadCanceled()


# ====================================================================== 分发

def fetch_item(
    item: FetchItem,
    stage_root: str,
    scope_wgs84: tuple[float, float, float, float],
    ctx: FetchContext,
) -> dict:
    """抓取单个 FetchItem，返回元数据（写 manifest 用）。"""
    if item.kind == "vector":
        # kind=vector 的服务地址可能是 FeatureServer，也可能是 MapServer（ArcGIS
        # REST 两者都支持 query 拉要素）。source_type 归成 arcgis-image 的 MapServer
        # 在这里仍走分页 query → SpatiaLite，而不是当影像下载。
        return fetch_feature_sqlite(item, stage_root, scope_wgs84, ctx)
    if item.kind == "files" or item.source_type == "file":
        return fetch_files(item, stage_root, ctx)
    if item.kind == "imagery":
        return fetch_raster(item, stage_root, scope_wgs84, ctx)
    raise FetchError(f"暂不支持的数据类型：{item.source_type}（{item.label}）")


def estimate_item_bytes(
    item: FetchItem,
    scope_wgs84: tuple[float, float, float, float],
    ctx: FetchContext,
) -> Optional[int]:
    """估算单个数据集合计字节（下载前 UI 展示 + 预检用）。失败返回 None。"""
    try:
        if item.kind == "vector":
            count = _estimate_feature_count(item, scope_wgs84, ctx)
            return None if count is None else _feature_bytes(count, item, ctx)
        if item.kind == "files" or item.source_type == "file":
            return _estimate_file_bytes(item, ctx)
        if item.kind == "imagery":
            return _estimate_raster_bytes(item, scope_wgs84, ctx)
    except FetchError:
        return None
    except Exception:  # noqa: BLE001  估算失败不阻断下载
        return None
    return None


# ====================================================================== 文件

def _estimate_file_bytes(item: FetchItem, ctx: FetchContext) -> Optional[int]:
    total = 0
    for url in _item_urls(item):
        length = _head_length(url, ctx)
        if length is None:
            return None
        total += length
    return total or None


def fetch_files(item: FetchItem, stage_root: str, ctx: FetchContext) -> dict:
    """下载直链文件到 ``<stage>/<item.target>/``，支持 ``.part`` 断点续传。"""
    urls = _item_urls(item)
    if not urls:
        raise FetchError(f"「{item.label}」没有文件地址。")
    dest_dir = os.path.join(stage_root, item.target.replace("/", os.sep))
    os.makedirs(dest_dir, exist_ok=True)
    entries: list[dict] = []
    for url in urls:
        ctx.check()
        name = _name_from_url(url)
        final = os.path.join(dest_dir, name)
        part = final + ".part"
        size = _download_with_resume(url, part, final, ctx)
        entries.append({"name": name, "size_bytes": size, "sha256": _sha256(final)})
        ctx.log(f"「{item.label}」下载完成：{name}（{_fmt(size)}）")
    return {"entries": entries, "size_bytes": sum(e["size_bytes"] for e in entries)}


def _download_with_resume(url: str, part: str, final: str, ctx: FetchContext) -> int:
    """带断点续传的文件下载：写 ``.part``（Range 续传），完成后原子改名。

    服务器忽略 Range（返回 200 全量）→ 丢弃半成品重新全量下载；HTTP 416（Range
    超尾）说明已下载完整 → 直接完成。
    """
    url = _encode_non_ascii(url)
    for attempt in range(3):
        ctx.check()
        try:
            offset = os.path.getsize(part) if os.path.exists(part) else 0
            req = urllib.request.Request(url, headers=dict(ctx.headers))
            if offset:
                req.add_header("Range", f"bytes={offset}-")
            with urllib.request.urlopen(req, timeout=ctx.timeout, context=_ssl_context()) as resp:  # noqa: S310
                status = int(getattr(resp, "status", 200))
                if status == 416:
                    os.replace(part, final)
                    return offset
                if status == 200 and offset:
                    # 服务器忽略 Range → 重新全量下载
                    os.remove(part)
                    offset = 0
                with open(part, "ab" if (status == 206 and offset) else "wb") as f:
                    total = offset
                    while True:
                        chunk = resp.read(64 * 1024)
                        if not chunk:
                            break
                        f.write(chunk)
                        total += len(chunk)
                        ctx.report(len(chunk))
                os.replace(part, final)
                return total
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                raise FetchError(f"「{url}」下载被拒绝（HTTP {exc.code}），请检查任务凭证。") from exc
            ctx.log(f"下载重试 {attempt + 1}/3：HTTP {exc.code} {url}")
        except (urllib.error.URLError, OSError) as exc:
            ctx.log(f"下载重试 {attempt + 1}/3：{exc}")
        time.sleep(0.5 * (attempt + 1))
    raise FetchError(f"「{url}」多次下载失败。")


# ====================================================================== 矢量 → SpatiaLite .sqlite

def _estimate_feature_count(
    item: FetchItem, scope_wgs84: tuple, ctx: FetchContext
) -> Optional[int]:
    meta = _feature_service_meta(item, ctx)
    params = _scope_query_params(scope_wgs84, meta["wkid"])
    params.update({"where": "1=1", "returnCountOnly": "true", "f": "json"})
    body = _http_json(_arcgis_query_endpoint(meta["url"]), params, ctx)
    try:
        return int(body.get("count"))
    except (KeyError, TypeError, ValueError):
        return None


def _feature_bytes(count: int, item: FetchItem, ctx: FetchContext) -> Optional[int]:
    meta = _feature_service_meta(item, ctx)
    row = 120  # geometry 均摊
    for fld in meta.get("fields", []):
        t = str(fld.get("type", ""))
        if "Integer" in t:
            row += 8
        elif "Double" in t or "Single" in t:
            row += 16
        elif "Date" in t:
            row += 16
        else:
            row += int(fld.get("length") or 80) * 2  # UTF-8 放大
    return count * row


def fetch_feature_sqlite(
    item: FetchItem,
    stage_root: str,
    scope_wgs84: tuple[float, float, float, float],
    ctx: FetchContext,
) -> dict:
    """REST 拉全量要素（按空间范围过滤）→ 写 SpatiaLite ``矢量.db``。

    复用项目 util 三件套（与数据管理/本地库完全同源）：
    - ``arcgis_server_util.ArcGISServer``：图层元信息 + 分页拉取（原生
      ``{"attributes":…, "geometry":…}``，几何在图层 SR）；
    - ``local_db_util.create_arcgis_layer``：建 shape 几何列空间表（MULTIPOLYGON
      等，自动建空间索引、注册 user_relation）；
    - ``local_db_util.batch_add_vector_data``：ST_GeomFromText 批量写几何。
    """
    url = _arcgis_sublayer(item)

    client = ArcGISServer(timeout=max(10.0, float(ctx.timeout or 30)))
    layer_meta = client.layer_metadata(url)
    src_sr = layer_meta.get("sourceSpatialReference") or layer_meta.get("spatialReference") or {}
    wkid = int(src_sr.get("latestWkid") or src_sr.get("wkid") or 4326)
    if wkid in (102100, 102113):
        wkid = 3857
    # 建表统一多部件类型（与 batch_add 自动 ST_Multi 归一匹配）
    geom_type = _spatialite_geom_name({"geometryType": layer_meta.get("geometryType")})

    db = os.path.join(stage_root, SQLITE_CONTAINER)
    lib = local_db_util(db, load_spatialite=True)
    try:
        lib.create_arcgis_layer(
            layer_meta,
            table_name=item.target,
            geometry_column="shape",
            geometry_type=geom_type,
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
            chunk = features[start : start + 1000]
            total += lib.batch_add_vector_data(item.target, chunk)
            ctx.check()
        lib.conn.commit()
    finally:
        try:
            lib.conn.close()
        except Exception:  # noqa: BLE001
            pass
    if total:
        _verify_geometry_blob(db, item.target)
    ctx.log(f"「{item.label}」写入 SpatiaLite {SQLITE_CONTAINER} 完成：{total} 要素")
    return {"feature_count": total, "crs": f"EPSG:{wkid}"}


class _VectorSink:
    """SpatiaLite 矢量写入器（mod_spatialite 原生建表，OGR 只用于几何换算）。"""

    _TYPE_MAP = {
        "esriFieldTypeOID": "INTEGER",
        "esriFieldTypeInteger": "INTEGER",
        "esriFieldTypeSmallInteger": "INTEGER",
        "esriFieldTypeDouble": "REAL",
        "esriFieldTypeSingle": "REAL",
        "esriFieldTypeDate": "TEXT",
        "esriFieldTypeString": "TEXT",
    }

    def __init__(self, db: str, table: str, meta: dict) -> None:

        self.table = table
        self.conn = sqlite3.connect(db)
        try:
            self.conn.enable_load_extension(True)
            self.conn.load_extension("mod_spatialite")
        except sqlite3.OperationalError as exc:
            raise FetchError(
                f"无法加载 mod_spatialite（{exc}）。请用带 SpatiaLite 的 QGIS python 运行后重新下载。"
            ) from exc
        self._wkid = int(meta.get("wkid") or 4490)
        self._wkb_target = _ogr_geom_type(meta)
        self._fields = [f for f in meta.get("fields", []) if str(f.get("name") or "")]
        self._create(table, meta)

    # ---------------------------------------------------------- 建表

    def _create(self, table: str, meta: dict) -> None:
        q = _quote_ident
        cols = ['"fid" INTEGER PRIMARY KEY AUTOINCREMENT']
        for fld in self._fields:
            name = str(fld["name"])
            if name.lower() in ("fid", "shape"):
                continue
            cols.append(f"{q(name)} {self._TYPE_MAP.get(str(fld.get('type')), 'TEXT')}")
        self.conn.execute(f"SELECT InitSpatialMetadata(1)")
        self.conn.execute(f'DROP TABLE IF EXISTS {q(table)}')
        self.conn.execute(f"CREATE TABLE {q(table)} ({', '.join(cols)})")
        gt = _spatialite_geom_name(meta)
        self.conn.execute(
            f"SELECT AddGeometryColumn('{table}', 'shape', {self._wkid}, '{gt}', 2)"
        )
        attrs = [str(f["name"]) for f in self._fields]
        self._attrs = [a for a in attrs if a.lower() not in ("fid", "shape")]
        placeholders = ", ".join("?" for _ in self._attrs) + ", ST_GeomFromText(?, ?)"
        col_names = ", ".join(q(a) for a in self._attrs) + ", " + q("shape")
        self._insert_sql = (
            f"INSERT INTO {q(table)} ({col_names}) VALUES ({placeholders})"
        )
        self.conn.commit()

    def write(self, gj: dict, wkid: int) -> None:
        props = gj.get("properties") or {}
        values = [props.get(a) for a in self._attrs]
        geom_gj = gj.get("geometry")
        wkt = None
        if geom_gj is not None:
            from osgeo import ogr, osr

            geom = ogr.CreateGeometryFromJson(json.dumps(geom_gj))
            if geom is not None:
                if wkid not in (4326, 4490):
                    tgt = osr.SpatialReference()
                    tgt.ImportFromEPSG(wkid)
                    try:
                        tgt.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
                    except Exception:  # noqa: BLE001
                        pass
                    geom.TransformTo(tgt)
                geom = _coerce_geom_type(geom, self._wkb_target)
                if geom is not None:
                    wkt = geom.ExportToWkt()
        self.conn.execute(self._insert_sql, values + [wkt, self._wkid])

    def commit(self) -> None:
        self.conn.commit()

    def close(self) -> None:
        try:
            self.conn.commit()
            self.conn.close()
        except Exception:  # noqa: BLE001
            pass


def _verify_geometry_blob(db: str, table: str) -> None:
    """自检 shape 几何列是 BLOB 而非文本。

    OGR 在运行时加载不到 libspatialite 时会把几何**降级存成 WKT 文本**（列类型
    TEXT），这样 QGIS/数据管理能读到属性但无图形。发现文本降级直接报明确错误，
    不再静默产出“有数据没图形”的库。
    """

    try:
        con = sqlite3.connect(db, timeout=5.0)
        con.execute("PRAGMA query_only = ON")
        try:
            declared = {
                r[1]: (r[2] or "").upper()
                for r in con.execute(f'PRAGMA table_info("{table}")')
            }
            shape_type = declared.get("shape", "")
            if "TEXT" in shape_type:
                raise FetchError(
                    f"几何列 shape 被写成了文本（当前环境无法加载 libspatialite，OGR 把几何"
                    "降级为 WKT）。请用带 SpatiaLite 的 QGIS python 运行应用后重新下载。"
                )
        finally:
            con.close()
    except FetchError:
        raise
    except Exception:  # noqa: BLE001  自检失败不阻断主流程
        pass


def _quote_ident(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def _spatialite_geom_name(meta: dict) -> str:
    """由 ArcGIS geometryType 生成 SpatiaLite AddGeometryColumn 类型名。

    与 :func:`_ogr_geom_type` 的 numeric 归一**严格一致**（该函数对 Polygon/
    Polyline 恒返回 Multi 类型）：多边形 → MULTIPOLYGON、线 → MULTILINESTRING，
    避免建 POLYGON 列却写 MULTIPOLYGON 触发 geometry 约束。
    """
    g = str(meta.get("geometryType") or "").lower()
    if "point" in g:
        return "MULTIPOINT" if "multi" in g else "POINT"
    if "polyline" in g:
        return "MULTILINESTRING"
    if "polygon" in g:
        return "MULTIPOLYGON"
    return "GEOMETRY"


def _feature_service_meta(item: FetchItem, ctx: FetchContext) -> dict:
    url = _arcgis_sublayer(item)

    def load() -> dict:
        body = _http_json(url, {"f": "json"}, ctx)
        sr = (body.get("spatialReference") or {})
        wkid = int(sr.get("latestWkid") or sr.get("wkid") or 4326)
        if wkid in (102100, 102113):
            wkid = 3857
        return {
            "url": url,
            "wkid": wkid,
            "maxRecordCount": int(body.get("maxRecordCount") or 0),
            "fields": body.get("fields") or [],
        }

    return _meta_cached(f"feat:{url}", load)


def _arcgis_query_endpoint(sublayer_url: str) -> str:
    """子图层地址 → query 操作端点：``…/MapServer/0`` → ``…/MapServer/0/query``。

    ArcGIS REST 的要素/图层查询必须打到 ``/query`` 上，拼到图层根会返回 400。
    """
    base = (sublayer_url or "").rstrip("/")
    if base.lower().endswith("/query"):
        return base
    return base + "/query"


def _scope_query_params(
    scope_wgs84: tuple[float, float, float, float], wkid: int
) -> dict:
    """空间范围 → ArcGIS envelope 参数（几何需在图层 SR 下，WGS84 先用原值，
    wkid 非 4326/4490 且 QGIS 可用时换算，见 :func:`_envelope_in`）。"""
    x1, y1, x2, y2 = scope_wgs84
    if wkid not in (4326, 4490, 102100, 102113, 3857):
        try:

            rect = srs.rect_wgs84_to(x1, y1, x2, y2, f"EPSG:{wkid}")
            if rect:
                x1, y1, x2, y2 = rect
        except Exception:  # noqa: BLE001
            pass
    return {
        "geometry": f"{x1},{y1},{x2},{y2}",
        "geometryType": "esriGeometryEnvelope",
        "inSR": str(wkid) if wkid not in (4326, 4490) else "4326",
    }


def _open_vector_sqlite_writer(db: str, item: FetchItem, meta: dict):
    """打开/创建 SpatiaLite 库，准备矢量图层并返回**数据源**（须由调用方保持存活，
    否则局部数据源被 GC 会销毁图层句柄，后续写盘抛 OGRLayerShadow 错误）。

    库首次创建时由驱动初始化 SpatiaLite 空间元数据；同一库已有同名图层则先删除
    重建（覆盖写入，幂等）。调用方用 ``ds.GetLayerByName(item.target)`` 取图层写要素。
    """
    from osgeo import ogr, osr

    driver = ogr.GetDriverByName("SQLite")
    if driver is None:
        raise FetchError("当前 GDAL 缺少 SQLite 驱动，无法创建矢量数据库。")
    if os.path.exists(db):
        ds = ogr.Open(db, 1) if driver else None
        if ds is not None:
            idx = ds.GetLayerByName(item.target)
            if idx is not None:
                ds.DeleteLayer(idx)
    else:
        ds = driver.CreateDataSource(db, options=["SPATIALITE=YES"])
    if ds is None:
        raise FetchError(f"创建 SpatiaLite 数据库失败：{db}")

    sr = osr.SpatialReference()
    sr.ImportFromEPSG(meta["wkid"])
    try:
        sr.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    except Exception:  # noqa: BLE001
        pass
    geom = _ogr_geom_type(meta)
    # 几何列名统一为 shape：项目本地库约定（gdb_to_postgis 同款
    # `-lco GEOMETRY_NAME=shape`，local_db_util 用 ST_AsText(shape) 读写）。
    # OGR 默认会建 GEOMETRY（大写）且与 SpatiaLite 元数据列名不一致，必须显式对齐。
    lyr = ds.CreateLayer(item.target, sr, geom, options=["GEOMETRY_NAME=shape"])
    if lyr is None:
        raise FetchError(f"SpatiaLite 创建图层失败：{item.target}")
    for fld in meta.get("fields", []):
        _add_ogr_field(lyr, fld)
    return ds


def _add_ogr_field(lyr: object, fld: dict) -> None:
    from osgeo import ogr

    name = str(fld.get("name") or "")
    if not name or name.lower() == "objectid":
        return
    t = str(fld.get("type", ""))
    if "Integer" in t:
        fdef = ogr.FieldDefn(name, ogr.OFTInteger64)
    elif "Double" in t or "Single" in t:
        fdef = ogr.FieldDefn(name, ogr.OFTReal)
    elif "Date" in t:
        fdef = ogr.FieldDefn(name, ogr.OFTDateTime)
    else:
        fdef = ogr.FieldDefn(name, ogr.OFTString)
        try:
            fdef.SetWidth(min(int(fld.get("length") or 255), 4096))
        except (TypeError, ValueError):
            fdef.SetWidth(255)
    lyr.CreateField(fdef)


def _append_vector_feature(lyr: object, gj: dict, wkid: int) -> None:
    from osgeo import ogr, osr

    props = gj.get("properties") or {}
    geom_gj = gj.get("geometry")
    feat = ogr.Feature(lyr.GetLayerDefn())
    for i in range(lyr.GetLayerDefn().GetFieldCount()):
        fname = lyr.GetLayerDefn().GetFieldDefn(i).GetName()
        if fname in props and props[fname] is not None:
            feat.SetField(fname, props[fname])
    if geom_gj is not None:
        # ArcGIS 的 f=geojson 几何按 GeoJSON 规范恒为 WGS84 坐标；
        # 目标矢量库图层 SR = 服务 SR。wkid≈4326（含 4490）时近似不转，其它转目标 SR。
        geom = ogr.CreateGeometryFromJson(json.dumps(geom_gj))
        if geom is not None and wkid not in (4326, 4490):
            tgt = osr.SpatialReference()
            tgt.ImportFromEPSG(wkid)
            try:
                tgt.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
            except Exception:  # noqa: BLE001
                pass
            geom.TransformTo(tgt)
        geom = _coerce_geom_type(geom, lyr.GetGeomType())
        if geom is not None:
            feat.SetGeometry(geom)
    lyr.CreateFeature(feat)
    feat = None  # noqa: F841 释放 C++ 资源


def _coerce_geom_type(geom, declared_type: int):
    """把单要素几何归一成图层声明的 Multi 类型（SpatiaLite 严格校验，见探针）。"""
    from osgeo import ogr

    if geom is None or declared_type == ogr.wkbUnknown:
        return geom
    gtype = geom.GetGeometryType()
    multi_by_single = {
        ogr.wkbPolygon: ogr.wkbMultiPolygon,
        ogr.wkbLineString: ogr.wkbMultiLineString,
        ogr.wkbPoint: ogr.wkbMultiPoint,
    }
    if gtype in multi_by_single and multi_by_single[gtype] == declared_type:
        multi = ogr.Geometry(declared_type)
        multi.AddGeometry(geom)
        return multi
    # 图层声明单类型却给多几何：拆第一个子几何（罕见，安全起见）
    if gtype in (ogr.wkbMultiPolygon, ogr.wkbMultiLineString, ogr.wkbMultiPoint):
        single_of_multi = {
            ogr.wkbMultiPolygon: ogr.wkbPolygon,
            ogr.wkbMultiLineString: ogr.wkbLineString,
            ogr.wkbMultiPoint: ogr.wkbPoint,
        }.get(gtype)
        if single_of_multi == declared_type:
            return geom.GetGeometryRef(0)
    return geom


def _ogr_geom_type(meta: dict) -> int:
    from osgeo import ogr

    gtype = str(meta.get("geometryType") or "")
    if "Point" in gtype and "Multi" in gtype:
        return ogr.wkbMultiPoint
    if "Point" in gtype:
        return ogr.wkbPoint
    if "Polyline" in gtype:
        return ogr.wkbMultiLineString
    if "Polygon" in gtype:
        return ogr.wkbMultiPolygon
    return ogr.wkbUnknown


# ====================================================================== 影像

def _count_tiles_in(rect: tuple, zmin: int, zmax: int) -> int:
    """按 WGS84 外包与层级范围统计 slippy 瓦片数。"""
    return sum(
        (_xspan(rect, z) + 1) * (_yspan(rect, z) + 1)
        for z in range(zmin, zmax + 1)
    )


def _estimate_raster_bytes(item: FetchItem, scope_wgs84: tuple, ctx: FetchContext):
    """影像估算 = 实际下载范围的瓦片数 × 平均瓦片字节。

    实际下载范围 = 所选范围 ∩ 影像服务实际范围（fullExtent），层级再限到服务缓存
    层级的交集；XYZ/WMS 模板无服务范围信息，用所选范围全算。
    """
    zmin, zmax = item.zmin or 0, item.zmax or 0
    rect = scope_wgs84
    if item.source_type == "arcgis-image":
        meta = _arcgis_image_meta(item, ctx) or {}
        levels = meta.get("tile_levels") or {}
        if not levels:
            return None
        zmin = max(zmin, min(levels))
        zmax = min(zmax, max(levels))
        if zmin > zmax:
            return 0
        svc = meta.get("service_extent_wgs84")
        if svc:
            inter = _intersect_rect(scope_wgs84, svc)
            if inter is None:
                return 0
            rect = inter
    return _count_tiles_in(rect, zmin, zmax) * _AVG_TILE_BYTES


def fetch_raster(
    item: FetchItem,
    stage_root: str,
    scope_wgs84: tuple[float, float, float, float],
    ctx: FetchContext,
) -> dict:
    """把影像源按所选范围 + 层级范围抓成 ``{z}/{x}/{y}.png`` XYZ 金字塔目录。

    网格按 Web Mercator slippy 规范枚举（QGIS 本地 XYZ 加载的预期形态，方案 §6.4）；
    每个瓦片按其 WGS84 外包换算成源服务的请求参数。
    """
    dest = os.path.join(stage_root, item.target.replace("/", os.sep))
    os.makedirs(dest, exist_ok=True)
    if item.source_type in ("xyz", "wmts") or (
        item.service_url and "{" in item.service_url
    ):
        template = item.service_url or ""
        if "{z}" not in template:
            raise FetchError(f"「{item.label}」地址不是 {{z}}/{{x}}/{{y}} 瓦片模板。")
    else:
        template = ""
    arc_meta = None
    eff_scope = scope_wgs84
    if item.source_type == "arcgis-image":
        arc_meta = _arcgis_image_meta(item, ctx)
        levels = (arc_meta or {}).get("tile_levels") or {}
        if not levels:
            raise FetchError(
                f"「{item.label}」：服务未提供可对齐的 Web Mercator(3857, 256px, 标准LOD)缓存切片。"
                "已停用动态导出，无法下载——请确认服务已建缓存，或改用 XYZ/WMTS 影像源。"
            )
        lo, hi = min(levels), max(levels)
        zmin, zmax = item.zmin or 0, item.zmax or 0
        # 只下载服务缓存实际有的层级（请求层级与服务层级交集；无交集才报错）
        zmin, zmax = max(zmin, lo), min(zmax, hi)
        if zmin > zmax:
            raise FetchError(
                f"「{item.label}」：请求影像层级 {item.zmin}-{item.zmax} 与该服务缓存切片 "
                f"z{lo}-{hi} 无交集，请调整弹窗影像层级后重试。"
            )
        if (zmin, zmax) != (item.zmin or 0, item.zmax or 0):
            ctx.log(f"「{item.label}」层级收敛到服务缓存 z{zmin}-{zmax}")
        # 用服务 fullExtent（影像范围）与所选范围取交，缩小枚举、减少 404 命中
        svc_ext = (arc_meta or {}).get("service_extent_wgs84")
        if svc_ext:
            eff_scope = _intersect_rect(scope_wgs84, svc_ext)
            if eff_scope is None:
                raise FetchError(
                    f"「{item.label}」：所选范围与影像服务范围无交集"
                    f"（服务范围 WGS84 {svc_ext[0]:.4f},{svc_ext[1]:.4f}-{svc_ext[2]:.4f},{svc_ext[3]:.4f}）。"
                    "请改用其它空间范围（视图/框选）后重试。"
                )
    else:
        zmin, zmax = item.zmin or 0, item.zmax or 0
    total = 0
    skipped = 0
    for z in range(zmin, zmax + 1):
        for x in range(_xmin(eff_scope, z), _xmax(eff_scope, z) + 1):
            for y in range(_ymin(eff_scope, z), _ymax(eff_scope, z) + 1):
                ctx.check()
                url, ext = _tile_request(item, template, z, x, y, ctx, arc_meta)
                if not url:
                    continue
                try:
                    data = _http_bytes(url, None, ctx)
                except FetchError as exc:
                    if "HTTP 404" in str(exc):
                        # 紧凑缓存也可能有个别瓦片未生成：跳过统计，不整批失败
                        skipped += 1
                        continue
                    raise
                # 以实际图片字节识别扩展名，避免服务端 jpg/png 与预估不一致导致加载失败
                ext = _sniff_image_ext(data) or ext
                final = os.path.join(dest, str(z), str(x), f"{y}.{ext}")
                os.makedirs(os.path.dirname(final), exist_ok=True)
                with open(final, "wb") as f:
                    f.write(data)
                ctx.report(len(data))
                total += 1
    if total == 0:
        raise FetchError(
            f"「{item.label}」：范围内没有抓到任何切片（404 {skipped} 张）。请确认该服务已生成缓存"
            "（含所选层级/区域），或改用已建缓存的 XYZ/WMTS 影像源。"
        )
    ctx.log(f"「{item.label}」瓦片金字塔完成：{total} 张（z {zmin}-{zmax}，跳过未生成 {skipped}）")
    return {"tile_count": total}


def _tile_request(
    item: FetchItem,
    template: str,
    z: int,
    x: int,
    y: int,
    ctx: FetchContext,
    arc_meta: Optional[dict] = None,
) -> tuple[str, str]:
    ext = "png"
    if template:
        url = template.replace("{z}", str(z)).replace("{x}", str(x)).replace("{y}", str(y))
        url = url.replace("{-y}", str((1 << z) - 1 - y)).replace("{q}", str(x) + str(y))
        if url.lower().endswith((".jpg", ".jpeg")):
            ext = "jpg"
        return url, ext
    st = item.source_type
    if st in ("wms", "wmts"):
        return _wms_tile(item, z, x, y, ctx), "png"
    if st == "arcgis-image":
        if arc_meta is None:
            arc_meta = _arcgis_image_meta(item, ctx)
        tile_url = _arcgis_tile_url(item, z, x, y, arc_meta)
        if not tile_url:
            raise FetchError(f"「{item.label}」：z{z} 不在该服务缓存切片层级内，无法切片下载。")
        return tile_url
    raise FetchError(f"「{item.label}」不支持的影像类型：{st}")


def _arcgis_tile_url(item: FetchItem, z: int, x: int, y: int, meta: dict):
    """缓存切片：``…/<MapServer|ImageServer>/tile/{level}/{row}/{col}``。

    返回 ``(url, ext)``；token 等查询参数从原 service_url 保留。层级不在缓存映射内
    返回 None，由调用方给出明确提示（不启用动态 export）。
    """
    base, _q = _split_query(item.service_url or "")
    levels = meta.get("tile_levels") or {}
    if z not in levels:
        return None
    url = base.rstrip("/") + f"/tile/{levels[z]}/{y}/{x}"
    if _q:
        url = _with_params(url, _q)
    return url, meta.get("format", "png")


def _arcgis_image_meta(item: FetchItem, ctx: FetchContext) -> dict:
    base = (item.service_url or "").split("?")[0]

    def load() -> dict:
        body = _http_json(base, {"f": "json"}, ctx)
        sr = body.get("spatialReference") or {}
        wkid = int(sr.get("latestWkid") or sr.get("wkid") or 4326)
        if wkid in (102100, 102113):
            wkid = 3857
        fmts = str(body.get("supportedImageFormats") or "PNG").upper()
        info = {
            "wkid": wkid,
            "format": "jpg" if "JPG" in fmts else "png",
            #: slippy z → ArcGIS 缓存 level 映射（仅 Web Mercator 256px 缓存）；None = 不能切片
            "tile_levels": _arcgis_tile_level_map(body.get("tileInfo"), wkid),
            #: 影像服务实际范围（WGS84），用于与所选范围取交
            "service_extent_wgs84": _full_extent_wgs84(body),
        }
        return info

    return _meta_cached(f"img:{base}", load)


def _full_extent_wgs84(body: dict):
    """读服务 JSON 的 fullExtent/extent 并归一成 WGS84 外包；取不到返回 None。"""
    ext = body.get("fullExtent") or body.get("extent")
    if not ext or not isinstance(ext, dict):
        return None
    sr = ext.get("spatialReference") or body.get("spatialReference") or {}
    try:
        wkid = int(sr.get("latestWkid") or sr.get("wkid") or 0)
        coords = [float(ext[k]) for k in ("xmin", "ymin", "xmax", "ymax")]
    except (TypeError, ValueError, KeyError):
        return None
    if wkid in (102100, 102113):
        wkid = 3857  # ArcGIS Web Mercator 别名
    if wkid in (4326, 4490):
        return (coords[0], coords[1], coords[2], coords[3])
    if wkid == 3857:
        return _merc_rect_to_wgs84(coords[0], coords[1], coords[2], coords[3])
    try:

        return srs.rect_to_wgs84(coords[0], coords[1], coords[2], coords[3], f"EPSG:{wkid}")
    except Exception:  # noqa: BLE001
        return None


def _merc_rect_to_wgs84(x1: float, y1: float, x2: float, y2: float):
    """Web Mercator 矩形 → WGS84（无 QGIS 依赖，纯几何换算）。"""
    r_x = 20037508.342789244  # 经度方向：半周长
    r_y = 6378137.0           # 纬度方向：地球半径（y = R·ln(tan(π/4+φ/2))）

    def _x(v: float) -> float:
        return v / r_x * 180.0

    def _y(v: float) -> float:
        return math.degrees(2.0 * math.atan(math.exp(v / r_y)) - math.pi / 2.0)

    return (_x(x1), _y(y1), _x(x2), _y(y2))


def _intersect_rect(a, b):
    """两个 WGS84 外包矩形取交；无交返回 None。"""
    x1, y1, x2, y2 = max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])
    if x2 <= x1 or y2 <= y1:
        return None
    return (x1, y1, x2, y2)


#: Web Mercator z0 分辨率（m/px）
_MERC_RES0 = 156543.03392804097


def _arcgis_tile_level_map(tileinfo, wkid: int, max_z: int = 22):
    """从 ArcGIS 服务 tileInfo 建 slippy z → 缓存 level 映射；无法对齐返回 None。

    条件：Web Mercator（3857）＋ 原点在 (-20037508.34, 20037508.34) ＋ 瓦片 256px
    ＋ LOD 分辨率等于标准 slippy 分辨率（r0/2^z）。满足才说明 `/tile` 切片与我们的
    XYZ 网格一一对应，可直接抓切片；否则返回 None，抓取时给出明确原因（不导出）。
    """
    try:
        if not tileinfo or wkid != 3857:
            return None
        origin = tileinfo.get("origin") or {}
        ox = float(origin.get("x") or 0)
        oy = float(origin.get("y") or 0)
        if abs(ox + 20037508.342789244) > 10.0 or abs(oy - 20037508.342789244) > 10.0:
            return None
        rows = int(tileinfo.get("rows") or 0)
        cols = int(tileinfo.get("cols") or 0)
        if (rows and rows != 256) or (cols and cols != 256):
            return None  # 非 256 瓦片，XYZ 目录不再直接对得上
        mapping: dict[int, int] = {}
        for lod in tileinfo.get("lods") or []:
            try:
                res = float(lod.get("resolution") or 0)
                level = int(lod.get("level") or -1)
            except (TypeError, ValueError):
                continue
            if res <= 0 or level < 0:
                continue
            z = int(round(math.log2(_MERC_RES0 / res)))
            if 0 <= z <= max_z:
                mapping.setdefault(z, level)
        return mapping or None
    except Exception:  # noqa: BLE001
        return None


def _wms_tile(item: FetchItem, z: int, x: int, y: int, ctx: FetchContext) -> str:
    lon1, lat2, lon2, lat1 = _tile_bounds(x, y, z)
    base, q = _split_query(item.service_url or "")
    existing = _parse_qs(q)
    crs_param = "CRS" if existing.get("CRS") else ("SRS" if existing.get("SRS") else None)
    crs = (existing.get(crs_param) if crs_param else None) or "EPSG:4326"
    x1, y1, x2, y2 = (lon1, lat1, lon2, lat2)
    if crs.upper() != "EPSG:4326":

        rect = srs.rect_wgs84_to(lon1, lat1, lon2, lat2, crs)
        if rect:
            x1, y1, x2, y2 = rect
    if not existing.get("LAYERS"):
        raise FetchError("WMS 地址需带 LAYERS 参数（或换用 WMTS/XYZ 影像源）。")
    params = dict(existing)
    params["REQUEST"] = "GetMap"
    params["SERVICE"] = "WMS"
    params["WIDTH"] = "256"
    params["HEIGHT"] = "256"
    params[crs_param or "CRS"] = crs
    params["BBOX"] = f"{x1},{y1},{x2},{y2}"
    params["FORMAT"] = existing.get("FORMAT") or "image/png"
    params["VERSION"] = existing.get("VERSION") or "1.3.0"
    return _with_params(base, params)


# ====================================================================== 瓦片网格（Web Mercator slippy）

def _sniff_image_ext(data: bytes) -> Optional[str]:
    """按文件头识别 png/jpg/webp；无法识别返回 None（保留预估扩展名）。"""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:3] == b"\xff\xd8\xff":
        return "jpg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None

def _xmin(scope: tuple, z: int) -> int:
    x1, _, x2, _ = scope
    return min(_lon_to_x(x1, z), _lon_to_x(x2, z))


def _xmax(scope: tuple, z: int) -> int:
    x1, _, x2, _ = scope
    return max(_lon_to_x(x1, z), _lon_to_x(x2, z))


def _ymin(scope: tuple, z: int) -> int:
    _, y1, _, y2 = scope
    return min(_lat_to_y(y1, z), _lat_to_y(y2, z))


def _ymax(scope: tuple, z: int) -> int:
    _, y1, _, y2 = scope
    return max(_lat_to_y(y1, z), _lat_to_y(y2, z))


def _xspan(scope: tuple, z: int) -> int:
    return _xmax(scope, z) - _xmin(scope, z)


def _yspan(scope: tuple, z: int) -> int:
    return _ymax(scope, z) - _ymin(scope, z)


def _lon_to_x(lon: float, z: int) -> int:
    return int(math.floor((lon + 180.0) / 360.0 * (1 << z)))


def _lat_to_y(lat: float, z: int) -> int:

    rad = math.radians(lat)
    return int(math.floor((1.0 - math.asinh(math.tan(rad)) / math.pi) / 2.0 * (1 << z)))


def _tile_bounds(x: int, y: int, z: int) -> tuple[float, float, float, float]:
    """瓦片 WGS84 外包 (lon1, lat2, lon2, lat1)，即 (西, 北, 东, 南)。"""

    n = 1 << z
    lon1 = x / n * 360.0 - 180.0
    lon2 = (x + 1) / n * 360.0 - 180.0
    lat1 = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / n))))
    lat2 = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * (y + 1) / n))))
    return (lon1, lat2, lon2, lat1)


# ====================================================================== HTTP

_SSL_CTX = None


def _ssl_context():
    """内网 ArcGIS 服务多用自签名 HTTPS，urllib 默认校验证书会失败。

    返回一个不做证书校验的 SSL 上下文（局域网内部任务数据源，凭据另有 token 鉴权）。
    """
    global _SSL_CTX
    if _SSL_CTX is None:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        _SSL_CTX = ctx
    return _SSL_CTX


def _http_bytes(url: str, params: Optional[dict], ctx: FetchContext) -> bytes:
    if params:
        url = _with_params(url, params)
    url = _encode_non_ascii(url)
    last: object = None
    for attempt in range(3):
        ctx.check()
        try:
            req = urllib.request.Request(url, headers=dict(ctx.headers))
            with urllib.request.urlopen(req, timeout=ctx.timeout, context=_ssl_context()) as resp:  # noqa: S310
                return resp.read()
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                raise FetchError(f"请求被拒绝（HTTP {exc.code}）：{url}") from exc
            last = exc
        except (urllib.error.URLError, OSError) as exc:
            last = exc
        time.sleep(0.4 * (attempt + 1))
    raise FetchError(f"请求失败：{url}（{last}）")


def _http_json(url: str, params: dict, ctx: FetchContext) -> dict:
    if params:
        url = _with_params(url, params)
    try:
        data = _http_bytes(url, None, ctx)
        return json.loads(data.decode("utf-8", "replace"))
    except json.JSONDecodeError as exc:
        raise FetchError(f"服务返回非 JSON（可能是地址错误或需凭证）：{url}") from exc


def _head_length(url: str, ctx: FetchContext) -> Optional[int]:
    try:
        req = urllib.request.Request(_encode_non_ascii(url), method="HEAD", headers=dict(ctx.headers))
        with urllib.request.urlopen(req, timeout=ctx.timeout, context=_ssl_context()) as resp:  # noqa: S310
            return int(resp.headers.get("Content-Length") or 0) or None
    except Exception:  # noqa: BLE001
        return None


def _encode_non_ascii(url: str) -> str:
    """把 URL 里的非 ASCII 字节百分号编码（幂等，已编码的 ``%xx`` 不受影响）。

    urllib 不会自动编码中文路径，需先转成干净 ASCII URL 再发请求。
    """
    return re.sub(
        r"[^\x00-\x7F]",
        lambda m: "".join(f"%{b:02X}" for b in m.group(0).encode("utf-8")),
        url,
    )


def _with_params(url: str, params: dict) -> str:
    base, q = _split_query(url)
    merged = dict(_parse_qs(q))
    merged.update({k: str(v) for k, v in params.items()})
    if not merged:
        return base
    return base + "?" + urllib.parse.urlencode(merged)


def _split_query(url: str) -> tuple[str, dict]:
    p = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit((p.scheme, p.netloc, p.path, "", "")), _parse_qs(p.query)


def _parse_qs(q: str) -> dict:
    return {k: vs[0] if vs else "" for k, vs in urllib.parse.parse_qs(q).items()}


# ====================================================================== 小工具

def _item_urls(item: FetchItem) -> list[str]:
    urls = list(item.file_urls)
    if not urls and item.service_url and item.source_type == "file":
        urls = [item.service_url]
    return urls


def _name_from_url(url: str) -> str:
    path = urllib.parse.urlsplit(url).path
    name = urllib.parse.unquote(os.path.basename(path))

    if not name or "." not in name:
        name = safe_name(name or "file") + ".dat"
    return safe_name(name)


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(1 << 20)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _fmt(num: int) -> str:

    return format_bytes(num)


def _arcgis_sublayer(item: FetchItem) -> str:

    if not item.service_url:
        raise FetchError(f"「{item.label}」缺服务地址。")
    return arcgis_sublayer_url(item.service_url, item.layer_id)
