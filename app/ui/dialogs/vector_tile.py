"""矢量瓦片生成 Dialog。"""

from __future__ import annotations

import os
import shutil
from typing import Optional

from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsMapLayer,
    QgsProject,
    QgsRectangle,
)
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from app.core.domain.tiles import TileJob, TileService, write_vector_tile_viewer
from app.core.domain.tiles.inspect import inspect_vector_tiles
from app.infra.logging import get_logger
from app.infra.messagebar import send
from app.infra.viewer_server import open_viewer
from app.ui.common.algo_desc import make_description_panel
from app.ui.common.base_dialog import BaseDialog
from app.ui.common.layer_combo import layer_source, live_layer
from app.ui.common.widgets import CenteredCheckDelegate, open_file_dir
from app.ui.dialogs.vector_tile_advanced import (
    DEFAULT_TEMPLATE,
    VectorTileAdvancedDialog,
    VectorTileAdvancedOptions,
    template_is_valid,
)
from app.ui.dialogs.vector_tile_batch import VectorTileBatchDialog

logger = get_logger(__name__)

#: 预估瓦片数超过该值时，执行前弹窗确认（默认最大等级 14，大范围数据可能极慢）
TILE_COUNT_WARN_THRESHOLD = 20_000


class VectorTileDialog(BaseDialog):
    """矢量瓦片生成 Dialog。"""

    def __init__(self, parent=None, title: str = "", dia_type: Optional[str] = None) -> None:
        super().__init__(parent, title)
        self.dia_type = dia_type
        if not self.dia_type:
            send("warning", "功能待开发", duration=3)
            self.close()
            return
        self.project = QgsProject.instance()
        # ToolsDock 复用同一对话框实例；工程图层变化时同步刷新列表，
        # 否则后加入的矢量图层不会出现在输入列表里。
        self.project.layersAdded.connect(self._on_project_layers_changed)
        self.project.layersRemoved.connect(self._on_project_layers_changed)
        self.select_layers: list = []
        self.out_extent: QgsRectangle | None = None
        self._extent_authid = ""
        self.task = None
        self.tile_service = TileService()
        #: 高级选项（模板以主面板输入框为准，这里存清空 / 查看器开关）
        self._advanced = VectorTileAdvancedOptions()
        #: 待执行队列 [(TileJob, 显示名)] 与整体进度计数
        self._queue: list[tuple[TileJob, str]] = []
        self._queue_total = 0
        self._queue_index = 0
        self._queue_tiles = 0
        self._queue_completed = 0
        self._cancel_requested = False
        self._viewer_path = ""
        #: 本轮生成但 pbf 里没有任何要素的任务标签（低缩放级别会简化掉面/线）
        self._no_feature_runs: list[str] = []
        #: 本轮扫描到的要素总数
        self._feature_count = 0
        self._init_ui()

    # ============================================================== UI

    def _init_ui(self) -> None:
        layout = QVBoxLayout()

        # ---- 左右两部分：左参数 + 右算法描述 ----
        main = QHBoxLayout()

        param_panel = QWidget()
        self._init_parameters_panel(param_panel)
        main.addWidget(param_panel, stretch=3)

        # 右侧算法描述（与工具树共用同一套描述逻辑，见 algo_desc）
        main.addWidget(make_description_panel(self.dia_type), stretch=1)
        layout.addLayout(main, stretch=1)

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
            "用默认浏览器打开「高级」里生成的 Leaflet + VectorGrid 页面"
            "（建议通过本地 HTTP 服务打开）"
        )
        self.viewer_btn.clicked.connect(self._open_viewer)
        bottom.addWidget(self.viewer_btn)
        self.advanced_btn = QPushButton("高级…")
        self.advanced_btn.setToolTip("文件模板 / 清空输出目录 / 生成网页查看器")
        self.advanced_btn.clicked.connect(self._open_advanced)
        bottom.addWidget(self.advanced_btn)
        self.batch_btn = QPushButton("批量处理…")
        self.batch_btn.setToolTip("勾选多个图层，每个图层输出到独立子目录")
        self.batch_btn.clicked.connect(self._open_batch)
        bottom.addWidget(self.batch_btn)
        bottom.addStretch()
        self.cancel_btn = QPushButton("取消")
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.clicked.connect(self._on_cancel)
        bottom.addWidget(self.cancel_btn)
        self.execute_btn = QPushButton("执行")
        self.execute_btn.clicked.connect(self._on_execute)
        bottom.addWidget(self.execute_btn)
        close = QPushButton("关闭")
        close.clicked.connect(self.close)
        bottom.addWidget(close)
        b_l.addLayout(bottom)
        layout.addLayout(b_l)

        self.setLayout(layout)
        self.fit_height_to_content(getattr(self, "_param_content", param_panel), extra=40)

    def _init_parameters_panel(self, parent) -> None:
        layout = QVBoxLayout(parent)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        content = QWidget()
        sl = QVBoxLayout(content)

        # 1. 文件模版
        row = QVBoxLayout()
        row.setSpacing(8)
        row.addWidget(QLabel("文件模版"))
        h = QHBoxLayout()
        self.xyz_temp = QLineEdit(DEFAULT_TEMPLATE)
        h.addWidget(self.xyz_temp)
        row.addLayout(h)
        sl.addLayout(row)
        sl.addSpacing(15)

        # 2. 输入图层
        row = QVBoxLayout()
        row.setSpacing(8)
        row.addWidget(QLabel("输入图层"))
        self.list_widget = QListWidget()
        self.list_widget.setItemDelegate(CenteredCheckDelegate(self.list_widget))
        self.list_widget.setFixedHeight(130)
        # 勾选状态变化统一走 itemChanged：点击复选框、键盘空格、全选都能同步；
        # 只接 itemClicked 时键盘勾选会漏掉，执行时 LAYERS 为空、静默无输出。
        self.list_widget.itemChanged.connect(self._on_layer_item_changed)
        self._populate_layers()
        row.addWidget(self.list_widget)
        sl.addLayout(row)
        sl.addSpacing(15)

        # 3. 最小/最大等级
        #    默认最大 14：面/线要素在低缩放级别会被抽稀到 0 个，
        #    z0–3 的小范围面数据常常生成「有图层、没有要素」的空 pbf。
        for label, default, attr in (
            ("最小等级", 0, "min_zoom_spin"),
            ("最大等级", 14, "max_zoom_spin"),
        ):
            row = QVBoxLayout()
            row.setSpacing(8)
            row.addWidget(QLabel(label))
            h = QHBoxLayout()
            spin = QSpinBox()
            spin.setRange(0, 20)
            spin.setValue(default)
            setattr(self, attr, spin)
            h.addWidget(spin)
            row.addLayout(h)
            sl.addLayout(row)
            sl.addSpacing(15)
        self.max_zoom_spin.setToolTip(
            "面 / 线要素在低缩放级别会被简化掉，可能生成不含要素的空瓦片。\n"
            "小范围数据建议最大等级 ≥ 14。"
        )

        # 4. 输出范围
        row = QVBoxLayout()
        row.setSpacing(8)
        row.addWidget(QLabel("输出范围"))
        h = QHBoxLayout()
        self.extent_text = QLineEdit()
        self.extent_text.setReadOnly(True)
        h.addWidget(self.extent_text, stretch=8)
        self.extent_edit = QComboBox()
        self.extent_edit.addItems(["图层范围", "当前画布范围"])
        self.extent_edit.setCurrentIndex(-1)
        self.extent_edit.currentTextChanged.connect(self._on_extent_changed)
        h.addWidget(self.extent_edit, stretch=1)
        row.addLayout(h)
        sl.addLayout(row)
        sl.addSpacing(15)

        # 5. 输出目录
        row = QVBoxLayout()
        row.setSpacing(8)
        row.addWidget(QLabel("输出目录"))
        h = QHBoxLayout()
        self.output_edit = QLineEdit()
        self.output_edit.setPlaceholderText("选择输出目录（不存在会自动创建）")
        self.output_edit.setReadOnly(True)
        h.addWidget(self.output_edit)
        sel = QPushButton("选择")
        sel.clicked.connect(self._choose_output_dir)
        h.addWidget(sel)
        row.addLayout(h)
        sl.addLayout(row)

        sl.addStretch()
        self._param_content = content
        scroll.setWidget(content)
        layout.addWidget(scroll)

    def _choose_output_dir(self) -> None:
        path = open_file_dir(self, is_dir=True, title="选择矢量瓦片输出目录")
        if path:
            self.output_edit.setText(path)

    # ------------------------------------------------------------ 图层列表

    def _populate_layers(self) -> None:
        """把工程里的矢量图层填充进列表（勾选状态按 source 保留）。

        图层可能刚被移除（``layersRemoved`` 触发刷新时 wrapper 已销毁），
        一律经 :func:`~app.ui.common.layer_combo.live_layer` 过滤，避免
        ``RuntimeError: wrapped C/C++ object ... has been deleted``。
        """
        # 记录当前勾选图层的 source（比 name 稳定，重名也安全）
        checked_sources = {
            src
            for src in (layer_source(layer) for layer in self.select_layers)
            if src
        }
        self.list_widget.blockSignals(True)
        try:
            self.list_widget.clear()
            for layer in list(self.project.mapLayers().values()):
                layer = live_layer(layer)
                if layer is None or layer.type() != QgsMapLayer.VectorLayer:
                    continue
                try:
                    name, source = layer.name(), layer.source()
                except (AttributeError, RuntimeError):
                    continue
                item = QListWidgetItem(name, self.list_widget)
                item.setData(Qt.ItemDataRole.UserRole, source)
                item.setToolTip(source)
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                item.setCheckState(
                    Qt.CheckState.Checked
                    if source in checked_sources
                    else Qt.CheckState.Unchecked
                )
        finally:
            self.list_widget.blockSignals(False)
        self._sync_selected_layers()

    def _sync_selected_layers(self) -> None:
        """按列表勾选状态重建 ``select_layers``（保持工程图层顺序）。"""
        checked = {
            str(self.list_widget.item(row).data(Qt.ItemDataRole.UserRole) or "")
            for row in range(self.list_widget.count())
            if self.list_widget.item(row).checkState() == Qt.CheckState.Checked
        }
        selected = []
        for layer in list(self.project.mapLayers().values()):
            layer = live_layer(layer)
            if layer is None or layer.type() != QgsMapLayer.VectorLayer:
                continue
            if layer_source(layer) in checked:
                selected.append(layer)
        self.select_layers = selected
        # 首次填充时「输出范围」下拉框还没构造好（参数面板按顺序创建），跳过
        extent_combo = getattr(self, "extent_edit", None)
        if extent_combo is not None and extent_combo.currentText() == "图层范围":
            self._on_extent_changed("图层范围")

    def _on_layer_item_changed(self, _item) -> None:
        self._sync_selected_layers()

    def showEvent(self, event) -> None:  # noqa: D401
        """每次显示都重刷图层列表：对话框实例被复用，首次打开后再加入的图层也能选到。"""
        super().showEvent(event)
        self._populate_layers()
        if self.task is None and not self._queue:
            self.execute_btn.setEnabled(True)
            self.cancel_btn.setEnabled(False)
            self.advanced_btn.setEnabled(True)
            self.batch_btn.setEnabled(True)

    def _on_project_layers_changed(self, *_args) -> None:
        """工程加/删图层时同步列表（对话框打开状态下加入图层也能立刻选到）。"""
        self._populate_layers()

    # ------------------------------------------------------------ 范围

    def _project_authid(self) -> str:
        crs = self.project.crs()
        if crs.isValid():
            return crs.authid()
        for layer in list(self.project.mapLayers().values()):
            layer = live_layer(layer)
            if layer is None or layer.type() != QgsMapLayer.VectorLayer:
                continue
            try:
                layer_crs = layer.crs()
            except (AttributeError, RuntimeError):
                continue
            if layer_crs.isValid():
                return layer_crs.authid()
        return "EPSG:4490"

    def _combined_extent(self, layers) -> tuple[QgsRectangle | None, str]:
        """把多个图层范围换算到同一 CRS 后合并，返回 ``(范围, authid)``。"""
        layers = [layer for layer in (live_layer(item) for item in layers) if layer is not None]
        if not layers:
            return None, self._project_authid()
        target = self.project.crs()
        if not target.isValid():
            try:
                target = layers[0].crs()
            except (AttributeError, RuntimeError):
                target = None
        authid = target.authid() if target is not None and target.isValid() else self._project_authid()
        combined: QgsRectangle | None = None
        for layer in layers:
            try:
                layer_crs = layer.crs()
                ext = layer.extent()
            except (AttributeError, RuntimeError):
                logger.warning("合并范围：图层已失效，跳过", exc_info=True)
                continue
            try:
                if layer_crs.isValid() and target is not None and target.isValid() and layer_crs != target:
                    ext = QgsCoordinateTransform(
                        layer_crs, target, self.project.transformContext()
                    ).transformBoundingBox(ext)
            except Exception:  # noqa: BLE001 - 单个图层换算失败时按原范围合并
                logger.warning("合并范围：图层坐标系换算失败 %s", layer.name(), exc_info=True)
            combined = ext if combined is None else combined.combineExtentWith(ext)
        return combined, authid

    def _canvas_extent(self) -> QgsRectangle | None:
        parent = self.parent()
        canvas = getattr(parent, "canvas", None) if parent is not None else None
        if canvas is not None and hasattr(canvas, "extent"):
            return canvas.extent()
        return None

    def _on_extent_changed(self, value: str) -> None:
        try:
            if value == "当前画布范围":
                rect = self._canvas_extent()
                if rect is not None:
                    self.out_extent = rect
                    self._extent_authid = self._project_authid()
                    self.extent_text.setText(rect.toString())
                    return
            # 「图层范围」或未选择时：用当前勾选图层合并；没有勾选就清空
            rect, authid = self._combined_extent(self.select_layers)
            self.out_extent = rect
            self._extent_authid = authid
            if rect is not None:
                self.extent_text.setText(rect.toString())
            else:
                self.extent_text.clear()
        except Exception:  # noqa: BLE001
            logger.warning("计算输出范围失败", exc_info=True)

    def _resolve_extent(self, layers) -> tuple[tuple[float, float, float, float, str] | None, str]:
        """执行前确定最终范围；返回 ``((xmin,xmax,ymin,ymax,authid), 错误信息)``。"""
        if self.extent_edit.currentText() == "当前画布范围":
            rect = self._canvas_extent()
            if rect is not None:
                return (
                    (rect.xMinimum(), rect.xMaximum(), rect.yMinimum(), rect.yMaximum(),
                     self._project_authid()),
                    "",
                )
            # 没有画布（无界面 / 测试）时退回图层范围
        rect, authid = self._combined_extent(layers)
        if rect is None:
            rect = self.out_extent
            authid = self._extent_authid or self._project_authid()
        if rect is None:
            return None, "无法确定输出范围，请勾选图层或选择「当前画布范围」"
        if rect.isNull() or rect.isEmpty():
            return None, "输出范围为空，请检查勾选图层是否有有效几何"
        return (
            (rect.xMinimum(), rect.xMaximum(), rect.yMinimum(), rect.yMaximum(), authid),
            "",
        )

    # ------------------------------------------------------------ 执行

    def _checked_layers(self) -> list:
        """当前勾选且仍有效的图层（移除中的图层 wrapper 可能已失效）。"""
        return [
            layer
            for layer in (live_layer(item) for item in self.select_layers)
            if layer is not None
        ]

    @staticmethod
    def _estimate_tiles(
        extent: tuple[float, float, float, float, str],
        min_zoom: int,
        max_zoom: int,
    ) -> int:
        """把作业范围换算到 WGS84 后估算瓦片数；失败返回 0（不阻断执行）。"""
        from app.core.domain.imagery_tiles.service import estimate_tile_count  # noqa: PLC0415

        xmin, xmax, ymin, ymax, authid = extent
        rect = QgsRectangle(float(xmin), float(ymin), float(xmax), float(ymax))
        try:
            src = QgsCoordinateReferenceSystem(str(authid or "EPSG:4326"))
            dst = QgsCoordinateReferenceSystem("EPSG:4326")
            if src.isValid() and src != dst:
                rect = QgsCoordinateTransform(
                    src, dst, QgsProject.instance().transformContext()
                ).transformBoundingBox(rect)
        except Exception:  # noqa: BLE001 - 估算失败不阻止切片
            logger.warning("估算矢量瓦片数失败，跳过执行前预警", exc_info=True)
            return 0
        return estimate_tile_count(rect, int(min_zoom), int(max_zoom))

    def _current_template(self) -> str:
        return self.xyz_temp.text().strip() or DEFAULT_TEMPLATE

    def _validate_options(self) -> str:
        """与图层勾选无关的通用校验（批量处理允许在主面板一个都不勾）。"""
        if self.min_zoom_spin.value() > self.max_zoom_spin.value():
            return "最小等级不能大于最大等级"
        if not template_is_valid(self._current_template()):
            return "文件模板必须包含 {z}、{x}、{y}，例如 {z}/{x}/{y}.pbf"
        return ""

    def _validate_common(self) -> str:
        """执行前的通用校验，返回错误文本（空串表示通过）。"""
        if not self._checked_layers():
            return "请至少勾选一个输入图层"
        return self._validate_options()

    def _on_execute(self) -> None:
        if self.task is not None or self._queue:
            send("warning", "已有矢量瓦片任务正在执行", duration=3)
            return
        error = self._validate_common()
        if error:
            send("warning", error, duration=4)
            return
        out_dir = self.output_edit.text().strip()
        if not out_dir:
            send("warning", "请先选择输出目录", duration=3)
            return
        extent, error = self._resolve_extent(self._checked_layers())
        if extent is None:
            send("warning", error, duration=4)
            return

        template = self._current_template()
        self.xyz_temp.setText(template)
        layers = [live_layer(layer) for layer in self.select_layers]
        layers = [layer for layer in layers if layer is not None]
        if not layers:
            send("warning", "勾选的图层已失效，请重新勾选", duration=4)
            return
        job = TileJob(
            output_dir=out_dir,
            xyz_template=template,
            layer_sources=[layer.source() for layer in layers],
            layer_names=tuple(layer.name() for layer in layers),
            min_zoom=self.min_zoom_spin.value(),
            max_zoom=self.max_zoom_spin.value(),
            extent=extent,
        )
        self._start_jobs([(job, "、".join(layer.name() for layer in layers))])

    def _open_advanced(self) -> None:
        """打开高级设置（文件模板 / 清空输出目录 / 网页查看器）。"""
        if self.task is not None or self._queue:
            send("warning", "任务执行中，暂时不能修改高级设置", duration=3)
            return
        current = VectorTileAdvancedOptions(
            template=self._current_template(),
            clear_output=self._advanced.clear_output,
            viewer=self._advanced.viewer,
        )
        dialog = VectorTileAdvancedDialog(self, current)
        accepted = dialog.exec()
        options = dialog.options() if accepted else None
        dialog.deleteLater()
        if options is None:
            return
        self._advanced = options
        self.xyz_temp.setText(options.template)
        logger.info(
            "矢量瓦片高级设置：模板=%s 清空=%s 查看器=%s",
            options.template,
            options.clear_output,
            options.viewer,
        )

    def _open_batch(self) -> None:
        """打开批量处理：每个勾选图层输出到独立子目录并顺序执行。"""
        if self.task is not None or self._queue:
            send("warning", "任务执行中，暂时不能启动批量处理", duration=3)
            return
        error = self._validate_options()
        if error:
            send("warning", error, duration=4)
            return
        layers = [
            layer
            for layer in (
                live_layer(item) for item in list(self.project.mapLayers().values())
            )
            if layer is not None and layer.type() == QgsMapLayer.VectorLayer
        ]
        if not layers:
            send("warning", "工程中没有矢量图层", duration=3)
            return
        dialog = VectorTileBatchDialog(
            self,
            layers,
            preselected=self._checked_layers(),
            output_root=self.output_edit.text().strip(),
        )
        accepted = dialog.exec()
        targets = dialog.selected_targets() if accepted else []
        root = dialog.output_root() if accepted else ""
        use_layer_extent = dialog.use_layer_extent() if accepted else True
        dialog.deleteLater()
        if not targets:
            return

        template = self._current_template()
        self.xyz_temp.setText(template)
        fallback_extent, fallback_error = self._resolve_extent(
            [target.layer for target in targets]
        )
        jobs: list[tuple[TileJob, str]] = []
        for target in targets:
            layer = live_layer(target.layer)
            if layer is None:
                send("warning", "批量列表中有图层已失效，请重新打开批量处理", duration=4)
                return
            try:
                name, source = layer.name(), layer.source()
            except (AttributeError, RuntimeError):
                send("warning", "批量列表中有图层已失效，请重新打开批量处理", duration=4)
                return
            if use_layer_extent:
                try:
                    rect = layer.extent()
                    layer_crs = layer.crs()
                except (AttributeError, RuntimeError):
                    send("warning", f"图层「{name}」已失效", duration=4)
                    return
                authid = layer_crs.authid() if layer_crs.isValid() else self._project_authid()
                if rect.isNull() or rect.isEmpty():
                    extent = fallback_extent
                else:
                    extent = (
                        rect.xMinimum(), rect.xMaximum(),
                        rect.yMinimum(), rect.yMaximum(), authid,
                    )
            else:
                extent = fallback_extent
            if extent is None:
                send("warning", f"图层「{name}」无法确定输出范围：{fallback_error}", duration=4)
                return
            jobs.append((
                TileJob(
                    output_dir=os.path.join(root, target.subdir),
                    xyz_template=template,
                    layer_sources=[source],
                    layer_names=(name,),
                    min_zoom=self.min_zoom_spin.value(),
                    max_zoom=self.max_zoom_spin.value(),
                    extent=extent,
                ),
                name,
            ))
        self._start_jobs(jobs)

    def _start_jobs(self, jobs: list[tuple[TileJob, str]]) -> None:
        """校验输出目录、确认瓦片规模、按需清空，然后启动顺序执行队列。"""
        if not jobs:
            return
        # 默认最大等级 14 在大范围数据上可能产生数十万瓦片，先估算并确认
        n_tiles = sum(
            self._estimate_tiles(job.extent, job.min_zoom, job.max_zoom)
            for job, _label in jobs
        )
        if n_tiles > TILE_COUNT_WARN_THRESHOLD:
            answer = QMessageBox.question(
                self,
                "切片确认",
                f"当前设置预计生成约 {n_tiles:,} 个瓦片，耗时可能很长。\n"
                f"缩放级别 {jobs[0][0].min_zoom}-{jobs[0][0].max_zoom}。是否继续？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        dirs = [job.output_dir for job, _label in jobs]
        try:
            for path in dirs:
                os.makedirs(path, exist_ok=True)
        except OSError as exc:
            send("critical", f"无法创建输出目录：{exc}", duration=5)
            return

        if self._advanced.clear_output and not self._confirm_clear(dirs):
            return
        if self._advanced.clear_output:
            for path in dirs:
                self._clear_dir_contents(path)

        self._queue = list(jobs)
        self._queue_total = len(jobs)
        self._queue_index = 0
        self._queue_tiles = 0
        self._queue_completed = 0
        self._cancel_requested = False
        self._viewer_path = ""
        self._no_feature_runs = []
        self._feature_count = 0
        self.viewer_btn.setEnabled(False)
        self.execute_btn.setEnabled(False)
        self.cancel_btn.setEnabled(True)
        self.cancel_btn.setText("取消")
        self.advanced_btn.setEnabled(False)
        self.batch_btn.setEnabled(False)
        self.loading.setRange(0, 0)
        self._start_next_job()

    def _start_next_job(self) -> None:
        if self._cancel_requested:
            self._finish_queue(cancelled=True)
            return
        if self._queue_index >= len(self._queue):
            self._finish_queue()
            return
        job, label = self._queue[self._queue_index]
        index = self._queue_index
        self.status_label.setText(
            f"第 {index + 1}/{self._queue_total} 套：{label} · 正在准备瓦片…"
        )
        self.status_label.setToolTip(job.output_dir)
        self.loading.setRange(0, 0)
        try:
            task = self.tile_service.create(job)
            # 先连信号再提交：极快的任务可能在 addTask 后立即完成
            task.progress_updated.connect(self._on_task_progress)
            task.progress_text.connect(self._on_task_progress_text)
            task.taskCompleted.connect(self._on_task_completed)
            task.taskTerminated.connect(self._on_task_terminated)
            self.task = task
            self.tile_service.start(task)
        except Exception as exc:  # noqa: BLE001
            logger.error("启动矢量瓦片任务失败：%s", exc, exc_info=True)
            self.task = None
            self._finish_queue(failed=f"启动失败：{exc}")

    def _on_task_progress(self, value: int) -> None:
        if self.loading.minimum() == 0 and self.loading.maximum() == 0:
            self.loading.setRange(0, 100)
        value = max(0, min(100, int(value)))
        overall = (
            (self._queue_index + value / 100.0) / max(1, self._queue_total) * 100.0
        )
        self.loading.setValue(int(round(overall)))
        self.status_label.setText(self._job_progress_text(value))

    def _job_progress_text(self, value: int) -> str:
        """当前这套瓦片的「缩放级别 + 百分比」说明（前面带批处理序号）。"""
        job, label = self._queue[self._queue_index]
        total_zooms = max(1, job.max_zoom - job.min_zoom + 1)
        if value >= 100:
            level = total_zooms
        else:
            level = min(total_zooms, int(value / 100.0 * total_zooms) + 1)
        zoom = job.min_zoom + level - 1
        prefix = (
            f"第 {self._queue_index + 1}/{self._queue_total} 套："
            if self._queue_total > 1
            else ""
        )
        return (
            f"{prefix}{label} · 缩放级别 {zoom}（{level}/{total_zooms}）"
            f" · {value}%"
        )

    def _on_task_progress_text(self, text: str) -> None:
        self.status_label.setText(f"{text} · {self.loading.value()}%")

    def _on_task_completed(self) -> None:
        task = self.task
        if task is None:
            return
        self.task = None
        job = task.job
        label = self._queue[self._queue_index][1]
        count = self._count_pbf(job.output_dir)
        self._queue_tiles += count
        self._queue_completed += 1
        if count <= 0:
            logger.warning("矢量瓦片任务完成但没有瓦片：%s", job.output_dir)
        # 读取 pbf 实际图层名（查看器样式键）与要素数（判断是否空白）
        info = None
        if count > 0:
            try:
                info = inspect_vector_tiles(job.output_dir, job.xyz_template)
            except Exception:  # noqa: BLE001 - 检查失败不影响切片结果
                logger.warning("读取矢量瓦片统计失败：%s", job.output_dir, exc_info=True)
        if info is not None:
            self._feature_count += info.feature_count
            if not info.has_features:
                self._no_feature_runs.append(label)
                logger.warning("矢量瓦片中没有要素（可能是最大等级过低）：%s", job.output_dir)
        # 没有要素时生成的查看器必然空白，不生成、也不启用按钮
        if self._advanced.viewer and (info is None or info.has_features):
            try:
                self._viewer_path = write_vector_tile_viewer(
                    job,
                    title=label,
                    layer_names=info.layer_names if info is not None else None,
                )
                self.viewer_btn.setEnabled(True)
            except Exception:  # noqa: BLE001 - 查看器失败不影响瓦片结果
                logger.warning("生成矢量瓦片网页查看器失败：%s", job.output_dir, exc_info=True)
        self._queue_index += 1
        if self._queue_index < len(self._queue):
            self._start_next_job()
        else:
            self._finish_queue()

    def _on_task_terminated(self) -> None:
        task = self.task
        if task is None:
            return
        self.task = None
        exc = getattr(task, "exception", None)
        self._finish_queue(
            cancelled=bool(self._cancel_requested or task.isCanceled()),
            failed="" if exc is None else str(exc),
        )

    def _finish_queue(self, *, cancelled: bool = False, failed: str = "") -> None:
        total = self._queue_total
        completed = self._queue_completed
        tiles = self._queue_tiles
        no_feature = list(self._no_feature_runs)
        features = self._feature_count
        self._queue = []
        self._queue_total = 0
        self._queue_index = 0
        self._no_feature_runs = []
        self._feature_count = 0
        self.loading.setRange(0, 100)
        self.execute_btn.setEnabled(True)
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.setText("取消")
        self.advanced_btn.setEnabled(True)
        self.batch_btn.setEnabled(True)
        if cancelled:
            self.loading.setValue(0)
            self.status_label.setText(f"已取消：已完成 {completed}/{total} 套，共 {tiles} 个瓦片")
            send("warning", f"矢量瓦片已取消（已完成 {completed}/{total} 套）", duration=3)
            return
        if failed:
            self.loading.setValue(0)
            self.status_label.setText(f"第 {completed + 1}/{max(1, total)} 套失败：{failed}")
            logger.error("矢量瓦片任务失败：%s", failed)
            send("critical", f"矢量瓦片生成失败：{failed}", duration=5)
            return
        self.loading.setValue(100)
        if tiles <= 0:
            self.status_label.setText("完成，但没有生成瓦片")
            send("warning", "矢量瓦片任务完成，但输出目录中没有瓦片", duration=5)
            return
        if no_feature:
            # 有 pbf 但全是空瓦片：多半是最大等级太低，面/线被抽稀掉
            names = "、".join(no_feature[:3])
            if len(no_feature) > 3:
                names += f" 等 {len(no_feature)} 套"
            self.status_label.setText(
                f"完成：{tiles} 个瓦片，但「{names}」中没有要素"
            )
            hint = (
                f"矢量瓦片已生成（{tiles} 个文件），但 {len(no_feature)}/{total} 套中"
                f"没有任何要素，网页查看器会是空白。\n"
                "面 / 线要素在低缩放级别会被简化掉，请把「最大等级」提高到 14 级"
                "或更高后重新生成。"
            )
            logger.warning("矢量瓦片完成但 %d 套没有要素", len(no_feature))
            send("warning", hint, duration=8)
            return
        feature_text = f"，{features} 个要素" if features else ""
        self.status_label.setText(f"完成：{total} 套，共 {tiles} 个瓦片{feature_text}")
        message = f"矢量瓦片生成完成（{total} 套，{tiles} 个瓦片{feature_text}）"
        if self._viewer_path:
            message += f"\n网页查看器：{self._viewer_path}"
        send("success", message, duration=3)

    def _on_cancel(self) -> None:
        task = self.task
        if task is None:
            return
        self._cancel_requested = True
        self.tile_service.cancel(task)
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.setText("取消中…")
        self.status_label.setText("正在取消…")
        logger.info("矢量瓦片：已请求取消")

    # ------------------------------------------------------------ 辅助

    @staticmethod
    def _count_pbf(out_dir: str) -> int:
        if not out_dir or not os.path.isdir(out_dir):
            return 0
        return sum(
            1
            for _root, _dirs, files in os.walk(out_dir)
            for name in files
            if name.lower().endswith(".pbf")
        )

    @staticmethod
    def _clear_dir_contents(path: str) -> None:
        """删除目录内全部内容（保留目录本身）。"""
        if not os.path.isdir(path):
            return
        try:
            names = os.listdir(path)
        except OSError:
            logger.warning("无法列出输出目录：%s", path, exc_info=True)
            return
        for name in names:
            target = os.path.join(path, name)
            try:
                if os.path.isdir(target) and not os.path.islink(target):
                    shutil.rmtree(target)
                else:
                    os.remove(target)
            except OSError:
                logger.warning("清空输出目录时无法删除：%s", target, exc_info=True)

    def _confirm_clear(self, dirs: list[str]) -> bool:
        """非空输出目录在清空前确认一次。"""
        nonempty: list[str] = []
        for path in dirs:
            try:
                if os.path.isdir(path) and os.listdir(path):
                    nonempty.append(path)
            except OSError:
                logger.warning("无法检查输出目录是否为空：%s", path, exc_info=True)
        if not nonempty:
            return True
        preview = "\n".join(nonempty[:5])
        if len(nonempty) > 5:
            preview += f"\n… 共 {len(nonempty)} 个目录"
        answer = QMessageBox.question(
            self,
            "清空输出目录",
            "已勾选「执行前清空输出目录」，以下目录内的全部文件将被删除：\n\n"
            f"{preview}\n\n是否继续？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return answer == QMessageBox.StandardButton.Yes

    def _open_viewer(self) -> None:
        path = self._viewer_path
        if not path or not os.path.isfile(path):
            send("warning", "网页查看器不存在，请在「高级」里勾选后重新执行", duration=3)
            self.viewer_btn.setEnabled(False)
            return
        if not open_viewer(path):
            send("warning", f"无法打开浏览器，请手动打开：{path}", duration=5)

    def _stop_running_task(self) -> None:
        """关闭窗口时取消在跑的任务，避免任务继续给已关窗口发信号。"""
        task = self.task
        if task is not None:
            self._cancel_requested = True
            self.tile_service.cancel(task)
            for signal, slot in (
                (task.progress_updated, self._on_task_progress),
                (task.progress_text, self._on_task_progress_text),
                (task.taskCompleted, self._on_task_completed),
                (task.taskTerminated, self._on_task_terminated),
            ):
                try:
                    signal.disconnect(slot)
                except (TypeError, RuntimeError):
                    pass
        self.task = None
        self._queue = []
        self._queue_total = 0
        self._queue_index = 0
        self.execute_btn.setEnabled(True)
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.setText("取消")
        self.advanced_btn.setEnabled(True)
        self.batch_btn.setEnabled(True)
        self.loading.setRange(0, 100)

    def reject(self) -> None:  # noqa: N802 —— Qt 命名
        """标题栏关闭 / Esc 走 ``reject()``（不发 ``closeEvent``），这里同样收尾。"""
        self._stop_running_task()
        super().reject()

    def closeEvent(self, event) -> None:  # noqa: N802 —— Qt 命名
        """「关闭」按钮走 ``close()``，收尾逻辑与 :meth:`reject` 一致。"""
        self._stop_running_task()
        super().closeEvent(event)
