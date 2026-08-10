"""MBTiles 影像切片后台任务。

在普通 :class:`threading.Thread` 里执行 ``native:tilesxyzmbtiles``，把当前地图
画布渲染为单个 MBTiles 文件。

.. note::
   不用 :class:`QgsTask`：与 directory 版同源的问题——该系列算法内部用
   :class:`QgsMapRendererSequentialJob` 渲染，经 :class:`QgsTaskManager` 后台
   线程运行会原生崩溃（退出码 127）；改走普通 ``threading.Thread`` 则稳定，
   主线程事件循环照常泵事件。

进度经 :attr:`progress_updated` 信号回报（``0~100`` int）；算法输出经
:attr:`log_message` 信号回报（``level, message``），同时写入应用日志
（日志面板 / ``logs/app.log``）。日志捕获经 :class:`ProcessingLogFeedback`
（见 :mod:`app.core.domain.imagery_tiles.feedback`）完成。
"""

from __future__ import annotations

import os
import sqlite3
import threading
from html import escape as html_escape

from PyQt6.QtCore import QObject, pyqtSignal

from app.core.domain.imagery_tiles.feedback import ProcessingLogFeedback
from app.infra.logging import get_logger

from .service import TILE_FORMAT_LABELS, ImageryMbtilesJob

logger = get_logger(__name__)


class CreateImageryMbtilesTask(QObject):
    """MBTiles 切片任务（普通线程执行）。

    信号面与 :class:`~app.core.domain.imagery_tiles.task.CreateImageryTileTask`
    一致：``progress_updated`` / ``log_message`` / ``taskCompleted`` /
    ``taskTerminated``，外加 ``exception`` / ``result_output`` / ``feedback``
    属性，对话框收尾逻辑照搬即可。
    """

    progress_updated = pyqtSignal(int)
    #: (level, message)；level ∈ {INFO, CMD, DEBUG, WARNING, ERROR}
    log_message = pyqtSignal(str, str)
    taskCompleted = pyqtSignal()
    taskTerminated = pyqtSignal()

    def __init__(self, description: str, job: ImageryMbtilesJob) -> None:
        super().__init__()
        self.job = job
        self.exception: object = None
        self.result_output: dict = {}
        self.feedback = ProcessingLogFeedback()
        self.feedback.progressChanged.connect(self._on_progress)
        # feedback.log_line 在渲染线程发出 → 投递到主线程的普通 Python 方法（安全）
        self.feedback.log_line.connect(self._on_log_line)
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """启动后台线程。

        调用前请先连接好 ``taskCompleted`` / ``taskTerminated`` 等信号——
        快速任务可能立即完成，错过连接就会丢完成事件。
        """
        self._thread = threading.Thread(
            target=self._run, name="imagery-mbtiles", daemon=True
        )
        self._thread.start()

    def cancel(self) -> None:
        """请求取消（算法在切片间隙检查 ``feedback.isCanceled()``）。"""
        if self.feedback is not None and not self.feedback.isCanceled():
            self.feedback.cancel()

    # ------------------------------------------------------------ 回调

    def _on_progress(self, value: float) -> None:
        self.progress_updated.emit(int(value))

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

    def _run(self) -> None:
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
                    self._log_line("INFO", f"网页查看器：{html_path}")
                except Exception as e:  # noqa: BLE001
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
        cx, cy = (minx + maxx) / 2.0, (miny + maxy) / 2.0
        start_zoom = (min_zoom + max_zoom) // 2
        attribution = html_escape("Created by QGIS")

        html_path = os.path.join(preview_dir, "index.html")
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(
                "<!DOCTYPE html>"
                "<html><head><meta charset=\"utf-8\"/>"
                f"<title>{html_escape(title)}</title>"
                "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1.0\">"
                "<link rel=\"stylesheet\" href=\"https://unpkg.com/leaflet@1.9.4/dist/leaflet.css\""
                "integrity=\"sha384-sHL9NAb7lN7rfvG5lfHpm643Xkcjzp4jFvuavGOndn6pjVqS6ny56CAt3nsEVT4H\""
                "crossorigin=\"\"/>"
                "<script src=\"https://unpkg.com/leaflet@1.9.4/dist/leaflet.js\""
                "integrity=\"sha384-cxOPjt7s7Iz04uaHJceBmS+qpjv2JkIHNVcuOrM+YHwZOmJGBXI00mdUXEq65HTH\""
                "crossorigin=\"\"></script>"
                "<style>body{margin:0}html,body,#map{width:100%;height:100%}</style>"
                "</head><body><div id=\"map\"></div><script>"
                f"var map=L.map('map',{{attributionControl:false}}).setView([{cy:.6f},{cx:.6f}],{start_zoom});"
                "L.control.attribution({prefix:false}).addTo(map);"
                f"L.tileLayer('{{z}}/{{x}}/{{y}}.{ext}',"
                f"{{minZoom:{min_zoom},maxZoom:{max_zoom},tms:false,attribution:'{attribution}'}})"
                ".addTo(map);"
                "</script></body></html>"
            )
        return html_path
