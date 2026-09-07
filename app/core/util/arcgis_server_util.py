'''
    ArcGIS Server REST 要素查询工具

    提供对 ArcGIS Server 要素图层(MapServer/FeatureServer 子图层)的
    查询、计数、取 ID、顺序分页拉取与按 OID 分片并发拉取。

    返回的每一条要素是 ArcGIS 原始结构::

        {"attributes": {...}, "geometry": {"rings": ...} / ...}

    可直接喂给 :mod:`core.util.local_db_util` 的
    ``batch_add_vector_data`` 落库。全程只用标准库 ``urllib``。
'''

import json
import re
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, quote, urlencode, urlparse, urlunparse
from urllib.request import Request, urlopen

_DEFAULT_TIMEOUT = 30.0
#: 取 ID 时单页大小(服务端一般远大于此,很少真正触发分页)
_IDS_PAGE = 1000


def _decode_http_text(raw: bytes, charset: str | None = None) -> str:
    """把 HTTP 响应字节解成文本(ArcGIS JSON 通常是 UTF-8)。

    - 未声明 charset / 声明为 UTF-8 系: 优先 ``utf-8-sig``(顺带去掉 BOM)
    - 声明了其他 charset(如 gbk): 先按声明解,失败再退回 UTF-8
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
    """从错误响应文本里尽量挖一句人话: JSON ``error.message``,否则去 HTML 标签。"""
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


def _split_layer_url(url: str) -> tuple[str, dict[str, str]]:
    """清洗图层地址 → (基址, 自带查询参数)。

    - 去掉 ``?f=json`` 等查询串(除 token 类参数会保留给后续每个请求)
    - 去掉尾部 ``/query``
    - 路径中的非 ASCII(如中文)补百分号编码;已编码的 ``%XX`` 原样保留
    """
    s = (url or "").strip()
    if not s:
        raise ValueError("layer_url 不能为空")
    parts = urlparse(s)
    if parts.scheme not in ("http", "https"):
        raise ValueError(f"仅支持 http/https 地址,收到: {parts.scheme!r}")
    path = quote(parts.path.rstrip("/"), safe="/%:@!$&'()*+,;=~._-")
    if path.endswith("/query"):
        path = path[: -len("/query")]
    base = urlunparse((parts.scheme, parts.netloc, path or "/", "", "", ""))
    query = {
        key: value
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key != "f"
    }
    return base, query


def _encode_param(value: Any) -> str:
    """查询参数值 → 文本(bool 转 true/false,dict/list 转紧凑 JSON)。"""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return str(value)


def _fields_str(out_fields: str | list[str]) -> str:
    """``out_fields`` 形参归一成逗号串: 接受 ``"*"`` / ``"a,b"`` / ``["a","b"]``。"""
    if isinstance(out_fields, (list, tuple)):
        return ",".join(str(f).strip() for f in out_fields if str(f).strip())
    return str(out_fields or "*")


class ArcGISServer:
    """ArcGIS Server REST 要素图层查询工具。

    每个实例可复用于多个图层/服务;图层元信息按地址缓存,可用
    :meth:`clear_cache` 清空。

    Args:
        timeout: 单次请求超时秒数。
        retries: 网络层失败(连接/超时)的额外重试次数;HTTP 4xx/5xx
            与 ArcGIS ``error`` 信封不重试。
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
        """清空图层元信息缓存。"""
        self._meta_cache.clear()

    def _compose(
        self,
        layer_url: str,
        params: dict[str, Any],
        *,
        query: bool,
    ) -> str:
        base, base_query = _split_layer_url(layer_url)
        merged = dict(base_query)
        merged.update({key: _encode_param(value) for key, value in params.items()})
        merged["f"] = "json"
        tail = "/query" if query else ""
        return base + tail + "?" + urlencode(merged)

    def _fetch_json(self, url: str) -> dict[str, Any]:
        headers = {
            "User-Agent": "qgis-dev/arcgis_server_util",
            "Accept": "application/json",
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
                    body_charset = exc.headers.get_content_charset() if exc.headers else None
                    detail = _http_error_hint(_decode_http_text(body, body_charset))
                except Exception:
                    pass
                raise RuntimeError(
                    f"ArcGIS 请求失败 HTTP {exc.code}: {detail or exc.reason}"
                ) from exc
            except (URLError, TimeoutError, OSError) as exc:
                error = exc
                if attempt < self._retries:
                    time.sleep(0.3 * (attempt + 1))
        if raw is None:
            raise RuntimeError(f"请求 ArcGIS 服务失败: {error}") from error

        text = _decode_http_text(raw, charset)
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
    # 元信息 / 计数 / ID
    # ------------------------------------------------------------------
    def layer_metadata(self, layer_url: str, *, refresh: bool = False) -> dict[str, Any]:
        """取图层元信息(即 ``…/layers/0?f=json`` 对象),按地址缓存。

        Args:
            layer_url: 图层 REST 地址(可带 ``?f=json`` / 尾 ``/query``)。
            refresh: 为 True 时绕过缓存重新抓取。
        """
        base, _ = _split_layer_url(layer_url)
        if not refresh and base in self._meta_cache:
            return self._meta_cache[base]
        meta = self._fetch_json(self._compose(layer_url, {}, query=False))
        self._meta_cache[base] = meta
        return meta

    def count(self, layer_url: str, *, where: str = "1=1", extra: dict[str, Any] | None = None) -> int:
        """返回满足 ``where`` 的要素总数(服务端 ``returnCountOnly``)。"""
        params: dict[str, Any] = {"where": where, "returnCountOnly": "true"}
        params.update(extra or {})
        data = self._fetch_json(self._compose(layer_url, params, query=True))
        value = data.get("count")
        if value is None:
            raise RuntimeError("ArcGIS 服务没有返回 count")
        return int(value)

    def query_ids(
        self,
        layer_url: str,
        *,
        where: str = "1=1",
        extra: dict[str, Any] | None = None,
    ) -> list[int]:
        """返回满足 ``where`` 的全部要素 OID(``returnIdsOnly``,自动分页)。"""
        ids: list[int] = []
        first_batch: list[int] = []
        offset = 0
        while True:
            params: dict[str, Any] = {
                "where": where,
                "returnIdsOnly": "true",
                "resultRecordCount": _IDS_PAGE,
            }
            if offset:
                params["resultOffset"] = offset
            params.update(extra or {})
            data = self._fetch_json(self._compose(layer_url, params, query=True))
            batch = data.get("objectIds") or []
            batch = [int(i) for i in batch] if isinstance(batch, list) else []
            if not first_batch:
                first_batch = batch
            ids.extend(batch)
            if not data.get("exceededTransferLimit") or not batch:
                break
            if offset and batch == first_batch:
                raise RuntimeError(
                    "服务分页未生效(重复返回同一批 ID),"
                    "请改用 query_all_features 顺序分页拉取"
                )
            offset += _IDS_PAGE
        return ids

    # ------------------------------------------------------------------
    # 拉取要素
    # ------------------------------------------------------------------
    def query_page(
        self,
        layer_url: str,
        *,
        where: str = "1=1",
        out_fields: str | list[str] = "*",
        out_sr: int | dict[str, Any] | None = None,
        return_geometry: bool = True,
        offset: int = 0,
        limit: int | None = None,
        extra: dict[str, Any] | None = None,
    ) -> tuple[list[dict[str, Any]], bool]:
        """单次 REST ``query`` 取一页要素。

        Args:
            out_sr: 输出坐标系(如 ``4527`` 或 ``{"wkid": 4326}``),None 用服务原生。
            offset / limit: ``resultOffset`` / ``resultRecordCount`` 分页游标。

        Returns:
            (features, exceededTransferLimit);features 是
            ``{"attributes": …, "geometry": …}`` 列表。
        """
        params: dict[str, Any] = {
            "where": where,
            "outFields": _fields_str(out_fields),
            "returnGeometry": return_geometry,
        }
        if out_sr is not None:
            params["outSR"] = out_sr
        if offset:
            params["resultOffset"] = offset
        if limit:
            params["resultRecordCount"] = limit
        params.update(extra or {})
        data = self._fetch_json(self._compose(layer_url, params, query=True))
        features = data.get("features") or []
        if not isinstance(features, list):
            features = []
        return features, bool(data.get("exceededTransferLimit"))

    def _record_cap(self, layer_url: str) -> int:
        """单次 query 的推荐页大小: 图层 maxRecordCount,缺省 1000。"""
        meta = self.layer_metadata(layer_url)
        try:
            cap = int(meta.get("maxRecordCount") or 1000)
        except (TypeError, ValueError):
            cap = 1000
        return max(1, min(cap, 5000))

    def _query_page_adaptive(
        self,
        layer_url: str,
        *,
        cap: int,
        offset: int = 0,
        **common: Any,
    ) -> tuple[list[dict[str, Any]], bool, int]:
        """带页大小自适应的一页拉取。

        个别服务对"单次返回要素数/几何体量"有隐性上限,超过就报 5xx
        (如 code=500 Error performing query operation)。遇到这类错误时把页
        大小折半重试同一游标,直到成功(最小 1),返回 (features, exceeded,
        实际成功的页大小)。真正的参数/权限错误(400/404 等)直接抛。
        """
        cur = max(1, int(cap))
        while True:
            try:
                page, exceeded = self.query_page(
                    layer_url, limit=cur, offset=offset, **common
                )
                return page, exceeded, cur
            except RuntimeError as exc:
                msg = str(exc)
                shrinkable = ("HTTP 50" in msg) or ("code=500" in msg)
                if not shrinkable or cur <= 1:
                    raise
                cur = max(1, cur // 2)

    @staticmethod
    def _page_signature(features: list[dict[str, Any]], oid_field: str) -> str:
        """页面指纹: 判断分页游标是否真的前进了(防服务端忽略 resultOffset)。"""
        if features:
            attrs = features[0].get("attributes") or {}
            value = attrs.get(oid_field)
            if value is not None:
                return f"{len(features)}@{value}"
            return json.dumps(
                features[0], ensure_ascii=False, sort_keys=True, default=str
            )
        return ""

    def query_all_features(
        self,
        layer_url: str,
        *,
        where: str = "1=1",
        out_fields: str | list[str] = "*",
        out_sr: int | dict[str, Any] | None = None,
        return_geometry: bool = True,
        page_size: int | None = None,
        progress: Callable[[int, int | None], Any] | None = None,
        extra: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """顺序分页拉取满足 ``where`` 的全部要素(按 ``resultOffset`` 翻页)。

        分页由服务端 ``exceededTransferLimit`` 驱动,不需要预先 count;
        只有传了 ``progress`` 回调时才额外发一次 ``returnCountOnly`` 求总数。
        ``progress(done, total)``: done 为已拉取条数,total 为总数或 None。

        Returns:
            全部要素列表(顺序稳定,与页序一致)。
        """
        cap = page_size or self._record_cap(layer_url)
        total: int | None = None
        if progress is not None:
            try:
                total = self.count(layer_url, where=where, extra=extra)
            except RuntimeError:
                total = None
        common: dict[str, Any] = {
            "where": where,
            "out_fields": out_fields,
            "out_sr": out_sr,
            "return_geometry": return_geometry,
            "extra": extra,
        }
        features: list[dict[str, Any]] = []
        offset = 0
        guard_sig = ""
        while True:
            page, exceeded, cap = self._query_page_adaptive(
                layer_url, cap=cap, offset=offset, **common
            )
            features.extend(page)
            if progress is not None:
                progress(len(features), total)
            if not exceeded or not page:
                break
            if offset:
                meta = self.layer_metadata(layer_url)
                oidf = str(
                    meta.get("objectIdField")
                    or meta.get("objectIdFieldName")
                    or "objectid"
                )
                sig = self._page_signature(page, oidf)
                if sig and sig == guard_sig:
                    raise RuntimeError(
                        "服务分页未生效(重复返回同一页),"
                        "请改用 query_all_features_parallel(按 OID 分片)"
                    )
                guard_sig = sig
            offset += len(page)
        return features

    def query_all_features_parallel(
        self,
        layer_url: str,
        *,
        where: str = "1=1",
        out_fields: str | list[str] = "*",
        out_sr: int | dict[str, Any] | None = None,
        return_geometry: bool = True,
        workers: int | None = None,
        ids_per_chunk: int = 500,
        progress: Callable[[int, int | None], Any] | None = None,
        extra: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """并发拉取满足 ``where`` 的全部要素(推荐大图层用)。

        先 ``returnIdsOnly`` 取全部 OID,再按 ``ids_per_chunk`` 切成互不相交的
        OID 区间,交给 ``workers`` 个线程各自 ``query``(线程内如超出单次上限
        会继续分页)。相比顺序分页:总请求数更少、天然支持并发;缺点是结果
        顺序不定(按需自行排序)。

        Args:
            workers: 并发线程数,缺省 4(自动钳制到分片数与 [1, 32])。
            ids_per_chunk: 每个线程一次承担的 OID 数,缺省 500。
            progress: 每个分片完成后回调 ``progress(done, total)``,
                done 为已收集条数,total 为 OID 总数。

        Returns:
            全部要素列表(乱序)。若 ``where`` 无匹配,返回空列表。
        """
        ids = self.query_ids(layer_url, where=where, extra=extra)
        if not ids:
            return []
        ordered = sorted(set(ids))
        chunks = [
            ordered[i : i + ids_per_chunk]
            for i in range(0, len(ordered), ids_per_chunk)
        ]
        meta = self.layer_metadata(layer_url)
        oidf = str(meta.get("objectIdField") or meta.get("objectIdFieldName") or "objectid")
        n_workers = max(1, min(int(workers or 4), len(chunks), 32))
        page_cap = self._record_cap(layer_url)
        base_where = str(where or "1=1").strip()

        def fetch_range(chunk: list[int]) -> list[dict[str, Any]]:
            lo, hi = chunk[0], chunk[-1]
            cond = f"{oidf} >= {lo} AND {oidf} <= {hi}"
            cond_where = f"({base_where}) AND ({cond})" if base_where not in ("", "1=1") else cond
            return self.query_all_features(
                layer_url,
                where=cond_where,
                out_fields=out_fields,
                out_sr=out_sr,
                return_geometry=return_geometry,
                page_size=page_cap,
                extra=extra,
            )

        results: list[dict[str, Any]] = []
        done = 0
        with ThreadPoolExecutor(max_workers=n_workers) as pool:
            futures = [pool.submit(fetch_range, chunk) for chunk in chunks]
            for future in as_completed(futures):
                results.extend(future.result())
                done += 1
                if progress is not None:
                    progress(len(results), len(ordered))
        return results

if __name__ == '__main__':
    url = 'http://127.0.0.1:6080/arcgis/rest/services/%E6%B1%9F%E8%A5%BF%E7%9C%81%E5%8E%BF%E7%BA%A7%E8%A1%8C%E6%94%BF%E5%8C%BA%E5%9F%9F/MapServer/0/query'
    server = ArcGISServer()
    count = server.count(layer_url=url)
    print(count)

    data = server.query_all_features(url, where="objectid=1")
    print(1)
