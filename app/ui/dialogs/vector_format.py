"""矢量数据格式转换 Dialog。"""

from __future__ import annotations

import contextlib
import os

from qgis.core import Qgis, QgsMapLayer, QgsProject, QgsProviderRegistry, QgsVectorLayer, QgsWkbTypes
from qgis.PyQt.QtCore import Qt, QTimer
from qgis.PyQt.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from app.core.domain.vector_format import VECTOR_FORMATS, VectorConvertJob, VectorConvertService
from app.core.domain.vector_format.task import VectorConvertTask
from app.infra.logging import get_logger
from app.infra.messagebar import send
from app.ui.common.algo_desc import make_description_panel
from app.ui.common.base_dialog import BaseDialog, make_scrollable
from app.ui.common.message_box import MessageBox

logger = get_logger(__name__)

#: 文本 / XML 矢量格式：文件过大时 OGR 解析可能 OOM（KML 报 out of memory），
#: 超过该大小即便勾选「加载到工程」也跳过。
_TEXT_LOAD_DRIVERS = frozenset({"KML", "GML", "GeoJSON"})
_LARGE_TEXT_LOAD_BYTES = 50 * 1024 * 1024

#: 浏览本地输入数据时的文件过滤器（GDAL/OGR 常见矢量格式）
_INPUT_VECTOR_FILTER = (
    "矢量数据 (*.shp *.gpkg *.geojson *.json *.gml *.kml *.sqlite *.tab *.mif *.fgb *.dxf *.csv *.gpx *.000);;"
    "所有文件 (*)"
)


def _with_extension(path: str, extension: str) -> str:
    """把路径扩展名替换为指定格式的扩展名（路径为空时原样返回）。"""
    if not path:
        return path
    root, current = os.path.splitext(path)
    if current.lower() == extension.lower():
        return path
    return root + extension


class _SublayerChoiceDialog(QDialog):
    """多图层文件选择要素类（SQLite / GPKG 等浏览后弹出）。"""

    def __init__(self, path: str, details: list, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("选择要素类")
        self.setMinimumSize(460, 340)
        self._details = details

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(f"文件：{os.path.basename(path)}\n请选择要转换的要素类："))

        self.list = QListWidget()
        for detail in details:
            geometry = QgsWkbTypes.displayString(detail.wkbType())
            text = f"{detail.name()}（{geometry}）" if geometry else detail.name()
            item = QListWidgetItem(text)
            item.setData(Qt.ItemDataRole.UserRole, detail.uri())
            self.list.addItem(item)
        if details:
            self.list.setCurrentRow(0)
        self.list.itemDoubleClicked.connect(lambda *_: self.accept())
        layout.addWidget(self.list, 1)

        buttons = QHBoxLayout()
        buttons.addStretch()
        ok = QPushButton("确定")
        ok.clicked.connect(self.accept)
        buttons.addWidget(ok)
        cancel = QPushButton("取消")
        cancel.clicked.connect(self.reject)
        buttons.addWidget(cancel)
        layout.addLayout(buttons)

    def selected_uri(self) -> str:
        """返回选中的要素类 URI；未选时返回空串。"""
        item = self.list.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item is not None else ""


