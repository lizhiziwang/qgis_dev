"""任务信息模型：TaskInfo / DataSource / ImageryItem / SpatialExtent。

任务信息来自平台服务端 JSON（仓库暂无真实结构），本模块是「全部下载数据源地址」
的唯一入口（方案 §3）。**纯逻辑**：不依赖 QGIS / Qt；``from_dict`` 对未知键
宽容，平台新增字段不破坏解析。

约定：
- 字段空 = 「该任务没有此数据」（UI 置灰）；「有但不下载」由请求 include 控制；
- ``task_extent`` 是「全部空间范围」的唯一来源，**不从图斑几何推算**；
- ``source_type`` 显式缺失（``auto``）时按 URL 形态推断（见 :func:`infer_source_type`）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

_SOURCE_TYPE_AUTO = "auto"


@dataclass(frozen=True)
class DataSource:
    """单个数据集来源：服务地址或直接文件下载地址二选一或兼有。"""

    name: str                                # 数据集名称（中文，去重后用作落盘/显示名）
    service_url: Optional[str] = None        # 服务地址（ArcGIS REST / WMS / WMTS 模板…）
    file_urls: tuple[str, ...] = ()          # 直接文件下载地址
    layer_id: Optional[str] = None           # FeatureServer 子图层 id（FeatureServer 用）
    source_type: Optional[str] = None        # 见 infer_source_type；None/auto 自动推断


@dataclass(frozen=True)
class ImageryItem:
    """一条影像（一条时相/一张本底等）。"""

    phase: str                               # before / after / baseline / reference …
    name: str                                # 时相标签（如 2024-06-12），用作目录名
    service_url: Optional[str] = None
    file_urls: tuple[str, ...] = ()


@dataclass(frozen=True)
class SpatialExtent:
    """任务空间范围（矩形外包）。"""

    xmin: float
    ymin: float
    xmax: float
    ymax: float
    crs: str = "EPSG:4326"

    @property
    def tuple_wgs84(self) -> tuple[float, float, float, float]:
        """近似按 WGS84 使用（EPSG:4326/4490 时即原值；其它请先用 srs 换算）。"""
        return (self.xmin, self.ymin, self.xmax, self.ymax)


@dataclass(frozen=True)
class TaskInfo:
    """任务信息：全部下载数据源的地址都从这里取。"""

    task_id: str
    task_name: str
    #: 任务下发的空间范围（「全部空间范围」取此，与是否画图斑无关；可为空 → 需改用视图/框选）
    task_extent: Optional[SpatialExtent] = None
    features: Optional[DataSource] = None            # 图斑矢量（下载必选，规划时强约束）
    attr_template: Optional[DataSource] = None       # 属性模版
    rule_package: Optional[DataSource] = None        # 规则包
    dictionaries: tuple[DataSource, ...] = ()        # 字典（可多个）
    imageries: tuple[ImageryItem, ...] = ()          # 前后景等全部影像条目
    basemap_data: Optional[ImageryItem] = None       # 本底数据（影像）
    admin_boundary: Optional[DataSource] = None      # 政区边界（矢量）
    reference: Optional[DataSource] = None           # 参考数据（矢量/影像由 source_type 区分）

    # ------------------------------------------------------------------ 解析

    @classmethod
    def from_dict(cls, d: dict) -> "TaskInfo":
        """宽松解析任务 JSON；未知键忽略，缺省字段取 None/空。

        兼容常见键名变体：``taskId``/``task_id``、``taskName``/``task_name``、
        ``id``/``name``；空间范围接受 ``extent``/``scope_extent``/``task_extent``
        下的 ``[xmin,ymin,xmax,ymax]`` 或 ``{xmin,..}`` 对象。
        """
        def _pick(*names: str) -> object:
            for n in names:
                if n in d:
                    return d[n]
            return None

        task_id = _pick("task_id", "taskId", "id")
        task_name = _pick("task_name", "taskName", "name")
        if task_id is None or task_name is None:
            raise ValueError("任务信息缺少 task_id / task_name")

        extent = _pick("task_extent", "scope_extent", "extent", "range")
        task_extent = _parse_extent(extent)

        def _ds(v: object, name: str) -> Optional[DataSource]:
            if not v:
                return None
            if isinstance(v, DataSource):
                return v
            if isinstance(v, dict):
                return cls._source_from_dict(v, name)
            return None

        imageries: list[ImageryItem] = []
        for raw in d.get("imageries", d.get("imagery", []) or []):
            if isinstance(raw, dict):
                imageries.append(
                    ImageryItem(
                        phase=str(raw.get("phase", "imagery")),
                        name=str(raw.get("name", raw.get("time", "")) or "影像"),
                        service_url=_none(raw.get("service_url", raw.get("url"))),
                        file_urls=_urls(raw),
                    )
                )
        return cls(
            task_id=str(task_id),
            task_name=str(task_name),
            task_extent=task_extent,
            features=_ds(_pick("features", "feature"), "features"),
            attr_template=_ds(_pick("attr_template", "attrTemplate", "template"), "attr_template"),
            rule_package=_ds(_pick("rule_package", "rulePackage", "rules"), "rule_package"),
            dictionaries=tuple(
                cls._source_from_dict(x, f"dictionary{i}")
                for i, x in enumerate(d.get("dictionaries", []) or [])
                if isinstance(x, dict)
            ),
            imageries=tuple(imageries),
            basemap_data=(
                ImageryItem(
                    phase="baseline",
                    name="本底数据",
                    service_url=_none(bm.get("service_url", bm.get("url"))),
                    file_urls=_urls(bm),
                )
                if isinstance(bm := _pick("basemap_data", "baseline", "bottom"), dict)
                else None
            ),
            admin_boundary=_ds(_pick("admin_boundary", "adminBoundary", "boundary"), "admin_boundary"),
            reference=_ds(_pick("reference", "ref"), "reference"),
        )

    @staticmethod
    def _source_from_dict(d: dict, name: str) -> DataSource:
        return DataSource(
            name=str(d.get("name") or name),
            service_url=_none(d.get("service_url", d.get("url"))),
            file_urls=_urls(d),
            layer_id=_none_str(d.get("layer_id", d.get("layerId"))),
            source_type=_none_str(d.get("source_type", d.get("sourceType"))),
        )

    def to_dict(self) -> dict:
        def _ds(s: Optional[DataSource]) -> Optional[dict]:
            if s is None:
                return None
            d: dict = {"name": s.name}
            if s.service_url:
                d["service_url"] = s.service_url
            if s.file_urls:
                d["file_urls"] = list(s.file_urls)
            if s.layer_id is not None:
                d["layer_id"] = s.layer_id
            if s.source_type and s.source_type != _SOURCE_TYPE_AUTO:
                d["source_type"] = s.source_type
            return d

        out: dict = {
            "task_id": self.task_id,
            "task_name": self.task_name,
            "features": _ds(self.features),
        }
        if self.task_extent:
            e = self.task_extent
            out["extent"] = {
                "xmin": e.xmin,
                "ymin": e.ymin,
                "xmax": e.xmax,
                "ymax": e.ymax,
                "crs": e.crs,
            }
        for key, val in (
            ("attr_template", _ds(self.attr_template)),
            ("rule_package", _ds(self.rule_package)),
            ("basemap_data", self.basemap_data),
            ("admin_boundary", _ds(self.admin_boundary)),
            ("reference", _ds(self.reference)),
        ):
            if val is not None:
                out[key] = (
                    {"phase": val.phase, "name": val.name, "service_url": val.service_url}
                    if isinstance(val, ImageryItem)
                    else val
                )
        if self.dictionaries:
            out["dictionaries"] = [_ds(x) for x in self.dictionaries if x is not None]
        if self.imageries:
            out["imageries"] = [
                {"phase": i.phase, "name": i.name, "service_url": i.service_url}
                for i in self.imageries
            ]
        return out


# ---------------------------------------------------------------------- 工具

def resolve_source_type(ds: DataSource) -> str:
    """返回数据集实际抓取类型：显式给定优先，否则按 URL 推断。"""
    if ds.source_type and ds.source_type != _SOURCE_TYPE_AUTO:
        return ds.source_type
    url = ds.service_url or (ds.file_urls[0] if ds.file_urls else "")
    return infer_source_type(url)


def infer_source_type(url: str) -> str:
    """按 URL 形态推断抓取类型（方案 §3 约定）。

    ``{z}/{x}/{y}`` 占位模板 → ``xyz``；``/FeatureServer`` → ``arcgis-feature``；
    ``/ImageServer``/``/MapServer`` → ``arcgis-image``；含 wmts/wms → 对应；
    否则视为直链文件 ``file``。
    """
    if not url:
        return "file"
    low = url.lower()
    if "{" in low and "{x}" in low and "{z}" in low:
        return "xyz"
    if "/featureserver" in low:
        return "arcgis-feature"
    if "/imageserver" in low or "/mapserver" in low:
        return "arcgis-image"
    if "wmts" in low:
        return "wmts"
    if "wms" in low:
        return "wms"
    return "file"


def arcgis_sublayer_url(service_url: str, layer_id: Optional[str]) -> str:
    """把 ArcGIS REST 服务根 URL 归一到带图层 id 的完整地址。

    ``…/FeatureServer`` / ``…/MapServer`` → 按 ``layer_id``（缺省 0）拼 ``/N``；
    已带 ``/N`` 原样保留。MapServer 同样支持 ``query`` 拉要素，矢量下载也用它。
    """
    url = service_url.rstrip("/")
    if re.search(r"/(featureserver|mapserver)/\d+$", url, re.IGNORECASE):
        return url
    return f"{url}/{layer_id or 0}"


def _parse_extent(v: object) -> Optional[SpatialExtent]:
    if not v:
        return None
    if isinstance(v, SpatialExtent):
        return v
    if isinstance(v, (list, tuple)) and len(v) >= 4:
        try:
            return SpatialExtent(*[float(x) for x in v[:4]])
        except (TypeError, ValueError):
            return None
    if isinstance(v, dict):
        try:
            return SpatialExtent(
                xmin=float(v.get("xmin", v.get("minx"))),
                ymin=float(v.get("ymin", v.get("miny"))),
                xmax=float(v.get("xmax", v.get("maxx"))),
                ymax=float(v.get("ymax", v.get("maxy"))),
                crs=str(v.get("crs", "EPSG:4326")),
            )
        except (TypeError, ValueError):
            return None
    return None


def _none(v: object) -> Optional[str]:
    return str(v).strip() if v else None


def _none_str(v: object) -> Optional[str]:
    s = _none(v)
    return s if s else None


def _urls(d: dict) -> tuple[str, ...]:
    raw = d.get("file_urls", d.get("files", d.get("urls", [])))
    if isinstance(raw, str):
        return (raw,)
    if isinstance(raw, (list, tuple)):
        return tuple(str(x) for x in raw if x)
    return ()


__all__ = [
    "DataSource",
    "ImageryItem",
    "SpatialExtent",
    "TaskInfo",
    "arcgis_sublayer_url",
    "infer_source_type",
    "resolve_source_type",
]
