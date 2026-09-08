"""离线下载后台任务：普通 ``threading.Thread`` 跑整个 FetchPlan（方案 §8.2）。

线程模型遵循方案 §6.6 红线：QObject 存活在主线程，worker 跑 ``daemon Thread``，
进度/日志/完成一律经信号 emit，控件更新由调用方在主线程槽做；**先连信号再
``start()``**（快速任务可能立刻完成）。绝不把本任务放进 ``QgsTask``。
"""

from __future__ import annotations

import os
import shutil
import threading
from typing import Optional

from qgis.PyQt.QtCore import QObject, pyqtSignal

from app.infra.logging import get_logger

from . import fetcher, layout
from .fetcher import DownloadCanceled
from .model import FetchPlan

logger = get_logger(__name__)


class OfflineDownloadTask(QObject):
    """一次离线下载的后台任务。

    - ``progress_updated(int)``：0~100（下载按已写字节/估算总量折算，打包前后置 90→100）；
    - ``stage_changed(str)``：当前阶段（如「抓图斑矢量」「打包 zip」）；
    - ``log_message(level, message)``：与 app 日志面板同款四级；
    - ``taskCompleted`` / ``taskTerminated``：收尾信号；
    - ``exception``：异常对象；``partial``：是否有数据集失败（走了终止但有已完成项）。

    调用约定：先 connect 信号 → ``start()``。
    """

    progress_updated = pyqtSignal(int)
    stage_changed = pyqtSignal(str)
    log_message = pyqtSignal(str, str)
    taskCompleted = pyqtSignal()
    taskTerminated = pyqtSignal()

    def __init__(self, job_id: str, plan: FetchPlan) -> None:
        super().__init__()
        self.job_id = job_id
        self.plan = plan
        self.exception: object = None
        self.partial = False
        self._cancel = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._done_bytes = 0
        self._est_total = 0

    # ------------------------------------------------------------ 启动/取消

    def start(self) -> None:
        """启动后台线程（调用前请先连好 taskCompleted/taskTerminated）。"""
        self._thread = threading.Thread(
            target=self._run, name=f"offline-download-{self.job_id}", daemon=True
        )
        self._thread.start()

    def cancel(self) -> None:
        self._cancel.set()

    # ------------------------------------------------------------ 回调

    def _progress(self, value: int) -> None:
        self.progress_updated.emit(max(0, min(100, int(value))))

    def _stage(self, text: str) -> None:
        self.stage_changed.emit(text)

    def _log_line(self, level: str, message: str) -> None:
        text = str(message).rstrip()
        if not text:
            return
        _WRITE = {
            "INFO": logger.info,
            "DEBUG": logger.debug,
            "WARNING": logger.warning,
            "ERROR": logger.error,
        }
        _WRITE.get(level, logger.info)(text)
        self.log_message.emit(level, text)

    def _on_bytes(self, n: int) -> None:
        self._done_bytes += max(0, n)
        if self._est_total > 0:
            self._progress(self._done_bytes * 100.0 / self._est_total)

    # ------------------------------------------------------------ 执行

    def _run(self) -> None:
        try:
            self._stage("准备")
            self._log_line("INFO", f"开始离线下载：{self.plan.task_name}（{self.plan.task_id}）")
            self._prepare_estimates()

            layout.ensure_stage(self.plan)
            precheck = self._disk_precheck()
            if precheck:
                raise RuntimeError(precheck)

            ctx = fetcher.FetchContext(
                cancel=self._cancel,
                report=self._on_bytes,
                log=lambda m: self._log_line("DEBUG", m),
            )
            results: dict = {}
            errors: list[str] = []

            # 1) 逐个数据集抓取（按计划顺序，失败不阻断后续，见方案 §8.2）
            for item in self.plan.items:
                if self._cancel.is_set():
                    raise DownloadCanceled()
                self._stage(f"下载 {item.label}")
                try:
                    meta = fetcher.fetch_item(item, self.plan.stage_root, self.plan.scope_wgs84, ctx)
                    results[item.label] = meta
                except DownloadCanceled:
                    raise
                except Exception as exc:  # noqa: BLE001  单个数据集失败不阻断后续
                    errors.append(f"{item.label}：{exc}")
                    self._log_line("ERROR", f"数据集失败 {item.label}：{exc}")

            # 2) 任一数据集失败 → 部分成功，保留 staging 不打包（可续传/重试）
            if errors:
                self.partial = True
                self.exception = RuntimeError("；".join(errors))
                for e in errors:
                    self._log_line("ERROR", e)
                self._log_line("WARNING", "存在失败数据集，未打包。已完成内容保留在 staging 供重试。")
                self.taskTerminated.emit()
                return

            # 3) 打包 zip + 写 manifest
            self._stage("打包")
            self._progress(90)
            manifest = layout.build_manifest_dict(self.plan, results)
            layout.write_manifest(self.plan.stage_root, manifest)
            zip_path = layout.make_zip(
                self.plan.stage_root, self.plan.target_dir, self.plan.zip_name
            )
            size = os.path.getsize(zip_path)
            from .disk import format_bytes

            self._log_line("INFO", f"下载完成，压缩包：{zip_path}（{format_bytes(size)}）")
            self._progress(100)
            self._cleanup_stage()
            self.taskCompleted.emit()
        except DownloadCanceled:
            self._log_line("WARNING", "下载已取消（staging 已保留，可在下载管理中重试/续传）")
            self.taskTerminated.emit()
        except Exception as exc:  # noqa: BLE001
            self.exception = exc
            self._log_line("ERROR", f"离线下载异常：{exc}")
            self.taskTerminated.emit()

    def _prepare_estimates(self) -> None:
        """下载前先给各数据集做估算（网络开销小），折算整体进度分母。"""
        ctx = fetcher.FetchContext(cancel=self._cancel, timeout=10.0)
        total = 0
        for item in self.plan.items:
            est = fetcher.estimate_item_bytes(item, self.plan.scope_wgs84, ctx)
            total += est if est else (1 << 20)  # 未知项按 1MB 兜底
        self._est_total = max(total, 1)
        self.plan.est_total_bytes = self._est_total

    def _disk_precheck(self) -> Optional[str]:
        """磁盘预检（方案 §7.3）：估算×冗余 > 剩余 → 中文提示；None 放行。"""
        from . import disk

        return disk.precheck_message(self.plan.target_dir, self.plan.est_total_bytes)

    def _cleanup_stage(self) -> None:
        if self.plan.stage_root and os.path.isdir(self.plan.stage_root):
            try:
                shutil.rmtree(self.plan.stage_root, ignore_errors=True)
            except Exception:  # noqa: BLE001
                self._log_line("DEBUG", f"清理 staging 失败：{self.plan.stage_root}")
