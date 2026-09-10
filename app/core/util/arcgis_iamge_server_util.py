'''
    ArcGIS 影像瓦片服务下载工具

    把 ArcGIS Server 的影像/缓存服务(MapServer/ImageServer)按金字塔逐级
    下载瓦片,按 :mod:`core.util.local_db_util` 的影像存储约定落库:瓦片
    数据表名与图层名一致并注册进 ``user_relation``(type=image),同时写
    MBTiles 规范 metadata(表结构即 MBTiles 瓦片表),供离线使用、可被
    ``query_all_table_name()`` 列出。落库目标库格式由 local_db_util 决定:
    新文件默认 GeoPackage(纯 sqlite3 即可写,无需 mod_spatialite),已存在
    文件按内容探测(spatialite / GeoPackage / mbtiles);元信息 / 取片规划
    本身只用标准库。

    **支持的两种取片方式**(按服务元数据自动选择):

    - ``tile`` —— 服务开了缓存(元数据带 ``tileInfo`` / ``lods``),直接按
      REST 标准 ``…/MapServer/tile/{level}/{row}/{col}`` 拉原始瓦片,
      与服务缓存完全一致、质量无损;
    - ``export`` —— 服务没开缓存(如纯 ImageServer / 动态 MapServer),退回
      用 ``/export``(MapServer)或 ``/exportimage``(ImageServer)按每块
      瓦片的 bbox 请求 256x256 出图,自行拼金字塔。此时服务坐标系必须是
      Web Mercator(102100/102113/3857)或度单位地理坐标(4326/4490/
      4610/4214 等,基准差异米级可忽略),本工具用纯数学合成标准缩放级别;
      其他投影需先自行按服务坐标给出 extent。

    **坐标系约定**:``extent`` 默认按服务坐标系给;extent_sr 显式传
    ``4326`` 等度单位地理坐标或 Web Mercator wkid 且服务坐标系属于上述
    两类之一时,本地做纯数学换算,不引入 pyproj 等第三方依赖。

    落库与 :mod:`core.util.arcgis_server_util`(要素)流程对齐:取元信息 →
    估算瓦片总数 → 逐级拉取 → 落库。落库由
    :meth:`ArcGISImageServer.save_tiles_to_db` 委托 local_db_util 的
    ``create_arcgis_image_store`` + ``batch_add_image_tile_data`` 完成:

    - 瓦片数据表名 == 图层名(清洗规则同 local_db_util),并注册进
      ``user_relation``(type=image),可被 ``query_all_table_name()`` 列出;
      注册行的 ``attr`` 字段以 JSON 存源服务的空间等属性(wkid/extent/
      bounds/级别/URL 等),供后续按注册名恢复源服务信息;
    - 表结构同 MBTiles 瓦片表(``zoom_level/tile_column/tile_row/tile_data``
      + 三重唯一索引);影像名/format/bounds/minzoom/maxzoom 及源地址
      写入库内全局 ``metadata`` 表(每库一份,同 local_db_util 约定);
    - ArcGIS 瓦片行号即 XYZ(行号自顶部向下)。本工具统一按 TMS 行号语义
      交给 local_db_util(``tile_row = 2**level - 1 - row``),落库端再按
      目标格式定最终行号 —— GeoPackage 库存 XYZ(top-origin, GPKG 规范 /
      GDAL 直读的行号), spatialite 等老格式库存 TMS(仅老库回读时翻转)。

    取片/规划只依赖标准库(urllib + concurrent.futures);落库: GeoPackage
    库走纯 sqlite3 路径(无第三方依赖),spatialite 旧库仍需 QGIS 环境的
    pysqlite3 + mod_spatialite(传已打开的 ``db_util`` 实例时同理)。
'''

import json
import math
import re
import time
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, quote, unquote, urlencode, urlparse, urlunparse
from urllib.request import Request, urlopen

from app.core.util.local_db_util import local_db_util as _local_db_util

_DEFAULT_TIMEOUT = 30.0
_TILE_SIZE = 256  # 导出方式单块瓦片边长(px);tile 方式按服务 tileInfo 实际尺寸

#: 支持的"纯数学可互转"坐标系:Web Mercator 与"度单位地理坐标"两大类。
#: 地理类坐标单位为度(经纬度),4490/4610/4214 等与 4326 的基准差异在
#: 米级,对离线切片可忽略,直接按同一套数学处理。
_SR_LL = (4326, 4490, 4610, 4214, 4258, 4269, 4267, 4283, 4230)
_SR_MERC = (102100, 102113, 3857, 900913)
#: Web Mercator 合成分辨率/原点(标准 XYZ 金字塔,z0 即整个世界一张瓦片)
_MERC_RES0 = 156543.03392804097
_MERC_ORIGIN = (-20037508.342789244, 20037508.342789244)
#: 经纬度合成分辨率/原点(z0 一张瓦片盖满 360°,仅供自洽离线下发)
_DEG_RES0 = 360.0 / float(_TILE_SIZE)
_DEG_ORIGIN = (-180.0, 90.0)
_SYNTH_MAX_LEVEL = 18  # 导出方式合成的最大级别


