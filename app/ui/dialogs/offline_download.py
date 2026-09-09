"""「下载离线图层」对话框（方案 §4 布局）。

- 顶行：选择任务 + 空间范围（全部空间范围 = 任务字段 task_extent，默认）；
- 中区：下载图层勾选列表，**每个数据集一行**（必选组锁定、可选组置灰规则见
  ``app.core.domain.offline_download``）；行尾预留估算大小位；
- 下载位置（含目标盘剩余空间）＋ 数据估算；
- 底部（参照 buffer 弹窗的“进度条 + 按钮”组合）：进度条在上，按钮行
  （下载管理 / 取消 / 开始下载）在下。

使用约定：
- ``dlg.show()``（非模态）——「框选范围」需要用户在画布上拖框时画布仍可交互；
- 任务来源：构造传 ``tasks`` 或经 :meth:`set_tasks`（主窗口 ``offline_tasks``
  注入）；暂无任务时「开始下载」禁用；
- 进度/阶段/完成经 ``OfflineDownloadService.start_connected`` 接回本弹窗。
"""

from __future__ import annotations

import os
from typing import Optional

from qgis.PyQt.QtCore import Qt, pyqtSignal
from qgis.PyQt.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QProgressBar,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)
from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsProject,
    QgsRectangle,
    QgsWkbTypes,
)
from qgis.gui import QgsMapTool, QgsMapToolEmitPoint, QgsRubberBand

from app.core.domain.offline_download import (
    KEY_LABELS,
    REQUIRED_KEYS,
    DownloadRequest,
    PlanError,
    SpatialScope,
    TaskInfo,
)
from app.core.domain.offline_download.service import OfflineDownloadService
from app.infra.logging import get_logger
from app.infra.messagebar import send
from app.ui.common.base_dialog import BaseDialog

logger = get_logger(__name__)

_STATUS_READY = "就绪：点「开始下载」执行"


