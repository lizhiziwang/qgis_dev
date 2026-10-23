"""最短路径分析 Dialog。

窗口设计参考 QGIS 原始"最短路径（点到点）"工具：网络图层 + 路径类型 +
方向/速度设置 + 起终点（画布取点）+ 输出。与 QGIS processing 对话框一致，
算法在**后台任务**里执行（QgsProcessingAlgRunnerTask），窗口不阻塞，带实时
进度条与取消按钮；结果图层加入工程并提示总代价。起点/终点选定后以画布标记
（QgsVertexMarker）显示在地图上，随输入框文本变化实时显示/隐藏。
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Optional

from qgis.core import (
    QgsApplication,
    QgsCoordinateTransform,
    QgsMapLayer,
    QgsPointXY,
    QgsProcessingContext,
    QgsProcessingUtils,
    QgsProject,
    QgsVectorLayer,
    QgsWkbTypes,
)
from qgis.gui import QgsMapToolEmitPoint, QgsVertexMarker
from qgis.PyQt.QtCore import Qt, QTimer
from qgis.PyQt.QtGui import QColor
from qgis.PyQt.QtWidgets import (
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from app.core.domain.shortestpath import ShortestPathJob
from app.core.domain.shortestpath.service import build_shortest_path_params
from app.core.domain.shortestpath.task import ShortestPathFeedback, ShortestPathRunnerTask
from app.infra.logging import get_logger
from app.infra.messagebar import send
from app.ui.common.algo_desc import make_description_panel
from app.ui.common.base_dialog import BaseDialog, make_scrollable
from app.ui.common.field_combo import make_field_combo_searchable

if TYPE_CHECKING:
    from qgis.gui import QgsMapTool

logger = get_logger(__name__)


class ShortestPathDialog(BaseDialog):
    """最短路径分析 Dialog（后台执行，交互逻辑与 QGIS processing 对话框一致）。"""

    def __init__(self, parent=None, title: str = "") -> None:
        super().__init__(parent, title)
        self.setMinimumSize(760, 620)
        self.project = QgsProject.instance()
        #: 画布取点时记录的前一个 map tool，取点结束恢复
        self._prev_tool: Optional["QgsMapTool"] = None
        self._pick_tool: Optional[QgsMapToolEmitPoint] = None
        #: 起点/终点画布标记（QgsVertexMarker），随输入框文本变化显示/隐藏。
        #: 对话框实例被工具树复用，关闭时 hide()、重新打开时 showEvent 恢复。
        self._start_marker: Optional[QgsVertexMarker] = None
        self._end_marker: Optional[QgsVertexMarker] = None
        #: 当前后台任务、其反馈与处理上下文；任务完成/终止后立即置 None。
        #: context 必须存活到任务结束：QgsProcessingAlgRunnerTask 内部保存的是
        #: 指向 QgsProcessingContext 的**裸指针**（worker 线程使用），若这里只当
        #: 局部变量，_on_execute 返回后 Python 回收 wrapper 会删除 C++ 对象 →
        #: worker 线程悬垂指针 → 原生崩溃（Segfault）。
        self._task: Optional[ShortestPathRunnerTask] = None
        self._feedback: Optional[ShortestPathFeedback] = None
        self._context: Optional[QgsProcessingContext] = None
        self._init_ui()
        # 工程图层增删时刷新网络下拉：先开窗口后加图层（含窗口复用场景）也能立即看到。
        # 注意：对话框实例被工具树复用，信号连接在多次打开/关闭间持续有效。
        # layersAdded 在加入后、layersRemoved 在移除后触发（layerWillBeRemoved 在
        # 移除前触发，此时 mapLayers() 仍含该层，刷新无效）。
        self.project.layersAdded.connect(self._refresh_network_layers)
        self.project.layersRemoved.connect(self._refresh_network_layers)

    # ------------------------------------------------------------------ UI

    def _init_ui(self) -> None:
        layout = QVBoxLayout()

        main = QHBoxLayout()
        param_panel = QWidget()
        self._init_parameters_panel(param_panel)
        main.addWidget(make_scrollable(param_panel), stretch=3)
        # 右侧算法描述：processing 注册表取真实 help，注册表不可用时走 algo_desc 兜底
        main.addWidget(make_description_panel("native:shortestpathpointtopoint"), stretch=1)
        layout.addLayout(main, stretch=1)

        b_l = QVBoxLayout()
        b_l.setContentsMargins(0, 0, 0, 0)
        self.loading = QProgressBar()
        self.loading.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.loading.setRange(0, 100)
        # 进度条手动置 0 → 完成置 100（feedback.progressChanged 接控件槽在本环境会
        # 原生崩溃 0xC0000409，见 buffer._init_ui 注释）
        b_l.addWidget(self.loading, stretch=2)

        bottom = QHBoxLayout()
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
        layout.setSpacing(12)

        # 1. 网络图层（仅线图层）
        layout.addWidget(QLabel("网络图层（线）"))
        self.network_combo = QComboBox()
        self.network_combo.currentIndexChanged.connect(self._on_network_changed)
        layout.addWidget(self.network_combo)
        layout.addSpacing(6)

        # 2. 路径类型
        layout.addWidget(QLabel("路径类型"))
        self.strategy_combo = QComboBox()
        self.strategy_combo.addItem("最短距离", 0)
        self.strategy_combo.addItem("最快时间", 1)
        layout.addWidget(self.strategy_combo)
        layout.addSpacing(6)

        # 3. 方向设置（方向字段 + 顺/逆/双向值 + 默认方向）
        layout.addWidget(QLabel("方向字段（单行线，可不选）"))
        self.direction_combo = QComboBox()
        make_field_combo_searchable(self.direction_combo)
        self.direction_combo.currentIndexChanged.connect(self._on_direction_changed)
        layout.addWidget(self.direction_combo)

        dir_values = QHBoxLayout()
        self.forward_edit = QLineEdit()
        self.forward_edit.setPlaceholderText("顺向值")
        self.backward_edit = QLineEdit()
        self.backward_edit.setPlaceholderText("逆向值")
        self.both_edit = QLineEdit()
        self.both_edit.setPlaceholderText("双向值")
        for w in (self.forward_edit, self.backward_edit, self.both_edit):
            w.setEnabled(False)
            dir_values.addWidget(w)
        layout.addLayout(dir_values)

        layout.addWidget(QLabel("默认方向（未设方向字段时）"))
        self.default_dir_combo = QComboBox()
        self.default_dir_combo.addItem("顺向", 0)
        self.default_dir_combo.addItem("逆向", 1)
        self.default_dir_combo.addItem("双向", 2)
        self.default_dir_combo.setCurrentIndex(2)
        layout.addWidget(self.default_dir_combo)
        layout.addSpacing(6)

        # 4. 速度设置（最快时间策略）
        layout.addWidget(QLabel("速度字段（最快时间策略）"))
        self.speed_combo = QComboBox()
        make_field_combo_searchable(self.speed_combo)
        layout.addWidget(self.speed_combo)
        speed_row = QHBoxLayout()
        self.speed_spin = QDoubleSpinBox()
        self.speed_spin.setRange(1.0, 500.0)
        self.speed_spin.setValue(50.0)
        self.speed_spin.setSuffix(" km/h")
        speed_row.addWidget(QLabel("默认速度"))
        speed_row.addWidget(self.speed_spin, stretch=1)
        layout.addLayout(speed_row)
        layout.addSpacing(6)

        # 方向/速度下拉依赖网络图层，必须等两者创建后再填充
        self._populate_network_layers()

        # 5. 起点 / 终点（画布取点或手输 "x, y"）
        self.start_edit = QLineEdit()
        self.start_edit.setPlaceholderText("如 116.39, 39.90 或点击「从画布选择」")
        # 文本变化（画布取点/手输/清除）→ 同步画布上的起点标记
        self.start_edit.textChanged.connect(lambda _text: self._update_point_marker("start"))
        self._add_point_row(layout, "起点", self.start_edit, "start")
        self.end_edit = QLineEdit()
        self.end_edit.setPlaceholderText("如 116.41, 39.92 或点击「从画布选择」")
        self.end_edit.textChanged.connect(lambda _text: self._update_point_marker("end"))
        self._add_point_row(layout, "终点", self.end_edit, "end")
        layout.addSpacing(6)

        # 6. 输出图层名
        layout.addWidget(QLabel("输出图层名"))
        self.output_edit = QLineEdit("最短路径")
        layout.addWidget(self.output_edit)
        layout.addSpacing(6)

        # 7. 输出文件路径（留空 = QGIS 处理临时目录里的临时文件）
        layout.addWidget(QLabel("输出文件路径（留空为临时图层）"))
        path_row = QHBoxLayout()
        self.output_path_edit = QLineEdit()
        self.output_path_edit.setPlaceholderText("留空则生成临时图层（处理临时目录）")
        path_row.addWidget(self.output_path_edit, stretch=1)
        browse_btn = QPushButton("浏览…")
        browse_btn.clicked.connect(self._browse_output_path)
        path_row.addWidget(browse_btn)
        layout.addLayout(path_row)

        layout.addStretch()

    def _add_point_row(self, layout, label: str, edit: QLineEdit, key: str) -> None:
        layout.addWidget(QLabel(label))
        row = QHBoxLayout()
        row.addWidget(edit, stretch=1)
        pick_btn = QPushButton("从画布选择")
        pick_btn.clicked.connect(lambda: self._pick_point(key))
        row.addWidget(pick_btn)
        clear_btn = QPushButton("清除")
        clear_btn.clicked.connect(lambda: edit.clear())
        row.addWidget(clear_btn)
        layout.addLayout(row)
        layout.addSpacing(6)

    # ------------------------------------------------------------- 数据填充

    def _populate_network_layers(self) -> None:
        self.network_combo.clear()
        for layer in self.project.mapLayers().values():
            if (
                layer.type() == QgsMapLayer.VectorLayer
                and layer.geometryType() == QgsWkbTypes.LineGeometry
            ):
                self.network_combo.addItem(layer.name(), layer)
        self._on_network_changed()

    def _refresh_network_layers(self, *args) -> None:
        """工程图层变化后刷新网络下拉，尽量保持当前选择。

        先开窗口后加图层（或对话框被工具树复用、关闭期间工程变了）时，下拉
        原本是空的/过期的；接到 ``layersAdded`` / ``layerWillBeRemoved`` 信号后
        重新填充，并把当前选中的图层找回来。
        """
        current = self.network_combo.currentData()
        self._populate_network_layers()
        if current is not None:
            idx = self.network_combo.findData(current)
            if idx >= 0:
                self.network_combo.setCurrentIndex(idx)

    def _on_network_changed(self) -> None:
        """网络图层变化后刷新方向/速度字段下拉。"""
        layer = self.network_combo.currentData()
        self.direction_combo.clear()
        self.speed_combo.clear()
        if layer is None:
            self.direction_combo.addItem("（不使用）", None)
            self.speed_combo.addItem("（使用默认速度）", None)
            return
        self.direction_combo.addItem("（不使用）", None)
        self.speed_combo.addItem("（使用默认速度）", None)
        for field in layer.fields():
            self.direction_combo.addItem(field.name(), field.name())
            self.speed_combo.addItem(field.name(), field.name())
        self._on_direction_changed()

    def _on_direction_changed(self) -> None:
        """有方向字段时启用顺/逆/双向值，禁用默认方向；反之亦然。"""
        has_field = self.direction_combo.currentData() is not None
        for w in (self.forward_edit, self.backward_edit, self.both_edit):
            w.setEnabled(has_field)
        self.default_dir_combo.setEnabled(not has_field)

    # ----------------------------------------------------------- 画布取点

    def _canvas(self):
        parent = self.parent()
        if parent is not None and hasattr(parent, "canvas"):
            return parent.canvas
        return None

    def _pick_point(self, key: str) -> None:
        canvas = self._canvas()
        if canvas is None:
            send("warning", "无法获取地图画布", duration=3)
            return
        self._pick_key = key
        self._prev_tool = canvas.mapTool()
        tool = QgsMapToolEmitPoint(canvas)
        tool.canvasClicked.connect(self._on_canvas_clicked)
        self._pick_tool = tool
        canvas.setMapTool(tool)
        send("info", f"在地图上点击选择{key}点（左键确认）", duration=3)

    def _on_canvas_clicked(self, point, button) -> None:
        if button != Qt.MouseButton.LeftButton:
            return
        edit = self.start_edit if self._pick_key == "start" else self.end_edit
        edit.setText(point.toString(6))
        self._finish_pick()

    def _finish_pick(self) -> None:
        canvas = self._canvas()
        if canvas is not None:
            if self._pick_tool is not None:
                canvas.unsetMapTool(self._pick_tool)
                self._pick_tool = None
            if self._prev_tool is not None:
                canvas.setMapTool(self._prev_tool)
                self._prev_tool = None

    def _point_marker(self, key: str) -> Optional[QgsVertexMarker]:
        """取起点/终点画布标记；未创建且画布可用时按需创建。

        创建失败（无画布，如测试/无地图上下文）返回 None。标记坐标沿用
        输入框里的画布坐标（= 用户点选或手输的坐标），不参与 CRS 变换。
        """
        attr = "_start_marker" if key == "start" else "_end_marker"
        marker = getattr(self, attr)
        if marker is not None:
            return marker
        canvas = self._canvas()
        if canvas is None:
            return None
        marker = QgsVertexMarker(canvas)
        marker.setIconType(QgsVertexMarker.ICON_RHOMBUS)
        marker.setIconSize(14)
        marker.setPenWidth(3)
        color = QColor(220, 40, 40) if key == "start" else QColor(40, 160, 70)
        marker.setColor(color)
        marker.setFillColor(color)
        setattr(self, attr, marker)
        return marker

    def _update_point_marker(self, key: str) -> None:
        """输入框文本变化（画布取点/手输/清除）时同步画布标记显示。

        文本可解析成坐标 → 显示在对应位置；空/非法 → 隐藏。标记与画布
        生命周期绑定：C++ 对象被销毁时（画布删除）丢弃引用，下次重建。
        """
        edit = self.start_edit if key == "start" else self.end_edit
        point = self._parse_point(edit.text())
        marker = self._point_marker(key)
        if marker is None:
            return
        try:
            if point is None:
                marker.hide()
            else:
                marker.setCenter(point)
                marker.show()
        except RuntimeError:
            # 画布/标记 C++ 对象已被销毁，丢弃旧引用，下次调用重建
            attr = "_start_marker" if key == "start" else "_end_marker"
            setattr(self, attr, None)

    def showEvent(self, event) -> None:
        """对话框重新打开时恢复起终点画布标记（实例被工具树复用）。"""
        super().showEvent(event)
        self._update_point_marker("start")
        self._update_point_marker("end")

    def closeEvent(self, event) -> None:
        self._finish_pick()
        # 关闭对话框时隐藏起终点标记（复用场景下次 show 再恢复）
        for attr in ("_start_marker", "_end_marker"):
            marker = getattr(self, attr, None)
            if marker is not None:
                try:
                    marker.hide()
                except RuntimeError:
                    setattr(self, attr, None)
        task = getattr(self, "_task", None)
        if task is not None:
            try:
                task.cancel()
            except RuntimeError:
                # 任务已完成，C++ 对象已被 QgsTaskManager 删除
                self._task = None
        super().closeEvent(event)

    # ------------------------------------------------------------ 执行

    def _browse_output_path(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self,
            "选择输出文件",
            self.output_path_edit.text(),
            "GeoPackage (*.gpkg);;Shapefile (*.shp)",
        )
        if path and not _has_ext(path):
            path += ".gpkg"
        if path:
            self.output_path_edit.setText(path)

    @staticmethod
    def _parse_point(text: str) -> Optional[QgsPointXY]:
        text = text.strip().replace("，", ",")
        if not text:
            return None
        parts = [p.strip() for p in text.split(",")]
        if len(parts) != 2:
            return None
        try:
            return QgsPointXY(float(parts[0]), float(parts[1]))
        except ValueError:
            return None

    def _to_layer_crs(self, point: QgsPointXY) -> QgsPointXY:
        """画布坐标 → 网络图层坐标系（两坐标系不同时做变换）。"""
        net = self.network_combo.currentData()
        if net is None:
            return point
        canvas = self._canvas()
        canvas_crs = canvas.mapSettings().destinationCrs() if canvas is not None else None
        net_crs = net.crs()
        if canvas_crs is None or not canvas_crs.isValid() or not net_crs.isValid():
            return point
        if canvas_crs.authid() == net_crs.authid():
            return point
        try:
            xf = QgsCoordinateTransform(canvas_crs, net_crs, self.project)
            return xf.transform(point)
        except Exception:  # noqa: BLE001
            logger.warning("坐标变换失败，按原坐标使用：%s", point.asWkt(), exc_info=True)
            return point

    def _on_execute(self) -> None:
        net = self.network_combo.currentData()
        if net is None:
            send("warning", "请先在工程中加载线图层作为网络", duration=3)
            return
        start = self._parse_point(self.start_edit.text())
        if start is None:
            send("warning", "请设置起点（输入坐标或点击画布选择）", duration=3)
            return
        end = self._parse_point(self.end_edit.text())
        if end is None:
            send("warning", "请设置终点（输入坐标或点击画布选择）", duration=3)
            return

        job = ShortestPathJob(
            network_layer=net,
            start_point=self._to_layer_crs(start),
            end_point=self._to_layer_crs(end),
            strategy=self.strategy_combo.currentData(),
            direction_field=self.direction_combo.currentData(),
            value_forward=self.forward_edit.text().strip(),
            value_backward=self.backward_edit.text().strip(),
            value_both=self.both_edit.text().strip(),
            default_direction=self.default_dir_combo.currentData(),
            speed_field=self.speed_combo.currentData(),
            default_speed=self.speed_spin.value(),
            output_name=self.output_edit.text().strip() or "最短路径",
            output_path=self.output_path_edit.text().strip() or None,
        )

        # 与 QGIS processing 对话框一致：算法在后台任务里执行
        # （QgsProcessingAlgRunnerTask），主线程事件循环不阻塞 → 窗口保持响应。
        # 之前的同步 processing.run 在网格较大时会阻塞主线程（200x200 实测 ~5s），
        # 表现为窗口无响应。输出走文件：用户指定路径或 QGIS 处理临时目录。
        output = job.output_path or QgsProcessingUtils.generateTempFilename("shortest_path.gpkg")
        params = build_shortest_path_params(job, output)
        context = QgsProcessingContext()
        context.setProject(self.project)
        feedback = ShortestPathFeedback()
        alg = QgsApplication.processingRegistry().algorithmById("native:shortestpathpointtopoint")
        if alg is None:
            alg = _ensure_processing_algorithm("native:shortestpathpointtopoint")
        if alg is None:
            send("critical", "无法获取最短路径算法（processing 未初始化）", duration=5)
            return

        task = ShortestPathRunnerTask(alg, params, context, feedback)
        task.progress_updated.connect(self._on_task_progress)
        task.taskCompleted.connect(self._on_task_completed)
        task.taskTerminated.connect(self._on_task_terminated)
        self._task = task
        self._feedback = feedback
        self._context = context
        self._run_output = output
        self._run_name = job.output_name
        self._run_dest = job.output_path

        self.execute_btn.setEnabled(False)
        self.cancel_btn.setEnabled(True)
        self.cancel_btn.setText("取消")
        self.loading.setValue(0)
        QgsApplication.taskManager().addTask(task)

    # ------------------------------------------------------------ 后台任务回调

    def _on_cancel(self) -> None:
        """用户点击「取消」：请求终止后台任务（与 QGIS 一致：可随时取消）。"""
        task = self._task
        if task is None:
            return
        try:
            task.cancel()
        except RuntimeError:
            # 任务已完成，C++ 对象已被 QgsTaskManager 删除
            self._task = None
            return
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.setText("取消中…")

    def _on_task_progress(self, value: int) -> None:
        self.loading.setValue(int(value))

    def _on_task_completed(self) -> None:
        """后台任务成功：主线程加载结果图层并加入工程。

        注意：此时任务对象即将被 QgsTaskManager 删除，绝不能再访问
        ``self._task`` 的成员（本构建的 dangling-pointer 坑）。
        """
        self._task = None
        self._feedback = None
        self._context = None
        self.execute_btn.setEnabled(True)
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.setText("取消")
        self.loading.setValue(100)

        try:
            layer = QgsVectorLayer(self._run_output, self._run_name, "ogr")
            if layer is None or not layer.isValid():
                raise RuntimeError(f"结果图层无效：{self._run_output}")
        except Exception as exc:  # noqa: BLE001
            logger.error("加载最短路径结果图层失败: %s", exc, exc_info=True)
            self.loading.setValue(0)
            send("critical", f"加载最短路径结果失败：{exc}", duration=5)
            return

        self.project.addMapLayer(layer)
        cost_txt = _format_cost(_read_first_feature_cost(layer))
        dest = f" → {self._run_dest}" if self._run_dest else ""
        send("success", f"最短路径分析完成：总代价 {cost_txt}{dest}", duration=3)
        QTimer.singleShot(0, self._refresh_canvas)

    def _on_task_terminated(self) -> None:
        """后台任务失败或取消。"""
        self._task = None
        self._context = None
        self.execute_btn.setEnabled(True)
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.setText("取消")
        self.loading.setValue(0)
        feedback = self._feedback
        self._feedback = None
        if feedback is not None and feedback.isCanceled():
            send("warning", "已取消最短路径分析", duration=3)
        elif feedback is not None and feedback.errors:
            send("critical", "；".join(feedback.errors), duration=5)
        else:
            send("critical", "最短路径分析失败", duration=5)

    def _refresh_canvas(self) -> None:
        canvas = self._canvas()
        if canvas is not None:
            canvas.refresh()


def _has_ext(path: str) -> bool:

    return bool(os.path.splitext(path)[1])


def _ensure_processing_algorithm(alg_id: str):
    """确保 processing 提供者已注册后按 id 取算法。

    冷启动 / 非 GUI 测试场景下 processing 可能尚未初始化，注册表为空；这里先
    ``Processing.initialize()`` 再查一次。返回 None 表示注册表仍取不到该算法。
    """
    try:
        from processing.core.Processing import Processing

        import processing  # noqa: F401

        Processing.initialize()
    except Exception:  # noqa: BLE001
        logger.warning("processing 初始化失败", exc_info=True)
    return QgsApplication.processingRegistry().algorithmById(alg_id)


def _read_first_feature_cost(layer: QgsVectorLayer) -> Optional[float]:
    """从结果图层首要素读取总代价（算法把 TRAVEL_COST 写进 'cost' 字段）。"""
    fields = layer.fields()
    if "cost" not in fields.names():
        return None
    feat = next(layer.getFeatures(), None)
    if feat is None:
        return None
    return feat["cost"]


def _format_cost(cost: Optional[float]) -> str:
    if cost is None:
        return "—"
    try:
        return f"{float(cost):.2f}"
    except (TypeError, ValueError):
        return str(cost)
