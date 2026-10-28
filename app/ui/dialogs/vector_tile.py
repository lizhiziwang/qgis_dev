"""矢量瓦片生成 Dialog。"""

from __future__ import annotations

import os
import shutil
import tempfile
from dataclasses import replace
from functools import partial
from typing import Optional

from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsMapLayer,
    QgsProject,
    QgsRectangle,
    QgsVectorFileWriter,
)
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (
    QApplication,
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
from app.core.util.text_encoding import repair_mojibake
from app.infra.logging import get_logger
from app.infra.messagebar import send
from app.infra.viewer_server import open_viewer
from app.ui.common.algo_desc import make_description_panel
from app.ui.common.base_dialog import ANALYSIS_DIALOG_MAX_HEIGHT, BaseDialog
from app.ui.common.layer_combo import LayerNameWatcher, layer_source, live_layer
from app.ui.common.layer_order import layers_in_tree_order
from app.ui.common.message_box import MessageBox
from app.ui.common.validation import focus_and_highlight
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

#: 执行前耗时预估（仅用于提示，不是精确值）：实测 148,278 个面要素的 GDB 图层，
#: 0-7 级约 87 秒；单独跑 10 级 / 12 级各约 96 秒——算法**每级都要重新遍历一遍
#: 全部要素**，耗时主要跟「要素数 × 层级数」走，瓦片数多寡反而是次要因素。
#: 低层级逐要素开销较小，8 级起基本按该速率线性增长。
_SECONDS_PER_FEATURE_LOW = 0.00005
_SECONDS_PER_FEATURE_HIGH = 0.00065
_LOW_ZOOM_MAX = 7
#: 预估耗时超过该值（秒）时，即便瓦片数很少也先弹窗确认
_ESTIMATE_WARN_SECONDS = 60


class VectorTileDialog(BaseDialog):
    """矢量瓦片生成 Dialog。"""

    #: 分析工具弹窗统一高度上限，超出后参数区滚动
    max_window_height = ANALYSIS_DIALOG_MAX_HEIGHT

    def __init__(self, parent=None, title: str = "", dia_type: Optional[str] = None) -> None:
        super().__init__(parent, title)
        self.dia_type = dia_type
        if not self.dia_type:
            MessageBox.warning(self, "矢量瓦片", "功能待开发")
            self.close()
            return
        self.project = QgsProject.instance()
        # ToolsDock 复用同一对话框实例；工程图层变化时同步刷新列表，
        # 否则后加入的矢量图层不会出现在输入列表里。
        self.project.layersAdded.connect(self._on_project_layers_changed)
        self.project.layersRemoved.connect(self._on_project_layers_changed)
        # 图层在面板里改名后列表文字同步（按 source 保留勾选状态）
        self._name_watcher = LayerNameWatcher(
            self.project, self._on_layer_renamed, self
        )
        self.select_layers: list = []
        self.out_extent: QgsRectangle | None = None
        self._extent_authid = ""
        #: 当前这套任务并行运行的子任务（按缩放级拆分；空 = 没在跑）
        self.tasks: list = []
        #: 当前并行组的分组 / 进度状态（key 都是子任务对象）
        self._group_ranges: list[tuple[int, int]] = []
        self._group_task_levels: dict = {}
        self._group_progress: dict = {}
        self._group_bindings: dict = {}
        self._group_label = ""
        self._group_failed = ""
        #: 输出字段过滤用的临时 GeoPackage 目录（整轮结束后删除）
        self._field_work_dir = ""
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

    def _on_layer_renamed(self) -> None:
        """图层面板改名后重建列表，勾选状态按 source 保留。"""
        self._populate_layers()

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
            for layer in layers_in_tree_order(self.project):
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
        for layer in layers_in_tree_order(self.project):
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
        if not self.tasks and not self._queue:
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

    def _show_validation_error(self, error: str) -> None:
        """把校验错误定位到对应控件（组件级提示，不写主窗口浮动条）。"""
        widget = None
        if error == "最小等级不能大于最大等级":
            widget = self.max_zoom_spin
        elif error.startswith("文件模板"):
            widget = self.xyz_temp
        elif error.startswith("请至少勾选"):
            widget = self.list_widget
        if widget is not None:
            focus_and_highlight(widget, error)
        else:
            MessageBox.warning(self, "矢量瓦片", error)

    def _on_execute(self) -> None:
        if self.tasks or self._queue:
            MessageBox.warning(self, "矢量瓦片", "已有矢量瓦片任务正在执行")
            return
        error = self._validate_common()
        if error:
            self._show_validation_error(error)
            return
        out_dir = self.output_edit.text().strip()
        if not out_dir:
            focus_and_highlight(self.output_edit, "请先选择输出目录")
            return
        extent, error = self._resolve_extent(self._checked_layers())
        if extent is None:
            focus_and_highlight(self.extent_edit, error)
            return

        template = self._current_template()
        self.xyz_temp.setText(template)
        layers = [live_layer(layer) for layer in self.select_layers]
        layers = [layer for layer in layers if layer is not None]
        if not layers:
            focus_and_highlight(self.list_widget, "勾选的图层已失效，请重新勾选")
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
        self._start_jobs(
            [(job, "、".join(layer.name() for layer in layers))], layers
        )

    def _open_advanced(self) -> None:
        """打开高级设置（文件模板 / 清空输出目录 / 网页查看器）。"""
        if self.tasks or self._queue:
            MessageBox.warning(self, "矢量瓦片", "任务执行中，暂时不能修改高级设置")
            return
        current = VectorTileAdvancedOptions(
            template=self._current_template(),
            clear_output=self._advanced.clear_output,
            viewer=self._advanced.viewer,
        )
        dialog = VectorTileAdvancedDialog(
            self,
            current,
            layers=self._checked_layers()
            or [
                layer
                for layer in layers_in_tree_order(self.project)
                if layer.type() == QgsMapLayer.VectorLayer
            ],
        )
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
        if self.tasks or self._queue:
            MessageBox.warning(self, "矢量瓦片", "任务执行中，暂时不能启动批量处理")
            return
        error = self._validate_options()
        if error:
            self._show_validation_error(error)
            return
        layers = [
            layer
            for layer in (
                live_layer(item) for item in layers_in_tree_order(self.project)
            )
            if layer is not None and layer.type() == QgsMapLayer.VectorLayer
        ]
        if not layers:
            focus_and_highlight(self.list_widget, "工程中没有矢量图层")
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
                MessageBox.warning(self, "矢量瓦片", "批量列表中有图层已失效，请重新打开批量处理")
                return
            try:
                name, source = layer.name(), layer.source()
            except (AttributeError, RuntimeError):
                MessageBox.warning(self, "矢量瓦片", "批量列表中有图层已失效，请重新打开批量处理")
                return
            if use_layer_extent:
                try:
                    rect = layer.extent()
                    layer_crs = layer.crs()
                except (AttributeError, RuntimeError):
                    MessageBox.warning(self, "矢量瓦片", f"图层「{name}」已失效")
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
                MessageBox.warning(
                    self, "矢量瓦片", f"图层「{name}」无法确定输出范围：{fallback_error}"
                )
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
        self._start_jobs(jobs, [target.layer for target in targets])

    @staticmethod
    def _estimate_seconds(layers, min_zoom: int, max_zoom: int) -> tuple[int, int]:
        """按「要素数 × 层级数」粗估切片耗时，返回 ``(秒, 要素总数)``。

        算法每个缩放级都会把全部要素重新过一遍，所以瓦片数很少也可能跑很久
        （例如 148,278 个面、0-14 级约 1,200 个瓦片，但需要十几分钟）。
        取不到要素数时返回 ``(0, 0)``，不弹提示。
        """
        features = 0
        for layer in layers or []:
            try:
                count = int(layer.featureCount())
            except (AttributeError, RuntimeError, TypeError):
                continue
            if count > 0:
                features += count
        if features <= 0 or max_zoom < min_zoom:
            return 0, 0
        low_levels = sum(
            1 for z in range(min_zoom, max_zoom + 1) if z <= _LOW_ZOOM_MAX
        )
        high_levels = (max_zoom - min_zoom + 1) - low_levels
        seconds = features * (
            low_levels * _SECONDS_PER_FEATURE_LOW
            + high_levels * _SECONDS_PER_FEATURE_HIGH
        )
        return int(seconds), features

    def _start_jobs(self, jobs: list[tuple[TileJob, str]], layers=None) -> None:
        """校验输出目录、确认瓦片规模 / 预估耗时、按需清空，然后启动顺序执行队列。"""
        if not jobs:
            return
        # 默认最大等级 14 在大范围数据上可能产生数十万瓦片；而且算法每个缩放级
        # 都会重新遍历全部要素，要素多 / 层级多时即使瓦片很少也会跑很久。
        n_tiles = sum(
            self._estimate_tiles(job.extent, job.min_zoom, job.max_zoom)
            for job, _label in jobs
        )
        min_zoom = jobs[0][0].min_zoom
        max_zoom = jobs[0][0].max_zoom
        estimate_seconds, features = self._estimate_seconds(
            layers if layers is not None else self._checked_layers(),
            min_zoom,
            max_zoom,
        )
        # 并行导出的墙钟时间按最慢一段 + 进程启动开销估算
        from app.core.domain.tiles.parallel import split_zoom_ranges  # noqa: PLC0415

        selected_layers = layers if layers is not None else self._checked_layers()
        serial_only = any(
            getattr(lyr, "providerType", lambda: "memory")() not in ("ogr", "spatialite")
            for lyr in selected_layers
        )
        parallel_groups = (
            1
            if serial_only
            else max(1, len(split_zoom_ranges(min_zoom, max_zoom)))
        )
        # 并行并不是线性加速（子进程启动 / 磁盘与 CPU 争用），乘 1.3 保守估算
        estimated_wall = int(estimate_seconds / parallel_groups * 1.3) + 15
        if (
            n_tiles > TILE_COUNT_WARN_THRESHOLD
            or estimated_wall >= _ESTIMATE_WARN_SECONDS
        ):
            details = []
            if n_tiles > TILE_COUNT_WARN_THRESHOLD:
                details.append(f"预计生成约 {n_tiles:,} 个瓦片")
            if estimated_wall >= _ESTIMATE_WARN_SECONDS:
                minutes = max(1, round(estimated_wall / 60))
                group_text = (
                    f"并行 {parallel_groups} 组"
                    if parallel_groups > 1
                    else "单进程执行"
                )
                details.append(
                    f"{features:,} 个要素 × {max_zoom - min_zoom + 1} 个缩放层级，"
                    f"{group_text}，预计耗时约 {minutes} 分钟"
                )
            message = "；\n".join(details) + f"。\n缩放级别 {min_zoom}-{max_zoom}，是否继续？"
            answer = QMessageBox.question(
                self,
                "切片确认",
                message,
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
            MessageBox.critical(self, "矢量瓦片", f"无法创建输出目录：{exc}")
            return

        if self._advanced.clear_output and not self._confirm_clear(dirs):
            return
        if self._advanced.clear_output:
            for path in dirs:
                self._clear_dir_contents(path)

        jobs = self._apply_field_filter(jobs)
        if not jobs:
            return

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

    def _apply_field_filter(self, jobs: list[tuple[TileJob, str]]) -> list[tuple[TileJob, str]]:
        """按「高级 → 输出字段」生成临时图层，替换作业里的数据源。

        QGIS 矢量瓦片算法没有字段筛选参数，只能先把图层写成只含所选字段的
        GeoPackage 再切。全部字段导出时，一个瓦片可能几十 MB（要素属性占大头），
        网页查看器会非常慢；这一步是“打开查看器很快”的关键。
        """
        self._cleanup_field_temps()
        fields = getattr(self._advanced, "fields", None)
        if not fields:
            return jobs
        try:
            self._field_work_dir = tempfile.mkdtemp(prefix="qgis_vt_fields_")
        except OSError as exc:
            MessageBox.warning(self, "矢量瓦片", f"创建输出字段副本目录失败：{exc}")
            return jobs

        source_layers: dict[str, object] = {}
        try:
            for layer in self.project.mapLayers().values():
                try:
                    source_layers[layer.source()] = layer
                except RuntimeError:
                    continue
        except RuntimeError:
            source_layers = {}

        self.status_label.setText("正在准备输出字段副本…")
        self.status_label.setToolTip("")
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        QApplication.processEvents()
        out_jobs: list[tuple[TileJob, str]] = []
        counter = 0
        try:
            for job, label in jobs:
                sources: list[str] = []
                for source in job.layer_sources:
                    layer = source_layers.get(source)
                    if layer is None:
                        sources.append(source)
                        continue
                    indices = [
                        layer.fields().indexOf(name)
                        for name in fields
                        if layer.fields().indexOf(name) >= 0
                    ]
                    if not indices:
                        sources.append(source)
                        continue
                    layer_name = f"tiles_{counter}"
                    path = os.path.join(self._field_work_dir, f"layer_{counter}.gpkg")
                    counter += 1
                    options = QgsVectorFileWriter.SaveVectorOptions()
                    options.driverName = "GPKG"
                    options.fileEncoding = "UTF-8"
                    options.layerName = layer_name
                    options.actionOnExistingFile = (
                        QgsVectorFileWriter.ActionOnExistingFile.CreateOrOverwriteFile
                    )
                    options.attributes = indices
                    # GDB 读出的字段名也可能是「UTF-8 被当成 Latin-1」的乱码；
                    # pbf 的属性键取的是字段名，顺手把副本里的字段名修正。
                    # attributesExportNames 需要与源字段一一对应的全量列表。
                    source_names = [field.name() for field in layer.fields()]
                    fixed_names = [repair_mojibake(name) for name in source_names]
                    if fixed_names != source_names:
                        options.attributesExportNames = fixed_names
                    try:
                        error, message, new_path, _layer = (
                            QgsVectorFileWriter.writeAsVectorFormatV3(
                                layer,
                                path,
                                self.project.transformContext(),
                                options,
                            )
                        )
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(
                            "矢量瓦片：准备字段副本失败，使用原图层 %s：%s",
                            layer.name(),
                            exc,
                            exc_info=True,
                        )
                        sources.append(source)
                        continue
                    if int(error) != 0:
                        logger.warning(
                            "矢量瓦片：字段副本写出失败，使用原图层 %s：%s",
                            layer.name(),
                            message,
                        )
                        sources.append(source)
                        continue
                    sources.append(f"{new_path}|layername={layer_name}")
                out_jobs.append((replace(job, layer_sources=sources), label))
        finally:
            QApplication.restoreOverrideCursor()
        return out_jobs

    def _cleanup_field_temps(self) -> None:
        """删除「输出字段」用的临时 GeoPackage 目录。"""
        path, self._field_work_dir = self._field_work_dir, ""
        if path:
            shutil.rmtree(path, ignore_errors=True)

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
        self._start_job_group(job, label)

    def _start_job_group(self, job: TileJob, label: str) -> None:
        """按缩放级并行：任务内部起多个子进程（见 ``tiles/parallel.py``）。

        同进程多线程并发跑 processing 在大会原生崩溃，所以子进程隔离；
        对话框这里只面对一个任务，进度 / 生命周期信号与单任务一致。
        """
        self._group_label = label
        self._group_task_levels = {}
        self._group_progress = {}
        self._group_bindings = {}
        self._group_failed = ""
        self.tasks = []
        try:
            task = self.tile_service.create_group(job)
            self._group_ranges = list(
                getattr(task, "ranges", [(job.min_zoom, job.max_zoom)])
            )
            on_progress = partial(self._on_group_progress, task)
            on_text = self._on_task_progress_text
            on_completed = partial(self._on_group_task_completed, task)
            on_terminated = partial(self._on_group_task_terminated, task)
            # 先连信号再提交：极快的任务可能在 addTask 后立即完成
            task.progress_updated.connect(on_progress)
            task.progress_text.connect(on_text)
            task.taskCompleted.connect(on_completed)
            task.taskTerminated.connect(on_terminated)
            self._group_task_levels[task] = sum(
                zmax - zmin + 1 for zmin, zmax in self._group_ranges
            )
            self._group_progress[task] = 0
            self._group_bindings[task] = (
                (task.progress_updated, on_progress),
                (task.progress_text, on_text),
                (task.taskCompleted, on_completed),
                (task.taskTerminated, on_terminated),
            )
            self.tasks.append(task)
        except Exception as exc:  # noqa: BLE001
            logger.error("启动矢量瓦片任务失败：%s", exc, exc_info=True)
            self._request_cancel_group()
            self._disconnect_group_bindings()
            self.tasks = []
            self._finish_queue(failed=f"启动失败：{exc}")
            return
        self.tile_service.start(self.tasks[0])

    def _on_group_progress(self, task, value: int) -> None:
        """某个子任务的进度 → 当前这套作业的加权进度（按各段层级数折算）。"""
        if task not in self._group_progress:
            return
        if self.loading.minimum() == 0 and self.loading.maximum() == 0:
            self.loading.setRange(0, 100)
        value = max(0, min(100, int(value)))
        self._group_progress[task] = value
        total_levels = sum(self._group_task_levels.values()) or 1
        done = sum(
            self._group_task_levels[t] * (p / 100.0)
            for t, p in self._group_progress.items()
        )
        job_pct = done / total_levels * 100.0
        overall = (
            (self._queue_index + job_pct / 100.0)
            / max(1, self._queue_total)
            * 100.0
        )
        self.loading.setValue(int(round(overall)))
        self.status_label.setText(self._job_progress_text(int(round(job_pct))))

    def _job_progress_text(self, value: int) -> str:
        """当前这套瓦片的「缩放级别 + 百分比」说明（前面带批处理序号）。"""
        job, label = self._queue[self._queue_index]
        prefix = (
            f"第 {self._queue_index + 1}/{self._queue_total} 套："
            if self._queue_total > 1
            else ""
        )
        if len(self._group_ranges) > 1:
            return (
                f"{prefix}{label} · 缩放级别 {job.min_zoom}-{job.max_zoom}"
                f"（并行 {len(self._group_ranges)} 组）· {value}%"
            )
        total_zooms = max(1, job.max_zoom - job.min_zoom + 1)
        if value >= 100:
            level = total_zooms
        else:
            level = min(total_zooms, int(value / 100.0 * total_zooms) + 1)
        zoom = job.min_zoom + level - 1
        return (
            f"{prefix}{label} · 缩放级别 {zoom}（{level}/{total_zooms}）"
            f" · {value}%"
        )

    def _on_task_progress_text(self, text: str) -> None:
        if len(getattr(self, "_group_ranges", [])) > 1:
            return  # 并行时各子任务文字会互相覆盖，进度条已给整体百分比
        self.status_label.setText(f"{text} · {self.loading.value()}%")

    def _on_group_task_completed(self, task) -> None:
        """一个子任务成功；整组都完成后收尾这套作业。"""
        if task not in self._group_task_levels:
            return  # 已被另一条生命周期信号处理
        self.tasks = [t for t in self.tasks if t is not task]
        if not self.tasks:
            self._finish_job_group()

    def _on_group_task_terminated(self, task) -> None:
        """子任务失败 / 取消；等整组都停下后再统一收尾。"""
        if task not in self._group_task_levels:
            return
        exc = getattr(task, "exception", None)
        self.tasks = [t for t in self.tasks if t is not task]
        if exc is not None and not self._group_failed:
            self._group_failed = str(exc)
            # 一个子任务失败就把其余一组停掉，避免继续白跑
            self._request_cancel_group()
        if self.tasks:
            return
        if self._cancel_requested:
            self._finish_queue(cancelled=True)
        elif self._group_failed:
            self._finish_queue(failed=self._group_failed)
        else:
            self._finish_queue(failed="矢量瓦片任务被终止")

    def _request_cancel_group(self) -> None:
        """请求取消当前组内所有子任务（等待它们的终止信号收尾）。"""
        for task in list(getattr(self, "tasks", []) or []):
            try:
                self.tile_service.cancel(task)
            except RuntimeError:
                pass

    def _disconnect_group_bindings(self) -> None:
        for bindings in getattr(self, "_group_bindings", {}).values():
            for signal, slot in bindings:
                try:
                    signal.disconnect(slot)
                except (TypeError, RuntimeError):
                    pass
        self._group_bindings = {}

    def _finish_job_group(self) -> None:
        """当前这套作业的所有缩放组都成功：统计 / 生成查看器 / 进入下一套。"""
        job = self._queue[self._queue_index][0]
        label = self._group_label
        self.tasks = []
        self._disconnect_group_bindings()
        self._group_task_levels = {}
        self._group_progress = {}
        self._group_ranges = []
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
        self.tasks = []
        self._disconnect_group_bindings()
        self._group_ranges = []
        self._group_task_levels = {}
        self._group_progress = {}
        self._group_failed = ""
        self._group_label = ""
        self._cleanup_field_temps()
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
            MessageBox.critical(self, "矢量瓦片", f"矢量瓦片生成失败：{failed}")
            return
        self.loading.setValue(100)
        if tiles <= 0:
            self.status_label.setText("完成，但没有生成瓦片")
            MessageBox.warning(self, "矢量瓦片", "矢量瓦片任务完成，但输出目录中没有瓦片")
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
            MessageBox.warning(self, "矢量瓦片", hint)
            focus_and_highlight(self.max_zoom_spin, "建议提高最大等级后重新生成")
            return
        feature_text = f"，{features} 个要素" if features else ""
        self.status_label.setText(f"完成：{total} 套，共 {tiles} 个瓦片{feature_text}")
        message = f"矢量瓦片生成完成（{total} 套，{tiles} 个瓦片{feature_text}）"
        if self._viewer_path:
            message += f"\n网页查看器：{self._viewer_path}"
        send("success", message, duration=3)

    def _on_cancel(self) -> None:
        if not self.tasks:
            return
        self._cancel_requested = True
        self._request_cancel_group()
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
            MessageBox.warning(
                self, "矢量瓦片", "网页查看器不存在，请在「高级」里勾选后重新执行"
            )
            self.viewer_btn.setEnabled(False)
            return
        if not open_viewer(path):
            MessageBox.warning(self, "矢量瓦片", f"无法打开浏览器，请手动打开：{path}")

    def _stop_running_task(self) -> None:
        """关闭窗口时取消在跑的全部子任务，避免任务继续给已关窗口发信号。"""
        if self.tasks:
            self._cancel_requested = True
            self._request_cancel_group()
            self._disconnect_group_bindings()
        self.tasks = []
        self._group_ranges = []
        self._group_task_levels = {}
        self._group_progress = {}
        self._group_failed = ""
        self._group_label = ""
        self._cleanup_field_temps()
        self._queue = []
        self._queue_total = 0
        self._queue_index = 0
        self.execute_btn.setEnabled(True)
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.setText("取消")
        self.advanced_btn.setEnabled(True)
        self.batch_btn.setEnabled(True)
        self.loading.setRange(0, 100)

    def is_exporting(self) -> bool:
        """是否正在导出矢量瓦片（当前并行组或待执行队列未结束）。"""
        return bool(getattr(self, "tasks", None)) or bool(
            getattr(self, "_queue", None)
        )

    def stop_export(self) -> None:
        """供主窗口关闭流程调用：不再询问，直接终止当前导出。"""
        self._stop_running_task()

    def _confirm_stop_export(self) -> bool:
        """导出进行中关闭窗口 / 程序时确认；返回 ``True`` 表示可以终止退出。"""
        if not self.is_exporting():
            return True
        box = MessageBox(self)
        box.setIcon(MessageBox.Icon.Warning)
        box.setWindowTitle("矢量瓦片导出")
        box.setText("矢量瓦片正在导出。")
        box.setInformativeText(
            "关闭会中断导出，尚未生成的瓦片不会继续生成"
            "（已生成的瓦片保留在输出目录）。"
        )
        stop_btn = box.addButton(
            "终止导出并关闭", MessageBox.ButtonRole.DestructiveRole
        )
        cancel_btn = box.addButton("继续导出", MessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(cancel_btn)
        box.exec()
        return box.clickedButton() is stop_btn

    def reject(self) -> None:  # noqa: N802 —— Qt 命名
        """标题栏关闭 / Esc 走 ``reject()``（不发 ``closeEvent``），这里同样收尾。"""
        if not self._confirm_stop_export():
            return
        self._stop_running_task()
        super().reject()

    def closeEvent(self, event) -> None:  # noqa: N802 —— Qt 命名
        """「关闭」按钮走 ``close()``，收尾逻辑与 :meth:`reject` 一致。"""
        if not self._confirm_stop_export():
            event.ignore()
            return
        self._stop_running_task()
        super().closeEvent(event)
