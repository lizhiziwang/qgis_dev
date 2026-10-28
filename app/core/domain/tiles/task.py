"""矢量瓦片生成任务。

迁自 :class:`component.TreeTool.CreateTileTask`，与 :class:`TileJob` 配合。

任务经 :class:`QgsApplication.taskManager` 在后台线程运行，进度 / 日志经
Qt 信号投递回主线程。取消时把取消标志同步给算法反馈对象，算法会在当前
缩放级别之间退出。
"""

from __future__ import annotations

import json
import shutil
import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from qgis.core import QgsTask

from qgis.PyQt.QtCore import pyqtSignal
from qgis.core import QgsTask

from app.core.domain.imagery_tiles.feedback import ProcessingLogFeedback
from app.core.domain.imagery_tiles.worker_runner import ProgressTail
from app.infra.logging import get_logger

from .parallel import kill_worker, spawn_worker
from .service import TileJob

logger = get_logger(__name__)


class CreateTileTask(QgsTask):
    """后台运行的矢量瓦片生成任务。

    进度通过 :attr:`progress_updated` 信号回报（``0~100`` int）；算法日志经
    :attr:`log_message` 信号回报（``level, message``），同时写入应用日志。
    """

    progress_updated = pyqtSignal(int)
    #: 算法 ``setProgressText`` 给出的文字（当前矢量瓦片算法只给百分比，
    #: 保留通用通道，供以后算法升级使用）
    progress_text = pyqtSignal(str)
    log_message = pyqtSignal(str, str)

    def __init__(self, description: str, job: TileJob) -> None:
        super().__init__(description, QgsTask.CanCancel)
        self.job = job
        self.exception: object = None
        self.result_output: dict = {}
        self.feedback = ProcessingLogFeedback()
        self.feedback.progressChanged.connect(self._on_progress)
        self.feedback.progress_text.connect(self._on_progress_text)
        # feedback 在工作线程发信号；先转到本对象的普通 Python 方法，再投递 UI
        self.feedback.log_line.connect(self._on_log_line)

    # ------------------------------------------------------------ 回调

    def _on_progress(self, value: float) -> None:
        self.progress_updated.emit(int(value))

    def _on_progress_text(self, text: str) -> None:
        self.progress_text.emit(str(text))

    def _on_log_line(self, level: str, message: str) -> None:
        """工作线程算法日志 → 应用日志 + UI 信号。"""
        text = str(message).rstrip()
        if not text:
            return
        _WRITE = {
            "INFO": logger.info,
            "CMD": logger.info,
            "DEBUG": logger.debug,
            "WARNING": logger.warning,
            "ERROR": logger.error,
        }
        _WRITE.get(level, logger.info)(text)
        self.log_message.emit(level, text)

    # ------------------------------------------------------------ 取消

    def cancel(self) -> None:  # noqa: N802 —— QgsTask 命名
        """请求取消：同时把取消标志传给算法反馈对象。

        QgsTask 自己的取消标志只有任务管理器看得到，``processing`` 算法检查的是
        :class:`QgsProcessingFeedback`；不转发的话点「取消」后要等当前任务跑完。
        """
        try:
            self.feedback.cancel()
        finally:
            super().cancel()

    # ------------------------------------------------------------ 执行

    def run(self) -> bool:
        try:
            # 延迟 import：避免 service 模块顶层依赖 processing
            import processing

            params = self.job.to_native_params()
            logger.info(
                "执行算法 native:writevectortiles_xyz：输出=%s 缩放=%s-%s 图层=%s",
                self.job.output_dir,
                self.job.min_zoom,
                self.job.max_zoom,
                list(self.job.layer_sources),
            )
            params["OUTPUT_DIRECTORY"] = self.job.output_dir
            self.result_output = processing.run(
                "native:writevectortiles_xyz", params, feedback=self.feedback
            )
            if self.feedback.failed:
                self.exception = RuntimeError("矢量瓦片生成命令执行失败（详见应用日志）")
                return False
            if self.isCanceled():
                logger.info("矢量瓦片任务已取消：%s", self.job.output_dir)
                return False
            logger.info("native:writevectortiles_xyz 完成 → %s", self.job.output_dir)
            return True
        except Exception as e:  # noqa: BLE001
            if self.isCanceled():
                logger.info("矢量瓦片任务已取消：%s", self.job.output_dir)
                return False
            self.exception = e
            logger.error("native:writevectortiles_xyz 执行失败：%s", e, exc_info=True)
            return False

    def finished(self, result: bool) -> None:  # noqa: D401
        """QgsTask 在主线程中回调；收尾由对话框订阅生命周期信号完成。"""
        return None


