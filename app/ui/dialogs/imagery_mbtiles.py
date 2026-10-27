"""影像切片（MBTiles 输出）生成 Dialog。

按 ``native:tilesxyzmbtiles`` 把当前地图画布渲染为单个 MBTiles 文件（SQLite
内嵌 XYZ 瓦片金字塔，便于分发 / 离线使用）。耗时较长时在后台线程运行（普通
``threading.Thread``，不用 :class:`QgsTask`——见 task 模块说明），可取消。
"""

from __future__ import annotations

import os
import shutil
import uuid
from html import escape
from typing import Optional

from qgis.PyQt.QtCore import QTimer
from qgis.PyQt.QtGui import QTextCursor
from qgis.PyQt.QtWidgets import (
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsMapLayer,
    QgsProject,
)

from app.core.domain.imagery_mbtiles import ImageryMbtilesJob, ImageryMbtilesService
from app.core.domain.imagery_mbtiles.service import TILE_FORMAT_LABELS
from app.core.domain.imagery_tiles.isolation import (
    isolate_other_layers,
    release_layer_isolation,
)
from app.core.domain.imagery_tiles.service import (
    TILE_COUNT_WARN_THRESHOLD,
    clip_to_mercator_bounds,
    estimate_tile_count,
    extent_to_wgs84,
)
from app.core.domain.imagery_tiles.worker_runner import WorkerSpec, prepare_worker_job
from app.infra.logging import get_logger
from app.infra.messagebar import send
from app.infra.viewer_server import open_viewer
from app.ui.common.algo_desc import make_description_panel
from app.ui.common.base_dialog import BaseDialog, make_scrollable
from app.ui.common.layer_combo import combo_current_layer, populate_layer_combo
from app.ui.common.tile_progress import MetatileProgressTracker
from app.ui.common.widgets import save_file

logger = get_logger(__name__)

#: 任务日志按级别上色（QTextBrowser HTML 颜色）
_LOG_COLORS = {
    "INFO": "#9a9a9a",
    "CMD": "#5a8fc4",
    "DEBUG": "#777777",
    "WARNING": "#b58900",
    "ERROR": "#d63031",
}