class OfflineDownloadDialog(BaseDialog):
    """下载离线图层弹窗。"""

    def __init__(self, parent=None, canvas=None, tasks=(), title: str = "下载离线图层") -> None:
        super().__init__(parent, title)
        # 下载弹窗不提供最大化：隐藏无边框标题栏的最大化按钮
        bar = getattr(self, "title_bar", None)
        if bar is not None:
            btn = getattr(bar, "_btn_max", None)
            if btn is not None:
                btn.hide()
        # 独立顶层窗口：最小化进任务栏（与 QGIS 弹窗一致；Dialog 类型会最小化到
        # 父窗口/消失）。FramelessWindowHint 已由 BaseDialog 设置，这里只升级窗口类型。
        self.setWindowFlag(Qt.WindowType.Window, True)
        self.canvas = canvas
        self.tasks: list[TaskInfo] = self._coerce_tasks(tasks)
        self.service = OfflineDownloadService()
        self._current_task: Optional[TaskInfo] = None
        self._box_tool = None
        self._box_extent: Optional[tuple[float, float, float, float]] = None  # WGS84
        self._job_id: Optional[str] = None
        self._rows: list[tuple[str, str, QCheckBox]] = []  # (key, label, checkbox)
        self._running = False
        #: 已打开下载管理弹窗（懒建单实例）
        self._manager_dialog = None
        # 弹窗默认缩小 1/3（宽），高度适度拉长以尽量完整显示可下载数据行
        self.setMinimumSize(500, 620)
        self._init_ui()
        self._reload_tasks()
        self.resize(500, 620)
        self._detach_from_parent()

    # ================================================================ UI

    def _detach_from_parent(self) -> None:
        """脱离父窗口成独立顶层窗口（最小化进任务栏，同 QGIS 弹窗）。

        带父的 Dialog 是 owned window：最小化只缩到父窗口旁，没有独立任务栏按钮。
        先记住父几何再 setParent(None)，并居中显示，避免跳位。
        """
        parent = self.parentWidget()
        geo = parent.geometry() if parent is not None else None
        self.setParent(None)
        if geo is not None:
            self.move(
                geo.center().x() - self.width() // 2,
                geo.center().y() - self.height() // 2,
            )

    def _init_ui(self) -> None:
        layout = QVBoxLayout()
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(8)

        # ---- 顶行：选择任务 + 空间范围 ----
        top = QGridLayout()
        top.setHorizontalSpacing(8)
        top.setVerticalSpacing(6)
        top.addWidget(QLabel("选择任务"), 0, 0)
        self.task_combo = QComboBox()
        self.task_combo.currentIndexChanged.connect(self._on_task_changed)
        top.addWidget(self.task_combo, 0, 1, 1, 2)

        top.addWidget(QLabel("空间范围"), 1, 0)
        self.scope_group = QButtonGroup(self)
        scope_row = QHBoxLayout()
        for text, val in (
            ("全部空间范围（任务范围）", SpatialScope.ALL),
            ("视图范围", SpatialScope.VIEW),
            ("框选范围", SpatialScope.BOX),
        ):
            rb = QRadioButton(text)
            rb.setProperty("scope", val.value)
            self.scope_group.addButton(rb)
            scope_row.addWidget(rb)
        self.scope_group.buttonClicked.connect(self._on_scope_clicked)
        scope_row.addStretch(1)
        top.addLayout(scope_row, 1, 1, 1, 2)
        self._default_scope_radio()
        layout.addLayout(top)

        # ---- 下载图层勾选列表（每个数据集一行，可滚动）----
        group = QGroupBox("下载图层")
        group_layout = QVBoxLayout(group)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self._rows_container = QWidget()
        self._rows_layout = QVBoxLayout(self._rows_container)
        self._rows_layout.setContentsMargins(0, 2, 0, 2)
        # 行间距收敛到 1/5（原 4 → 1），尽量完整展示可下载数据
        self._rows_layout.setSpacing(1)
        self._rows_layout.addStretch(1)
        self.scroll.setWidget(self._rows_container)
        group_layout.addWidget(self.scroll)
        layout.addWidget(group, 1)

        # ---- 影像层级范围（瓦片金字塔 zmin-zmax，直接影响瓦片数与估算量）----
        zoom_row = QHBoxLayout()
        zoom_row.setSpacing(6)
        zoom_row.addWidget(QLabel("影像层级"))
        zoom_row.addWidget(QLabel("z 最小"))
        self.zmin_spin = QSpinBox()
        self.zmin_spin.setRange(0, 22)
        self.zmin_spin.setValue(0)
        zoom_row.addWidget(self.zmin_spin)
        zoom_row.addWidget(QLabel("z 最大"))
        self.zmax_spin = QSpinBox()
        self.zmax_spin.setRange(0, 22)
        self.zmax_spin.setValue(16)  # 默认收敛到 z16，避免 z18 全范围瓦片数爆炸
        zoom_row.addWidget(self.zmax_spin)
        hint = QLabel("（每 +1 级约 ×4 瓦片，估算量随 zmax 显著增大）")
        hint.setStyleSheet("color: #888;")
        zoom_row.addWidget(hint)
        zoom_row.addStretch(1)
        layout.addLayout(zoom_row)

        # ---- 下载位置 + 数据估算 ----
        loc = QGridLayout()
        loc.setHorizontalSpacing(8)
        loc.addWidget(QLabel("下载位置"), 0, 0)
        self.target_edit = QLineEdit()
        self.target_edit.setText(self._default_target_dir(None))
        self.target_edit.textChanged.connect(self._refresh_disk)
        loc.addWidget(self.target_edit, 0, 1)
        browse = QPushButton("浏览…")
        browse.clicked.connect(self._browse_target)
        loc.addWidget(browse, 0, 2)
        self.free_label = QLabel()
        loc.addWidget(self.free_label, 0, 3)
        loc.addWidget(QLabel("数据估算"), 1, 0)
        self.estimate_label = QLabel("勾选数据集在开始下载前估算并按需预警")
        self.estimate_label.setWordWrap(True)
        loc.addWidget(self.estimate_label, 1, 1, 1, 3)
        layout.addLayout(loc)
        self._refresh_disk()

        # ---- 进度条 + 按钮（参照 buffer：进度条贴按钮行上方）----
        self.loading = QProgressBar()
        self.loading.setRange(0, 100)
        self.loading.setValue(0)
        self.loading.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.stage_label = QLabel(_STATUS_READY)
        self.stage_label.setStyleSheet("color: #888;")
        layout.addWidget(self.stage_label)
        layout.addWidget(self.loading)

        bottom = QHBoxLayout()
        self.manage_btn = QPushButton("下载管理")
        self.manage_btn.clicked.connect(self._open_manager)
        bottom.addWidget(self.manage_btn)
        bottom.addStretch(1)
        cancel_btn = QPushButton("取消")
        cancel_btn.clicked.connect(self.close)
        bottom.addWidget(cancel_btn)
        self.start_btn = QPushButton("开始下载")
        self.start_btn.clicked.connect(self._on_start)
        bottom.addWidget(self.start_btn)
        layout.addLayout(bottom)

        self.setLayout(layout)

    # ================================================================ 任务

    def set_tasks(self, tasks) -> None:
        """外部（如主窗口）注入可用任务列表并默认选中第一个。

        接受 ``TaskInfo`` 或平台 JSON dict（内部经 :meth:`TaskInfo.from_dict` 转换）；
        解析失败的条目跳过并记日志，避免弹窗打不开。
        """
        tasks = [{
            "task_id": "T20260908001",
            "task_name": "示例核查任务（模板）",
            "extent": {
                "xmin": 113.572778,
                "ymin": 24.2,
                "xmax": 118.482141,
                "ymax": 30.1,
                "crs": "EPSG:4490"
            },
            "features": {
                "name": "图斑矢量",
                "service_url": "http://192.168.17.196:6081/arcgis/rest/services/JXxian/MapServer/0",
                "layer_id": "0"
            },
            "attr_template": {
                "name": "属性模板",
                "file_urls": ["http://192.168.17.196:8080/外业/属性模板.xls"]
            },
            "rule_package": {
                "name": "质检规则包",
                "file_urls": ["http://192.168.17.196:8080/外业/规则包.zip"]
            },
            "dictionaries": [
                {
                "name": "土地利用字典",
                "file_urls": ["http://192.168.17.196:8080/外业/土地利用字典.csv"]
                }
            ],
            "reference": {
                "name": "参考影像",
                "service_url": "http://192.168.17.13:6080/arcgis/rest/services/test_image/MapServer/0"
                # "service_url": "http://192.168.17.196:6081/arcgis/rest/services/THL/MapServer"
            }
        }]
        self.tasks = self._coerce_tasks(tasks)
        self._reload_tasks()

    @staticmethod
    def _coerce_tasks(tasks) -> list[TaskInfo]:
        out: list[TaskInfo] = []
        for t in tasks or ():
            if isinstance(t, TaskInfo):
                out.append(t)
                continue
            try:
                out.append(TaskInfo.from_dict(dict(t)))
            except Exception as exc:  # noqa: BLE001
                logger.warning("跳过无效任务记录：%s", exc)
        return out

    def _reload_tasks(self) -> None:
        self.task_combo.clear()
        for i, t in enumerate(self.tasks):
            self.task_combo.addItem(f"{t.task_name}（{t.task_id}）", t)
        if self.tasks:
            self.task_combo.setCurrentIndex(0)
        else:
            self._set_current_task(None)
            self.stage_label.setText("暂无可下载任务：请在主窗口加载任务后重试")
            self.start_btn.setEnabled(False)

    def _on_task_changed(self) -> None:
        self._set_current_task(self.task_combo.currentData())

    def _set_current_task(self, task: Optional[TaskInfo]) -> None:
        self._current_task = task
        self._box_extent = None
        # 默认导出路径 = 程序目录/任务/<任务名>
        if task is not None:
            self.target_edit.setText(self._default_target_dir(task.task_name))
        self._rebuild_rows(task)

    def _rebuild_rows(self, task: Optional[TaskInfo]) -> None:
        # 清空现有行
        while self._rows_layout.count() > 1:
            item = self._rows_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        self._rows = []
        if task is None:
            self.start_btn.setEnabled(False)
            return

        rows = _available_rows(task)
        for key, label, locked in rows:
            box = QCheckBox(label)
            if locked:
                box.setChecked(True)
                box.setEnabled(False)
            else:
                # 任务里有的数据默认全勾（含可选影像），用户可手动取消
                box.setChecked(True)
            row_layout = QHBoxLayout()
            row_layout.addWidget(box)
            est = QLabel("…")
            est.setAlignment(Qt.AlignmentFlag.AlignRight)
            est.setStyleSheet("color: #888;")
            row_layout.addWidget(est, 1)
            holder = QWidget()
            holder.setLayout(row_layout)
            self._rows_layout.insertWidget(self._rows_layout.count() - 1, holder)
            self._rows.append((key, label, box))
            box.stateChanged.connect(self._on_rows_changed)
        self._on_rows_changed()
        self.start_btn.setEnabled(True)

    def _on_rows_changed(self) -> None:
        n = sum(1 for _k, _l, box in self._rows if box.isChecked())
        self.estimate_label.setText(
            f"已勾选 {n} 个数据集；估算大小在「开始下载」前由下载器按服务元数据测算"
        )

    # ================================================================ 空间范围

    def _default_scope_radio(self) -> None:
        for btn in self.scope_group.buttons():
            if btn.property("scope") == SpatialScope.ALL.value:
                btn.setChecked(True)
                return

    def selected_scope(self) -> SpatialScope:
        for btn in self.scope_group.buttons():
            if btn.isChecked():
                return SpatialScope(btn.property("scope"))
        return SpatialScope.ALL

    def _on_scope_clicked(self, btn) -> None:
        if btn.property("scope") == SpatialScope.BOX.value:
            if self._box_extent is None and self.canvas is not None:
                send(
                    "info",
                    "已进入「框选范围」：请在画布上拖拽一个矩形，松开即开始下载",
                    duration=6,
                )
            self._engage_box_tool()
        else:
            self._release_box_tool()

    def _engage_box_tool(self) -> None:
        if self.canvas is None or self._box_tool is not None:
            return
        self._prev_tool = self.canvas.mapTool()
        self._box_tool = _RubberRectTool(self.canvas)
        self._box_tool.rect_finished.connect(self._on_box_finished)
        self.canvas.setMapTool(self._box_tool)

    def _release_box_tool(self) -> None:
        if self._box_tool is not None and self.canvas is not None:
            prev = getattr(self, "_prev_tool", None)
            self.canvas.setMapTool(prev if prev is not None else QgsMapToolEmitPoint(self.canvas))
            self._box_tool = None

    def _on_box_finished(self, rect: QgsRectangle) -> None:
        ext = _rect_to_wgs84(self.canvas, rect)
        if ext is None:
            send("warning", "无法换算框选范围坐标系，请重试", duration=5)
            return
        self._box_extent = ext
        send("success", "已获得框选范围，开始下载", duration=3)
        # 松开即开始（方案 §4：框选后直接进入下载）
        self._on_start()

    # ================================================================ 位置与磁盘

    def _default_target_dir(self, task_name) -> str:
        """默认下载位置 = 程序目录/任务（有任务时 /<任务名>）。"""
        import sys

        base = None
        exe = getattr(sys, "frozen", False) or os.path.basename(sys.executable).lower() in (
            "app.exe",
            "app",
        )
        if exe:
            base = os.path.dirname(sys.executable)
        if not base:
            base = os.getcwd()
        root = os.path.join(base, "任务")
        name = str(task_name or "").strip()
        if not name:
            return root
        from app.core.domain.offline_download.layout import safe_name

        return os.path.join(root, safe_name(name))

    def _browse_target(self) -> None:
        path = QFileDialog.getExistingDirectory(
            self, "选择下载位置", self.target_edit.text() or os.path.expanduser("~")
        )
        if path:
            self.target_edit.setText(path)

    def _refresh_disk(self) -> None:
        from app.core.domain.offline_download.disk import format_bytes, free_bytes

        free = free_bytes(self.target_edit.text())
        self.free_label.setText(f"剩余 {format_bytes(free)}" if free is not None else "磁盘不可用")

    # ================================================================ 下载

    def _on_start(self) -> None:
        if self._running or self._current_task is None:
            return
        target = self.target_edit.text().strip()
        if not target:
            send("warning", "请选择下载位置", duration=4)
            return
        scope = self.selected_scope()
        include = {
            key for key, _label, box in self._rows if box.isChecked()
        } | set(REQUIRED_KEYS)

        if scope == SpatialScope.VIEW and self.canvas is None:
            send("warning", "当前未绑定画布，无法取「视图范围」，请改用「全部空间范围」", duration=6)
            return
        if scope == SpatialScope.VIEW:
            view_rect = self.canvas.mapSettings().extent()
            scope_extent = _rect_to_wgs84(self.canvas, view_rect)
        elif scope == SpatialScope.BOX:
            scope_extent = self._box_extent
            if scope_extent is None:
                send("info", "请先在画布上拖拽框选范围", duration=4)
                self._engage_box_tool()
                return
        else:
            scope_extent = None

        request = DownloadRequest(
            task=self._current_task,
            scope=scope,
            scope_extent=scope_extent,
            include=frozenset(include),
            target_dir=target,
            zoom_range=(self.zmin_spin.value(), self.zmax_spin.value()),
        )
        try:
            job_id, task = self.service.start_connected(
                request,
                on_progress=self._on_progress,
                on_stage=self._on_stage,
                on_done=self._on_done,
                on_fail=self._on_fail,
            )
        except PlanError as exc:
            send("warning", str(exc), duration=8)
            return
        except Exception as exc:  # noqa: BLE001
            logger.error("发起离线下载失败：%s", exc, exc_info=True)
            send("critical", f"发起下载失败：{exc}", duration=6)
            return
        self._job_id = job_id
        self._running = True
        self.start_btn.setEnabled(False)
        self._set_controls_enabled(False)
        self._release_box_tool()
        self.loading.setValue(0)
        self.stage_label.setText("准备中…")

    def _set_controls_enabled(self, enabled: bool) -> None:
        self.task_combo.setEnabled(enabled)
        for btn in self.scope_group.buttons():
            btn.setEnabled(enabled)
        for _k, _l, box in self._rows:
            if not box.isEnabled():  # 必选锁定行不重开
                continue
            box.setEnabled(enabled)

    def _on_progress(self, value: int) -> None:
        self.loading.setValue(int(value))

    def _on_stage(self, stage: str) -> None:
        self.stage_label.setText(stage)

    def _on_done(self) -> None:
        self._running = False
        self.start_btn.setEnabled(True)
        self._set_controls_enabled(True)
        self.stage_label.setText("下载完成，压缩包已生成（可在「下载管理」查看/打开目录）")
        self.loading.setValue(100)

    def _on_fail(self) -> None:
        self._running = False
        self.start_btn.setEnabled(True)
        self._set_controls_enabled(True)
        self.stage_label.setText("下载中止：部分失败或已取消，可在「下载管理」查看详情")
        self.loading.setValue(0)

    # ================================================================ 下载管理

    def _open_manager(self) -> None:
        from app.ui.dialogs.offline_manager import OfflineDownloadManagerDialog

        if self._manager_dialog is None:
            self._manager_dialog = OfflineDownloadManagerDialog(self, service=self.service)
        if self._manager_dialog.isMinimized():
            self._manager_dialog.showNormal()
        else:
            self._manager_dialog.show()
        self._manager_dialog.raise_()
        self._manager_dialog.activateWindow()
        self._manager_dialog.refresh()

    # ================================================================ 关闭

    def closeEvent(self, event) -> None:  # noqa: N802
        self._release_box_tool()
        super().closeEvent(event)


