"""影像切片（栅格瓦片）后台任务。

有两种执行方式：

1. **子进程（默认，界面可继续使用）**：把当前工程另存临时工程，交给
   :mod:`.worker_runner` 起的独立进程跑 ``native:tilesxyzdirectory``。
   算法的 ``processAlgorithm`` 在 C++ 里整段执行并持有 GIL，放在主进程任何
   线程里都会把界面卡住（实测 6.7 s 的切片主线程最长停顿 5.3 s）；子进程
   与主进程隔离后主线程完全不受影响。
2. **进程内（回退）**：子进程准备失败（如临时工程另存失败）时，退回普通
   :class:`threading.Thread` 直接调 ``processing.run``，行为与以前一致。

.. note::
   不用 :class:`QgsTask`：实测 ``native:tilesxyzdirectory`` 内部用
   :class:`QgsMapRendererParallelJob` 渲染，经 :class:`QgsTaskManager`
   后台线程运行会原生崩溃（退出码 127）。

进度经 :attr:`progress_updated` 信号回报（``0~100`` int）；算法输出经
:attr:`log_message` 信号回报（``level, message``），同时写入应用日志
（日志面板 / ``logs/app.log``）。
"""

from __future__ import annotations

import threading

from qgis.PyQt.QtCore import QObject, pyqtSignal

from app.infra.logging import get_logger

from .active_tasks import register as _register_active
from .active_tasks import unregister as _unregister_active
from .feedback import ProcessingLogFeedback
from .service import TILE_FORMAT_LABELS, ImageryTileJob, write_image_tile_viewer
from .worker_runner import (
    WorkerCallbacks,
    WorkerSpec,
    run_worker_process,
)

logger = get_logger(__name__)


