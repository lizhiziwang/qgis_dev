"""影像切片（MBTiles 输出）生成 Dialog。

按 ``native:tilesxyzmbtiles`` 把当前地图画布渲染为单个 MBTiles 文件（SQLite
内嵌 XYZ 瓦片金字塔，便于分发 / 离线使用）。耗时较长时在后台线程运行（普通
``threading.Thread``，不用 :class:`QgsTask`——见 task 模块说明），可取消。
"""

from __future__ import annotations

import os
from html import escape
from typing import Optional

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
from app.core.domain.imagery_tiles.service import (
    TILE_COUNT_WARN_THRESHOLD,
    estimate_tile_count,
)
from app.infra.logging import get_logger
from app.infra.messagebar import send
from app.ui.common.algo_desc import make_description_browser
from app.ui.common.base_dialog import BaseDialog
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
        self._init_ui()
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
        main.addWidget(param_panel, stretch=3)

        # 右侧算法描述（与工具树共用同一套描述逻辑，见 algo_desc）
        main.addWidget(make_description_browser("native:tilesxyzmbtiles"), stretch=2)
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
        self.loading = QProgressBar()
        self.loading.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.loading.setRange(0, 100)
        b_l.addWidget(self.loading, stretch=2)

        bottom = QHBoxLayout()
        bottom.addStretch()
        self.execute_btn = QPushButton("执行")
        self.execute_btn.clicked.connect(self._on_execute)
        bottom.addWidget(self.execute_btn)
        close = QPushButton("关闭")
        close.clicked.connect(self.close)
        bottom.addWidget(close)
        b_l.addLayout(bottom)
        layout.addLayout(b_l)

        self.setLayout(layout)

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

        # 3. 瓦片格式
        layout.addWidget(QLabel("瓦片格式"))
        self.format_combo = QComboBox()
        self.format_combo.addItems(TILE_FORMAT_LABELS)
        layout.addWidget(self.format_combo)
        layout.addSpacing(4)

        # 4. 抗锯齿
        self.antialias_check = QCheckBox("启用抗锯齿")
        self.antialias_check.setChecked(True)
        layout.addWidget(self.antialias_check)
        layout.addSpacing(4)

        # 5. 网页查看器（MBTiles 无内置 HTML，工具在输出旁生成预览目录）
        self.viewer_check = QCheckBox("生成网页查看器（HTML）")
        layout.addWidget(self.viewer_check)
        layout.addSpacing(4)

        # 6. 输出文件（.mbtiles）
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

    def _choose_output(self) -> None:
        """弹出保存对话框选择 .mbtiles 输出路径（取消不改）。"""
        path = save_file(self, title="保存 MBTiles", filter_="MBTiles 文件 (*.mbtiles)")
        if path:
            self.output_edit.setText(path)

    def _populate_layers(self) -> None:
        """把工程里的栅格图层填充进下拉框（图层对象存 itemData），保留当前选中项。"""
        current = self.input_combo.currentData()
        current_src = current.source() if current is not None else None
        self.input_combo.clear()
        for layer in self.project.mapLayers().values():
            if layer.type() == QgsMapLayer.RasterLayer:
                self.input_combo.addItem(layer.name(), layer)
                if current_src is not None and layer.source() == current_src:
                    self.input_combo.setCurrentIndex(self.input_combo.count() - 1)

    def showEvent(self, event) -> None:  # noqa: D401
        """每次显示都重刷图层列表：对话框实例被复用，首次打开后再加入的影像也能选到。"""
        super().showEvent(event)
        self._populate_layers()

    def _on_project_layers_changed(self, *_args) -> None:
        """工程加/删图层时同步下拉框（对话框打开状态下加入影像也能立刻选到）。"""
        self._populate_layers()

    def _estimate_tiles(self, layer, min_z: int, max_z: int) -> int:
        """把所选图层范围换算到 WGS84 并估算瓦片总数（供执行前预警）。"""
        try:
            wgs84 = QgsCoordinateReferenceSystem("EPSG:4326")
            ct = QgsCoordinateTransform(layer.crs(), wgs84, self.project.transformContext())
            return estimate_tile_count(ct.transformBoundingBox(layer.extent()), min_z, max_z)
        except Exception:  # noqa: BLE001
            logger.warning("估算瓦片数失败，跳过执行前预警", exc_info=True)
            return 0

    # ------------------------------------------------------------ 交互

    def _on_execute(self) -> None:
        layer = self.input_combo.currentData()
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

        job = ImageryMbtilesJob(
            layer=layer,
            output_file=out,
            min_zoom=min_z,
            max_zoom=max_z,
            tile_format=self.format_combo.currentIndex(),
            antialias=self.antialias_check.isChecked(),
            html=self.viewer_check.isChecked(),
        )

        # 瓦片数是耗时主导因素：超过阈值先弹窗确认，避免误开超大任务
        n_tiles = self._estimate_tiles(layer, min_z, max_z)
        if n_tiles > TILE_COUNT_WARN_THRESHOLD:
            ret = QMessageBox.question(
                self,
                "切片确认",
                f"预计生成约 {n_tiles:,} 个瓦片，耗时可能较长。\n"
                f"当前范围为图层全幅（{min_z}-{max_z} 级）。是否继续？",
            )
            if ret != QMessageBox.StandardButton.Yes:
                return

        self.execute_btn.setEnabled(False)
        self.loading.setValue(0)
        self.log_browser.clear()
        self.task = self.mbtiles_service.submit(job)
        if self.task is not None:
            # progress_updated / log_message 在 worker 线程发出 → 跨线程
            # QueuedConnection 投递到普通 Python 方法（投递到 QObject 槽在本环境
            # 原生崩溃 0xC0000409），再更新控件。
            self.task.progress_updated.connect(self._on_task_progress)
            self.task.log_message.connect(self._on_task_log)
            # 生命周期信号收尾（成功 / 失败或取消）
            self.task.taskCompleted.connect(self._on_task_completed)
            self.task.taskTerminated.connect(self._on_task_terminated)
            # 先连好信号再 start()：快速任务可能立即完成，错过连接会丢完成事件
            self.task.start()

    def _on_task_progress(self, value: int) -> None:
        """worker 线程进度跨线程投递到这里（普通 Python 方法，投递安全）后更新进度条。"""
        self.loading.setValue(int(value))

    def _on_task_log(self, level: str, message: str) -> None:
        """worker 线程日志跨线程投递到这里，逐行追加到任务日志框并滚到底部。"""
        self.log_browser.append(
            f'<p style="margin:0;color:{_LOG_COLORS.get(level, "#9a9a9a")};">'
            f"{escape(message)}</p>"
        )
        self.log_browser.moveCursor(QTextCursor.MoveOperation.End)

    def _on_task_completed(self) -> None:
        """任务成功结束（主线程回调）。"""
        self.execute_btn.setEnabled(True)
        self.loading.setValue(100)
        out = self.output_edit.text().strip()
        msg = f"MBTiles 切片完成，输出文件：{out}"
        if self.viewer_check.isChecked() and out:
            msg += f"\n网页查看器：{os.path.splitext(out)[0]}_preview/index.html"
        send("success", msg, duration=3)

    def _on_task_terminated(self) -> None:
        """任务被取消或失败（主线程回调）。"""
        self.execute_btn.setEnabled(True)
        self.loading.setValue(0)
        task = self.task
        if task is not None and task.exception is not None:
            logger.error("MBTiles 切片失败: %s", task.exception, exc_info=True)
            send("critical", f"MBTiles 切片失败：{task.exception}", duration=5)
        else:
            send("warning", "MBTiles 切片已取消", duration=3)
