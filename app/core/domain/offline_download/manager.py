"""下载管理：任务注册表 + 状态落盘（方案 §8.3）。

每个离线下载任务（job）登记一条记录（含进度/阶段/状态/目标 zip），并把运行中
任务对象与重建所需请求（TaskInfo JSON 等）持久化到 ``paths.tmp/offline_download/
records.json``——应用重启后可恢复状态并允许“重试”。
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from typing import Optional

from qgis.PyQt.QtCore import QObject, pyqtSignal

from app.infra.fs.paths import Paths
from app.infra.logging import get_logger

from .model import DownloadRequest, DownloadStatus, FetchPlan

logger = get_logger(__name__)


class DownloadManager(QObject):
    """单例下载管理。

    - :attr:`record_changed`：记录变更（``job_id``）时发，供「下载管理」面板刷新。
    - 线程安全：worker 线程只通过 Qt 队列信号更新记录（主线程槽）。
    """

    record_changed = pyqtSignal(str)

    _INSTANCE: Optional["DownloadManager"] = None

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._tasks: dict[str, object] = {}          # job_id -> task
        self._records: dict[str, dict] = {}
        self._lock = threading.RLock()
        self._last_save = 0.0
        self._records_dir = self._records_dir()
        self._load()

    # ------------------------------------------------------------ 单例/路径

    @classmethod
    def instance(cls) -> "DownloadManager":
        if cls._INSTANCE is None:
            cls._INSTANCE = cls()
        return cls._INSTANCE

    @staticmethod
    def _records_dir() -> str:
        try:
            base = os.path.join(str(Paths.default().tmp), "offline_download")
            os.makedirs(base, exist_ok=True)
            return base
        except Exception:  # noqa: BLE001
            return os.path.join(os.getcwd(), ".tmp", "offline_download")

    @property
    def records_file(self) -> str:
        return os.path.join(self._records_dir, "records.json")

    # ------------------------------------------------------------ 注册

    def register(self, job_id: str, task, plan: FetchPlan, request: DownloadRequest) -> None:
        rec = {
            "job_id": job_id,
            "task_id": plan.task_id,
            "task_name": plan.task_name,
            "status": DownloadStatus.PENDING.value,
            "progress": 0,
            "stage": "待下载",
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "target_dir": plan.target_dir,
            "zip_name": plan.zip_name,
            "error": "",
            #: 重建请求所需（重试/恢复）
            "request": {
                "task": request.task.to_dict(),
                "scope": request.scope.value,
                "scope_extent": list(request.scope_extent) if request.scope_extent else None,
                "include": sorted(request.include or ()),
                "target_dir": request.target_dir,
                "target_name": request.target_name,
                "zoom_range": list(request.zoom_range),
            },
        }
        with self._lock:
            self._tasks[job_id] = task
            self._records[job_id] = rec
            self._save()
        self._wire(task, job_id, rec)

    def _wire(self, task, job_id: str, rec: dict) -> None:
        task.progress_updated.connect(lambda v: self._update(job_id, progress=v))
        task.stage_changed.connect(lambda s: self._update(job_id, stage=s))
        task.taskCompleted.connect(lambda: self._finish(job_id))
        task.taskTerminated.connect(lambda: self._finish(job_id, terminated=True))

    def _update(self, job_id: str, **kw) -> None:
        with self._lock:
            rec = self._records.get(job_id)
            if not rec:
                return
            rec.update(kw)
            if "progress" in kw:
                rec["status"] = DownloadStatus.RUNNING.value
            self._persist_throttled()
        self.record_changed.emit(job_id)

    def _finish(self, job_id: str, terminated: bool = False) -> None:
        with self._lock:
            rec = self._records.get(job_id)
            task = self._tasks.get(job_id)
            if not rec:
                return
            if terminated:
                if task is not None and getattr(task, "partial", False):
                    rec["status"] = DownloadStatus.PARTIAL.value
                    rec["error"] = _exc_text(task.exception)
                elif task is not None and getattr(task, "_cancel", None) and task._cancel.is_set():
                    rec["status"] = DownloadStatus.CANCELLED.value
                else:
                    rec["status"] = DownloadStatus.FAILED.value
                    rec["error"] = _exc_text(task.exception)
            else:
                rec["status"] = DownloadStatus.SUCCEEDED.value
                rec["progress"] = 100
                rec["stage"] = "完成"
            self._save()
        self.record_changed.emit(job_id)

    def _persist_throttled(self) -> None:
        """进度高频更新时最多每秒写盘一次；收尾一律全量写（_save）。"""
        now = time.time()
        if now - self._last_save >= 1.0:
            self._last_save = now
            self._save()

    # ------------------------------------------------------------ 查询

    def list_records(self) -> list[dict]:
        with self._lock:
            return [dict(r) for r in sorted(
                self._records.values(), key=lambda x: x["created_at"], reverse=True
            )]

    def get_record(self, job_id: str) -> Optional[dict]:
        with self._lock:
            r = self._records.get(job_id)
            return dict(r) if r else None

    def get_task(self, job_id: str):
        with self._lock:
            return self._tasks.get(job_id)

    # ------------------------------------------------------------ 操作

    def cancel(self, job_id: str) -> bool:
        task = self.get_task(job_id)
        if task is None:
            return False
        task.cancel()
        return True

    def new_job_id(self) -> str:
        return f"job_{int(time.time())}_{uuid.uuid4().hex[:6]}"

    # ------------------------------------------------------------ 持久化

    def _save(self) -> None:
        try:
            with open(self.records_file, "w", encoding="utf-8") as f:
                json.dump(list(self._records.values()), f, ensure_ascii=False, indent=2)
        except Exception:  # noqa: BLE001
            logger.warning("下载管理记录写盘失败")

    def _load(self) -> None:
        try:
            if not os.path.exists(self.records_file):
                return
            with open(self.records_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            for rec in data:
                job_id = rec.get("job_id")
                if job_id:
                    rec["status"] = DownloadStatus.FAILED.value if rec.get("status") in (
                        DownloadStatus.RUNNING.value,
                        DownloadStatus.PENDING.value,
                    ) else rec.get("status")
                    self._records[job_id] = rec
        except Exception:  # noqa: BLE001
            logger.warning("下载管理记录读取失败（忽略）")


def _exc_text(exc: object) -> str:
    if exc is None:
        return ""
    return str(exc)