def _decode_http_text(raw: bytes, charset: str | None = None) -> str:
    """把 HTTP 响应字节解成文本(ArcGIS JSON 通常是 UTF-8)。

    - 未声明 charset / 声明为 UTF-8 系:优先 ``utf-8-sig``(顺带去掉 BOM)
    - 声明了其他 charset(如 gbk):先按声明解,失败再退回 UTF-8
    - 全失败才用 ``errors="replace"`` 容错
    """
    candidates: list[str] = []
    if charset and "utf" not in charset.lower():
        candidates.append(charset)
    candidates += ["utf-8-sig", "utf-8"]
    for enc in candidates:
        try:
            return raw.decode(enc)
        except (LookupError, UnicodeDecodeError):
            continue
    return raw.decode("utf-8", errors="replace")


def _http_error_hint(text: str) -> str:
    """从错误响应文本里尽量挖一句人话:JSON ``error.message``,否则去 HTML 标签。"""
    try:
        obj = json.loads(text)
    except ValueError:
        pass
    else:
        if isinstance(obj, dict):
            err = obj.get("error")
            if isinstance(err, dict):
                msg = (err.get("message") or "").strip()
                code = err.get("code")
                return f"(code={code}) {msg}" if msg else str(err)[:200]
            return str(obj)[:200]
    hint = re.sub(r"<[^>]+>", " ", text)
    hint = re.sub(r"\s+", " ", hint).strip()
    return hint[:200]


def _split_service_url(url: str) -> tuple[str, dict[str, str]]:
    """清洗服务地址 → (清洗后基址, 需随每个请求保留的查询参数)。

    - 只剥掉 ``f`` 参数(每次按需重发),token 等其余参数原样保留
    - 路径中的非 ASCII(如中文)补百分号编码;已编码的 ``%XX`` 原样保留
    """
    s = (url or "").strip()
    if not s:
        raise ValueError("服务地址不能为空")
    parts = urlparse(s)
    if parts.scheme not in ("http", "https"):
        raise ValueError(f"仅支持 http/https 地址,收到: {parts.scheme!r}")
    path = quote(parts.path.rstrip("/"), safe="/%:@!$&'()*+,;=~._-") or "/"
    keep = {
        key: value
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key != "f"
    }
    return urlunparse((parts.scheme, parts.netloc, path, "", "", "")), keep


def _ll_to_merc(x: float, y: float) -> tuple[float, float]:
    """经纬度 → Web Mercator 米(纯数学,供 extent 换算)。"""
    y = max(-85.05112878, min(85.05112878, y))
    mx = x * 20037508.342789244 / 180.0
    my = math.log(math.tan((90.0 + y) * math.pi / 360.0)) * 20037508.342789244 / math.pi
    return mx, my


def _merc_to_ll(x: float, y: float) -> tuple[float, float]:
    """Web Mercator 米 → 经纬度(纯数学,供 extent 换算)。"""
    lng = x * 180.0 / 20037508.342789244
    lat = math.degrees(2 * math.atan(math.exp(y * math.pi / 20037508.342789244)) - math.pi / 2)
    return lng, lat


def _convert_point(x: float, y: float, src: int, dst: int) -> tuple[float, float]:
    """在两个“纯数学可互转”坐标系间换算单点;不支持时抛中文 RuntimeError。"""
    if src == dst:
        return x, y
    if src in _SR_LL and dst in _SR_LL:
        # 同为度单位地理坐标(4326/4490/4610...), 基准差异米级可忽略
        return x, y
    if src in _SR_LL and dst in _SR_MERC:
        return _ll_to_merc(x, y)
    if src in _SR_MERC and dst in _SR_LL:
        return _merc_to_ll(x, y)
    raise RuntimeError(
        f"坐标系 {src} → {dst} 无法本地换算;"
        "请把 extent 换成服务坐标系(Web Mercator 或经纬度之外需自行换算)"
    )


def _normalize_extent(
    extent: Any, extent_sr: int | None, service_wkid: int | None, what: str
) -> list[float]:
    """extent 形参 → 服务坐标系下的 ``[xmin, ymin, xmax, ymax]``。"""
    nums = (
        [float(x) for x in extent.split(",") if x.strip()]
        if isinstance(extent, str)
        else [float(x) for x in extent]
    )
    if len(nums) != 4:
        raise ValueError(f"{what} 需要 4 个数值(minx,miny,maxx,maxy),收到: {extent!r}")
    if extent_sr is None or service_wkid is None or extent_sr == service_wkid:
        return [nums[0], nums[1], nums[2], nums[3]]
    (ax, ay) = _convert_point(nums[0], nums[1], int(extent_sr), service_wkid)
    (bx, by) = _convert_point(nums[2], nums[3], int(extent_sr), service_wkid)
    return [min(ax, bx), min(ay, by), max(ax, bx), max(ay, by)]


def _clip_extent(extent: list[float], full: list[float]) -> list[float] | None:
    """两盒求交;无交集返回 None。"""
    box = [max(extent[0], full[0]), max(extent[1], full[1]),
           min(extent[2], full[2]), min(extent[3], full[3])]
    if box[0] >= box[2] or box[1] >= box[3]:
        return None
    return box