class CreateImageryTileTask(QObject):
    """影像切片任务（子进程 / 普通线程执行）。

    提供与旧 :class:`QgsTask` 兼容的 ``taskCompleted`` / ``taskTerminated``
    信号与 ``exception`` 属性，对话框无需改动收尾逻辑。
    """

    progress_updated = pyqtSignal(int)
    #: 算法 ``setProgressText`` 给出的文字（当前算法未使用，保留通用通道）
    progress_text = pyqtSignal(str)
    #: (level, message)；level ∈ {INFO, CMD, DEBUG, WARNING, ERROR}，
    #: 供对话框实时展示任务日志。
    log_message = pyqtSignal(str, str)
    taskCompleted = pyqtSignal()
    taskTerminated = pyqtSignal()

    def __init__(
        self,
        description: str,
        job: ImageryTileJob,
        worker_spec: WorkerSpec | None = None,
    ) -> None:
        super().__init__()
        self.job = job
        #: 子进程作业；为空表示走进程内回退路径
        self.worker_spec = worker_spec
        self.exception: object = None
        self.result_output: dict = {}
        #: 勾选网页查看器时生成的离线 index.html 路径；未生成/失败为空串
        self.viewer_html = ""
        self.feedback = ProcessingLogFeedback()
        self.feedback.progressChanged.connect(self._on_progress)
        # feedback.log_line 在渲染线程发出 → 投递到主线程的 _on_log_line
        # （普通 Python 方法，投递安全，见进度信号同款处理）
        self.feedback.log_line.connect(self._on_log_line)
        self.feedback.progress_text.connect(self._on_progress_text)
        self._thread: threading.Thread | None = None
        #: 子进程取消标志（置位后 worker_runner 立即终止子进程）
        self._cancel_event = threading.Event()

    def start(self) -> None:
        """启动后台线程并登记为「正在切片」（关闭程序时据此提示）。

        调用前请先连接好 ``taskCompleted`` / ``taskTerminated`` 等信号——
        快速任务可能立即完成，错过连接就会丢完成事件。
        """
        target = self._run_subprocess if self.worker_spec is not None else self._run
        _register_active(self, self._describe())
        self._thread = threading.Thread(
            target=self._run_registered, args=(target,), name="imagery-tile", daemon=True
        )
        try:
            self._thread.start()
        except Exception:
            _unregister_active(self)
            raise

    def _run_registered(self, target) -> None:
        """跑真正的执行函数，结束时（无论成败）注销登记。"""
        try:
            target()
        finally:
            _unregister_active(self)

    def _describe(self) -> str:
        """关闭提示里显示的任务描述：图层名 + 输出位置。"""
        try:
            name = self.job.layer.name()
        except Exception:  # noqa: BLE001 - 图层已删除时只影响提示文字
            name = "影像图层"
        return f"{name} → {self.job.output_dir}"

    def cancel(self) -> None:
        """请求取消。"""
        self._cancel_event.set()
        # 进程内回退路径：算法在切片间隙检查反馈上的取消标志
        if self.feedback is not None and not self.feedback.isCanceled():
            self.feedback.cancel()

    # ------------------------------------------------------------ 回调

    def _on_progress(self, value: float) -> None:
        self.progress_updated.emit(int(value))

    def _on_progress_text(self, text: str) -> None:
        """算法阶段性进度文字 → 主线程 UI。"""
        self.progress_text.emit(text)

    def _on_log_line(self, level: str, message: str) -> None:
        """渲染线程算法日志经队列投递到这里（主线程），转发给应用日志与 UI。"""
        self._log_line(level, message)

    def _log_line(self, level: str, message: str) -> None:
        """记录一行日志：写入应用日志（线程安全）并转发 :attr:`log_message`。"""
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

    # ------------------------------------------------------------ 执行

    def _run_subprocess(self) -> None:
        """子进程执行切片：等待期间主线程可以正常操作界面。"""
        spec = self.worker_spec
        job = self.job
        assert spec is not None  # start() 只在有 spec 时走这里
        try:
            self._log_line("INFO", "影像切片在后台进程中运行，可继续使用其他功能")
            result = run_worker_process(
                spec,
                self._cancel_event,
                WorkerCallbacks(
                    progress=self.progress_updated.emit,
                    text=self.progress_text.emit,
                    log=self._log_line,
                ),
            )
            if result.canceled:
                self._log_line("WARNING", "影像切片已取消")
                self.taskTerminated.emit()
                return
            if not result.ok:
                self.exception = RuntimeError(result.error or "影像切片失败")
                self._log_line("ERROR", f"影像切片失败：{result.error}")
                self.taskTerminated.emit()
                return
            if job.html:
                try:
                    self.viewer_html = write_image_tile_viewer(job)
                    self._log_line("INFO", f"网页查看器：{self.viewer_html}")
                except Exception as e:  # noqa: BLE001 - 查看器失败不影响瓦片结果
                    self.viewer_html = ""
                    self._log_line("ERROR", f"生成网页查看器失败：{e}")
            self.taskCompleted.emit()
        except Exception as e:  # noqa: BLE001
            self.exception = e
            self._log_line("ERROR", f"影像切片异常：{e}")
            self.taskTerminated.emit()
        finally:
            try:
                spec.cleanup()
            except Exception:  # noqa: BLE001 - 清理失败不影响结果
                logger.debug("清理切片临时目录失败", exc_info=True)

    def _run(self) -> None:
        """进程内回退路径（子进程准备失败时）。"""
        try:
            # 延迟 import：避免 service 模块顶层依赖 processing
            import processing

            job = self.job
            self._log_line("INFO", f"开始影像切片：{job.layer.name()}")
            self._log_line(
                "INFO",
                f"参数：缩放 {job.min_zoom}-{job.max_zoom}，"
                f"格式 {TILE_FORMAT_LABELS[job.tile_format]}，"
                f"瓦片 {job.tile_size}×{job.tile_size}px，"
                f"抗锯齿 {'开' if job.antialias else '关'}，"
                f"网页查看器 {'生成' if job.html else '不生成'}",
            )
            self._log_line("INFO", f"输出目录：{job.output_dir}")

            self.result_output = processing.run(
                "native:tilesxyzdirectory", job.to_native_params(), feedback=self.feedback
            )
            if self.feedback.failed:
                self.exception = RuntimeError("瓦片生成命令执行失败（详见任务日志）")
                self.taskTerminated.emit()
                return
            if job.html:
                try:
                    self.viewer_html = write_image_tile_viewer(job)
                    self._log_line("INFO", f"网页查看器：{self.viewer_html}")
                except Exception as e:  # noqa: BLE001 - 查看器失败不影响瓦片结果
                    self.viewer_html = ""
                    self._log_line("ERROR", f"生成网页查看器失败：{e}")
            self._log_line("INFO", "影像切片完成")
            self.taskCompleted.emit()
        except Exception as e:  # noqa: BLE001
            self.exception = e
            self._log_line("ERROR", f"影像切片异常：{e}")
            self.taskTerminated.emit()
