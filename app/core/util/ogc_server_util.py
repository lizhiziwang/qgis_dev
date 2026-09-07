"""
    OGC WFS(Web Feature Service)要素查询工具

    对标准 WFS 服务(GeoServer / ArcGIS Server WFS / MapServer 等)提供
    GetCapabilities / DescribeFeatureType / 计数 / 分页拉取 / 并发拉取能力,
    与 :mod:`core.util.arcgis_server_util.ArcGISServer` 的用法对齐。

    返回的每一条要素统一为::

        {"id": "typename.1", "attributes": {...}, "geometry": "POLYGON (...)"}

    - ``attributes``: 属性字典(GeoJSON 直接透传,GML 取各属性元素的文本)
    - ``geometry``: 已转成 WKT 文本(GeoJSON 与 GML 都归一到 WKT,
      可直接交给 :meth:`core.util.local_db_util.local_db_util.batch_add_vector_data`);
      无几何时为 None

    能力范围:
    - WFS 1.0.0 / 1.1.0 / 2.0.0 的 GetCapabilities 自动协商版本
    - 分页: WFS 2.0 用 ``count`` + ``startIndex``;1.x 用 ``maxFeatures`` + ``startIndex``
      (GeoServer 等支持 startIndex 的服务均可用)
    - 响应: 优先请求 GeoJSON(``outputFormat=application/json``);服务端忽略并
      返回 GML 时自动按 GML 2.1/3.1/3.2 解析
    - 只用标准库(urllib + xml.etree),不引入第三方依赖
"""

import json
import re
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, quote, urlencode, urlparse, urlunparse
from urllib.request import Request, urlopen

try:
    from core.util.wkt_util import geojson_geometry_to_wkt
except ImportError:  # 以脚本方式直接运行时 app/ 目录不在 sys.path 的退路
    from wkt_util import geojson_geometry_to_wkt

_DEFAULT_TIMEOUT = 60.0
_WFS_VERSIONS = ("2.0.0", "1.1.0", "1.0.0")

#: GML 几何元素本地名(忽略命名空间前缀)
_GML_GEOM_LOCALS = {
    "Point", "LineString", "Polygon",
    "MultiPoint", "MultiCurve", "MultiSurface",
    "MultiLineString", "MultiPolygon", "MultiGeometry",
}
#: 属性读取时跳过的 GML 包装元素
_GML_SKIP_LOCALS = {"boundedBy", "Envelope", "Box"}
#: 空文本视为缺省(不进入 attributes)


def _local(tag: str) -> str:
    """元素 tag 去掉命名空间前缀后的本地名。"""
    return tag.rsplit("}", 1)[-1]


def _decode_http_text(raw: bytes, charset: str | None = None) -> str:
    """把 HTTP 响应字节解成文本(优先 utf-8-sig 去掉 BOM)。"""
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


def _sort_hint(detail: str) -> str:
    """GeoServer 等“无主键数据源需显式排序键”报错 → 中文引导。"""
    low = detail.lower()
    if any(k in low for k in ("natural order", "primary key", "manual sort")):
        return (
            "(提示: 该服务端做偏移分页时要求显式排序键;请在查询方法传 "
            "sort_by=某属性名,属性名可用 describe_feature_type(type_name) 查看)"
        )
    return ""


def _http_error_hint(text: str) -> str:
    """从错误响应里挖可读信息(WFS 常见 XML 异常 / GeoServer JSON 异常)。"""
    stripped = text.lstrip()
    if stripped.startswith("<"):
        try:
            root = ET.fromstring(text)
        except ET.ParseError:
            return re.sub(r"<[^>]+>", " ", text)[:200]
        parts = [
            (node.text or "").strip()
            for node in root.iter()
            if _local(node.tag) in ("ExceptionText", "ServiceException", "text")
            and (node.text or "").strip()
        ]
        if parts:
            return "; ".join(parts)
        return (root.attrib.get("exceptionCode") or _local(root.tag))[:200]
    if stripped.startswith("{"):
        try:
            obj = json.loads(text)
        except ValueError:
            return text[:200]
        if isinstance(obj, dict):
            excs = obj.get("exceptions") or []
            if isinstance(excs, list) and excs:
                return "; ".join(
                    str(e.get("text") or e.get("code") or "") for e in excs if e
                )
            if "error" in obj:
                err = obj["error"]
                if isinstance(err, dict):
                    return str(err.get("message") or err)[:200]
                return str(err)[:200]
    return text[:200]