# ====================================================================== 数据行

def _available_rows(task: TaskInfo):
    """把任务数据展开成勾选行：(key, label, required锁定)。

    行文案用任务里的服务/数据集名（``DataSource.name``）或默认标签；库内表名/日志
    才用服务地址里的服务名（见 planner）。
    """
    rows = []
    for key in ("features", "attr_template", "rule_package"):
        ds = _ds_for(task, key)
        if ds is not None:
            rows.append((key, ds.name or KEY_LABELS[key], key in REQUIRED_KEYS))
    for i, ds in enumerate(task.dictionaries):
        rows.append(("dictionaries", f"字典 {ds.name or i + 1}".strip(), True))
    for img in task.imageries:
        from app.core.domain.offline_download.layout import phase_dir

        rows.append(("imageries", img.name or phase_dir(img.phase), False))
    if task.basemap_data is not None:
        bm = task.basemap_data
        rows.append(("basemap_data", bm.name or "本底数据", False))
    for key, fallback in (
        ("admin_boundary", "政区边界"),
        ("reference", "参考数据"),
    ):
        ds = _ds_for(task, key)
        if ds is not None:
            rows.append((key, ds.name or fallback, False))
    return rows


def _ds_for(task: TaskInfo, key: str):
    if key == "features":
        return task.features
    if key == "attr_template":
        return task.attr_template
    if key == "rule_package":
        return task.rule_package
    if key == "admin_boundary":
        return task.admin_boundary
    if key == "reference":
        return task.reference
    return None