class ImageryMbtilesDialog(BaseDialog):
    """MBTiles 影像切片 Dialog（后台任务执行）。"""

    def __init__(self, parent=None, title: str = "", dia_type: Optional[str] = None) -> None:
        super().__init__(parent, title)
        self.project = QgsProject.instance()
        self.task = None
        self.mbtiles_service = ImageryMbtilesService()
        #: 元瓦片计划（从算法日志解析，用于把百分比翻译成 n/m）
        self._progress = MetatileProgressTracker()
        #: 已生成的网页查看器 index.html；无则空串
        self._viewer_path = ""
        #: 图层隔离 token（开始执行时设置，任务收尾/启动失败时释放）
        self._isolation_token = ""
        #: 最近一次进度值（日志到达时用它刷新状态栏）
        self._current_progress = 0
        #: 当前选择的切片范围（WGS84），由「输出范围」下拉框决定
        self.out_extent_wgs84 = None
        self._init_ui()
        # 第一个元瓦片可能渲染数分钟、进度停在 0%：每秒刷新「已用」时间
        self._progress_timer = QTimer(self)
        self._progress_timer.setInterval(1000)
        self._progress_timer.timeout.connect(self._on_progress_tick)
        # ToolsDock 会复用同一对话框实例；工程图层变化时同步刷新下拉框，
        # 否则后加入的影像图层不会出现在输入列表里。
        self.project.layersAdded.connect(self._on_project_layers_changed)
        self.project.layersRemoved.connect(self._on_project_layers_changed)

    def _init_ui(self) -> None:
        layout = QVBoxLayout()

        # ---- 左右两部分：左参数 + 右算法描述 ----
        main = QHBoxLayout()

        param_panel = QWidget()
        self._init_parameters_panel(param_panel)
        main.addWidget(make_scrollable(param_panel), stretch=3)

        # 右侧算法描述（与工具树共用同一套描述逻辑，见 algo_desc）
        main.addWidget(make_description_panel("native:tilesxyzmbtiles"), stretch=1)
        layout.addLayout(main, stretch=1)

        # ---- 任务日志（实时显示算法输出）----
        layout.addWidget(QLabel("任务日志"))
        self.log_browser = QTextBrowser()
        self.log_browser.setMinimumHeight(110)
        self.log_browser.setMaximumHeight(150)
        self.log_browser.setReadOnly(True)
        layout.addWidget(self.log_browser)

        # 进度 + 按钮
        b_l = QVBoxLayout()
        b_l.setContentsMargins(0, 0, 0, 0)
        self.status_label = QLabel("就绪")
        self.status_label.setWordWrap(True)
        b_l.addWidget(self.status_label)
        self.loading = QProgressBar()
        self.loading.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.loading.setRange(0, 100)
        b_l.addWidget(self.loading, stretch=2)

        bottom = QHBoxLayout()
        self.viewer_btn = QPushButton("打开网页查看器")
        self.viewer_btn.setEnabled(False)
        self.viewer_btn.setToolTip(
            "用默认浏览器打开切片生成的 Leaflet 预览页（内置离线资源，无需联网）"
        )
        self.viewer_btn.clicked.connect(self._open_viewer)
        bottom.addWidget(self.viewer_btn)
        bottom.addStretch()
        self.execute_btn = QPushButton("执行")
        self.execute_btn.clicked.connect(self._on_execute)
        bottom.addWidget(self.execute_btn)
        self.cancel_btn = QPushButton("取消")
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.clicked.connect(self._on_cancel)
        bottom.addWidget(self.cancel_btn)
        close = QPushButton("关闭")
        close.clicked.connect(self.close)
        bottom.addWidget(close)
        b_l.addLayout(bottom)
        layout.addLayout(b_l)

        self.setLayout(layout)
        self.fit_height_to_content(param_panel)

    def _init_parameters_panel(self, parent) -> None:
        layout = QVBoxLayout(parent)
        layout.setSpacing(10)

        # 1. 输入影像图层（单选，仅栅格图层）
        layout.addWidget(QLabel("输入影像图层"))
        self.input_combo = QComboBox()
        self._populate_layers()
        layout.addWidget(self.input_combo)
        layout.addSpacing(4)

        # 2. 缩放等级范围
        layout.addWidget(QLabel("缩放等级"))
        zoom_row = QHBoxLayout()
        self.min_zoom_spin = QSpinBox()
        self.min_zoom_spin.setRange(0, 24)
        self.min_zoom_spin.setValue(0)
        self.max_zoom_spin = QSpinBox()
        self.max_zoom_spin.setRange(0, 24)
        self.max_zoom_spin.setValue(8)
        zoom_row.addWidget(QLabel("最小"))
        zoom_row.addWidget(self.min_zoom_spin)
        zoom_row.addWidget(QLabel("最大"))
        zoom_row.addWidget(self.max_zoom_spin)
        layout.addLayout(zoom_row)
        layout.addSpacing(4)

        # 3. 输出范围：默认图层全幅；也可只切当前地图视图（适合全球底图）
        layout.addWidget(QLabel("输出范围"))
        extent_row = QHBoxLayout()
        self.extent_text = QLineEdit()
        self.extent_text.setReadOnly(True)
        self.extent_text.setPlaceholderText("按所选图层 / 当前地图视图自动计算")
        extent_row.addWidget(self.extent_text, stretch=1)
        self.extent_edit = QComboBox()
        self.extent_edit.addItems(["图层范围", "当前画布范围"])
        self.extent_edit.setCurrentIndex(0)
        self.extent_edit.setToolTip(
            "图层范围：切所选图层的完整范围；当前画布范围：只切地图当前显示的区域"
        )
        self.extent_edit.currentTextChanged.connect(self._on_extent_changed)
        extent_row.addWidget(self.extent_edit)
        layout.addLayout(extent_row)
        layout.addSpacing(4)

        # 4. 瓦片格式
        layout.addWidget(QLabel("瓦片格式"))
        self.format_combo = QComboBox()
        self.format_combo.addItems(TILE_FORMAT_LABELS)
        layout.addWidget(self.format_combo)
        layout.addSpacing(4)

        # 5. 抗锯齿
        self.antialias_check = QCheckBox("启用抗锯齿")
        self.antialias_check.setChecked(True)
        layout.addWidget(self.antialias_check)
        layout.addSpacing(4)

        # 6. 只渲染所选图层：算法按画布渲染，其他可见图层（尤其在线底图、
        #    要素图层）会被逐元瓦片重复渲染，是切片慢的主因
        self.only_layer_check = QCheckBox("仅渲染所选图层（隐藏其他图层）")
        self.only_layer_check.setChecked(True)
        self.only_layer_check.setToolTip(
            "切片算法渲染的是整个地图画布。勾选后执行期间临时隐藏其他可见图层"
            "（在线底图、矢量叠加等），避免它们被烧进瓦片，通常能大幅提速；"
            "完成后自动恢复。取消勾选则按画布当前内容切片，可能很慢。"
        )
        layout.addWidget(self.only_layer_check)
        layout.addSpacing(4)

        # 7. 网页查看器（MBTiles 无内置 HTML，工具在输出旁生成预览目录）
        self.viewer_check = QCheckBox("生成网页查看器（HTML）")
        layout.addWidget(self.viewer_check)
        layout.addSpacing(4)

        # 8. 输出文件（.mbtiles）
        layout.addWidget(QLabel("输出文件（MBTiles）"))
        out_row = QHBoxLayout()
        self.output_edit = QLineEdit()
        self.output_edit.setPlaceholderText("选择输出 .mbtiles 文件")
        self.output_edit.setReadOnly(True)
        out_row.addWidget(self.output_edit)
        sel = QPushButton("选择")
        sel.clicked.connect(self._choose_output)
        out_row.addWidget(sel)
        layout.addLayout(out_row)

        layout.addStretch()

        # 图层切换 / 范围模式切换时同步范围预览与瓦片数估算
        self.input_combo.currentIndexChanged.connect(self._on_extent_changed)
        self._refresh_extent()

    def _choose_output(self) -> None:
        """弹出保存对话框选择 .mbtiles 输出路径（取消不改）。"""
        path = save_file(self, title="保存 MBTiles", filter_="MBTiles 文件 (*.mbtiles)")
        if path:
            self.output_edit.setText(path)

    def _populate_layers(self) -> None:
        """把工程里的栅格图层填充进下拉框（图层对象存 itemData），保留当前选中项。

        图层可能刚被移除、itemData 里的 wrapper 已失效，交给
        :func:`~app.ui.common.layer_combo.populate_layer_combo` 安全处理。
        """
        populate_layer_combo(
            self.input_combo,
            self.project.mapLayers().values(),
            layer_type=QgsMapLayer.RasterLayer,
        )

    def showEvent(self, event) -> None:  # noqa: D401
        """每次显示都重刷图层列表：对话框实例被复用，首次打开后再加入的影像也能选到。"""
        super().showEvent(event)
        self._populate_layers()
        # 画布可能已被平移/缩放，重新显示时刷新范围预览
        self._refresh_extent()
        # 复用实例：上次任务已收尾（self.task 为 None）时把按钮复位，避免残留灰色
        if self.task is None:
            self.execute_btn.setEnabled(True)
            self.cancel_btn.setEnabled(False)

    def _on_project_layers_changed(self, *_args) -> None:
        """工程加/删图层时同步下拉框（对话框打开状态下加入影像也能立刻选到）。"""
        self._populate_layers()

    def _canvas_extent_wgs84(self):
        """当前地图视图范围（换算到 WGS84）；无画布（无界面 / 测试）时返回 None。"""
        parent = self.parent()
        canvas = getattr(parent, "canvas", None) if parent is not None else None
        if canvas is None or not hasattr(canvas, "extent"):
            return None
        rect = canvas.extent()
        if rect is None or rect.isEmpty():
            return None
        try:
            src = canvas.mapSettings().destinationCrs()
        except Exception:  # noqa: BLE001
            src = self.project.crs()
        try:
            if src.isValid() and src.authid() != "EPSG:4326":
                ct = QgsCoordinateTransform(
                    src,
                    QgsCoordinateReferenceSystem("EPSG:4326"),
                    self.project.transformContext(),
                )
                rect = ct.transformBoundingBox(rect)
            return clip_to_mercator_bounds(rect)
        except Exception:  # noqa: BLE001
            logger.warning("当前画布范围换算到 WGS84 失败", exc_info=True)
            return None

    def _current_extent_wgs84(self, layer=None):
        """按「输出范围」下拉框解析切片范围；画布不可用时退回图层范围。"""
        if layer is None:
            layer = combo_current_layer(self.input_combo)
        if self.extent_edit.currentText() == "当前画布范围":
            rect = self._canvas_extent_wgs84()
            if rect is not None:
                return rect
        if layer is None:
            return None
        try:
            return extent_to_wgs84(layer)
        except Exception:  # noqa: BLE001
            logger.warning("计算切片范围失败", exc_info=True)
            return None

    def _on_extent_changed(self, *_args) -> None:
        self._refresh_extent()

    def _refresh_extent(self) -> None:
        """刷新范围显示；「当前画布范围」但无画布时提示已退回图层范围。"""
        ext = self._current_extent_wgs84()
        self.out_extent_wgs84 = ext
        if ext is None:
            self.extent_text.clear()
            return
        prefix = ""
        if (
            self.extent_edit.currentText() == "当前画布范围"
            and self._canvas_extent_wgs84() is None
        ):
            prefix = "无地图画布，按图层范围 "
        self.extent_text.setText(
            f"{prefix}{ext.xMinimum():.6f}, {ext.yMinimum():.6f}, "
            f"{ext.xMaximum():.6f}, {ext.yMaximum():.6f} (EPSG:4326)"
        )

    def _estimate_tiles(self, layer, min_z: int, max_z: int) -> int:
        """按当前选择的范围（图层 / 画布）估算瓦片总数（供执行前预警）。"""
        try:
            ext = self._current_extent_wgs84(layer)
            if ext is None:
                return 0
            return estimate_tile_count(ext, min_z, max_z)
        except Exception:  # noqa: BLE001
            logger.warning("估算瓦片数失败，跳过执行前预警", exc_info=True)
            return 0

    # ------------------------------------------------------------ 交互

    def _apply_layer_isolation(self, layer) -> None:
        """开始切片前临时隐藏其他可见图层（算法按画布渲染，见 isolation 模块）。"""
        self._release_layer_isolation()
        if not self.only_layer_check.isChecked():
            return
        token = f"imagery-mbtiles-{uuid.uuid4().hex}"
        try:
            hidden = isolate_other_layers(self.project, layer, token)
        except Exception:  # noqa: BLE001 - 隔离失败不影响切片，只是可能很慢
            logger.warning("MBTiles 切片：临时隐藏其他图层失败，按画布内容切片", exc_info=True)
            return
        self._isolation_token = token
        logger.info("MBTiles 切片：临时隐藏其他可见图层 %s", hidden)
        if hidden:
            self.log_browser.append(
                f'<p style="margin:0;color:{_LOG_COLORS["INFO"]};">'
                f"仅渲染所选图层：已临时隐藏 {len(hidden)} 个其他可见图层"
                f"（{escape('、'.join(hidden))}），完成后自动恢复</p>"
            )
        else:
            self.log_browser.append(
                f'<p style="margin:0;color:{_LOG_COLORS["INFO"]};">'
                "仅渲染所选图层：画布上没有其他可见图层</p>"
            )

    def _release_layer_isolation(self) -> None:
        """任务收尾 / 启动失败时恢复被临时隐藏的图层。"""
        token = self._isolation_token
        if not token:
            return
        self._isolation_token = ""
        try:
            release_layer_isolation(self.project, token)
        except Exception:  # noqa: BLE001 - 恢复失败只记录，别影响任务收尾
            logger.warning("MBTiles 切片：恢复图层可见性失败", exc_info=True)

    def _prepare_worker(self, job) -> "WorkerSpec | None":
        """准备子进程切片作业；失败返回 ``None``（退回进程内切片）。

        子进程读的是另存的临时工程，主进程的图层可见性完全不动，所以这里
        不再临时隐藏其他图层——「仅渲染所选图层」由子进程在自己的工程副本里
        执行。
        """
        try:
            spec = prepare_worker_job(
                job, "mbtiles", only_layer=self.only_layer_check.isChecked()
            )
        except Exception:  # noqa: BLE001 - 子进程准备失败退回进程内，不中断执行
            logger.warning(
                "MBTiles 切片：准备后台子进程异常，退回进程内切片（界面可能短暂卡顿）",
                exc_info=True,
            )
            spec = None
        if spec is None:
            logger.warning(
                "MBTiles 切片：准备后台子进程失败，退回进程内切片（界面可能短暂卡顿）"
            )
        return spec

    @staticmethod
    def _preview_dir_for(output_file: str) -> str:
        """与任务端一致的预览目录：``{文件名}_preview/``。"""
        return os.path.splitext(output_file)[0] + "_preview"

    def _prepare_output_file(self, output_file: str) -> bool:
        """覆盖已有 .mbtiles 前先确认并清理旧文件 / 预览目录。

        ``native:tilesxyzmbtiles`` 不会覆盖已存在的文件，会直接报
        ``Failed to create MBTiles file``；这里先确认，避免用户以为任务没跑。
        返回 ``False`` 表示用户取消。
        """
        if not os.path.isfile(output_file):
            return True
        answer = QMessageBox.question(
            self,
            "覆盖已有文件",
            f"文件已存在：\n{output_file}\n\n"
            "继续将删除该文件及其预览目录后重新生成。是否覆盖？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return False
        for candidate in (
            output_file,
            output_file + "-wal",
            output_file + "-shm",
        ):
            try:
                if os.path.exists(candidate):
                    os.remove(candidate)
            except OSError as exc:
                logger.error("删除旧 MBTiles 失败：%s（%s）", candidate, exc)
                send("critical", f"无法删除旧文件：{candidate}\n{exc}", duration=5)
                return False
        preview_dir = self._preview_dir_for(output_file)
        if os.path.isdir(preview_dir):
            try:
                shutil.rmtree(preview_dir)
            except OSError:
                logger.warning("删除旧预览目录失败：%s", preview_dir, exc_info=True)
        return True

    def _on_execute(self) -> None:
        layer = combo_current_layer(self.input_combo)
        if layer is None:
            send("warning", "请先在工程中加载一个影像图层", duration=3)
            return
        min_z = self.min_zoom_spin.value()
        max_z = self.max_zoom_spin.value()
        if min_z > max_z:
            send("warning", "最小等级不能大于最大等级", duration=3)
            return
        out = self.output_edit.text().strip()
        if not out:
            send("warning", "请先选择输出 MBTiles 文件路径", duration=3)
            return
        if not out.lower().endswith(".mbtiles"):
            out += ".mbtiles"
            self.output_edit.setText(out)

        job = ImageryMbtilesJob(
            layer=layer,
            output_file=out,
            min_zoom=min_z,
            max_zoom=max_z,
            tile_format=self.format_combo.currentIndex(),
            antialias=self.antialias_check.isChecked(),
            html=self.viewer_check.isChecked(),
            extent_wgs84=self._current_extent_wgs84(layer),
        )

        # 瓦片数是耗时主导因素：超过阈值先弹窗确认，避免误开超大任务
        n_tiles = self._estimate_tiles(layer, min_z, max_z)
        if n_tiles > TILE_COUNT_WARN_THRESHOLD:
            ret = QMessageBox.question(
                self,
                "切片确认",
                f"预计生成约 {n_tiles:,} 个瓦片，耗时可能较长。\n"
                f"当前范围为{self.extent_edit.currentText()}（{min_z}-{max_z} 级）。"
                "是否继续？",
            )
            if ret != QMessageBox.StandardButton.Yes:
                return

        # 先确认瓦片规模，再处理覆盖：用户因规模取消时不能已经把旧文件删了
        if not self._prepare_output_file(out):
            return

        self.execute_btn.setEnabled(False)
        self.cancel_btn.setEnabled(True)
        self.cancel_btn.setText("取消")
        self._progress.reset()
        self._current_progress = 0
        self._progress.start_clock()
        self._progress_timer.start()
        self._viewer_path = ""
        self.viewer_btn.setEnabled(False)
        self.status_label.setText("正在准备瓦片…")
        self.status_label.setToolTip("")
        self.loading.setRange(0, 0)  # 首批元瓦片到达前显示忙碌动画
        self.log_browser.clear()
        # 优先在独立子进程里切片：算法整段持有 GIL，放主进程任何线程里都会
        # 卡住界面（见 worker_runner 模块说明）。子进程准备失败才退回进程内。
        spec = self._prepare_worker(job)
        if spec is None:
            # 回退路径：算法按画布渲染，先隔离其他图层避免每块元瓦片重复渲染
            self._apply_layer_isolation(layer)
        try:
            task = self.mbtiles_service.submit(job, worker_spec=spec)
            if task is None:
                self._release_layer_isolation()
                if spec is not None:
                    spec.cleanup()
                self._reset_task_ui()
                send("critical", "MBTiles 切片任务创建失败", duration=5)
                return
            # progress_updated / log_message 在 worker 线程发出 → 跨线程
            # QueuedConnection 投递到普通 Python 方法（投递到 QObject 槽在本环境
            # 原生崩溃 0xC0000409），再更新控件。
            task.progress_updated.connect(self._on_task_progress)
            task.progress_text.connect(self._on_task_progress_text)
            task.log_message.connect(self._on_task_log)
            # 生命周期信号收尾（成功 / 失败或取消）
            task.taskCompleted.connect(self._on_task_completed)
            task.taskTerminated.connect(self._on_task_terminated)
            self.task = task
            # 先连好信号再 start()：快速任务可能立即完成，错过连接会丢完成事件
            # 子进程模式下界面全程可交互；回退到进程内时受算法持有 GIL 影响，
            # 切片期间界面会发顿（见 worker_runner 模块说明）。
            task.start()
        except Exception as exc:  # noqa: BLE001 - 启动失败也必须恢复按钮，否则永远灰着
            logger.error("启动 MBTiles 切片任务失败：%s", exc, exc_info=True)
            self._release_layer_isolation()
            if spec is not None:
                spec.cleanup()
            self._reset_task_ui()
            send("critical", f"MBTiles 切片启动失败：{exc}", duration=5)

    def _on_cancel(self) -> None:
        """请求取消正在跑的切片任务（子进程直接终止）。"""
        task = self.task
        if task is None:
            return
        self.mbtiles_service.cancel(task)
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.setText("取消中…")
        logger.info("MBTiles 切片：已请求取消")

    def _reset_task_ui(self) -> None:
        """复位「执行中」状态：执行按钮恢复可点、取消按钮禁用。"""
        self._progress_timer.stop()
        self._progress.stop_clock()
        self.task = None
        self.execute_btn.setEnabled(True)
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.setText("取消")
        self.loading.setRange(0, 100)

    def _on_task_progress(self, value: int) -> None:
        """worker 线程进度跨线程投递到这里（普通 Python 方法，投递安全）后更新进度条。"""
        self._apply_progress(value)

    def _on_progress_tick(self) -> None:
        """每秒刷新一次状态文字，让「已用」时间持续走动。"""
        if self.task is not None:
            self._apply_progress(self._current_progress)

    def _apply_progress(self, value: int) -> None:
        """把百分比同步到进度条与「元瓦片 n/m」状态文字。"""
        value = max(0, min(100, int(value)))
        self._current_progress = value
        if self.loading.maximum() == 0:
            if value <= 0:
                # 首个元瓦片完成前保持忙碌动画，避免看起来卡死
                self.status_label.setText(self._progress.describe(0))
                self.status_label.setToolTip(self._progress.tooltip())
                return
            self.loading.setRange(0, 100)
        self.loading.setValue(value)
        self.status_label.setText(self._progress.describe(value))
        self.status_label.setToolTip(self._progress.tooltip())

    def _on_task_progress_text(self, text: str) -> None:
        """算法给出的阶段性文字（若有）优先显示。"""
        self._progress.feed_progress_text(text)
        elapsed = self._progress.format_elapsed(self._progress.elapsed_seconds)
        self.status_label.setText(f"{text} · {self.loading.value()}% · 已用 {elapsed}")

    def _on_task_log(self, level: str, message: str) -> None:
        """worker 线程日志跨线程投递到这里，逐行追加到任务日志框并滚到底部。"""
        recognized = self._progress.feed_log(level, message)
        self.log_browser.append(
            f'<p style="margin:0;color:{_LOG_COLORS.get(level, "#9a9a9a")};">'
            f"{escape(message)}</p>"
        )
        self.log_browser.moveCursor(QTextCursor.MoveOperation.End)
        if recognized:
            self._apply_progress(self._current_progress)

    def _on_task_completed(self) -> None:
        """任务成功结束（主线程回调）。"""
        self._release_layer_isolation()
        task = self.task
        out = str(getattr(getattr(task, "job", None), "output_file", "") or "")
        preview = str(getattr(task, "preview_html", "") or "")
        self._reset_task_ui()
        self.loading.setValue(100)
        n = self._count_mbtiles_tiles(out)
        if n <= 0:
            # 算法报「完成」但库里没有瓦片：多为范围 / CRS 问题，明确提示
            self.status_label.setText("完成，但文件里没有瓦片")
            logger.warning("MBTiles 切片任务完成但没有瓦片：%s", out)
            send("warning", f"MBTiles 切片完成，但文件里没有瓦片：{out}", duration=5)
            return
        self.status_label.setText(f"完成：{n} 个瓦片")
        self.status_label.setToolTip(self._progress.tooltip())
        if preview and os.path.isfile(preview):
            self._viewer_path = preview
            self.viewer_btn.setEnabled(True)
        message = f"MBTiles 切片完成（{n} 个瓦片），输出文件：{out}"
        if self._viewer_path:
            message += f"\n网页查看器：{self._viewer_path}"
        send("success", message, duration=3)

    def _on_task_terminated(self) -> None:
        """任务被取消或失败（主线程回调）。"""
        self._release_layer_isolation()
        task = self.task
        exc = getattr(task, "exception", None)
        self._reset_task_ui()
        self.loading.setValue(0)
        if exc is not None:
            self.status_label.setText("切片失败")
            logger.error("MBTiles 切片失败: %s", exc, exc_info=True)
            send("critical", f"MBTiles 切片失败：{exc}", duration=5)
        else:
            self.status_label.setText("已取消")
            send("warning", "MBTiles 切片已取消", duration=3)

    def _open_viewer(self) -> None:
        """用默认浏览器打开 MBTiles 预览页。"""
        path = self._viewer_path
        if not path or not os.path.isfile(path):
            send("warning", "网页查看器不存在，请重新勾选「生成网页查看器」后执行", duration=3)
            self.viewer_btn.setEnabled(False)
            return
        if not open_viewer(path):
            send("warning", f"无法打开浏览器，请手动打开：{path}", duration=5)

    @staticmethod
    def _count_mbtiles_tiles(path: str) -> int:
        """读 MBTiles 的 ``tiles`` 表统计瓦片数；文件不存在 / 打不开返回 0。"""
        if not path or not os.path.isfile(path):
            return 0
        try:
            import sqlite3  # noqa: PLC0415 - 只在任务收尾时用一次

            with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as conn:
                row = conn.execute("SELECT count(*) FROM tiles").fetchone()
            return int(row[0]) if row else 0
        except Exception:  # noqa: BLE001 - 统计失败不影响切片结果本身
            logger.warning("读取 MBTiles 瓦片数失败：%s", path, exc_info=True)
            return 0

    def _stop_running_task(self) -> None:
        """关闭窗口时取消在跑的任务（实例会被工具树复用，别让任务继续给已关窗口发信号）。"""
        if self.task is not None:
            self.mbtiles_service.cancel(self.task)
            self._reset_task_ui()

    def reject(self) -> None:  # noqa: N802 —— Qt 命名
        """标题栏关闭 / Esc 走 ``reject()``（不发 ``closeEvent``），这里同样收尾。"""
        self._stop_running_task()
        super().reject()

    def closeEvent(self, event) -> None:  # noqa: N802 —— Qt 命名
        """「关闭」按钮走 ``close()``，收尾逻辑与 :meth:`reject` 一致。"""
        self._stop_running_task()
        super().closeEvent(event)
