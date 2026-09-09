"""计划编排：把 DownloadRequest 展开成有序抓取清单（纯逻辑，可 headless 单测）。

规则（方案 §4 要点 2/3、§8.1）：
- 空间范围：``ALL`` 从 ``task.task_extent`` 取（换算 WGS84）；``VIEW/BOX`` 取
  请求自带的 WGS84 外包；拿不到 → :class:`PlanError`（弹窗提示改用视图/框选）。
- 必选「图斑矢量」缺失 → :class:`PlanError`；其余按 include ∩ 任务实有数据展开。
- 影像默认层级范围取自请求 ``zoom_range``（可被服务 tileInfo 覆盖，见 fetcher）。
"""

from __future__ import annotations

import math
from typing import Optional

from . import layout
from . import srs
from .model import (
    ALL_KEYS,
    KEY_LABELS,
    DownloadRequest,
    FetchItem,
    FetchPlan,
    PlanError,
    SpatialScope,
)
from .task_info import (
    DataSource,
    ImageryItem,
    TaskInfo,
    infer_source_type,
    resolve_source_type,
    service_name,
)

#: 有序数据集键：先矢量/文件必选组，后可选影像/参考（决定进度条推进顺序）
_ORDER = [
    "features",
    "attr_template",
    "rule_package",
    "dictionaries",
    "imageries",
    "basemap_data",
    "admin_boundary",
    "reference",
]