def _rect_to_wgs84(canvas, rect: QgsRectangle):
    """把画布坐标系下的外包矩形换算成 WGS84 (xmin,ymin,xmax,ymax)；失败 None。"""
    try:
        canvas_crs = canvas.mapSettings().destinationCrs()
        if canvas_crs.authid() in ("EPSG:4326", "EPSG:4490", ""):
            return (rect.xMinimum(), rect.yMinimum(), rect.xMaximum(), rect.yMaximum())
        out = QgsCoordinateTransform(
            canvas_crs, QgsCoordinateReferenceSystem("EPSG:4326"), QgsProject.instance()
        ).transformBoundingBox(rect)
        return (out.xMinimum(), out.yMinimum(), out.xMaximum(), out.yMaximum())
    except Exception:  # noqa: BLE001
        return None


class _RubberRectTool(QgsMapTool):
    """画布矩形框选工具：拖拽一个矩形，松开发 ``rect_finished(QgsRectangle)``。

    交互与 :class:`app.ui.tools.select_feature.RectSelectTool` 同款（5 点闭环画
    矩形、Esc/失活清空）。
    """

    rect_finished = pyqtSignal(object)

    def __init__(self, canvas) -> None:
        super().__init__(canvas)
        self._start = None
        self.rubber = QgsRubberBand(canvas, QgsWkbTypes.PolygonGeometry)
        self.rubber.setColor(Qt.GlobalColor.red)
        self.rubber.setWidth(2)
        self.rubber.setFillColor(Qt.GlobalColor.transparent)

    def canvasPressEvent(self, event) -> None:  # noqa: N802
        if event.button() != Qt.MouseButton.LeftButton:
            return
        self._start = event.mapPoint()
        self._reset_rubber()

    def canvasMoveEvent(self, event) -> None:  # noqa: N802
        if self._start is None:
            return
        rect = self._rect_to(event)
        self._draw_rect(rect)

    def canvasReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() != Qt.MouseButton.LeftButton or self._start is None:
            return
        rect = self._rect_to(event)
        self._start = None
        self._reset_rubber()
        if rect.width() <= 0.0 or rect.height() <= 0.0:
            return
        self.rect_finished.emit(rect)

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if event.key() == Qt.Key.Key_Escape:
            self._start = None
            self._reset_rubber()
            return
        super().keyPressEvent(event)

    def deactivate(self) -> None:  # noqa: N802
        self._start = None
        self._reset_rubber()
        super().deactivate()

    # -------------------------------------------------- 内部

    def _rect_to(self, event) -> QgsRectangle:
        from qgis.core import QgsPointXY

        pt = event.mapPoint()
        start = self._start
        return QgsRectangle(start, QgsPointXY(pt.x(), pt.y()), True)

    def _draw_rect(self, rect: QgsRectangle) -> None:
        from qgis.core import QgsPointXY

        self.rubber.reset(QgsWkbTypes.PolygonGeometry)
        for p in (
            QgsPointXY(rect.xMinimum(), rect.yMinimum()),
            QgsPointXY(rect.xMaximum(), rect.yMinimum()),
            QgsPointXY(rect.xMaximum(), rect.yMaximum()),
            QgsPointXY(rect.xMinimum(), rect.yMaximum()),
            QgsPointXY(rect.xMinimum(), rect.yMinimum()),
        ):
            self.rubber.addPoint(p)
        self.rubber.show()

    def _reset_rubber(self) -> None:
        try:
            self.rubber.reset(QgsWkbTypes.PolygonGeometry)
            self.rubber.hide()
        except Exception:  # noqa: BLE001
            pass
