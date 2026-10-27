"""MBTiles 影像切片后台任务。

与 XYZ 目录版同款，有两种执行方式：

1. **子进程（默认）**：把当前工程另存临时工程，交给独立进程跑
   ``native:tilesxyzmbtiles``，主进程界面不受算法持有 GIL 的影响；
2. **进程内（回退）**：子进程准备失败时退回普通 :class:`threading.Thread`。

.. note::
   不用 :class:`QgsTask`：该系列算法内部用
   :class:`QgsMapRendererSequentialJob` 渲染，经
   :class:`QgsTaskManager` 后台线程运行会原生崩溃（退出码 127）。

进度经 :attr:`progress_updated` 信号回报（``0~100`` int）；算法输出经
:attr:`log_message` 信号回报（``level, message``），同时写入应用日志
（日志面板 / ``logs/app.log``）。
"""

from __future__ import annotations

import os
import sqlite3
import threading

from qgis.PyQt.QtCore import QObject, pyqtSignal

from app.core.domain.imagery_tiles.active_tasks import (
    register as _register_active,
)
from app.core.domain.imagery_tiles.active_tasks import (
    unregister as _unregister_active,
)
from app.core.domain.imagery_tiles.feedback import ProcessingLogFeedback
from app.core.domain.imagery_tiles.worker_runner import (
    WorkerCallbacks,
    WorkerSpec,
    run_worker_process,
)
from app.infra.logging import get_logger

from .service import TILE_FORMAT_LABELS, ImageryMbtilesJob

logger = get_logger(__name__)


