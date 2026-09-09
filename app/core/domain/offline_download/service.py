"""离线下载服务门面（方案 §8.1）：请求 → 计划 → staging → 后台任务。

调用方（弹窗/下载管理面板）流程：
1. :meth:`build_plan`（校验、含空间范围与必选数据约束，可先做 UI 估算展示）；
2. 用户点「开始下载」→ :meth:`start` 返回 ``(job_id, task)``，随后把
   ``task`` 的进度/日志/完成信号连到弹窗（先连再 start，见任务类 docstring）。

staging 统一放 ``paths.tmp/offline_download/stage/<job_id>``（打包成功后清理）。
"""

from __future__ import annotations

import os
import shutil
from typing import Optional

from app.infra.fs.paths import Paths
from app.infra.logging import get_logger

from . import disk, planner
from .manager import DownloadManager
from .model import ALL_KEYS, DownloadRequest, FetchPlan, SpatialScope
from .task import OfflineDownloadTask
from .task_info import TaskInfo

logger = get_logger(__name__)


class OfflineDownloadService:
    """离线下载领域服务门面。"""

    def __init__(self, manager: Optional[DownloadManager] = None) -> None:
        self.manager = manager or DownloadManager.instance()

    # ------------------------------------------------------------------ 计划

    def build_plan(self, request: DownloadRequest) -> FetchPlan:
        """校验并把请求展开成 FetchPlan（可能抛 PlanError，中文提示）。"""
        return planner.build_plan(request)

    def precheck(self, request: DownloadRequest) -> Optional[str]:
        """下载前磁盘预检（估算未知时返回 None 放行，最终以任务内预检为准）。"""
        try:
            plan = self.build_plan(request)
        except ValueError:
            return None
        return disk.precheck_message(request.target_dir, plan.est_total_bytes or None)

    # ------------------------------------------------------------------ 启动

    def start(
        self,
        request: DownloadRequest,
        job_id: Optional[str] = None,
        stage_root: Optional[str] = None,
    ) -> tuple[str, OfflineDownloadTask]:
        """构造计划 + 后台任务，登记进下载管理并启动。

        :return: ``(job_id, task)``。任务信号由调用方连接（务必在返回后立即连，
            或先连再用 :meth:`submit_connected`）。
        """
        plan = self.build_plan(request)
        job_id = job_id or self.manager.new_job_id()
        plan.stage_root = stage_root or self._default_stage(job_id)
        os.makedirs(plan.stage_root, exist_ok=True)
        task = OfflineDownloadTask(job_id, plan)
        self.manager.register(job_id, task, plan, request)
        return job_id, task

    def start_connected(
        self,
        request: DownloadRequest,
        on_progress=None,
        on_stage=None,
        on_log=None,
        on_done=None,
        on_fail=None,
    ) -> tuple[str, OfflineDownloadTask]:
        """便捷版：连好回调再启动（避免快速任务错过完成事件）。"""
        job_id, task = self.start(request)
        if on_progress:
            task.progress_updated.connect(on_progress)
        if on_stage:
            task.stage_changed.connect(on_stage)
        if on_log:
            task.log_message.connect(on_log)
        if on_done:
            task.taskCompleted.connect(on_done)
        if on_fail:
            task.taskTerminated.connect(on_fail)
        task.start()
        return job_id, task

    # ------------------------------------------------------------------ 操作

    def cancel(self, job_id: str) -> bool:
        return self.manager.cancel(job_id)

    def retry_job(self, job_id: str) -> bool:
        """用记录里的请求重建计划重新下载（覆盖同 job 记录，失败项重跑）。"""
        rec = self.manager.get_record(job_id)
        if not rec or not rec.get("request"):
            return False
        request = self._request_from_record(rec)
        try:
            _job_id, task = self.start(request, job_id=job_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("重试 %s 失败：%s", job_id, exc)
            return False
        task.start()
        return True

    def _request_from_record(self, rec: dict) -> DownloadRequest:
        req = rec["request"]
        task = TaskInfo.from_dict(req.get("task") or {})
        extent = req.get("scope_extent")
        scope_extent = tuple(float(x) for x in extent) if extent else None
        include = frozenset(str(x) for x in req.get("include") or []) or ALL_KEYS
        zoom = req.get("zoom_range") or [0, 18]
        return DownloadRequest(
            task=task,
            scope=SpatialScope(req.get("scope", "all")),
            scope_extent=scope_extent,
            include=include,
            target_dir=str(req.get("target_dir") or ""),
            target_name=req.get("target_name"),
            zoom_range=(int(zoom[0]), int(zoom[1])),
        )

    def list_jobs(self) -> list[dict]:
        return self.manager.list_records()

    def delete_staging(self, job_id: str) -> None:
        root = self._default_stage(job_id)
        if os.path.isdir(root):
            shutil.rmtree(root, ignore_errors=True)

    # ------------------------------------------------------------------ 内部

    @staticmethod
    def _default_stage(job_id: str) -> str:
        try:
            base = os.path.join(str(Paths.default().tmp), "offline_download", "stage")
        except Exception:  # noqa: BLE001
            base = os.path.join(os.getcwd(), ".tmp", "offline_download", "stage")
        return os.path.join(base, job_id)