def _split_service_url(url: str) -> tuple[str, dict[str, str]]:
    """清洗 WFS 服务地址 → (基址, 保留的查询参数)。

    - 支持直接给 ``/wfs``、``/ows`` 根地址,也支持 GetCapabilities 地址
    - 剥掉查询串里的 service/request/version(每次请求按需重发),其余保留
      (如 token / map 等参数)
    - 非 ASCII 路径(中文)百分号编码
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
        if key.lower() not in ("service", "request", "version", "f")
    }
    return urlunparse((parts.scheme, parts.netloc, path, "", "", "")), keep


def _bbox_text(bbox: Any, bbox_srs: Any = None) -> str:
    """bbox 形参 → KVP 文本 ``minx,miny,maxx,maxy[,srs]``。"""
    nums = (
        [float(x) for x in bbox.split(",") if x.strip()]
        if isinstance(bbox, str)
        else [float(x) for x in bbox]
    )
    if len(nums) != 4:
        raise ValueError(f"bbox 需要 4 个数值(minx,miny,maxx,maxy),收到: {bbox!r}")
    text = ",".join(repr(x) if isinstance(x, float) else str(x) for x in nums)
    if bbox_srs is not None:
        text += "," + str(bbox_srs)
    return text


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _normalize_response_format(value: Any) -> str | None:
    """response_format 形参归一: None/auto→None,json/gml 家族→小写枚举。"""
    if value is None:
        return None
    v = str(value).strip().lower()
    if v in ("", "auto"):
        return None
    if v in ("json", "geojson", "application/json"):
        return "json"
    if v in ("gml", "xml", "gml2", "gml3", "gml2.1", "gml3.1", "gml3.2",
             "application/gml+xml", "text/xml"):
        return "gml"
    raise ValueError(
        f"不支持的 response_format: {value!r};可选 json / geojson / gml / xml,"
        "None/auto 表示按响应内容自动识别"
    )


def _output_format_kvp(version: str, mode: str | None) -> str:
    """按版本与模式选择 outputFormat KVP(默认优先请求 GeoJSON)。"""
    if mode == "json":
        return "application/json"
    if mode == "gml":
        if version.startswith("2"):
            return "application/gml+xml; version=3.2"
        if version.startswith("1.1"):
            return "text/xml; subtype=gml/3.1.1"
        return "text/xml; subtype=gml/2.1.2"
    return "application/json"


class WfsServer:
    """OGC WFS 要素服务查询工具。

    Args:
        timeout: 单次请求超时秒数。
        retries: 网络层失败(连接/超时)的额外重试次数。
        extra_headers: 追加到每次请求的 HTTP 头。
        version: 锁定的 WFS 版本(如 ``"2.0.0"``);None 时首次
            GetCapabilities 自动在 2.0.0 → 1.1.0 → 1.0.0 间协商。
    """

    def __init__(
        self,
        *,
        timeout: float = _DEFAULT_TIMEOUT,
        retries: int = 1,
        extra_headers: dict[str, str] | None = None,
        version: str | None = None,
    ) -> None:
        self._timeout = timeout
        self._retries = max(0, int(retries))
        self._extra_headers = dict(extra_headers or {})
        self._version = version
        self._caps_cache: dict[str, dict[str, Any]] = {}
        self._service_url: str | None = None
        self._query_keep: dict[str, str] = {}

    # ------------------------------------------------------------------
    # HTTP 基础
    # ------------------------------------------------------------------
    def clear_cache(self) -> None:
        """清空 GetCapabilities 缓存。"""
        self._caps_cache.clear()

    def _fetch(self, url: str) -> str:
        """GET 并返回文本;非 200/网络错抛中文 RuntimeError。"""
        headers = {
            "User-Agent": "qgis-dev/ogc_server_util",
            "Accept": "application/json, application/gml+xml, text/xml, */*",
        }
        headers.update(self._extra_headers)
        req = Request(url, headers=headers)
        raw: bytes | None = None
        charset: str | None = None
        error: Exception | None = None
        for attempt in range(self._retries + 1):
            try:
                with urlopen(req, timeout=self._timeout) as resp:
                    raw = resp.read()
                    charset = resp.headers.get_content_charset()
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
                sort_tip = _sort_hint(detail)
                raise RuntimeError(
                    f"OGC 请求失败 HTTP {exc.code}: {detail or exc.reason}{sort_tip}"
                ) from exc
            except (URLError, TimeoutError, OSError) as exc:
                error = exc
                if attempt < self._retries:
                    time.sleep(0.3 * (attempt + 1))
        if raw is None:
            raise RuntimeError(f"请求 OGC 服务失败: {error}") from error
        return _decode_http_text(raw, charset)

    def _compose(
        self,
        base: str,
        base_query: dict[str, str],
        params: dict[str, Any],
    ) -> str:
        merged = dict(base_query)
        merged.update({key: _encode_param(value) for key, value in params.items()})
        return base + "?" + urlencode(merged)

    def _request(
        self,
        url: str,
        params: dict[str, Any],
    ) -> str:
        """发 OGC 请求;若返回 WFS/OWS 异常信封则抛中文 RuntimeError。"""
        text = self._fetch(self._compose(url, self._query_keep, params))
        stripped = text.lstrip()
        if stripped.startswith("<"):
            root = ET.fromstring(text)
            if _local(root.tag) in ("ExceptionReport", "ServiceExceptionReport"):
                raise RuntimeError(f"WFS 服务返回异常: {_http_error_hint(text)}")
        if stripped.startswith("{"):
            obj = json.loads(text)
            if isinstance(obj, dict) and obj.get("exceptions"):
                raise RuntimeError(f"WFS 服务返回异常: {_http_error_hint(text)}")
        return text

    def _request_caps_text(self, version: str) -> str:
        """GetCapabilities 请求(不查异常,由调用方按版本协商处理)。"""
        params = {"service": "WFS", "request": "GetCapabilities", "version": version}
        return self._fetch(self._compose(self._service_url, self._query_keep, params))

    # ------------------------------------------------------------------
    # 版本协商 & GetCapabilities
    # ------------------------------------------------------------------
    def _ensure_service(self, url: str | None) -> None:
        """(重)设服务地址;url 为空则沿用已存的地址。"""
        if url:
            self._service_url, self._query_keep = _split_service_url(url)
        elif self._service_url is None:
            raise ValueError("请先传入 WFS 服务地址(url 参数)")

    def capabilities(
        self,
        url: str | None = None,
        *,
        refresh: bool = False,
        version: str | None = None,
    ) -> dict[str, Any]:
        """取(并缓存)GetCapabilities 解析结果。

        Args:
            url: WFS 服务地址(首次必须给,之后可省)。
            refresh: 绕过缓存重抓。
            version: 指定版本;None 时按 2.0.0 → 1.1.0 → 1.0.0 尝试,以第一个
                成功响应的版本为准,并写入实例供后续查询默认使用。

        Returns:
            ``{"version", "title", "feature_types": [{name, title, abstract,
            srs, bbox4}]}``;bbox4 为 WGS84 ``[minx, miny, maxx, maxy]`` 或 None。
        """
        if url:
            self._ensure_service(url)
        if self._service_url is None:
            raise ValueError("请先传入 WFS 服务地址(url 参数)")
        want = [version] if version else ([self._version] if self._version else list(_WFS_VERSIONS))
        last_err: Exception | None = None
        for ver in want:
            if ver is None:
                continue
            if not refresh and ver in self._caps_cache:
                return self._caps_cache[ver]
            try:
                text = self._request_caps_text(ver)
            except RuntimeError as exc:
                last_err = exc
                continue
            parsed = self._parse_capabilities(text)
            if not parsed:
                continue
            self._caps_cache[parsed["version"]] = parsed
            if self._version is None:
                self._version = parsed["version"]
            return parsed
        if last_err is not None and not version and not self._version:
            raise RuntimeError(
                f"GetCapabilities 请求失败(尝试了 {_WFS_VERSIONS}): {last_err}"
            ) from last_err
        if version:
            raise RuntimeError(f"GetCapabilities(version={version}) 请求失败: {last_err}") from last_err
        raise RuntimeError(f"GetCapabilities 请求失败: {last_err}") from last_err

    @staticmethod
    def _parse_capabilities(text: str) -> dict[str, Any]:
        stripped = text.lstrip()
        if not stripped.startswith("<"):
            return {}
        try:
            root = ET.fromstring(text)
        except ET.ParseError:
            return {}
        if _local(root.tag) not in ("WFS_Capabilities", "Capabilities"):
            return {}
        version = root.attrib.get("version") or ""
        title = ""
        for node in root.iter():
            if _local(node.tag) == "Title" and (node.text or "").strip():
                title = node.text.strip()
                break
        ftypes: list[dict[str, Any]] = []
        for el in root.iter():
            if _local(el.tag) != "FeatureType":
                continue
            children = {
                _local(c.tag): (c.text or "").strip()
                for c in el if _local(c.tag) in (
                    "Name", "Title", "Abstract", "DefaultSRS", "DefaultCRS",
                )
            }
            if not children.get("Name"):
                continue
            srs = children.get("DefaultCRS") or children.get("DefaultSRS") or None
            bbox4 = None
            for bbox_el in [c for c in el.iter() if _local(c.tag) in
                            ("WGS84BoundingBox", "BoundingBox")]:
                nums: list[float] = []
                for corner in ("LowerCorner", "UpperCorner"):
                    for node in bbox_el.iter():
                        if _local(node.tag) == corner and (node.text or "").strip():
                            nums.extend(float(x) for x in node.text.split())
                if len(nums) == 4:
                    bbox4 = [nums[0], nums[1], nums[2], nums[3]]
                    break
            ftypes.append({
                "name": children["Name"],
                "title": children.get("Title") or children["Name"],
                "abstract": children.get("Abstract") or "",
                "srs": srs,
                "bbox4": bbox4,
            })
        return {"version": version, "title": title, "feature_types": ftypes}

    def feature_types(self, url: str | None = None, *, refresh: bool = False) -> list[dict[str, Any]]:
        """GetCapabilities 里声明的要素类型列表(便捷入口)。"""
        return self.capabilities(url, refresh=refresh)["feature_types"]

    # ------------------------------------------------------------------
    # 描述 / 计数 / 查询
    # ------------------------------------------------------------------
    def describe_feature_type(
        self,
        type_name: str,
        *,
        url: str | None = None,
        version: str | None = None,
    ) -> list[dict[str, Any]]:
        """DescribeFeatureType → 属性字段定义 ``[{name, type, min_occurs, max_occurs, nillable}]``。"""
        self._ensure_service(url) if url else None
        if self._service_url is None:
            raise ValueError("请先传入 WFS 服务地址(url 参数)")
        ver = version or self._version or "2.0.0"
        params = {
            "service": "WFS",
            "request": "DescribeFeatureType",
            "version": ver,
            "typeName": type_name,
        }
        text = self._request(self._service_url, params)
        if not text.lstrip().startswith("<"):
            return []
        root = ET.fromstring(text)
        out: list[dict[str, Any]] = []
        for el in root.iter():
            if _local(el.tag) != "element" or not el.attrib.get("name"):
                continue
            out.append({
                "name": el.attrib["name"],
                "type": el.attrib.get("type") or "",
                "min_occurs": el.attrib.get("minOccurs", "1"),
                "max_occurs": el.attrib.get("maxOccurs", "1"),
                "nillable": el.attrib.get("nillable", "false") == "true",
            })
        return out

    def count(
        self,
        type_name: str,
        *,
        url: str | None = None,
        version: str | None = None,
        bbox: Any = None,
        bbox_srs: Any = None,
        extra: dict[str, Any] | None = None,
    ) -> int | None:
        """要素总数(resultType=hits)。

        服务端可能不返回匹配总数(部分实现不支持 hits),此时返回 None,
        由调用方决定退路(顺序分页不依赖总数)。
        """
        if url:
            self._ensure_service(url)
        if self._service_url is None:
            raise ValueError("请先传入 WFS 服务地址(url 参数)")
        ver = version or self._version or "2.0.0"
        params = {
            "service": "WFS",
            "request": "GetFeature",
            "version": ver,
            self._type_key(ver): type_name,
            "resultType": "hits",
        }
        if bbox is not None:
            params["bbox"] = _bbox_text(bbox, bbox_srs)
        params.update(extra or {})
        text = self._request(self._service_url, params)
        stripped = text.lstrip()
        if stripped.startswith("{"):
            obj = json.loads(text)
            return _int_or_none(obj.get("numberMatched") or obj.get("totalFeatures"))
        root = ET.fromstring(text)
        for node in root.iter():
            for key, value in node.attrib.items():
                low = _local(key).lower()
                if "numbermatched" in low or "numberoffeatures" in low or low == "featurecount":
                    n = _int_or_none(value)
                    if n is not None:
                        return n
        return None

    @staticmethod
    def _type_key(version: str) -> str:
        """WFS 2.0 用复数 typeNames,1.x 用 typeName。"""
        return "typeNames" if version.startswith("2") else "typeName"

    def query_page(
        self,
        type_name: str,
        *,
        url: str | None = None,
        version: str | None = None,
        start_index: int = 0,
        count: int | None = None,
        bbox: Any = None,
        bbox_srs: Any = None,
        sort_by: str | None = None,
        include_z: bool = False,
        response_format: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> tuple[list[dict[str, Any]], dict[str, int | None]]:
        """单次 GetFeature 拉一页要素。

        Args:
            start_index: WFS 2.0 ``startIndex``(1.x 也按同语义透传)。
            count: 页大小(2.0 ``count``,1.x ``maxFeatures``);None 不传、
                用服务端默认。
            bbox: ``[minx, miny, maxx, maxy]`` 空间过滤(WFS ``bbox`` KVP)。
            sort_by: ``sortBy`` 参数(如 ``"area D"``),分页/并发建议配合稳定排序。
            include_z: GML/GeoJSON 几何带 Z 时是否输出 Z WKT。
            response_format: 响应格式控制 —— 决定请求的 ``outputFormat``
                与解析器选择:

                - ``None``/``"auto"``(默认): 优先请求 GeoJSON,按响应内容
                  自动识别(JSON 或 GML/XML 都能解析)
                - ``"json"``/``"geojson"``: 强制走 GeoJSON 解析器;响应不是
                  JSON 时报错(可提示服务端忽略输出格式)
                - ``"gml"``/``"xml"``(可带 2/3 版本后缀): 请求对应版本的
                  GML,并强制走 GML/XML 解析器

        Returns:
            (features, meta);meta 为 ``{"numberMatched", "numberReturned"}``,
            服务端未返回时为 None。
        """
        if url:
            self._ensure_service(url)
        if self._service_url is None:
            raise ValueError("请先传入 WFS 服务地址(url 参数)")
        ver = version or self._version or "2.0.0"
        fmt_mode = _normalize_response_format(response_format)
        params: dict[str, Any] = {
            "service": "WFS",
            "request": "GetFeature",
            "version": ver,
            self._type_key(ver): type_name,
            "outputFormat": _output_format_kvp(ver, fmt_mode),
        }
        if ver.startswith("2"):
            if count is not None:
                params["count"] = count
            if start_index:
                params["startIndex"] = start_index
        else:
            if count is not None:
                params["maxFeatures"] = count
            if start_index:
                params["startIndex"] = start_index
        if bbox is not None:
            params["bbox"] = _bbox_text(bbox, bbox_srs)
        if sort_by:
            params["sortBy"] = sort_by
        params.update(extra or {})
        text = self._request(self._service_url, params)
        return self._parse_getfeature(
            text, include_z=include_z, response_format=fmt_mode
        )

    # ------------------------------------------------------------------
    # 响应解析: GeoJSON / GML → 统一要素
    # ------------------------------------------------------------------
    def _parse_getfeature(
        self,
        text: str,
        *,
        include_z: bool,
        response_format: str | None = None,
    ) -> tuple[list[dict[str, Any]], dict[str, int | None]]:
        """按 response_format(已有归一: None/"json"/"gml")解析 GetFeature 响应。

        - None: 按响应内容自动识别(GeoJSON 或 GML/XML)
        - "json": 强制 GeoJSON 解析,响应不是 JSON 时报错
        - "gml": 强制 XML/GML 解析,响应不是 XML 时报错
        """
        stripped = text.lstrip()
        is_json = stripped.startswith("{")
        is_xml = stripped.startswith("<")
        if response_format == "json":
            if not is_json:
                raise RuntimeError(
                    "已指定 response_format='json',但服务端响应不是 JSON"
                    "(可能忽略了 outputFormat);响应开头: "
                    f"{stripped[:100]!r}。可改传 response_format=None 自动识别"
                )
            return self._parse_geojson(text, include_z=include_z)
        if response_format == "gml":
            if not is_xml:
                raise RuntimeError(
                    "已指定 response_format='gml',但服务端响应不是 XML"
                    "(可能忽略了 outputFormat);响应开头: "
                    f"{stripped[:100]!r}。可改传 response_format=None 自动识别"
                )
            return self._parse_gml_xml(text, include_z=include_z)
        if is_json:
            return self._parse_geojson(text, include_z=include_z)
        if not is_xml:
            raise RuntimeError(
                f"WFS GetFeature 响应既不是 GeoJSON 也不是 GML/XML,"
                f"开头: {stripped[:100]!r}"
            )
        return self._parse_gml_xml(text, include_z=include_z)

    def _parse_gml_xml(
        self, text: str, *, include_z: bool
    ) -> tuple[list[dict[str, Any]], dict[str, int | None]]:
        try:
            root = ET.fromstring(text)
        except ET.ParseError as exc:
            raise RuntimeError(
                f"WFS GetFeature 返回的 XML 无法解析: {exc};前 200 字符: {text[:200]!r}"
            ) from exc
        if _local(root.tag) != "FeatureCollection":
            raise RuntimeError(
                f"WFS GetFeature 返回的不是要素集合,根元素: {_local(root.tag)!r}"
            )
        return self._parse_gml(root, include_z=include_z)

    @staticmethod
    def _parse_geojson(
        text: str, *, include_z: bool
    ) -> tuple[list[dict[str, Any]], dict[str, int | None]]:
        obj = json.loads(text)
        if not isinstance(obj, dict):
            raise RuntimeError("WFS GeoJSON 响应不是 JSON 对象")
        matched = _int_or_none(obj.get("numberMatched") or obj.get("totalFeatures"))
        gtype = obj.get("type")
        if gtype == "Feature":
            raw_features = [obj]
        elif gtype == "FeatureCollection":
            raw_features = obj.get("features")
        else:
            raise RuntimeError(
                f"WFS GeoJSON 响应既不是 FeatureCollection 也不是 Feature,type: {gtype!r}"
            )
        features: list[dict[str, Any]] = []
        for f in raw_features or []:
            if not isinstance(f, dict):
                continue
            geometry = f.get("geometry")
            wkt = geojson_geometry_to_wkt(geometry, include_z=include_z) if geometry else None
            features.append({
                "id": f.get("id"),
                "attributes": dict(f.get("properties") or {}),
                "geometry": wkt,
            })
        returned = _int_or_none(obj.get("numberReturned"))
        return features, {"numberMatched": matched, "numberReturned": returned}

    def _parse_gml(
        self, root: ET.Element, *, include_z: bool
    ) -> tuple[list[dict[str, Any]], dict[str, int | None]]:
        features: list[dict[str, Any]] = []
        for feature_el in self._gml_feature_elements(root):
            fid = None
            for key, value in feature_el.attrib.items():
                if _local(key).lower() in ("id", "fid"):
                    fid = value
                    break
            attributes: dict[str, Any] = {}
            geometry_wkt: str | None = None
            for child in feature_el:
                if not isinstance(child.tag, str):
                    continue
                cln = _local(child.tag)
                if cln in _GML_SKIP_LOCALS:
                    continue
                geom_node = self._find_geom(child)
                if geom_node is not None:
                    gj = self._gml_geom_to_geojson(geom_node)
                    if gj is not None:
                        geometry_wkt = geojson_geometry_to_wkt(gj, include_z=include_z)
                    continue
                text_value = "".join(child.itertext()).strip()
                if text_value:
                    attributes[cln] = text_value
            features.append({"id": fid, "attributes": attributes, "geometry": geometry_wkt})
        matched = returned = None
        for node in root.iter():
            for key, value in node.attrib.items():
                low = _local(key).lower()
                if matched is None and ("numbermatched" in low or "numberoffeatures" in low):
                    matched = _int_or_none(value)
                if returned is None and "numberreturned" in low:
                    returned = _int_or_none(value)
        return features, {"numberMatched": matched, "numberReturned": returned}

    @staticmethod
    def _gml_feature_elements(root: ET.Element) -> list[ET.Element]:
        """从 GML FeatureCollection 里找出要素根元素。"""
        out: list[ET.Element] = []
        for el in root.iter():
            ln = _local(el.tag)
            if ln in ("member", "featureMember", "featureMembers"):
                out.extend(c for c in el if isinstance(c.tag, str))
        return out

    def _find_geom(self, el: ET.Element) -> ET.Element | None:
        for node in el.iter():
            if _local(node.tag) in _GML_GEOM_LOCALS:
                return node
        return None

    # -- GML 几何 → GeoJSON 几何(再交给 wkt_util 统一转 WKT) -----------
    def _gml_coords_pts(self, el: ET.Element) -> list[tuple[float, ...]] | None:
        """在 el 内找 posList / pos / coordinates,解析成坐标点列表。"""
        dim: int | None = None
        for node in el.iter():
            ln = _local(node.tag)
            if ln == "srsDimension":
                dim = _int_or_none((node.text or "").strip()) or None
            if ln == "posList" and (node.text or "").strip():
                text = node.text.strip()
                nums = [float(t) for t in text.split() if t.strip()]
                if dim is None:
                    dim = 2 if len(nums) % 2 == 0 else 3
                    if len(nums) % 3 == 0 and len(nums) % 2 != 0:
                        dim = 3
                step = dim or 2
                pts = [tuple(nums[i:i + step]) for i in range(0, len(nums), step)]
                if pts and len(pts[-1]) == step:
                    return pts
            if ln == "pos" and (node.text or "").strip():
                nums = [float(t) for t in node.text.split() if t.strip()]
                return [tuple(nums)]
            if ln == "coordinates" and (node.text or "").strip():
                pts = []
                for token in node.text.split():
                    raw = [float(v) for v in token.split(",") if v.strip()]
                    if raw:
                        pts.append(tuple(raw))
                if pts:
                    return pts
        return None

    def _gml_ring_points(self, ring_el: ET.Element) -> list[tuple[float, ...]] | None:
        """LinearRing/Ring 或直接含 posList/coordinates 的容器 → 环坐标点。"""
        return self._gml_coords_pts(ring_el)

    def _gml_geom_to_geojson(self, el: ET.Element) -> dict[str, Any] | None:
        ln = _local(el.tag)
        if ln == "Point":
            pts = self._gml_coords_pts(el)
            return {"type": "Point", "coordinates": list(pts[0])} if pts else None
        if ln == "LineString":
            pts = self._gml_coords_pts(el)
            return {"type": "LineString", "coordinates": [list(p) for p in pts]} if pts else None
        if ln == "Polygon":
            rings: list[list[tuple[float, ...]]] = []
            for child in el:
                cln = _local(child.tag)
                # GML3: exterior/interior;GML2: outerBoundaryIs/innerBoundaryIs
                if cln not in ("exterior", "interior", "outerBoundaryIs", "innerBoundaryIs"):
                    continue
                pts = self._gml_coords_pts(child)
                if pts:
                    rings.append(pts)
            if not rings:
                return None
            return {
                "type": "Polygon",
                "coordinates": [[list(p) for p in ring] for ring in rings],
            }
        if ln in (
            "MultiPoint", "MultiCurve", "MultiSurface",
            "MultiLineString", "MultiPolygon", "MultiGeometry",
        ):
            parts: list[dict[str, Any]] = []
            for child in el:
                geom_node = self._find_geom(child)
                if geom_node is None:
                    continue
                gj = self._gml_geom_to_geojson(geom_node)
                if gj is not None:
                    parts.append(gj)
            return self._merge_geojson_parts(parts)
        return None

    @staticmethod
    def _merge_geojson_parts(parts: list[dict[str, Any]]) -> dict[str, Any] | None:
        """多部件容器: 按成员类型聚合,混合类型退回 GeometryCollection。"""
        if not parts:
            return None
        kinds = {p.get("type") for p in parts}
        if len(parts) == 1:
            return parts[0]
        if kinds == {"Point"}:
            return {"type": "MultiPoint", "coordinates": [p["coordinates"] for p in parts]}
        if kinds == {"LineString"}:
            return {"type": "MultiLineString", "coordinates": [p["coordinates"] for p in parts]}
        if kinds == {"Polygon"}:
            return {"type": "MultiPolygon", "coordinates": [p["coordinates"] for p in parts]}
        return {"type": "GeometryCollection", "geometries": parts}

    # ------------------------------------------------------------------
    # 顺序 / 并发拉全
    # ------------------------------------------------------------------
    def query_all_features(
        self,
        type_name: str,
        *,
        url: str | None = None,
        version: str | None = None,
        page_size: int | None = None,
        bbox: Any = None,
        bbox_srs: Any = None,
        sort_by: str | None = None,
        include_z: bool = False,
        response_format: str | None = None,
        extra: dict[str, Any] | None = None,
        progress: Callable[[int, int | None], Any] | None = None,
    ) -> list[dict[str, Any]]:
        """顺序分页拉取全部匹配要素。

        用服务端返回的 ``numberMatched`` / ``totalFeatures`` 判断终点;服务端
        不返回时以“本页不足 page_size”为终点。传 ``progress`` 时会先发一次
        resultType=hits 求总数(拿不到则为 None)。

        注意: GeoServer 等在“无主键数据源”(如 SQL 视图)上对第二页起的
        startIndex 分页会报 HTTP 400 "Cannot do natural order without a primary
        key",此时需传 ``sort_by`` 指定稳定排序键。

        ``response_format`` 与 :meth:`query_page` 语义一致(默认自动识别;
        也可强制 json / gml),并原样传给每一页。

        Returns:
            全部要素列表。
        """
        page_size = page_size or 1000
        if url:
            self._ensure_service(url)
        ver = version or self._version or "2.0.0"
        total: int | None = None
        if progress is not None:
            total = self.count(type_name, url=url, version=ver, bbox=bbox,
                               bbox_srs=bbox_srs, extra=extra)
        features: list[dict[str, Any]] = []
        start_index = 0
        while True:
            page, meta = self.query_page(
                type_name, url=url, version=ver, start_index=start_index,
                count=page_size, bbox=bbox, bbox_srs=bbox_srs,
                sort_by=sort_by, include_z=include_z,
                response_format=response_format, extra=extra,
            )
            features.extend(page)
            if progress is not None:
                progress(len(features), total)
            matched = meta.get("numberMatched")
            if matched and len(features) >= matched:
                break
            if not page or len(page) < page_size:
                break
            start_index += len(page)
        return features

    def query_all_features_parallel(
        self,
        type_name: str,
        *,
        url: str | None = None,
        version: str | None = None,
        workers: int | None = None,
        span: int = 500,
        page_size: int | None = None,
        total: int | None = None,
        bbox: Any = None,
        bbox_srs: Any = None,
        sort_by: str | None = None,
        include_z: bool = False,
        response_format: str | None = None,
        extra: dict[str, Any] | None = None,
        progress: Callable[[int, int | None], Any] | None = None,
    ) -> list[dict[str, Any]]:
        """并发拉取全部匹配要素(按 ``startIndex`` 区间切分)。

        WFS 没有 OID 概念,只能按“偏移区间”切并发片,这要求服务端对同一
        查询的排序在请求期间稳定(GeoServer 默认排序稳定;不稳定会重复/漏)。
        建议大数据量时传 ``sort_by`` 固定排序。无主键数据源(如 SQL 视图)
        必须传 ``sort_by``,否则第二页起会报 HTTP 400。

        Args:
            span: 每个线程负责的起止偏移跨度(约等于每线程要拉的条数)。
            total: 匹配总数;None 时先发 resultType=hits 取,取不到则抛错
                提示改用顺序拉取。

        Returns:
            全部要素列表(顺序不定)。
        """
        page_size = page_size or 1000
        if url:
            self._ensure_service(url)
        ver = version or self._version or "2.0.0"
        if total is None:
            total = self.count(type_name, url=url, version=ver, bbox=bbox,
                               bbox_srs=bbox_srs, extra=extra)
        if total is None:
            raise RuntimeError(
                "服务端未返回匹配总数(resultType=hits 无 numberMatched/numberOfFeatures),"
                "无法按偏移区间并发切分;请改用 query_all_features 顺序拉取,"
                "或自行估算后传 total= 参数"
            )
        if total <= 0:
            return []
        spans = list(range(0, total, span))
        n_workers = max(1, min(int(workers or 4), len(spans)))
        features: list[dict[str, Any]] = []
        done = 0

        def fetch_range(begin: int) -> list[dict[str, Any]]:
            end = min(begin + span, total)
            out: list[dict[str, Any]] = []
            idx = begin
            while idx < end:
                page, _ = self.query_page(
                    type_name, url=url, version=ver, start_index=idx,
                    count=min(page_size, end - idx), bbox=bbox, bbox_srs=bbox_srs,
                    sort_by=sort_by, include_z=include_z,
                    response_format=response_format, extra=extra,
                )
                out.extend(page)
                if not page:
                    break
                idx += len(page)
            return out

        with ThreadPoolExecutor(max_workers=n_workers) as pool:
            futures = [pool.submit(fetch_range, begin) for begin in spans]
            for future in as_completed(futures):
                features.extend(future.result())
                done += 1
                if progress is not None:
                    progress(len(features), total)
        return features


def _encode_param(value: Any) -> str:
    """查询参数值 → 文本(bool 转 true/false,dict/list 转紧凑 JSON)。"""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return str(value)

if __name__ == '__main__':
    url = 'http://localhost:8080/geoserver/test/ows?service=WFS&version=1.0.0&request=GetFeature&typeName=test%3Aldsp_ldtb&outputFormat=application%2Fjson&maxFeatures=50'

    server = WfsServer()
    count = server.count(type_name='test:ldsp_ldtb',url=url)
    # server.query_all_features(type_name='test:ldsp_ldtb',url=url)
    print(count)

    data = server.query_page(type_name='test:ldsp_ldtb',url=url,sort_by='objectid',start_index=1000)
    print(data)
