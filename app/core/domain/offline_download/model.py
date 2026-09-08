"""离线下载领域模型（纯逻辑，不依赖 QGIS / Qt，可 headless 单测）。

分层约定见方案 §8.1：
- :class:`DownloadRequest` 是弹窗一次「开始下载」的完整输入；
- :class:`planner` 把请求展开成有序 :class:`FetchItem` 清单（:class:`FetchPlan`）；
- :class:`task` 逐个执行 FetchItem，进度/日志经信号汇报，完成后打包 zip。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from .task_info import TaskInfo

#: 数据集勾选键 → 中文标签（必选组在前，供弹窗勾选列表/日志使用）
KEY_LABELS: dict[str, str] = {
    "features": "图斑矢量",
    "attr_template": "属性模版",
    "rule_package": "规则包",
    "dictionaries": "字典",
    "imageries": "前后景时相影像",
    "basemap_data": "本底数据",
    "admin_boundary": "政区边界",
    "reference": "参考数据",
}

#: 必选数据集键（图斑矢量、属性模版、规则包、字典），弹窗默认勾选不可取消
REQUIRED_KEYS: frozenset[str] = frozenset(
    {"features", "attr_template", "rule_package", "dictionaries"}
)

#: 全部可选键（含必选，作 include 默认全集）
ALL_KEYS: frozenset[str] = frozenset(KEY_LABELS)


class SpatialScope(str, Enum):
    """空间范围三选一。

    ``ALL`` = 任务字段 ``task_extent``（任务下发的空间范围，与是否画图斑无关，
    见方案 §3/§5）；``VIEW``/``BOX`` 需在 :attr:`DownloadRequest.scope_extent`
    给出 WGS84 外包（由弹窗经坐标变换后填入）。
    """

    ALL = "all"
    VIEW = "view"
    BOX = "box"


class DownloadStatus(str, Enum):
    """下载任务状态（下载管理面板/持久化用）。"""

    PENDING = "pending"      # 待下载（已入队未开始）
    RUNNING = "running"      # 下载中
    PAUSED = "paused"        # 暂停（保留 staging 供续传）
    SUCCEEDED = "succeeded"  # 全部成功（已打 zip）
    PARTIAL = "partial"      # 部分成功（个别数据集失败，已完成部分保留）
    FAILED = "failed"        # 失败（无可续传内容）
    CANCELLED = "cancelled"  # 用户取消


class PlanError(ValueError):
    """计划/校验失败（含中文提示，供弹窗直接展示）。"""


@dataclass(frozen=True)
class DownloadRequest:
    """一次离线下载的完整输入（对应弹窗一次「开始下载」）。

    :param scope_extent: ``(xmin, ymin, xmax, ymax)`` **WGS84**。仅 ``VIEW``/
        ``BOX`` 必须给；``ALL`` 时由 planner 从 ``task.task_extent`` 取（按其
        坐标系换算，缺省按 WGS84），为空会校验失败（见方案 §4 要点 2 兜底）。
    :param zoom_range: 影像默认层级范围，可被服务 tileInfo 覆盖（方案 §6.4）。
    """

    task: "TaskInfo"
    scope: SpatialScope = SpatialScope.ALL
    scope_extent: Optional[tuple[float, float, float, float]] = None
    include: frozenset[str] = field(default_factory=lambda: frozenset(ALL_KEYS))
    target_dir: str = ""
    target_name: Optional[str] = None
    zoom_range: tuple[int, int] = (0, 18)


@dataclass(frozen=True)
class FetchItem:
    """计划中的一个可下载单元（一个 FeatureClass / 一条影像 / 一组文件）。"""

    key: str                    # 归属的数据集键（KEY_LABELS 之一；imageries 拆多条）
    kind: str                   # "vector" | "imagery" | "files"
    source_type: str            # arcgis-feature / arcgis-image / wms / wmts / xyz / file
    label: str                  # 中文显示名（图斑矢量 / 前时相 2024-06 等）
    service_url: Optional[str] = None
    file_urls: tuple[str, ...] = ()
    layer_id: Optional[str] = None
    phase: Optional[str] = None         # 影像：before/after/baseline/reference
    zmin: Optional[int] = None          # 影像：本单元层级范围
    zmax: Optional[int] = None
    #: 目标落点（相对 staging/zip 根）：
    #: vector → SpatiaLite .sqlite 内图层名；imagery/files → 目录相对路径。
    target: str = ""
    #: gdb 中文显示名 → ASCII 落盘名的对应（写进 manifest name_map）
    display_name: Optional[str] = None
    #: 估算字节（下载前由 fetcher 估算，供权重/预检/UI）
    estimated_bytes: Optional[int] = None


@dataclass
class FetchPlan:
    """一次下载任务的展开计划（顺序决定进度条推进：矢量 gdb→文件→影像→zip）。"""

    task_id: str
    task_name: str
    items: list[FetchItem] = field(default_factory=list)
    #: WGS84 (xmin, ymin, xmax, ymax)
    scope_wgs84: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)
    target_dir: str = ""
    zip_name: str = ""
    scope_type: str = "all"
    #: staging 根（打包前的目录树，见方案 §6.1 决策 3）
    stage_root: str = ""
    est_total_bytes: int = 0
    errors: list[str] = field(default_factory=list)

    def by_key(self, key: str) -> list[FetchItem]:
        return [i for i in self.items if i.key == key]

    def total(self) -> int:
        known = [i.estimated_bytes for i in self.items if i.estimated_bytes]
        return sum(known) if known else 0