class VectorFormatDialog(BaseDialog):
    """矢量数据格式转换 Dialog（同步执行）。"""

    def __init__(self, parent=None, title: str = "") -> None:
        super().__init__(parent, title)
        self.setMinimumSize(760, 520)
        self.project = QgsProject.instance()
        self.convert_service = VectorConvertService()
        #: 当前后台转换任务；运行期间不为 None，防止重复触发
        self._task: VectorConvertTask | None = None
        #: 当前任务参数（持有 prepare 产生的临时图层，避免线程使用中被 GC）
        self._job: VectorConvertJob | None = None
        #: 通过「浏览…」加载的本地文件图层（不属于工程，随对话框保留）
        self._file_layers: list = []
        self._init_ui()
        self.project.layersAdded.connect(self._refresh_input_layers)
        self.project.layersRemoved.connect(self._refresh_input_layers)

    def _init_ui(self) -> None:
        layout = QVBoxLayout()

        main = QHBoxLayout()

        param_panel = QWidget()
        self._init_parameters_panel(param_panel)
        main.addWidget(make_scrollable(param_panel), stretch=3)

        main.addWidget(make_description_panel("vector:convert"), stretch=1)
        layout.addLayout(main, stretch=1)

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
        self.fit_height_to_content(param_panel)

    def _init_parameters_panel(self, parent) -> None:
        layout = QVBoxLayout(parent)
        layout.setSpacing(12)

        layout.addWidget(QLabel("输入图层"))
        input_row = QHBoxLayout()
        self.input_combo = QComboBox()
        self._populate_layers()
        input_row.addWidget(self.input_combo, stretch=1)
        browse_input = QPushButton("浏览…")
        browse_input.setToolTip("从本地选择矢量文件（不加载到地图）")
        browse_input.clicked.connect(self._browse_input_file)
        input_row.addWidget(browse_input)
        layout.addLayout(input_row)
        layout.addSpacing(6)

        layout.addWidget(QLabel("输出格式"))
        self.format_combo = QComboBox()
        for spec in VECTOR_FORMATS:
            self.format_combo.addItem(f"{spec.label}（{spec.extension}）", spec)
        self.format_combo.currentIndexChanged.connect(self._on_format_changed)
        layout.addWidget(self.format_combo)
        self.format_hint = QLabel()
        self.format_hint.setWordWrap(True)
        self.format_hint.setObjectName("PropHint")
        layout.addWidget(self.format_hint)
        layout.addSpacing(6)

        layout.addWidget(QLabel("输出文件"))
        path_row = QHBoxLayout()
        self.output_path_edit = QLineEdit()
        self.output_path_edit.setPlaceholderText("选择输出文件路径")
        path_row.addWidget(self.output_path_edit, stretch=1)
        browse_btn = QPushButton("浏览…")
        browse_btn.clicked.connect(self._browse_output_path)
        path_row.addWidget(browse_btn)
        layout.addLayout(path_row)
        layout.addSpacing(6)

        self.only_selected_check = QCheckBox("仅导出选中要素")
        self.only_selected_check.setToolTip("只导出当前图层选中的要素；未选中时不会导出任何要素")
        layout.addWidget(self.only_selected_check)
        self.load_check = QCheckBox("转换后加载到工程")
        self.load_check.setChecked(True)
        layout.addWidget(self.load_check)

        layout.addStretch()
        self._on_format_changed(self.format_combo.currentIndex())

    def _browse_output_path(self) -> None:
        spec = self.format_combo.currentData()
        if spec is None:
            return
        path, _ = QFileDialog.getSaveFileName(
            self,
            "选择输出文件",
            self.output_path_edit.text().strip(),
            spec.file_filter,
        )
        if path:
            self.output_path_edit.setText(_with_extension(path, spec.extension))

    def _populate_layers(self) -> None:
        """把工程矢量图层与本地浏览加载的图层填充进下拉框。"""
        self.input_combo.clear()
        seen_sources: set[str] = set()
        for layer in self.project.mapLayers().values():
            if layer.type() == QgsMapLayer.VectorLayer:
                self.input_combo.addItem(layer.name(), layer)
                with contextlib.suppress(RuntimeError):
                    seen_sources.add(layer.source())
        kept: list = []
        for layer in self._file_layers:
            try:
                if not layer.isValid():
                    continue
                source = layer.source()
            except RuntimeError:
                continue
            if source in seen_sources:
                continue
            kept.append(layer)
            self.input_combo.addItem(f"{layer.name()}（本地文件）", layer)
        self._file_layers = kept

    def _browse_input_file(self) -> None:
        """从本地选择矢量文件直接作为输入（不自动加载到地图）。"""
        path, _ = QFileDialog.getOpenFileName(
            self,
            "选择输入矢量数据",
            "",
            _INPUT_VECTOR_FILTER,
        )
        if not path:
            return
        if os.path.isdir(path):
            MessageBox.warning(
                self,
                "无法直接选择目录",
                "GDB 等目录型数据请先在主界面加载，再在「输入图层」里选择。",
            )
            return

        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        details: list = []
        try:
            details = self._query_vector_sublayers(path)
        except Exception as exc:
            logger.error("读取要素类失败: %s", exc, exc_info=True)
        finally:
            QApplication.restoreOverrideCursor()

        chosen_uri = details[0].uri() if details else ""
        if len(details) > 1:
            chooser = _SublayerChoiceDialog(path, details, self)
            if chooser.exec() != QDialog.DialogCode.Accepted:
                return
            chosen_uri = chooser.selected_uri()
            if not chosen_uri:
                return

        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        candidates: list = []
        try:
            candidates = self._create_sublayers(path, details)
        except Exception as exc:
            logger.error("浏览加载矢量文件失败: %s", exc, exc_info=True)
        finally:
            QApplication.restoreOverrideCursor()
        if not candidates:
            MessageBox.warning(self, "无法打开", f"无法识别为矢量数据：\n{path}")
            return

        chosen = None
        for layer in candidates:
            if not layer.isValid():
                continue
            source = layer.source()
            existing = self._find_layer_by_source(source)
            if existing is not None:
                if chosen is None:
                    chosen = existing
                continue
            self._file_layers.append(layer)
            if chosen is None:
                chosen = layer
        # 优先选中用户在选择框里点的那一层
        for layer in candidates:
            try:
                if layer.source() == chosen_uri:
                    chosen = self._find_layer_by_source(chosen_uri) or layer
                    break
            except RuntimeError:
                continue
        if chosen is None:
            MessageBox.warning(self, "无法打开", f"无法识别为矢量数据：\n{path}")
            return
        self._populate_layers()
        idx = self.input_combo.findData(chosen)
        if idx >= 0:
            self.input_combo.setCurrentIndex(idx)

    def _query_vector_sublayers(self, path: str) -> list:
        """列出文件里的矢量要素类（SQLite / GPKG 等可包含多个图层）。"""
        from app.core.util.sublayers import hidden_tables

        details = QgsProviderRegistry.instance().querySublayers(path)
        hidden = hidden_tables(path)
        return [
            d
            for d in details
            if d.type() == Qgis.LayerType.Vector and d.name() not in hidden
        ]

    def _create_sublayers(self, path: str, details: list) -> list:
        """按要素类 URI 创建图层对象；无法枚举时退回直接打开文件。"""
        if not details:
            layer = QgsVectorLayer(path, os.path.basename(path), "ogr")
            return [layer] if layer.isValid() else []
        multi = len(details) > 1
        stem = os.path.basename(path)
        layers = []
        for detail in details:
            name = f"{stem} / {detail.name()}" if multi else stem
            layer = QgsVectorLayer(detail.uri(), name, "ogr")
            if layer.isValid():
                layers.append(layer)
        return layers

    def _find_layer_by_source(self, source: str):
        """在输入下拉里按 source 找已加载的图层，找不到返回 ``None``。"""
        for i in range(self.input_combo.count()):
            layer = self.input_combo.itemData(i)
            if layer is None:
                continue
            try:
                if layer.source() == source:
                    return layer
            except RuntimeError:
                continue
        return None

    def _refresh_input_layers(self, *args) -> None:
        """工程图层变化后刷新输入下拉，尽量保持当前选择。"""
        current = self.input_combo.currentData()
        self._populate_layers()
        if current is not None:
            idx = self.input_combo.findData(current)
            if idx >= 0:
                self.input_combo.setCurrentIndex(idx)

    def _on_format_changed(self, index: int) -> None:
        spec = self.format_combo.itemData(index)
        if spec is None:
            return
        self.format_hint.setText(spec.note)
        # GeoJSON / KML / GML 是文本格式，QGIS 打开大文件 / 改字段都很慢，
        # 部分驱动还会整文件解析导致 OOM；默认不自动加载（需要的用户可手动勾选）。
        self.load_check.setChecked(spec.load_by_default)
        path = self.output_path_edit.text().strip()
        if path:
            self.output_path_edit.setText(_with_extension(path, spec.extension))

    def _on_execute(self) -> None:
        if self._task is not None:
            send("warning", "转换正在进行，请等待完成", duration=3)
            return
        layer = self.input_combo.currentData()
        if layer is None:
            send("warning", "请选择输入图层（工程图层或浏览本地文件）", duration=3)
            return
        spec = self.format_combo.currentData()
        if spec is None:
            send("warning", "请选择输出格式", duration=3)
            return
        path = self.output_path_edit.text().strip()
        if not path:
            send("warning", "请选择输出文件路径", duration=3)
            return
        path = _with_extension(path, spec.extension)
        self.output_path_edit.setText(path)
        layer_name = os.path.splitext(os.path.basename(path))[0]

        if os.path.exists(path):
            # GPKG / SQLite 只替换同名图层，库内其它图层保留；其它格式整份覆盖。
            if spec.driver in ("GPKG", "SQLite"):
                question = (
                    f"输出文件已存在，将写入 / 替换其中的图层「{layer_name}」，"
                    f"库内其它图层保留。\n{path}\n\n是否继续？"
                )
            else:
                question = (
                    f"输出文件已存在，继续将覆盖整个文件（已有图层会丢失）。\n"
                    f"{path}\n\n是否继续？"
                )
            answer = MessageBox.question(
                self,
                "确认覆盖",
                question,
                MessageBox.StandardButton.Yes | MessageBox.StandardButton.No,
                MessageBox.StandardButton.No,
            )
            if answer != MessageBox.StandardButton.Yes:
                return

        only_selected = self.only_selected_check.isChecked()
        if only_selected and layer.selectedFeatureCount() == 0:
            send("warning", "已勾选「仅导出选中要素」，但输入图层没有选中要素", duration=3)
            return

        job = VectorConvertJob(
            layer=layer,
            output_path=path,
            fmt=spec,
            layer_name=layer_name,
            only_selected=only_selected,
        )

        self._job = job
        self.execute_btn.setEnabled(False)
        # 字段长度修正也放到后台线程；准备阶段进度未知，先显示忙碌状态。
        self.loading.setRange(0, 0)
        self.loading.setValue(0)

        task = VectorConvertTask(self.convert_service, job)
        self._task = task
        task.phase_changed.connect(self._on_task_phase)
        task.progress_updated.connect(self._on_task_progress)
        task.converted.connect(self._on_task_converted)
        task.failed.connect(self._on_task_failed)
        task.finished.connect(self._on_task_finished)
        task.start()

    def _on_task_phase(self, phase: str) -> None:
        """准备阶段（字段修正）显示忙碌，写出阶段切回 0–100 百分比。"""
        if phase == "exporting":
            self.loading.setRange(0, 100)
            self.loading.setValue(0)

    def _on_task_progress(self, value: int) -> None:
        """后台线程进度投递到这里（普通 Python 方法）后更新进度条。"""
        self.loading.setValue(int(value))

    def _on_task_converted(self, path: str, written: int = -1, expected: int = -1) -> None:
        job = self._job
        if job is None:
            return
        self.loading.setRange(0, 100)
        self.loading.setValue(100)
        count_text = f"，{written} 个要素" if written >= 0 else ""
        if job.only_selected and written >= 0:
            count_text = f"，已导出选中要素 {written} 个（源图层共 {job.layer.featureCount()} 个）"
        if expected >= 0 and written >= 0 and written != expected:
            logger.warning("矢量格式转换要素数不一致：预期 %s，实际 %s", expected, written)
            send(
                "warning",
                f"导出要素数与预期不一致：预期 {expected}，实际 {written}。"
                "请检查源图层是否有无效几何，或是否启用了筛选 / 选中集。",
                duration=8,
            )
        loaded = self.load_check.isChecked()
        note = ""
        if loaded:
            try:
                size = os.path.getsize(path)
            except OSError:
                size = 0
            if job.fmt.driver in _TEXT_LOAD_DRIVERS and size > _LARGE_TEXT_LOAD_BYTES:
                # KML / GML 的 OGR 驱动会整文件解析，超大文件直接 OOM；
                # 写出已经成功，这里只跳过加载，不把转换判为失败。
                loaded = False
                self.load_check.setChecked(False)
                note = f"（文件约 {size / (1024 * 1024):.0f} MB，已跳过加载；大文件请用 GeoPackage 查看 / 编辑）"
            else:
                try:
                    result = self.convert_service.load_result(path, job.layer_name)
                    self.project.addMapLayer(result)
                    if job.only_selected:
                        count_text = (
                            f"，已导出选中要素 {result.featureCount()} 个（源图层共 {job.layer.featureCount()} 个）"
                        )
                    else:
                        count_text = f"，{result.featureCount()} 个要素"
                except Exception as exc:
                    logger.warning("矢量格式转换成功，但结果加载失败: %s", exc, exc_info=True)
                    loaded = False
                    note = "（文件已写出，但加载失败；大文件请用 GeoPackage）"
                    send("warning", f"结果已写出，但加载到工程失败：{exc}", duration=5)
        if not loaded and not note:
            note = "（未加载到工程）"
        send(
            "success",
            f"格式转换完成：{job.fmt.label}{count_text} → {job.output_path}{note}",
            duration=3,
        )
        if loaded:
            QTimer.singleShot(0, self._refresh_canvas)

    def _on_task_failed(self, message: str) -> None:
        self.loading.setRange(0, 100)
        self.loading.setValue(0)
        send("critical", f"矢量格式转换失败：{message}", duration=5)

    def _on_task_finished(self) -> None:
        task = self._task
        self._task = None
        self._job = None
        self.loading.setRange(0, 100)
        # 避免下次打开对话框时沿用「仅导出选中要素」导致误以为数据缺失
        self.only_selected_check.setChecked(False)
        self.execute_btn.setEnabled(True)
        if task is not None:
            task.wait(1000)
            task.deleteLater()

    def _refresh_canvas(self) -> None:
        canvas = self.parent().canvas if self.parent() and hasattr(self.parent(), "canvas") else None
        if canvas is not None:
            canvas.refresh()