def build_plan(req: DownloadRequest) -> FetchPlan:
    task: TaskInfo = req.task
    include = req.include or ALL_KEYS
    _check_task(task, include)

    # ---- 范围解析（归一 WGS84） ------------------------------------------
    if req.scope == SpatialScope.ALL:
        if task.task_extent is None:
            raise PlanError(
                "该任务未配置空间范围（task_extent），且当前没画图斑无法自算；"
                "请改用「视图范围」或「框选范围」后重试。"
            )
        ext = task.task_extent
        scope = srs.rect_to_wgs84(ext.xmin, ext.ymin, ext.xmax, ext.ymax, ext.crs)
    else:
        if req.scope_extent is None:
            raise PlanError(
                "「视图/框选范围」需要画布外包矩形（WGS84），当前为空。"
            )
        scope = _checked_wgs84(req.scope_extent)

    items: list[FetchItem] = []
    used_targets: set[str] = set()

    def _unique_target(base: str) -> str:
        """同任务内重名自动追加序号，避免矢量/影像表互相覆盖。"""
        t, n = base, 1
        while t in used_targets:
            n += 1
            t = f"{base}_{n}"
        used_targets.add(t)
        return t

    def _vector(key: str, ds: DataSource) -> FetchItem:
        # 标签/表名优先用服务地址里的服务名（如 JXxian），退而 ds.name/默认
        label = service_name(ds.service_url) or ds.name or KEY_LABELS[key]
        _ensure_urls(label, ds)
        if infer_from(ds) == "file":
            return _files_item(key, label, ds, "文件", dir_name=label)
        base = layout.vector_table_name(label, layout.feature_class(key, label))
        return FetchItem(
            key=key,
            kind="vector",
            source_type=resolve_source_type(ds),
            label=label,
            service_url=ds.service_url,
            file_urls=ds.file_urls,
            layer_id=ds.layer_id,
            target=_unique_target(base),
            display_name=label,
        )

    def _files_item(
        key: str, label: str, ds: DataSource, group: str, dir_name: Optional[str] = None
    ) -> FetchItem:
        _ensure_urls(label, ds)
        return FetchItem(
            key=key,
            kind="files",
            source_type=resolve_source_type(ds),
            label=label,
            service_url=ds.service_url,
            file_urls=ds.file_urls,
            target=layout.files_target(key, dir_name or label),
        )

    def _imagery_item(
        key: str,
        phase: Optional[str],
        scene: str,
        src: object,
    ) -> FetchItem:
        if isinstance(src, ImageryItem):
            ds_url = src.service_url
            files = src.file_urls
            src_type = infer_source_type(src.service_url or (files[0] if files else ""))
        else:  # DataSource
            ds_url = getattr(src, "service_url", None)
            files = getattr(src, "file_urls", ())
            src_type = resolve_source_type(src)
        # 行文案优先服务名（如 THL/JXxian），退 scene 名，再退阶段目录名
        label = (
            service_name(ds_url)
            or (scene or "").strip()
            or layout.phase_dir(phase)
        )
        if not ds_url and not files:
            raise PlanError(f"「{label}」缺少下载地址（服务或文件）。")
        if src_type == "file" or (not ds_url and files):
            return FetchItem(
                key=key,
                kind="files",
                source_type="file",
                label=label,
                file_urls=files,
                target=layout.imagery_target(phase, scene).rsplit("/tiles", 1)[0],
            )
        zmin, zmax = req.zoom_range
        if src_type == "arcgis-image":
            # ArcGIS 影像 → 同一个 .gpkg 里的瓦片表（表名=服务名，唯一）
            target = _unique_target(
                layout.vector_table_name(label, layout.feature_class(key, label))
            )
        else:
            # 其它影像源暂不落 gpkg 瓦片表，保留目录落点用于报错提示
            target = layout.imagery_target(phase, scene)
        return FetchItem(
            key=key,
            kind="imagery",
            source_type=src_type,
            label=label,
            service_url=ds_url,
            file_urls=files,
            layer_id=getattr(src, "layer_id", None),
            phase=phase,
            zmin=zmin,
            zmax=zmax,
            target=target,
        )

    for key in _ORDER:
        if key not in include:
            continue
        if key == "features":
            if task.features is None:
                raise PlanError("任务信息缺少「图斑矢量」，无法离线下载。")
            items.append(_vector("features", task.features))
        elif key == "attr_template" and task.attr_template:
            items.append(_files_item(
                "attr_template",
                task.attr_template.name or KEY_LABELS["attr_template"],
                task.attr_template, "文件"))
        elif key == "rule_package" and task.rule_package:
            items.append(_files_item(
                "rule_package",
                task.rule_package.name or KEY_LABELS["rule_package"],
                task.rule_package, "文件"))
        elif key == "dictionaries" and task.dictionaries:
            for i, ds in enumerate(task.dictionaries):
                label = ds.name or f"字典 {i + 1}"
                items.append(_files_item("dictionaries", label, ds, "文件", dir_name="字典"))
        elif key == "imageries" and task.imageries:
            for img in task.imageries:
                items.append(_imagery_item("imageries", img.phase, img.name, img))
        elif key == "basemap_data" and task.basemap_data:
            items.append(
                _imagery_item("basemap_data", "baseline", "本底数据", task.basemap_data)
            )
        elif key == "admin_boundary" and task.admin_boundary:
            items.append(_vector("admin_boundary", task.admin_boundary))
        elif key == "reference" and task.reference:
            ref = task.reference
            if infer_from(ref) == "arcgis-feature":
                items.append(_vector("reference", ref))
            else:
                items.append(
                    _imagery_item("reference", "reference", ref.name or "参考数据", ref)
                )

    if not any(i.target for i in items):
        raise PlanError("没有可下载的数据集（请检查任务信息或勾选项）。")

    return FetchPlan(
        task_id=task.task_id,
        task_name=task.task_name,
        items=[i for i in items if i.target],
        scope_wgs84=scope,
        target_dir=req.target_dir or ".",
        zip_name=req.target_name or layout.zip_base_name(task.task_id, task.task_name),
        gpkg_name=layout.gpkg_filename(task.task_id, task.task_name),
        scope_type=req.scope.value,
    )


# ------------------------------------------------------------------- 内部

def infer_from(ds: DataSource) -> str:
    return resolve_source_type(ds)


def _ensure_urls(label: str, ds: DataSource) -> None:
    if not (ds.service_url or ds.file_urls):
        raise PlanError(f"任务「{label}」缺少服务/文件地址，无法下载。")


def _check_task(task: TaskInfo, include: frozenset[str]) -> None:
    if "features" in include and task.features is None:
        raise PlanError("任务信息缺少「图斑矢量」，无法离线下载。")


def _checked_wgs84(t: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
    x1, y1, x2, y2 = (float(v) for v in t)
    if not (math.isfinite(x1) and math.isfinite(y1) and math.isfinite(x2) and math.isfinite(y2)):
        raise PlanError("空间范围含非法数值。")
    return (min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2))