class CreateTileGroupTask(QgsTask):
    """按缩放级拆成多个**子进程**并行生成矢量瓦片。

    为什么不放在同一个进程的多个线程里跑：实测大图层同时跑多个
    processing 算法会原生崩溃（0xC0000409 / segfault），所以每个缩放区间都
    交给独立的 QGIS 子进程（:mod:`app.core.domain.tiles.worker`），父进程只
    负责拉起、读进度文件 / 汇总进度、收尾。

    信号与 :class:`CreateTileTask` 完全一致，对话框可以无差别使用。
    """

    progress_updated = pyqtSignal(int)
    progress_text = pyqtSignal(str)
    log_message = pyqtSignal(str, str)

    def __init__(
        self,
        description: str,
        job: TileJob,
        ranges: list[tuple[int, int]],
        work_dir: Path,
    ) -> None:
        super().__init__(description, QgsTask.CanCancel)
        self.job = job
        self.ranges = list(ranges)
        self.work_dir = Path(work_dir)
        self.exception: object = None
        self._procs: list = []
        self._tails: list[ProgressTail] = []
        self._progress = [0.0 for _ in self.ranges]
        self._errors: list[str] = ["" for _ in self.ranges]
        self._last_emitted = -1
        self._canceled = False
        self._failed_index: int | None = None
        self._levels = [zmax - zmin + 1 for zmin, zmax in self.ranges]

    # ------------------------------------------------------------ 取消

    def cancel(self) -> None:  # noqa: N802 —— QgsTask 命名
        """立即杀掉全部子进程（父进程侧不再等它们自己退出）。"""
        self._canceled = True
        self._kill_all()
        super().cancel()

    # ------------------------------------------------------------ 执行

    def run(self) -> bool:
        try:
            job_files = self._write_job_files()
            try:
                self._procs = [
                    spawn_worker(path, self.work_dir) for path in job_files
                ]
            except Exception:
                self._kill_all()
                raise
            self._tails = [
                ProgressTail(self.work_dir / f"progress_{i}.jsonl")
                for i in range(len(self.ranges))
            ]
            while True:
                self._pump()
                if self._canceled or self.isCanceled():
                    self._kill_all()
                    logger.info("矢量瓦片并行任务已取消：%s", self.job.output_dir)
                    return False
                codes = [proc.poll() for proc in self._procs]
                failed_index = next(
                    (
                        index
                        for index, code in enumerate(codes)
                        if code not in (None, 0)
                    ),
                    None,
                )
                if failed_index is not None:
                    # 一段失败就把其余段停掉，避免继续白跑；记住真正失败的那段
                    self._failed_index = failed_index
                    self._kill_all()
                    break
                if all(code is not None for code in codes):
                    break
                time.sleep(0.1)
            self._pump()
            self._wait_children()
            failed = self._failure_message()
            if failed:
                self.exception = RuntimeError(failed)
                logger.error("矢量瓦片并行任务失败：%s", failed)
                return False
            self._emit_progress(force=True)
            logger.info(
                "native:writevectortiles_xyz 完成 → %s（%s 组：%s）",
                self.job.output_dir,
                len(self.ranges),
                "、".join(f"{a}-{b}" for a, b in self.ranges),
            )
            return True
        except Exception as exc:  # noqa: BLE001
            self._kill_all()
            if self._canceled or self.isCanceled():
                logger.info("矢量瓦片并行任务已取消：%s", self.job.output_dir)
                return False
            self.exception = exc
            logger.error("矢量瓦片并行任务失败：%s", exc, exc_info=True)
            return False

    def finished(self, result: bool) -> None:  # noqa: D401
        """QgsTask 在主线程中回调；清理临时作业 / 进度 / 日志。"""
        self._kill_all()
        self._tails = []
        shutil.rmtree(self.work_dir, ignore_errors=True)

    # ------------------------------------------------------------ 内部

    def _write_job_files(self) -> list[Path]:
        """为每个缩放段写一个子进程作业文件，返回路径列表。"""
        xmin, xmax, ymin, ymax, authid = self.job.extent
        extent = f"{xmin},{xmax},{ymin},{ymax} [{authid}]"
        paths: list[Path] = []
        for index, (zmin, zmax) in enumerate(self.ranges):
            progress = self.work_dir / f"progress_{index}.jsonl"
            payload = {
                "output_dir": self.job.output_dir,
                "xyz_template": self.job.xyz_template,
                "layers": list(self.job.layer_sources),
                "min_zoom": zmin,
                "max_zoom": zmax,
                "extent": extent,
                "progress": str(progress),
            }
            path = self.work_dir / f"job_{index}.json"
            path.write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8"
            )
            paths.append(path)
        return paths

    def _pump(self) -> None:
        """读取各子进程的进度文件并汇总。"""
        for index, tail in enumerate(self._tails):
            for event in tail.read():
                kind = event.get("t")
                if kind == "p":
                    try:
                        self._progress[index] = max(
                            0.0, min(100.0, float(event.get("v", 0)))
                        )
                    except (TypeError, ValueError):
                        continue
                elif kind == "log":
                    level = str(event.get("level", "INFO"))
                    message = str(event.get("msg", "")).strip()
                    if message:
                        self.log_message.emit(level, message)
                elif kind == "text":
                    text = str(event.get("msg", "")).strip()
                    if text:
                        self.progress_text.emit(text)
                elif kind == "err":
                    message = str(event.get("msg", "")).strip()
                    if message:
                        self._errors[index] = message
        self._emit_progress()

    def _emit_progress(self, force: bool = False) -> None:
        total_levels = sum(self._levels) or 1
        done = sum(
            level * p / 100.0
            for level, p in zip(self._levels, self._progress, strict=True)
        )
        value = int(round(done / total_levels * 100.0))
        value = max(0, min(100, value))
        if force or value != self._last_emitted:
            self._last_emitted = value
            self.progress_updated.emit(value)

    def _failure_message(self) -> str:
        """任一子进程非 0 退出 / 有错误事件时给出可读原因。"""
        indices = []
        if self._failed_index is not None:
            indices.append(self._failed_index)
        indices.extend(index for index in range(len(self.ranges)) if index not in indices)
        for index in indices:
            proc = self._procs[index] if index < len(self._procs) else None
            code = proc.returncode if proc is not None else 1
            error = self._errors[index].strip()
            if code not in (None, 0) or error:
                detail = error or self._read_log(index)
                zmin, zmax = self.ranges[index]
                message = f"缩放 {zmin}-{zmax} 段失败"
                if code not in (None, 0):
                    message += f"（退出码 {code}）"
                return f"{message}：{detail}" if detail else message
        return ""

    def _read_log(self, index: int) -> str:
        log_file = self.work_dir / f"job_{index}.log"
        try:
            text = log_file.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            return ""
        return text[-1200:]

    def _wait_children(self, timeout: float = 5.0) -> None:
        deadline = time.time() + timeout
        for proc in self._procs:
            remaining = max(0.0, deadline - time.time())
            try:
                proc.wait(timeout=remaining)
            except Exception:  # noqa: BLE001
                kill_worker(proc)

    def _kill_all(self) -> None:
        for proc in self._procs:
            kill_worker(proc)