class CreateImageryMbtilesTask(QObject):
    """MBTiles 切片任务（子进程 / 普通线程执行）。

    信号面与 :class:`~app.core.domain.imagery_tiles.task.CreateImageryTileTask`
    一致：``progress_updated`` / ``log_message`` / ``taskCompleted`` /
    ``taskTerminated``，外加 ``exception`` / ``result_output`` / ``feedback``
    属性，对话框收尾逻辑照搬即可。
    """

    progress_updated = pyqtSignal(int)
    #: 算法 ``setProgressText`` 给出的文字（当前算法未使用，保留通用通道）
    progress_text = pyqtSignal(str)
    #: (level, message)；level ∈ {INFO, CMD, DEBUG, WARNING, ERROR}
    log_message = pyqtSignal(str, str)
    taskCompleted = pyqtSignal()
    taskTerminated = pyqtSignal()

    def __init__(
        self,
        description: str,
        job: ImageryMbtilesJob,
        worker_spec: WorkerSpec | None = None,
    ) -> None:
        super().__init__()
        self.job = job
        #: 子进程作业；为空表示走进程内回退路径
        self.worker_spec = worker_spec
        self.exception: object = None
        self.result_output: dict = {}
        #: 勾选网页查看器时，任务生成的 index.html 路径；未生成/失败为空串
        self.preview_html = ""
        self.feedback = ProcessingLogFeedback()
        self.feedback.progressChanged.connect(self._on_progress)
        # feedback.log_line 在渲染线程发出 → 投递到主线程的普通 Python 方法（安全）
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
            target=self._run_registered, args=(target,), name="imagery-mbtiles", daemon=True
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
        """关闭提示里显示的任务描述：图层名 + 输出文件。"""
        try:
            name = self.job.layer.name()
        except Exception:  # noqa: BLE001 - 图层已删除时只影响提示文字
            name = "影像图层"
        return f"{name} → {self.job.output_file}"

    def cancel(self) -> None:
        """请求取消（子进程直接终止；回退路径靠算法检查反馈上的取消标志）。"""
        self._cancel_event.set()
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
            self._log_line("INFO", "MBTiles 切片在后台进程中运行，可继续使用其他功能")
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
                self._log_line("WARNING", "MBTiles 切片已取消")
                self.taskTerminated.emit()
                return
            if not result.ok:
                self.exception = RuntimeError(result.error or "MBTiles 切片失败")
                self._log_line("ERROR", f"MBTiles 切片失败：{result.error}")
                self.taskTerminated.emit()
                return
            if job.html:
                try:
                    html_path = self._write_preview_html(job)
                    self.preview_html = html_path
                    self._log_line("INFO", f"网页查看器：{html_path}")
                except Exception as e:  # noqa: BLE001
                    self.preview_html = ""
                    self._log_line("ERROR", f"生成网页查看器失败：{e}")
            self.taskCompleted.emit()
        except Exception as e:  # noqa: BLE001
            self.exception = e
            self._log_line("ERROR", f"MBTiles 切片异常：{e}")
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
            self._log_line("INFO", f"开始 MBTiles 切片：{job.layer.name()}")
            self._log_line(
                "INFO",
                f"参数：缩放 {job.min_zoom}-{job.max_zoom}，"
                f"格式 {TILE_FORMAT_LABELS[job.tile_format]}，"
                f"瓦片 256×256px，抗锯齿 {'开' if job.antialias else '关'}",
            )
            self._log_line("INFO", f"输出文件：{job.output_file}")

            self.result_output = processing.run(
                "native:tilesxyzmbtiles", job.to_native_params(), feedback=self.feedback
            )
            if self.feedback.failed:
                self.exception = RuntimeError("瓦片生成命令执行失败（详见任务日志）")
                self.taskTerminated.emit()
                return
            if job.html:
                try:
                    html_path = self._write_preview_html(job)
                    self.preview_html = html_path
                    self._log_line("INFO", f"网页查看器：{html_path}")
                except Exception as e:  # noqa: BLE001
                    self.preview_html = ""
                    self._log_line("ERROR", f"生成网页查看器失败：{e}")
            self._log_line("INFO", "MBTiles 切片完成")
            self.taskCompleted.emit()
        except Exception as e:  # noqa: BLE001
            self.exception = e
            self._log_line("ERROR", f"MBTiles 切片异常：{e}")
            self.taskTerminated.emit()

    # ------------------------------------------------------------ 网页预览

    def _write_preview_html(self, job: ImageryMbtilesJob) -> str:
        """把 .mbtiles 里的瓦片解出成目录并生成 Leaflet 预览页。

        浏览器无法直接读取 SQLite 格式的 .mbtiles，所以预览复用 XYZ 目录版的
        方案：把瓦片按 ``{z}/{x}/{y}.png`` 解压到 ``{文件名}_preview/`` 目录，
        再写一个引用该目录的 Leaflet ``index.html``（相对路径，双击即可打开）。

        MBTiles 的 ``tile_row`` 存的是 **TMS** 行号（算法写入时经
        ``tile2tms``），解出到 XYZ 目录需换算：``xyz_y = 2^z - 1 - tms_y``。

        :return: 生成的 index.html 路径。
        """
        base = os.path.splitext(job.output_file)[0]
        preview_dir = base + "_preview"
        ext = "jpg" if job.tile_format == 1 else "png"

        con = sqlite3.connect(job.output_file)
        try:
            meta = dict(con.execute("SELECT name, value FROM metadata").fetchall())
            tiles = con.execute(
                "SELECT zoom_level, tile_column, tile_row, tile_data FROM tiles"
            ).fetchall()
        finally:
            con.close()

        for z, x, tms_y, data in tiles:
            xyz_y = (1 << z) - 1 - tms_y
            d = os.path.join(preview_dir, str(z), str(x))
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, f"{xyz_y}.{ext}"), "wb") as f:
                f.write(data)

        # 元数据里有 WGS84 bounds 与缩放范围，用于定位地图中心
        try:
            minx, miny, maxx, maxy = (float(v) for v in meta["bounds"].split(","))
        except (KeyError, ValueError):
            minx = miny = maxx = maxy = 0.0
        try:
            min_zoom = int(meta.get("minzoom", job.min_zoom))
            max_zoom = int(meta.get("maxzoom", job.max_zoom))
        except (TypeError, ValueError):
            min_zoom, max_zoom = job.min_zoom, job.max_zoom

        title = os.path.splitext(os.path.basename(job.output_file))[0]
        from app.core.domain.tile_viewer import write_tile_viewer  # noqa: PLC0415

        return write_tile_viewer(
            preview_dir,
            title=title,
            tile_template=f"{{z}}/{{x}}/{{y}}.{ext}",
            min_zoom=min_zoom,
            max_zoom=max_zoom,
            bounds=(minx, miny, maxx, maxy),
        )