def _sr_wkid(meta: dict[str, Any]) -> int | None:
    """从服务元数据里挖坐标系 wkid(顶层 spatialReference → extent 里兜底)。"""
    for holder in (meta.get("spatialReference"), (meta.get("extent") or {}).get("spatialReference")):
        if isinstance(holder, dict):
            try:
                return int(holder.get("latestWkid") or holder.get("wkid"))
            except (TypeError, ValueError):
                continue
    return None


def _ll_bounds_of(extent: list[float], wkid: int | None) -> str | None:
    """把服务坐标系的 extent 换成 WGS84 经纬度 bounds(MBTiles metadata 用)。"""
    if wkid is None:
        return None
    try:
        (minx, miny) = _convert_point(extent[0], extent[1], wkid, 4326)
        (maxx, maxy) = _convert_point(extent[2], extent[3], wkid, 4326)
    except RuntimeError:
        return None
    return f"{minx},{miny},{maxx},{maxy}"


def _wgs84_bounds_of(
    info: dict[str, Any], extent: Any, extent_sr: int | None
) -> tuple[float, float, float, float] | None:
    """取 WGS84 经纬度 bounds(local_db_util 建影像表写 metadata 用)。

    优先换算服务 fullExtent(数据源范围),其次换算调用方传入的 extent
    (按 extent_sr 理解);坐标系不在纯数学支持集合内返回 None,交由
    local_db_util 按元信息自行推导 / 报错提示显式传 bounds。
    """
    full = info.get("full_extent")
    if full is not None:
        b = _ll_bounds_of(full, info.get("wkid"))
        if b:
            return tuple(float(v) for v in b.split(","))
    if extent is not None:
        try:
            norm = _normalize_extent(extent, extent_sr, info.get("wkid"), "extent")
        except (RuntimeError, ValueError):
            return None
        b = _ll_bounds_of(norm, info.get("wkid"))
        if b:
            return tuple(float(v) for v in b.split(","))
    return None


def _format_of(tile_info: dict[str, Any] | None) -> str:
    """服务瓦片格式 → png/jpg。"""
    fmt = str((tile_info or {}).get("format") or "").upper()
    return "jpg" if "JPEG" in fmt or "JPG" in fmt else "png"


def _export_endpoint(url: str) -> str:
    """服务根地址 → 动态出图接口(ImageServer 用 exportimage,其余 export)。

    export/exportimage 只挂在**服务根**上(MapServer 子图层没有 export
    接口),因此地址里带数字子图层段(如 …/MapServer/0)时先剥掉,再拼接口。
    """
    parts = url.rstrip("/").split("/")
    while parts and parts[-1].isdigit():
        parts.pop()
    base = "/".join(parts)
    if re.search(r"/imageserver$", base, re.IGNORECASE):
        return base + "/exportimage"
    return base + "/export"


class ArcGISImageServer:
    """ArcGIS 影像瓦片服务下载工具。

    每个实例可复用于多个服务;服务元信息按地址缓存,可用 :meth:`clear_cache`
    清空。典型用法::

        server = ArcGISImageServer()
        # 打开本地库(新文件默认 GeoPackage,自动 init 含 user_relation)后落库:
        from app.core.util.local_db_util import local_db_util
        db = local_db_util(r"D:/离线/影像库.gpkg")   # 新规: 本体直接 .gpkg 后缀
        server.save_tiles_to_db(
            url="http://…/MapServer/0",
            db_util=db,  # 表名=图层名,并注册进 user_relation(type=image)
            extent=[113.5, 27.8, 117.5, 31.5], extent_sr=4326,
            min_zoom=0, max_zoom=10, overwrite=True,
            progress=lambda done, total: print(f"{done}/{total}"),
        )

    Args:
        timeout: 单次请求超时秒数。
        retries: 网络层失败(连接/超时)的额外重试次数;HTTP 4xx/5xx 不重试。
        extra_headers: 追加到每次请求的 HTTP 头。
    """

    def __init__(
        self,
        *,
        timeout: float = _DEFAULT_TIMEOUT,
        retries: int = 1,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        self._timeout = timeout
        self._retries = max(0, int(retries))
        self._extra_headers = dict(extra_headers or {})
        self._meta_cache: dict[str, dict[str, Any]] = {}

    # ------------------------------------------------------------------
    # HTTP 基础
    # ------------------------------------------------------------------
    def clear_cache(self) -> None:
        """清空服务元信息缓存。"""
        self._meta_cache.clear()

    def _compose(self, base: str, keep: dict[str, str], params: dict[str, Any]) -> str:
        merged = dict(keep)
        merged.update({key: str(value) for key, value in params.items()})
        return base + ("?" + urlencode(merged) if merged else "")

    def _fetch_bytes(self, url: str) -> bytes:
        """GET 返回原始字节;HTTP 4xx/5xx / 网络失败抛中文 RuntimeError。"""
        headers = {
            "User-Agent": "qgis-dev/arcgis_image_server_util",
            "Accept": "*/*",
        }
        headers.update(self._extra_headers)
        req = Request(url, headers=headers)
        raw: bytes | None = None
        error: Exception | None = None
        for attempt in range(self._retries + 1):
            try:
                with urlopen(req, timeout=self._timeout) as resp:
                    raw = resp.read()
                error = None
                break
            except HTTPError as exc:
                detail = ""
                try:
                    body = exc.read()
                    cs = exc.headers.get_content_charset() if exc.headers else None
                    detail = _http_error_hint(_decode_http_text(body, cs))
                except Exception:
                    pass
                raise RuntimeError(
                    f"ArcGIS 瓦片请求失败 HTTP {exc.code}: {detail or exc.reason}"
                ) from exc
            except (URLError, TimeoutError, OSError) as exc:
                error = exc
                if attempt < self._retries:
                    time.sleep(0.3 * (attempt + 1))
        if raw is None:
            raise RuntimeError(f"请求 ArcGIS 影像服务失败: {error}") from error
        return raw

    def _fetch_json(self, url: str) -> dict[str, Any]:
        """GET JSON(服务元信息用)。"""
        raw = self._fetch_bytes(url)
        text = _decode_http_text(raw)
        try:
            data = json.loads(text)
        except ValueError as exc:
            raise RuntimeError(
                f"ArcGIS 返回内容不是合法 JSON,前 200 字符: {text[:200]!r}"
            ) from exc
        if not isinstance(data, dict):
            raise RuntimeError("ArcGIS 返回内容不是 JSON 对象")
        err = data.get("error")
        if isinstance(err, dict):
            raise RuntimeError(f"ArcGIS 服务返回错误: {_http_error_hint(text)}")
        return data

    # ------------------------------------------------------------------
    # 元信息 / 瓦片方案
    # ------------------------------------------------------------------
    def metadata(self, url: str, *, refresh: bool = False) -> dict[str, Any]:
        """取服务元信息(``…/MapServer?f=json`` 对象),按地址缓存。

        Args:
            url: 服务 REST 地址(MapServer / ImageServer 根或子图层地址)。
            refresh: 为 True 时绕过缓存重新抓取。
        """
        base, keep = _split_service_url(url)
        if not refresh and base in self._meta_cache:
            return self._meta_cache[base]
        meta = self._fetch_json(self._compose(base, keep, {"f": "json"}))
        self._meta_cache[base] = meta
        return meta

    def tile_info(
        self, url: str, *, refresh: bool = False
    ) -> dict[str, Any]:
        """解析服务瓦片方案(自动选择 tile / export 取片方式)。

        服务元数据带 ``tileInfo`` → ``mode="tile"``,直接用其 lods;
        否则若坐标系是 Web Mercator / 度单位地理坐标(4326/4490/4610/
        4214 等)→ ``mode="export"``,用纯数学合成标准缩放级别;其他情况
        抛错(需自行按服务坐标给 extent 的场景也在这里提示)。

        Returns:
            ``{"mode", "format", "px": (宽, 高), "origin": (x, y),
            "wkid": int|None, "base": 清洗后服务地址,
            "full_extent": [xmin, ymin, xmax, ymax]|None,
            "lods": [{"level", "res", "scale"}]}``
        """
        base, keep = _split_service_url(url)
        meta = self.metadata(url, refresh=refresh)
        wkid = _sr_wkid(meta)
        tile_info = meta.get("tileInfo")
        full_extent = meta.get("fullExtent") or meta.get("extent")
        fe = None
        if isinstance(full_extent, dict):
            fe = [float(full_extent[k]) for k in ("xmin", "ymin", "xmax", "ymax")]

        if isinstance(tile_info, dict):
            origin = tile_info.get("origin") or {}
            lods = []
            for lod in tile_info.get("lods") or []:
                try:
                    res = float(lod.get("resolution"))
                except (TypeError, ValueError):
                    continue
                lods.append({
                    "level": int(lod.get("level") or len(lods)),
                    "res": res,
                    "scale": lod.get("scale"),
                })
            if not lods:
                raise RuntimeError("服务 tileInfo 里没有可用的 lods,无法下载")
            px = (
                int(tile_info.get("cols") or _TILE_SIZE),
                int(tile_info.get("rows") or _TILE_SIZE),
            )
            try:
                ox, oy = float(origin.get("x")), float(origin.get("y"))
            except (TypeError, ValueError):
                raise RuntimeError("服务 tileInfo 缺少 origin,无法下载") from None
            return {
                "mode": "tile",
                "format": _format_of(tile_info),
                "px": px,
                "origin": (ox, oy),
                "wkid": wkid,
                "base": base,
                "keep": keep,
                "full_extent": fe,
                "lods": lods,
                "res_by_level": {lod["level"]: lod["res"] for lod in lods},
            }

        # 无缓存:仅对可纯数学合成的坐标系走 export
        if wkid not in _SR_MERC and wkid not in _SR_LL:
            raise RuntimeError(
                f"服务未开缓存且坐标系 wkid={wkid} 不在支持集合"
                "(Web Mercator: 102100/102113/3857;度单位地理坐标:"
                "4326/4490/4610/4214 等),无法本地合成缩放级别;"
                "请开缓存,或用支持该投影的客户端下载"
            )
        if wkid in _SR_MERC:
            res0, origin0 = _MERC_RES0, _MERC_ORIGIN
        else:
            res0, origin0 = _DEG_RES0, _DEG_ORIGIN
        max_level = _SYNTH_MAX_LEVEL
        lods = [
            {"level": z, "res": res0 / (1 << z), "scale": None}
            for z in range(0, max_level + 1)
        ]
        return {
            "mode": "export",
            "format": "png",
            "px": (_TILE_SIZE, _TILE_SIZE),
            "origin": origin0,
            "wkid": wkid,
            "base": base,
            "keep": keep,
            "full_extent": fe,
            "lods": lods,
            "res_by_level": {lod["level"]: lod["res"] for lod in lods},
        }

    # ------------------------------------------------------------------
    # 取片范围规划 / 估算
    # ------------------------------------------------------------------
    def _select_lods(
        self,
        info: dict[str, Any],
        min_zoom: int | None,
        max_zoom: int | None,
    ) -> list[dict[str, Any]]:
        """按级别范围过滤 lods;None 取全部,越界给出可读报错。"""
        lods = sorted(info["lods"], key=lambda lod: lod["level"])
        lo = lods[0]["level"] if min_zoom is None else min_zoom
        hi = lods[-1]["level"] if max_zoom is None else max_zoom
        if lo > hi:
            lo, hi = hi, lo
        selected = [lod for lod in lods if lo <= lod["level"] <= hi]
        if not selected:
            raise RuntimeError(
                f"请求的级别范围 {lo}~{hi} 没有可用级别"
                f"(服务可用 {lods[0]['level']}~{lods[-1]['level']})"
            )
        return selected

    def tile_ranges(
        self,
        url: str,
        *,
        extent: Any = None,
        extent_sr: int | None = None,
        min_zoom: int | None = None,
        max_zoom: int | None = None,
        clip: bool = True,
        refresh: bool = False,
    ) -> tuple[list[dict[str, Any]], int]:
        """按 extent / 缩放范围规划取片区间。

        Args:
            extent: 下载范围 ``[minx, miny, maxx, maxy]``;None 用服务
                fullExtent。默认按服务坐标系,见模块 docstring 的换算约定。
            min_zoom / max_zoom: 级别(对应服务 lods 的 level,export 方式
                为标准 XYZ 级别);None 取服务全部级别。
            clip: 为 True 时 extent 与服务 fullExtent 求交,越界部分不请求。
            refresh: 绕过瓦片方案缓存重新抓取。

        Returns:
            (plans, total):plans 为每级一块的取片区间
            ``{"level", "res", "col0", "col1", "row0", "row1", "n"}``;
            total 为总瓦片数。
        """
        info = self.tile_info(url, refresh=refresh)
        full = info["full_extent"]
        wkid = info["wkid"]
        if extent is None:
            if full is None:
                raise RuntimeError("服务没有提供 fullExtent,请显式传 extent=")
            box = list(full)
        else:
            box = _normalize_extent(extent, extent_sr, wkid, "extent")
        if clip and full is not None and wkid is not None:
            fe_box = _normalize_extent(full, wkid, wkid, "fullExtent")
            clipped = _clip_extent(box, fe_box)
            if clipped is None:
                return [], 0
            box = clipped

        plans: list[dict[str, Any]] = []
        px_w, px_h = info["px"]
        origin_x, origin_y = info["origin"]
        for lod in self._select_lods(info, min_zoom, max_zoom):
            step_w = lod["res"] * px_w
            step_h = lod["res"] * px_h
            # 下界闭、上界开, 范围边界正好压在瓦片格线上时不多带下一块
            # (浮点除到恰好整数用 1e-9 相对缓冲吸收)
            col0 = math.floor((box[0] - origin_x) / step_w)
            col1 = math.ceil((box[2] - origin_x) / step_w - 1e-9) - 1
            row0 = math.floor((origin_y - box[3]) / step_h)
            row1 = math.ceil((origin_y - box[1]) / step_h - 1e-9) - 1
            if col1 < col0 or row1 < row0:
                continue
            n = (col1 - col0 + 1) * (row1 - row0 + 1)
            plans.append({
                "level": lod["level"],
                "res": lod["res"],
                "col0": col0, "col1": col1,
                "row0": row0, "row1": row1,
                "n": n,
            })
        return plans, sum(p["n"] for p in plans)

    def tile_count(
        self,
        url: str,
        *,
        extent: Any = None,
        extent_sr: int | None = None,
        min_zoom: int | None = None,
        max_zoom: int | None = None,
        clip: bool = True,
        refresh: bool = False,
    ) -> int:
        """估算满足范围的瓦片总数(下载前先算好,供进度回调 total 用)。"""
        _, total = self.tile_ranges(
            url, extent=extent, extent_sr=extent_sr,
            min_zoom=min_zoom, max_zoom=max_zoom,
            clip=clip, refresh=refresh,
        )
        return total

    # ------------------------------------------------------------------
    # 拉取单块瓦片
    # ------------------------------------------------------------------
    def download_tile(
        self,
        url: str,
        level: int,
        row: int,
        col: int,
        *,
        refresh: bool = False,
    ) -> bytes:
        """拉单块瓦片原始字节(与 save_tiles_to_db 同一套 tile/export 逻辑)。"""
        info = self.tile_info(url, refresh=refresh)
        if info["mode"] == "tile":
            return self._fetch_bytes(
                self._compose(
                    f"{info['base']}/tile/{level}/{row}/{col}", info["keep"], {}
                )
            )
        return self._export_tile_bytes(info, level, row, col)

    def _export_tile_bytes(
        self, info: dict[str, Any], level: int, row: int, col: int
    ) -> bytes:
        """export 方式:按瓦片 bbox 请求 256x256 出图, 影像外区域必须透明。

        ArcGIS export 的透明语义: 仅 ``format=png32`` + ``transparent=true``
        会把影像范围之外的背景置为 **alpha=0 的透明像素**; 默认
        (``png24``/不带 transparent) 背景是白/黑的不透明填充 —— 瓦片金字塔
        覆盖范围比影像大很多(整层棋盘都下载), 不透明的多余区域会盖住底图。
        因此取片格式固定为 png32+transparent; 服务不支持时退 png(透明参数
        对无 alpha 通道的 png24 常被忽略, 属兼容回退); jpg 无透明概念, 仅
        作最后手段(多余区域会是服务端背景色)。
        """
        try:
            res = info["res_by_level"][level]
        except KeyError:
            raise RuntimeError(f"服务没有级别 {level} 的合成方案") from None
        origin_x, origin_y = info["origin"]
        px_w, px_h = info["px"]
        xmin = origin_x + col * res * px_w
        xmax = xmin + res * px_w
        ymax = origin_y - row * res * px_h
        ymin = ymax - res * px_h
        wkid = info["wkid"]
        params: dict[str, Any] = {
            "bbox": f"{xmin},{ymin},{xmax},{ymax}",
            "bboxSR": wkid,
            "size": f"{px_w},{px_h}",
            "f": "image",
        }
        last: Exception | None = None
        for fmt in ("png32", "png", "jpg"):
            try:
                params["format"] = fmt
                if fmt == "jpg":
                    # jpg 无 alpha 通道, 服务端可能拒收/忽略 transparent
                    params.pop("transparent", None)
                else:
                    params["transparent"] = "true"
                return self._fetch_bytes(
                    self._compose(_export_endpoint(info["base"]), info["keep"], params)
                )
            except RuntimeError as exc:
                last = exc
        raise RuntimeError(f"export 出图失败: {last}") from last

    def _tile_url(self, info: dict[str, Any], level: int, row: int, col: int) -> str:
        """瓦片地址(tile 方式用,export 方式不发 URL 走 _export_tile_bytes)。"""
        return self._compose(f"{info['base']}/tile/{level}/{row}/{col}", info["keep"], {})

    # ------------------------------------------------------------------
    # 下载并保存到 db(落库逻辑与 local_db_util 一致)
    # ------------------------------------------------------------------
    def save_tiles_to_db(
        self,
        url: str,
        out_db: str | None = None,
        *,
        db_util: Any = None,
        extent: Any = None,
        extent_sr: int | None = None,
        min_zoom: int | None = None,
        max_zoom: int | None = None,
        workers: int = 4,
        overwrite: bool = False,
        clip: bool = True,
        table_name: str | None = None,
        bounds: tuple[float, float, float, float] | None = None,
        register: bool = True,
        progress: Callable[[int, int], Any] | None = None,
        cancel: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        """按范围下载影像瓦片金字塔,并按 local_db_util 约定写入本地库。

        DB 侧完全复用 :class:`local_db_util` 的影像存储逻辑:先用
        :meth:`create_arcgis_image_store` 按图层元信息建瓦片表 + 写全局
        ``metadata`` + 注册 ``user_relation``(type=image),再逐批
        :meth:`batch_add_image_tile_data` 落瓦片:

        - **表名与图层名一致**: 数据表名由图层名按 local_db_util 规则清洗
          (图层端点 ``layer_meta["name"]`` 即图层名;服务根端点缺 name 时
          退用 ``mapName``/``serviceDescription``/服务地址末段解码)。
          ``table_name`` 可显式覆盖;
        - **注册**: 新库默认 GeoPackage(kind 按内容探测),在
          ``user_relation`` 登记一行(type=image,表名与数据表同名),可被
          ``query_all_table_name()`` 列出;``.mbtiles`` 纯瓦片库没有
          user_relation,``register=True`` 直接报错;注册行的 ``attr``
          字段以 JSON 存服务空间等属性(wkid/crs/extent/bounds/级别/URL/
          total 及 serviceDescription 等标量属性);
        - **行号**: ArcGIS 瓦片行号即 XYZ(行号自顶部向下);本方法统一按 TMS
          语义交给 local_db_util(``tile_row = 2**level - 1 - row``),
          落库端再按目标格式定最终行号 —— GeoPackage 新库存 XYZ
          (top-origin,读回由 server/别名等按内容探测直读),spatialite
          老格式库存 TMS;
        - **metadata**: 影像名/format/bounds/minzoom/maxzoom 及源地址
          (``arcgis_url`` 等)写入库内全局 ``metadata`` 表(每库一份,
          同 local_db_util 约定:一个库适合放一个影像图层)。

        给了已打开的 ``db_util`` 实例即用之;只给 ``out_db`` 时内部惰性
        import 并按需新建(新文件默认 GeoPackage,纯 sqlite3 即可写,无需
        mod_spatialite;若需要老 spatialite 行为,请自行以
        ``local_db_util(path, load_spatialite=True)`` 打开后传 ``db_util``)。
        规划/取片(metadata / tile_ranges / download_tile)只依赖标准库。

        Args:
            out_db: 落库路径,仅当 ``db_util`` 未给时使用:文件不存在则
                新建(spatialite 库自动 init,含 user_relation),已存在则沿用。
            db_util: 已打开的 local_db_util 实例;给了就忽略 out_db
                (可把多个影像依次存进同一个库,各自成表并登记)。
            extent: 下载范围,默认服务 fullExtent(服务坐标系单位)。
            extent_sr: extent 的 wkid(缺省按服务坐标系理解)。
            min_zoom / max_zoom: 下载层级范围(缺省用服务/合成的全部可用级)。
            workers: HTTP 下载并发数(落库仍在主线程串行,每批 200 块提交)。
            overwrite: 是否重建。True=同名瓦片表先删后建、瓦片 INSERT OR
                REPLACE 覆盖;False=同名表已存在直接抛错(local_db_util
                语义,不做断点续传跳过)。
            clip: extent 与服务 fullExtent 求交,避免请求服务范围外瓦片。
            table_name: 数据表名,缺省由图层名清洗生成(与 user_relation
                注册名一致)。
            bounds: WGS84 经纬度 ``(min_lon, min_lat, max_lon, max_lat)``,
                写进 metadata。缺省由服务 fullExtent / extent 换算;服务
                坐标系不在纯数学支持集合内时必须显式传(否则 local_db_util
                建表会报错提示)。
            register: 是否在 user_relation 登记影像(type=image)。默认 True。
            progress: 每处理完一块瓦片回调 ``progress(done, total)``
                (成功/失败都算)。
            cancel: 每次处理完一块瓦片后调用;返回真则尽早停止提交新任务
                (进行中的请求等其自然结束)。

        Returns:
            ``{"mode", "total", "downloaded", "failed", "existing"(恒 0),
            "errors"(失败明细样本,至多前 3 条), "table", "name",
            "registered", "levels": [{level, ok, failed}]}``。
        """
        info = self.tile_info(url)
        plans, total = self.tile_ranges(
            url, extent=extent, extent_sr=extent_sr,
            min_zoom=min_zoom, max_zoom=max_zoom, clip=clip,
        )
        zero = {
            "mode": info["mode"], "total": 0,
            "downloaded": 0, "existing": 0, "failed": 0,
            "table": None, "name": None, "registered": False, "levels": [],
            "errors": [],
        }
        if total <= 0:
            return zero

        if db_util is None:
            if not out_db:
                raise ValueError("save_tiles_to_db 需传 out_db 或已打开的 db_util")
            db_util = _local_db_util(out_db)
        kind = getattr(db_util, "kind", None)
        if register and kind == "mbtiles":
            raise RuntimeError(
                "目标库是 .mbtiles 纯瓦片库,没有 user_relation 表,无法注册"
                "影像;请改用 .sqlite/.db 等 spatialite 库路径,或 register=False"
                "(影像落库新规建议直接用 GeoPackage 格式 .gpkg)"
            )

        # 图层名 → 建表/注册(local_db_util 内部按图层名清洗生成表名)
        meta = dict(self.metadata(url))
        name = str(meta.get("name") or "").strip()
        if not name:
            # 子图层端点(…/MapServer/0)与根端点都退到服务名那一段
            tail = url.rstrip("/")
            parts = [unquote(p) for p in tail.split("/") if p]
            while parts and (parts[-1].isdigit() or parts[-1] in ("MapServer", "ImageServer")):
                parts.pop()
            name = (
                str(meta.get("mapName") or meta.get("serviceDescription") or "").strip()
                or (parts[-1] if parts else tail)
            )
        meta["name"] = name or (table_name or "image_layer")
        meta.setdefault("url", url)
        levels = sorted({p["level"] for p in plans})
        wgs_bounds = _wgs84_bounds_of(info, extent, extent_sr) if bounds is None else bounds
        # 注册行 user_relation.attr:源服务的空间等属性(JSON 序列化友好)
        user_attr: dict[str, Any] = {
            "name": meta.get("name"),
            "url": url,
            "mode": info["mode"],
            "format": info["format"],
            "wkid": info["wkid"],
            "crs": f"EPSG:{info['wkid']}",
            "extent": info["full_extent"],  # 服务坐标系 [minx, miny, maxx, maxy]
            "bounds": list(wgs_bounds) if wgs_bounds else None,  # WGS84 经纬度
            "min_zoom": levels[0],
            "max_zoom": levels[-1],
            "total": total,
        }
        for key in ("type", "serviceDescription", "description",
                    "copyrightText", "units", "mapName"):
            val = meta.get(key)
            if isinstance(val, (str, int, float, bool)):
                user_attr[key] = val
        created = db_util.create_arcgis_image_store(
            meta,
            table_name=table_name,
            min_zoom=levels[0],
            max_zoom=levels[-1],
            tile_format=info["format"],
            bounds=wgs_bounds,
            register=register,
            overwrite=overwrite,
            attr=user_attr,
        )
        tn = created["table"]

        stats = {"downloaded": 0, "failed": 0}
        level_stats: dict[int, dict[str, int]] = {}
        sample_errors: list[str] = []
        done = 0
        stopped = False
        chunk: list[dict[str, Any]] = []
        batch_size = 200

        def flush() -> None:
            """把已取到的一块瓦片批落库(local_db_util 单事务)。"""
            if chunk:
                db_util.batch_add_image_tile_data(tn, chunk, overwrite=overwrite)
                chunk.clear()

        try:
            with ThreadPoolExecutor(max_workers=max(1, int(workers))) as pool:
                for plan in plans:
                    if stopped:
                        break
                    level = plan["level"]
                    n = 1 << level
                    pending: dict[Any, tuple[int, int, int]] = {}
                    it = iter(
                        (level, row, col)
                        for row in range(plan["row0"], plan["row1"] + 1)
                        for col in range(plan["col0"], plan["col1"] + 1)
                    )
                    while True:
                        if stopped:
                            break
                        while len(pending) < max(1, int(workers)):
                            try:
                                tile = next(it)
                            except StopIteration:
                                break
                            future = pool.submit(self._fetch_one_tile, info, tile)
                            pending[future] = tile
                        if not pending:
                            break
                        done_set, _ = wait(list(pending), return_when=FIRST_COMPLETED)
                        for future in done_set:
                            tile = pending.pop(future)
                            data, error = future.result()
                            done += 1
                            ls = level_stats.setdefault(level, {})
                            if error is not None:
                                stats["failed"] += 1
                                ls["failed"] = ls.get("failed", 0) + 1
                                if len(sample_errors) < 3:
                                    sample_errors.append(str(error))
                            else:
                                # ArcGIS 行号=XYZ(顶→下);瓦片表按 TMS 存(南→北)
                                chunk.append({
                                    "zoom_level": level,
                                    "tile_column": tile[2],
                                    "tile_row": n - 1 - tile[1],
                                    "tile_data": data,
                                })
                                stats["downloaded"] += 1
                                ls["ok"] = ls.get("ok", 0) + 1
                                if len(chunk) >= batch_size:
                                    flush()
                            if progress is not None:
                                progress(done, total)
                            if cancel is not None and cancel():
                                stopped = True
                                break
                flush()
        except Exception as exc:
            raise RuntimeError(
                f"落库失败(已下载 {done}/{total},本次待写 {len(chunk)} 块): {exc}"
            ) from exc
        # 落库对账: 表内行数 != 下载成功数 → 说明有静默丢失(锁/只读/未提交)
        if stats["downloaded"] and getattr(db_util, "cursor", None) is not None:
            try:
                cnt = db_util.cursor.execute(
                    f'SELECT COUNT(*) FROM "{tn}"'
                ).fetchone()[0]
            except Exception:
                cnt = None
            if cnt is not None and int(cnt) != stats["downloaded"]:
                raise RuntimeError(
                    f"落库对账失败: 表 {tn!r} 实际 {cnt} 行,应写入 "
                    f"{stats['downloaded']} 行。库文件可能被其他进程占用/只读,或写入未提交"
                )
        return {
            "mode": info["mode"],
            "total": total,
            "downloaded": stats["downloaded"],
            "existing": 0,  # batch_add 语义同 local_db_util:overwrite 覆盖或抛错,无跳过
            "failed": stats["failed"],
            "errors": sample_errors,  # 失败明细样本(至多前 3 条,便于排查)
            "table": tn,
            "name": created["name"],
            "registered": created["registered"],
            "levels": [
                {"level": p["level"], **level_stats.get(p["level"], {})}
                for p in plans
            ],
        }

    def _fetch_one_tile(
        self, info: dict[str, Any], tile: tuple[int, int, int]
    ) -> tuple[bytes | None, Exception | None]:
        """单个瓦片的取片动作(下载线程执行;返回 (数据, 错误) 不抛)。"""
        level, row, col = tile
        try:
            if info["mode"] == "tile":
                data = self._fetch_bytes(self._tile_url(info, level, row, col))
            else:
                data = self._export_tile_bytes(info, level, row, col)
            return data, None
        except Exception as exc:
            return None, exc


if __name__ == '__main__':
    url = 'http://127.0.0.1:6080/arcgis/rest/services/test_image/MapServer/0'
    server = ArcGISImageServer()
    info = server.tile_info(url)
    print('取片方式:', info['mode'], '格式:', info['format'], 'wkid:', info['wkid'])
    n = server.tile_count(url, min_zoom=0, max_zoom=18)
    print('0-18 级瓦片数估算:', n)

    # 落库:新库默认 GeoPackage(纯 sqlite3 即可写,无需 mod_spatialite);
    # QGIS 里跑本文件也行(pysqlite3)。注意:export 方式(未开缓存)会合成
    # 0~18 级,不传 min/max_zoom 且范围大时瓦片量可达数十万~百万级;
    # 先小级别验证通了再放大。
    from app.core.util.local_db_util import local_db_util
    db = local_db_util('test01.gpkg')  # 已存在则沿用,按内容探测格式;新规本体 .gpkg
    summary = server.save_tiles_to_db(
        url, db_util=db, min_zoom=0, max_zoom=15, overwrite=True,
        progress=lambda done, total: print(f'{done}/{total}'),
    )
    print(summary)
    if summary.get('errors'):
        print('失败原因样本:')
        for e in summary['errors']:
            print(' -', e)
