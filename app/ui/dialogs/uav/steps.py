"""无人机影像拼接向导的六个步骤页。

每页只负责**本步参数**与展示；扫描、刺点、预检等后台动作由
:class:`~app.ui.dialogs.uav.wizard.UavSurveyWizard` 统一调度。
"""

from __future__ import annotations

import os
from pathlib import Path

from qgis.core import QgsCoordinateReferenceSystem
from qgis.gui import QgsProjectionSelectionWidget
from qgis.PyQt.QtCore import Qt, QUrl
from qgis.PyQt.QtGui import QDesktopServices
from qgis.PyQt.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.core.uav.crs import recommend_crs
from app.core.uav.gcp import validate_gcp
from app.core.uav.intrinsics import estimate_footprint, estimate_gsd
from app.core.uav.position import find_mrk_file
from app.core.uav.precheck import default_output_dir
from app.core.uav.presets import PRESETS, get_preset
from app.ui.common.widgets import make_table, open_file_dir

_MAX_TABLE_ROWS = 200

_ALIGNMENT_LABELS = {"gps": "仅使用影像 GPS（默认）", "gcp": "影像 GPS + 控制点"}
_QUALITY_LABELS = {"low": "低（快）", "medium": "中（推荐）", "high": "高（慢）"}
_SEAMLINE_LABELS = {"auto": "自动拼接线（推荐）", "geometry": "几何拼接线", "none": "不处理"}
_RESAMPLING_LABELS = {"bilinear": "双线性（推荐）", "nearest": "最近邻", "cubic": "三次卷积"}
_FORMAT_LABELS = {"tif": "GeoTIFF + TFW（推荐）", "png": "PNG + PGW", "jpg": "JPEG + JGW"}


def _fmt(value: float | None, digits: int = 1, unit: str = "") -> str:
    if value is None:
        return "—"
    return f"{value:.{digits}f}{unit}"


def _duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    minutes, sec = divmod(seconds, 60)
    if minutes >= 60:
        hours, minutes = divmod(minutes, 60)
        return f"{hours} 小时 {minutes} 分"
    if minutes:
        return f"{minutes} 分 {sec} 秒"
    return f"{sec} 秒"


def _header(title: str, description: str) -> QVBoxLayout:
    box = QVBoxLayout()
    box.setSpacing(4)
    title_label = QLabel(title)
    title_label.setObjectName("SectionLabel")
    description_label = QLabel(description)
    description_label.setObjectName("PropHint")
    description_label.setWordWrap(True)
    box.addWidget(title_label)
    box.addWidget(description_label)
    return box


def _path_row(edit: QLineEdit, *buttons: QPushButton) -> QHBoxLayout:
    row = QHBoxLayout()
    row.setContentsMargins(0, 0, 0, 0)
    row.setSpacing(6)
    row.addWidget(edit, 1)
    for button in buttons:
        row.addWidget(button)
    return row


def _make_form() -> QFormLayout:
    form = QFormLayout()
    form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
    form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
    form.setSpacing(9)
    return form


class StepPage(QWidget):
    """步骤页基类。"""

    title = ""
    description = ""

    def __init__(self, wizard) -> None:
        super().__init__()
        self.wizard = wizard
        self._build_ui()

    @property
    def job(self):
        return self.wizard.job

    def _build_ui(self) -> None:
        raise NotImplementedError

    def refresh(self) -> None:
        """进入本页时从 ``job`` 同步一次界面。"""

    def commit(self) -> None:
        """离开本页前把界面值写回 ``job``。"""

    def validate(self) -> list[str]:
        """硬错误（会阻止「下一步」）。"""
        return []

    def warnings(self) -> list[str]:
        """提醒（只显示，不拦路）。"""
        return []

    def set_scan_result(self, result) -> None:
        """扫描完成（仅第 ① 步使用）。"""

    def show_result(self, outputs) -> None:
        """预检完成（仅第 ⑥ 步使用）。"""

    def show_errors(self, issues) -> None:
        """预检存在 error（仅第 ⑥ 步使用）。"""


# ---------------------------------------------------------------------------
# ① 工程与影像
# ---------------------------------------------------------------------------


class ProjectStep(StepPage):
    title = "① 工程与影像"
    description = "选择航拍照片所在的文件夹，软件会自动读取照片位置和相机参数。"

    def _build_ui(self) -> None:
        root = QVBoxLayout()
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(10)
        root.addLayout(_header(self.title, self.description))

        self._loading = False
        self._loading_crs = False
        self._crs_touched = False
        self._suggestion = None
        self._scan_agl: float | None = None

        form = _make_form()

        self.preset_combo = QComboBox()
        for preset in PRESETS:
            self.preset_combo.addItem(preset.label, preset.key)
        self.preset_combo.currentIndexChanged.connect(self._on_preset_changed)
        form.addRow("处理模式", self.preset_combo)

        self.image_dir_edit = QLineEdit()
        self.image_dir_edit.setPlaceholderText("选择存放航拍照片的文件夹")
        browse_images = QPushButton("浏览…")
        browse_images.clicked.connect(self._choose_image_dir)
        self.scan_button = QPushButton("重新扫描")
        self.scan_button.clicked.connect(self._start_scan)
        form.addRow(
            "无人机影像位置", _path_row(self.image_dir_edit, browse_images, self.scan_button)
        )

        self.position_edit = QLineEdit()
        self.position_edit.setPlaceholderText("可选：POS / MRK 位置文件，默认自动查找 *.MRK")
        browse_position = QPushButton("浏览…")
        browse_position.clicked.connect(self._choose_position_file)
        form.addRow("位置信息文件", _path_row(self.position_edit, browse_position))

        self.crs_widget = QgsProjectionSelectionWidget()
        self.crs_widget.setOptionVisible(
            QgsProjectionSelectionWidget.CrsOption.ProjectCrs, False
        )
        self.crs_widget.crsChanged.connect(self._on_crs_changed)
        crs_box = QVBoxLayout()
        crs_box.setSpacing(4)
        crs_box.addWidget(self.crs_widget)
        recommend_row = QHBoxLayout()
        recommend_row.setContentsMargins(0, 0, 0, 0)
        self.crs_hint = QLabel("扫描影像后自动推荐坐标系")
        self.crs_hint.setObjectName("PropHint")
        self.crs_hint.setWordWrap(True)
        self.recommend_button = QPushButton("使用推荐")
        self.recommend_button.setEnabled(False)
        self.recommend_button.clicked.connect(self._apply_recommended_crs)
        recommend_row.addWidget(self.crs_hint, 1)
        recommend_row.addWidget(self.recommend_button)
        crs_box.addLayout(recommend_row)
        form.addRow("输出坐标系", crs_box)

        self.gcp_edit = QLineEdit()
        self.gcp_edit.setPlaceholderText("可选：控制点 txt / csv")
        browse_gcp = QPushButton("浏览…")
        browse_gcp.clicked.connect(self._choose_gcp_file)
        import_gcp = QPushButton("导入")
        import_gcp.clicked.connect(self._import_gcp)
        form.addRow("控制点文件", _path_row(self.gcp_edit, browse_gcp, import_gcp))

        self.gcp_hint = QLabel("未导入控制点")
        self.gcp_hint.setObjectName("PropHint")
        form.addRow("", self.gcp_hint)

        root.addLayout(form)

        altitude_row = QHBoxLayout()
        altitude_row.setContentsMargins(0, 0, 0, 0)
        altitude_row.addWidget(QLabel("相对航高"))
        self.altitude_spin = QDoubleSpinBox()
        self.altitude_spin.setRange(0.0, 5000.0)
        self.altitude_spin.setDecimals(1)
        self.altitude_spin.setSuffix(" m")
        self.altitude_spin.setSpecialValueText("自动")
        self.altitude_spin.valueChanged.connect(self._on_altitude_changed)
        altitude_row.addWidget(self.altitude_spin)
        altitude_row.addSpacing(12)
        altitude_row.addWidget(QLabel("地面高程（可选）"))
        self.ground_spin = QDoubleSpinBox()
        self.ground_spin.setRange(-500.0, 9000.0)
        self.ground_spin.setDecimals(1)
        self.ground_spin.setSuffix(" m")
        self.ground_spin.setSpecialValueText("自动")
        self.ground_spin.valueChanged.connect(self._on_ground_changed)
        altitude_row.addWidget(self.ground_spin)
        altitude_row.addStretch(1)
        root.addLayout(altitude_row)

        self.summary_label = QLabel("尚未扫描影像")
        self.summary_label.setObjectName("PropHint")
        self.summary_label.setWordWrap(True)
        root.addWidget(self.summary_label)

        self.image_table = make_table(
            ["文件", "经度", "纬度", "椭球高", "相对航高", "航向", "RTK", "来源"]
        )
        self.image_table.setMinimumHeight(180)
        self.image_table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch
        )
        root.addWidget(self.image_table, 1)

        self.table_hint = QLabel()
        self.table_hint.setObjectName("PropHint")
        root.addWidget(self.table_hint)

        self._apply_crs(self.job.crs_epsg, touched=False)
        self.setLayout(root)

    # ------------------------------------------------------------ 扫描

    def _choose_image_dir(self) -> None:
        start = self.image_dir_edit.text().strip() or self.job.image_dir
        path = open_file_dir(
            self, is_dir=True, title="选择无人机影像文件夹", init_path=start
        )
        if not path:
            return
        self.image_dir_edit.setText(path)
        self._start_scan()

    def _start_scan(self) -> None:
        path = self.image_dir_edit.text().strip()
        if not path:
            self.wizard.show_message("请先选择无人机影像文件夹")
            return
        if not os.path.isdir(path):
            self.wizard.show_message("影像文件夹不存在，请重新选择")
            return
        self.wizard.start_scan(path, self.position_edit.text().strip())

    def _choose_position_file(self) -> None:
        start = self.position_edit.text().strip() or self.image_dir_edit.text().strip()
        path = open_file_dir(
            self,
            title="选择位置信息文件",
            init_path=start,
            filter_="位置文件 (*.txt *.csv *.pos *.MRK *.mrk);;所有文件 (*)",
        )
        if not path:
            return
        self.position_edit.setText(path)
        if self.job.images:
            self._start_scan()

    def set_scan_result(self, result) -> None:
        summary = result.summary
        self._scan_agl = summary.mean_agl
        self._refresh_summary(summary)
        self._populate_images(result.images)
        if result.position_source == "MRK" and not self.position_edit.text().strip():
            mrk = find_mrk_file(self.job.image_dir)
            if mrk:
                self.position_edit.setText(mrk)
                self.wizard.show_message("已自动识别同目录的 MRK 位置文件")
        if summary.mean_agl is not None:
            self._loading = True
            self.altitude_spin.setValue(round(summary.mean_agl, 1))
            self._loading = False
            self.job.relative_altitude_m = round(summary.mean_agl, 2)
        self._update_crs_suggestion(summary)
        self._update_crs_hint()
        suggestion = self._suggestion
        if suggestion is not None and not self._crs_touched:
            self._apply_crs(suggestion.epsg, touched=False)

    def _refresh_summary(self, summary=None) -> None:
        summary = summary or self.job.summary
        if summary is None:
            self.summary_label.setText("尚未扫描影像")
            return
        self.summary_label.setText(
            f"{summary.image_count} 张影像 · GPS {summary.positioned_count}/"
            f"{summary.image_count} · RTK 固定解 {summary.rtk_fixed_count} · "
            f"相对航高 {_fmt(summary.mean_agl, 1, ' m')} · "
            f"GSD {_fmt(summary.gsd_m * 100 if summary.gsd_m else None, 2, ' cm')} · "
            f"估算重叠 {_fmt(summary.forward_overlap * 100 if summary.forward_overlap is not None else None, 0, '%')}"
        )

    def _populate_images(self, images) -> None:
        shown = images[:_MAX_TABLE_ROWS]
        self.image_table.setRowCount(len(shown))
        for row, image in enumerate(shown):
            values = [
                image.name,
                _fmt(image.lon, 6),
                _fmt(image.lat, 6),
                _fmt(image.altitude_ellipsoid, 1),
                _fmt(image.relative_altitude, 1),
                _fmt(image.flight_yaw if image.flight_yaw is not None else image.gimbal_yaw, 1),
                "固定解" if image.rtk_flag == 50 else ("—" if image.rtk_flag is None else str(image.rtk_flag)),
                image.source or "—",
            ]
            for column, text in enumerate(values):
                self.image_table.setItem(row, column, QTableWidgetItem(str(text)))
        if len(images) > _MAX_TABLE_ROWS:
            self.table_hint.setText(
                f"共 {len(images)} 张，表格只显示前 {_MAX_TABLE_ROWS} 张；"
                "完整清单会写入任务清单。"
            )
        else:
            self.table_hint.setText("")

    def _on_altitude_changed(self, value: float) -> None:
        if self._loading:
            return
        manual = round(value, 2) if value > 0 else None
        self.job.relative_altitude_m = manual
        if self.job.summary is not None:
            self.job.summary.mean_agl = manual if manual is not None else self._scan_agl
        self._recompute_gsd()
        self._refresh_summary()

    def _on_ground_changed(self, value: float) -> None:
        if self._loading:
            return
        self.job.ground_elevation_m = value if value > 0 else 0.0

    def _recompute_gsd(self) -> None:
        summary = self.job.summary
        if summary is None or not self.job.images:
            return
        altitude = self.job.relative_altitude_m or self._scan_agl
        intrinsics = next(
            (image.intrinsics for image in self.job.images if image.intrinsics.is_complete),
            None,
        )
        if altitude is None:
            summary.gsd_m = None
            summary.footprint_width_m = None
            summary.footprint_height_m = None
            summary.forward_overlap = None
            return
        if intrinsics is None:
            return
        summary.gsd_m = estimate_gsd(
            altitude, intrinsics.focal_mm, intrinsics.sensor_width_mm, intrinsics.width_px
        )
        footprint = estimate_footprint(
            altitude,
            intrinsics.focal_mm,
            intrinsics.sensor_width_mm,
            intrinsics.width_px,
            intrinsics.height_px,
        )
        if footprint is not None:
            summary.footprint_width_m, summary.footprint_height_m = footprint
            if summary.median_spacing_m and summary.footprint_height_m:
                summary.forward_overlap = min(
                    1.0, max(0.0, 1.0 - summary.median_spacing_m / summary.footprint_height_m)
                )
            else:
                summary.forward_overlap = None

    # ------------------------------------------------------------ 坐标系

    def _apply_crs(self, epsg: int, *, touched: bool = False) -> None:
        self._loading_crs = True
        self.crs_widget.setCrs(QgsCoordinateReferenceSystem(f"EPSG:{epsg}"))
        self._loading_crs = False
        self.job.crs_epsg = int(epsg)
        if touched:
            self._crs_touched = True
        self._update_crs_hint()

    def _apply_recommended_crs(self) -> None:
        if self._suggestion is not None:
            self._apply_crs(self._suggestion.epsg, touched=True)

    def _on_crs_changed(self, crs) -> None:
        if self._loading_crs:
            return
        authid = crs.authid()
        if not authid.upper().startswith("EPSG:"):
            self.wizard.show_message(
                "任务清单只记录 EPSG 标准坐标系；已恢复上一个坐标系，"
                "请从标准坐标系列表选择或点击「使用推荐」。"
            )
            self._loading_crs = True
            self.crs_widget.setCrs(
                QgsCoordinateReferenceSystem(f"EPSG:{self.job.crs_epsg}")
            )
            self._loading_crs = False
            return
        try:
            self.job.crs_epsg = int(authid.split(":", 1)[1])
        except ValueError:
            return
        self._crs_touched = True
        self._update_crs_hint()

    def _update_crs_suggestion(self, summary) -> None:
        self._suggestion = None
        if summary is None or summary.bbox is None:
            self._update_crs_hint()
            return
        lon_min, lat_min, lon_max, lat_max = summary.bbox
        self._suggestion = recommend_crs(
            (lon_min + lon_max) / 2.0, (lat_min + lat_max) / 2.0
        )

    def _update_crs_hint(self) -> None:
        if self._suggestion is not None:
            self.crs_hint.setText(
                f"推荐：{self._suggestion.name}（EPSG:{self._suggestion.epsg}）；"
                "国内测绘成果不要直接用 WGS84 经纬度出图。"
            )
            self.recommend_button.setEnabled(True)
        else:
            self.crs_hint.setText(
                "国内测绘成果通常使用 CGCS2000 高斯-克吕格投影，不建议直接用经纬度。"
            )
            self.recommend_button.setEnabled(False)

    # ------------------------------------------------------------ 预设 / GCP

    def _on_preset_changed(self, _index: int) -> None:
        if self._loading:
            return
        key = self.preset_combo.currentData()
        if not key or key == self.job.preset:
            return
        if not self.wizard.apply_preset(str(key)):
            self._loading = True
            index = self.preset_combo.findData(self.job.preset)
            if index >= 0:
                self.preset_combo.setCurrentIndex(index)
            self._loading = False

    def _choose_gcp_file(self) -> None:
        start = self.gcp_edit.text().strip() or self.job.image_dir
        path = open_file_dir(
            self,
            title="选择控制点文件",
            init_path=start,
            filter_="控制点文件 (*.csv *.txt);;所有文件 (*)",
        )
        if not path:
            return
        self.gcp_edit.setText(path)
        self._import_gcp()

    def _import_gcp(self) -> None:
        path = self.gcp_edit.text().strip()
        if not path:
            self.wizard.show_message("请先选择控制点文件")
            return
        self.wizard.import_gcp(path)

    def set_gcp(self) -> None:
        gcp = self.job.gcp
        if gcp is None or not gcp.points:
            self.gcp_hint.setText("未导入控制点")
            return
        text = (
            f"控制点 {len(gcp.control_points)} 个，检查点 {len(gcp.check_points)} 个，"
            f"已刺点 {gcp.marked_point_count}/{len(gcp.points)} 个"
        )
        if gcp.errors:
            text += f"；{len(gcp.errors)} 行未能解析"
        self.gcp_hint.setText(text)

    # ------------------------------------------------------------ 生命周期

    def refresh(self) -> None:
        self._loading = True
        self.preset_combo.setCurrentIndex(max(0, self.preset_combo.findData(self.job.preset)))
        if not self.image_dir_edit.text().strip() and self.job.image_dir:
            self.image_dir_edit.setText(self.job.image_dir)
        if not self.position_edit.text().strip() and self.job.position_file:
            self.position_edit.setText(self.job.position_file)
        if not self.gcp_edit.text().strip() and self.job.gcp:
            self.gcp_edit.setText(self.job.gcp.source_path)
        if not self.gcp_edit.text().strip() and self.job.gcp_file:
            self.gcp_edit.setText(self.job.gcp_file)
        if self.job.gcp is not None:
            self.set_gcp()
        elif self.gcp_edit.text().strip():
            self.gcp_hint.setText("控制点文件尚未导入，请点击「导入」")
        else:
            self.gcp_hint.setText("未导入控制点")
        self._update_crs_hint()
        self._loading = False

    def commit(self) -> None:
        job = self.wizard.job
        job.image_dir = self.image_dir_edit.text().strip()
        job.position_file = self.position_edit.text().strip()
        gcp_path = self.gcp_edit.text().strip()
        job.gcp_file = gcp_path
        if not gcp_path:
            job.gcp = None
            if job.alignment_mode == "gcp":
                job.alignment_mode = "gps"

    def validate(self) -> list[str]:
        problems: list[str] = []
        image_dir = self.image_dir_edit.text().strip()
        if not image_dir:
            problems.append("请选择无人机影像文件夹")
        elif not os.path.isdir(image_dir):
            problems.append("影像文件夹不存在，请重新选择")
        elif not self.job.images:
            problems.append("影像尚未扫描或文件夹里没有可处理的影像")
        if not self.crs_widget.crs().isValid():
            problems.append("请选择有效的输出坐标系")
        elif not self.crs_widget.crs().authid().upper().startswith("EPSG:"):
            problems.append("请选择带 EPSG 编码的标准坐标系")
        if self.gcp_edit.text().strip():
            gcp = self.job.gcp
            if gcp is None or gcp.source_path != self.gcp_edit.text().strip():
                problems.append("控制点文件尚未导入，请点击「导入」")
            elif gcp.errors and not gcp.points:
                problems.append("控制点文件无法解析，请检查格式")
        return problems


# ---------------------------------------------------------------------------
# ② 空三解算
# ---------------------------------------------------------------------------


class AlignmentStep(StepPage):
    title = "② 空三解算"
    description = (
        "软件会提取同名点，解算每张照片的位置、姿态和镜头畸变；"
        "控制点可以明显提高成果精度。"
    )

    def _build_ui(self) -> None:
        root = QVBoxLayout()
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(10)
        root.addLayout(_header(self.title, self.description))

        form = _make_form()
        self.alignment_combo = QComboBox()
        for key, label in _ALIGNMENT_LABELS.items():
            self.alignment_combo.addItem(label, key)
        self.alignment_combo.currentIndexChanged.connect(self._on_alignment_changed)
        form.addRow("空三模式", self.alignment_combo)
        root.addLayout(form)

        gcp_row = QHBoxLayout()
        gcp_row.setContentsMargins(0, 0, 0, 0)
        self.gcp_label = QLabel("未导入控制点")
        self.gcp_label.setObjectName("PropHint")
        self.gcp_label.setWordWrap(True)
        self.mark_button = QPushButton("打开刺点…")
        self.mark_button.clicked.connect(self._open_marks)
        gcp_row.addWidget(self.gcp_label, 1)
        gcp_row.addWidget(self.mark_button)
        root.addLayout(gcp_row)

        self.point_table = make_table(["点号", "类型", "刺点影像", "状态"])
        self.point_table.setMinimumHeight(150)
        self.point_table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch
        )
        root.addWidget(self.point_table, 1)

        self.checklist_label = QLabel()
        self.checklist_label.setObjectName("PropHint")
        self.checklist_label.setWordWrap(True)
        root.addWidget(self.checklist_label)

        reasons = QLabel(
            "空三失败常见原因：航向重叠不足（低于 60%）、地面纹理单一（水面/草地/雪地）、"
            "照片模糊、曝光差异过大、部分照片缺少定位信息。"
        )
        reasons.setObjectName("PropHint")
        reasons.setWordWrap(True)
        root.addWidget(reasons)
        self.setLayout(root)

    def _on_alignment_changed(self, _index: int) -> None:
        key = self.alignment_combo.currentData()
        if key:
            self.job.alignment_mode = str(key)

    def _open_marks(self) -> None:
        self.wizard.open_mark_dialog()

    def set_gcp(self) -> None:
        gcp = self.job.gcp
        self.point_table.setRowCount(0)
        if gcp is None or not gcp.points:
            self.gcp_label.setText("未导入控制点。可在第 ① 步导入；仅用影像 GPS 也能处理。")
            self.mark_button.setEnabled(False)
            return
        self.mark_button.setEnabled(bool(self.job.images))
        self.gcp_label.setText(
            f"{Path(gcp.source_path).name if gcp.source_path else '控制点'}："
            f"控制点 {len(gcp.control_points)} 个，检查点 {len(gcp.check_points)} 个，"
            f"共 {gcp.total_marks} 个刺点"
        )
        self.point_table.setRowCount(len(gcp.points))
        for row, point in enumerate(gcp.points):
            self.point_table.setItem(row, 0, QTableWidgetItem(point.name))
            self.point_table.setItem(row, 1, QTableWidgetItem(point.role))
            self.point_table.setItem(row, 2, QTableWidgetItem(str(len(point.marks))))
            status = "已刺点" if point.marks else "未刺点"
            self.point_table.setItem(row, 3, QTableWidgetItem(status))

    def _checklist(self) -> list[str]:
        summary = self.job.summary
        items: list[str] = []
        if summary is not None:
            items.append(
                f"影像 {summary.image_count} 张，有定位 {summary.positioned_count} 张"
            )
            if summary.forward_overlap is not None:
                items.append(f"估算航向重叠 {summary.forward_overlap * 100:.0f}%")
            if summary.gsd_m:
                items.append(f"估算 GSD {summary.gsd_m * 100:.2f} cm")
        else:
            items.append("尚未扫描影像")
        gcp = self.job.gcp
        if gcp is not None and gcp.points:
            items.append(
                f"控制点 {len(gcp.control_points)} 个，已刺 {gcp.marked_point_count}/{len(gcp.points)} 个"
            )
            items.extend(validate_gcp(gcp))
            for point in gcp.check_points:
                items.append(f"检查点 {point.name} 残差：空三完成后显示")
        else:
            items.append("未导入控制点：将仅使用影像 GPS")
        return items

    def refresh(self) -> None:
        index = self.alignment_combo.findData(self.job.alignment_mode)
        self.alignment_combo.blockSignals(True)
        self.alignment_combo.setCurrentIndex(max(0, index))
        self.alignment_combo.blockSignals(False)
        self.set_gcp()
        self.checklist_label.setText("\n".join(f"· {item}" for item in self._checklist()))

    def warnings(self) -> list[str]:
        warnings: list[str] = []
        preset = get_preset(self.job.preset)
        gcp = self.job.gcp
        if preset.require_gcp:
            if gcp is None or not gcp.control_points:
                warnings.append("高精度模式要求至少 3 个控制点，请先导入并刺点")
            elif len(gcp.control_points) < 3:
                warnings.append(
                    f"高精度模式建议至少 3 个控制点，当前只有 {len(gcp.control_points)} 个"
                )
            else:
                warnings.extend(validate_gcp(gcp))
        elif gcp is not None and gcp.points:
            warnings.extend(validate_gcp(gcp))
        return warnings


# ---------------------------------------------------------------------------
# ③ 密集点云
# ---------------------------------------------------------------------------


class DenseStep(StepPage):
    title = "③ 密集点云"
    description = (
        "空三得到稀疏点云后，进一步匹配每个像素，生成密集点云；"
        "它是 DSM 和高质量正射影像的基础。"
    )

    def _build_ui(self) -> None:
        root = QVBoxLayout()
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(10)
        root.addLayout(_header(self.title, self.description))

        self.dense_check = QCheckBox("生成密集点云（推荐）")
        self.dense_check.toggled.connect(self._on_dense_toggled)
        root.addWidget(self.dense_check)

        form = _make_form()
        self.quality_combo = QComboBox()
        for key, label in _QUALITY_LABELS.items():
            self.quality_combo.addItem(label, key)
        self.quality_combo.currentIndexChanged.connect(self._on_quality_changed)
        form.addRow("点云质量", self.quality_combo)
        root.addLayout(form)

        note = QLabel(
            "质量越高，点云越密、DSM 越细，但耗时和磁盘占用也明显增加。"
            "快拼模式会跳过本步。"
        )
        note.setObjectName("PropHint")
        note.setWordWrap(True)
        root.addWidget(note)
        root.addStretch(1)
        self.setLayout(root)

    def _on_dense_toggled(self, checked: bool) -> None:
        self.job.dense_cloud = bool(checked)
        self.quality_combo.setEnabled(bool(checked))

    def _on_quality_changed(self, _index: int) -> None:
        key = self.quality_combo.currentData()
        if key:
            self.job.dense_quality = str(key)

    def refresh(self) -> None:
        self.dense_check.blockSignals(True)
        self.quality_combo.blockSignals(True)
        self.dense_check.setChecked(self.job.dense_cloud)
        self.quality_combo.setCurrentIndex(
            max(0, self.quality_combo.findData(self.job.dense_quality))
        )
        self.dense_check.blockSignals(False)
        self.quality_combo.blockSignals(False)
        self.quality_combo.setEnabled(self.job.dense_cloud)


# ---------------------------------------------------------------------------
# ④ DSM / DTM
# ---------------------------------------------------------------------------


class SurfaceStep(StepPage):
    title = "④ DSM / DTM"
    description = (
        "DSM 记录地表（含建筑、植被）高程；DTM 只保留裸地高程，"
        "需要先对点云做地面分类。正射影像的正射校正依赖 DSM。"
    )

    def _build_ui(self) -> None:
        root = QVBoxLayout()
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(10)
        root.addLayout(_header(self.title, self.description))

        self.dsm_check = QCheckBox("生成 DSM（推荐，正射校正需要）")
        self.dsm_check.toggled.connect(self._on_dsm_toggled)
        root.addWidget(self.dsm_check)

        self.dtm_check = QCheckBox("生成 DTM（需要点云分类，耗时更长）")
        self.dtm_check.toggled.connect(self._on_dtm_toggled)
        root.addWidget(self.dtm_check)

        form = _make_form()
        self.resolution_spin = QDoubleSpinBox()
        self.resolution_spin.setRange(0.0, 10.0)
        self.resolution_spin.setDecimals(2)
        self.resolution_spin.setSingleStep(0.05)
        self.resolution_spin.setSuffix(" m")
        self.resolution_spin.setSpecialValueText("自动（按 GSD）")
        self.resolution_spin.valueChanged.connect(self._on_resolution_changed)
        form.addRow("DSM 分辨率", self.resolution_spin)
        root.addLayout(form)

        note = QLabel(
            "不生成 DSM 时，正射校正只能用平均高程，地形起伏大的区域会出现明显错位。"
        )
        note.setObjectName("PropHint")
        note.setWordWrap(True)
        root.addWidget(note)
        root.addStretch(1)
        self.setLayout(root)

    def _on_dsm_toggled(self, checked: bool) -> None:
        self.job.build_dsm = bool(checked)

    def _on_dtm_toggled(self, checked: bool) -> None:
        self.job.build_dtm = bool(checked)

    def _on_resolution_changed(self, value: float) -> None:
        self.job.dsm_resolution_m = float(value)

    def refresh(self) -> None:
        self.dsm_check.blockSignals(True)
        self.dtm_check.blockSignals(True)
        self.resolution_spin.blockSignals(True)
        self.dsm_check.setChecked(self.job.build_dsm)
        self.dtm_check.setChecked(self.job.build_dtm)
        self.resolution_spin.setValue(self.job.dsm_resolution_m)
        self.dsm_check.blockSignals(False)
        self.dtm_check.blockSignals(False)
        self.resolution_spin.blockSignals(False)
        dtm_enabled = self.job.dense_cloud
        self.dtm_check.setEnabled(dtm_enabled)
        if not dtm_enabled and self.dtm_check.isChecked():
            self.dtm_check.setChecked(False)


# ---------------------------------------------------------------------------
# ⑤ 正射 / 镶嵌
# ---------------------------------------------------------------------------


class OrthoStep(StepPage):
    title = "⑤ 正射 / 镶嵌"
    description = (
        "把每张照片校正成正射影像，再按拼接线无缝拼接、统一颜色，"
        "得到最终的 DOM 正射影像。"
    )

    def _build_ui(self) -> None:
        root = QVBoxLayout()
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(10)
        root.addLayout(_header(self.title, self.description))

        form = _make_form()
        self.gsd_combo = QComboBox()
        self.gsd_combo.currentIndexChanged.connect(self._on_gsd_changed)
        form.addRow("输出 GSD", self.gsd_combo)

        self.gsd_spin = QDoubleSpinBox()
        self.gsd_spin.setRange(0.2, 50.0)
        self.gsd_spin.setDecimals(2)
        self.gsd_spin.setSingleStep(0.5)
        self.gsd_spin.setSuffix(" cm")
        self.gsd_spin.valueChanged.connect(self._on_gsd_spin_changed)
        form.addRow("自定义 GSD", self.gsd_spin)

        self.blend_check = QCheckBox("匀色处理（推荐）")
        self.blend_check.toggled.connect(self._on_blend_toggled)
        form.addRow("", self.blend_check)

        self.seamline_combo = QComboBox()
        for key, label in _SEAMLINE_LABELS.items():
            self.seamline_combo.addItem(label, key)
        self.seamline_combo.currentIndexChanged.connect(self._on_seamline_changed)
        form.addRow("拼接线", self.seamline_combo)

        self.resampling_combo = QComboBox()
        for key, label in _RESAMPLING_LABELS.items():
            self.resampling_combo.addItem(label, key)
        self.resampling_combo.currentIndexChanged.connect(self._on_resampling_changed)
        form.addRow("重采样", self.resampling_combo)
        root.addLayout(form)

        self.gsd_hint = QLabel()
        self.gsd_hint.setObjectName("PropHint")
        self.gsd_hint.setWordWrap(True)
        root.addWidget(self.gsd_hint)

        note = QLabel(
            "GSD 越小越清晰，但文件更大、处理更慢。"
            "自动模式按相对航高与相机参数估算，实测数据约 2.2 cm。"
        )
        note.setObjectName("PropHint")
        note.setWordWrap(True)
        root.addWidget(note)
        root.addStretch(1)
        self.setLayout(root)

    # ------------------------------------------------------------ 交互

    def _populate_gsd_choices(self) -> None:
        estimate = self._estimated_gsd_cm()
        self.gsd_combo.blockSignals(True)
        self.gsd_combo.clear()
        auto_label = "自动" + (f"（≈{estimate:.2f} cm）" if estimate else "")
        self.gsd_combo.addItem(auto_label, "auto")
        self.gsd_combo.addItem("5 cm", "5")
        self.gsd_combo.addItem("10 cm", "10")
        self.gsd_combo.addItem("自定义…", "custom")
        self.gsd_combo.blockSignals(False)

    def _estimated_gsd_cm(self) -> float | None:
        summary = self.job.summary
        if summary is not None and summary.gsd_m:
            return summary.gsd_m * 100.0
        return None

    def _current_choice(self) -> str:
        if self.job.gsd_mode != "manual" or self.job.gsd_cm <= 0:
            return "auto"
        if abs(self.job.gsd_cm - 5.0) < 1e-6:
            return "5"
        if abs(self.job.gsd_cm - 10.0) < 1e-6:
            return "10"
        return "custom"

    def _on_gsd_changed(self, _index: int) -> None:
        key = str(self.gsd_combo.currentData() or "auto")
        if key == "auto":
            self.job.gsd_mode = "auto"
            self.job.gsd_cm = 0.0
            self.gsd_spin.setEnabled(False)
        elif key == "custom":
            self.job.gsd_mode = "manual"
            self.job.gsd_cm = float(self.gsd_spin.value())
            self.gsd_spin.setEnabled(True)
        else:
            self.job.gsd_mode = "manual"
            self.job.gsd_cm = float(key)
            self.gsd_spin.setValue(self.job.gsd_cm)
            self.gsd_spin.setEnabled(False)
        self._update_gsd_hint()

    def _on_gsd_spin_changed(self, value: float) -> None:
        if self.job.gsd_mode != "manual":
            return
        self.job.gsd_cm = float(value)
        self._update_gsd_hint()

    def _on_blend_toggled(self, checked: bool) -> None:
        self.job.blend = bool(checked)

    def _on_seamline_changed(self, _index: int) -> None:
        key = self.seamline_combo.currentData()
        if key:
            self.job.seamline = str(key)

    def _on_resampling_changed(self, _index: int) -> None:
        key = self.resampling_combo.currentData()
        if key:
            self.job.resampling = str(key)

    def _update_gsd_hint(self) -> None:
        estimate = self._estimated_gsd_cm()
        if self.job.gsd_mode == "manual" and self.job.gsd_cm > 0:
            self.gsd_hint.setText(
                f"将输出 {self.job.gsd_cm:g} cm/px 的正射影像。"
                + (f"（估算原始 GSD ≈ {estimate:.2f} cm）" if estimate else "")
            )
        else:
            self.gsd_hint.setText(
                f"自动按原始分辨率输出，约 {estimate:.2f} cm/px。" if estimate else "自动按原始分辨率输出。"
            )

    def refresh(self) -> None:
        self._populate_gsd_choices()
        choice = self._current_choice()
        self.gsd_combo.blockSignals(True)
        index = self.gsd_combo.findData(choice)
        self.gsd_combo.setCurrentIndex(max(0, index))
        self.gsd_combo.blockSignals(False)
        self.gsd_spin.blockSignals(True)
        if self.job.gsd_cm > 0:
            self.gsd_spin.setValue(self.job.gsd_cm)
        self.gsd_spin.blockSignals(False)
        self.gsd_spin.setEnabled(choice == "custom")

        self.blend_check.blockSignals(True)
        self.blend_check.setChecked(self.job.blend)
        self.blend_check.blockSignals(False)

        self.seamline_combo.blockSignals(True)
        self.seamline_combo.setCurrentIndex(
            max(0, self.seamline_combo.findData(self.job.seamline))
        )
        self.seamline_combo.blockSignals(False)

        self.resampling_combo.blockSignals(True)
        self.resampling_combo.setCurrentIndex(
            max(0, self.resampling_combo.findData(self.job.resampling))
        )
        self.resampling_combo.blockSignals(False)
        self._update_gsd_hint()

    def validate(self) -> list[str]:
        if self.job.gsd_mode == "manual" and self.job.gsd_cm <= 0:
            return ["请填写大于 0 的输出 GSD"]
        return []


# ---------------------------------------------------------------------------
# ⑥ 导出 / 质检
# ---------------------------------------------------------------------------


class ExportStep(StepPage):
    title = "⑥ 导出 / 质检"
    description = (
        "确认输出目录与格式后开始处理。软件会依次完成空三、密集点云、"
        "DSM 与正射影像，并在最后生成质检报告。"
    )

    def _build_ui(self) -> None:
        root = QVBoxLayout()
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(10)
        root.addLayout(_header(self.title, self.description))

        engine_row = QHBoxLayout()
        self.engine_label = QLabel("拼接引擎：未检测")
        self.engine_label.setObjectName("PropHint")
        self.engine_label.setWordWrap(True)
        self.engine_button = QPushButton("引擎设置…")
        engine_row.addWidget(self.engine_label, 1)
        engine_row.addWidget(self.engine_button)
        root.addLayout(engine_row)

        form = _make_form()
        self.output_edit = QLineEdit()
        self.output_edit.setPlaceholderText("成果输出目录")
        self.output_edit.editingFinished.connect(self.commit)
        browse_output = QPushButton("浏览…")
        browse_output.clicked.connect(self._choose_output_dir)
        self.open_output_button = QPushButton("打开")
        self.open_output_button.clicked.connect(self._open_output_dir)
        form.addRow(
            "输出目录",
            _path_row(self.output_edit, browse_output, self.open_output_button),
        )

        self.format_combo = QComboBox()
        for key, label in _FORMAT_LABELS.items():
            self.format_combo.addItem(label, key)
        self.format_combo.currentIndexChanged.connect(self._on_format_changed)
        form.addRow("影像格式", self.format_combo)

        self.positions_check = QCheckBox("完成后在地图显示影像位置")
        self.positions_check.toggled.connect(self._on_positions_toggled)
        form.addRow("", self.positions_check)
        root.addLayout(form)

        self.checklist_label = QLabel()
        self.checklist_label.setObjectName("PropHint")
        self.checklist_label.setWordWrap(True)
        root.addWidget(self.checklist_label)

        self.result_label = QLabel()
        self.result_label.setObjectName("PropHint")
        self.result_label.setWordWrap(True)
        self.result_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        root.addWidget(self.result_label)

        self.result_buttons = QHBoxLayout()
        self.open_dir_button = QPushButton("打开输出目录")
        self.open_dir_button.clicked.connect(self._open_output_dir)
        self.open_report_button = QPushButton("打开预检报告")
        self.open_report_button.clicked.connect(self._open_report)
        self.result_buttons.addWidget(self.open_dir_button)
        self.result_buttons.addWidget(self.open_report_button)
        self.result_buttons.addStretch(1)
        self.open_dir_button.setVisible(False)
        self.open_report_button.setVisible(False)
        root.addLayout(self.result_buttons)
        root.addStretch(1)
        self.setLayout(root)
        self._report_path = ""

    # ------------------------------------------------------------ 交互

    def _choose_output_dir(self) -> None:
        start = self.output_edit.text().strip() or self.job.image_dir
        path = open_file_dir(self, is_dir=True, title="选择成果输出目录", init_path=start)
        if not path:
            return
        self.output_edit.setText(path)
        self.commit()
        self.refresh()

    def _open_output_dir(self) -> None:
        path = self.output_edit.text().strip() or self.job.output_dir
        if path:
            QDesktopServices.openUrl(QUrl.fromLocalFile(path))

    def _open_report(self) -> None:
        if self._report_path:
            QDesktopServices.openUrl(QUrl.fromLocalFile(self._report_path))

    def set_output_dir(self, path: str) -> None:
        """由向导在切换影像目录时更新默认输出目录。"""
        self.output_edit.setText(path)
        self.job.output_dir = path
        self.refresh()

    def _on_format_changed(self, _index: int) -> None:
        key = self.format_combo.currentData()
        if key:
            self.job.output_format = str(key)

    def _on_positions_toggled(self, checked: bool) -> None:
        self.job.show_positions = bool(checked)

    # ------------------------------------------------------------ 数据

    def _checklist(self) -> list[str]:
        summary = self.job.summary
        items: list[str] = []
        if summary is not None:
            items.append(f"影像 {summary.image_count} 张，GPS {summary.positioned_count} 张")
        items.append(f"输出坐标系 EPSG:{self.job.crs_epsg}")
        gcp = self.job.gcp
        if gcp is not None and gcp.points:
            items.append(
                f"控制点已刺 {gcp.marked_point_count}/{len(gcp.points)} 个"
            )
        else:
            items.append("未导入控制点（可正常处理，精度依赖影像 GPS）")
        preset = get_preset(self.job.preset)
        items.append(f"处理模式：{preset.label}")
        return items

    def refresh(self) -> None:
        if not self.output_edit.text().strip():
            path = self.job.output_dir or default_output_dir(self.job.image_dir)
            self.output_edit.setText(path)
            self.job.output_dir = path
        self.format_combo.blockSignals(True)
        self.format_combo.setCurrentIndex(
            max(0, self.format_combo.findData(self.job.output_format))
        )
        self.format_combo.blockSignals(False)
        self.positions_check.blockSignals(True)
        self.positions_check.setChecked(self.job.show_positions)
        self.positions_check.blockSignals(False)
        self.checklist_label.setText("\n".join(f"· {item}" for item in self._checklist()))

    def commit(self) -> None:
        self.job.output_dir = self.output_edit.text().strip()

    def validate(self) -> list[str]:
        if not self.output_edit.text().strip():
            return ["请选择成果输出目录"]
        return []

    def show_result(self, outputs) -> None:
        self._report_path = outputs.report_path
        self.result_label.setText(
            f"预检完成：{outputs.error_count} 个错误，{outputs.warning_count} 个警告。\n"
            f"预检报告：{outputs.report_path}\n任务清单：{outputs.manifest_path}"
        )
        self.result_label.setStyleSheet("")
        self.open_dir_button.setVisible(True)
        self.open_report_button.setVisible(True)

    def set_engine_status(self, text: str, ok: bool) -> None:
        self.engine_label.setText(f"拼接引擎：{text}")
        self.engine_label.setStyleSheet("" if ok else "color: #b8860b;")

    def show_processing_result(self, result) -> None:
        """显示真实解算完成后的成果摘要与路径。"""
        outputs = result.outputs
        summary = result.summary
        quality = outputs.quality or {}
        lines = [
            f"✔ 处理完成，用时 {_duration(result.elapsed_seconds)}",
            f"参与空三影像：{summary.registered_images}/{summary.total_images}，"
            f"重投影误差 {_fmt(summary.reprojection_error_px, 2, ' px')}",
        ]
        if quality.get("gcp_average_m") is not None:
            lines.append(
                f"控制点中误差：{_fmt(quality.get('gcp_average_m') * 100, 1, ' cm')}"
            )
        if quality.get("checkpoint_average_m") is not None:
            lines.append(
                f"检查点中误差：{_fmt(quality.get('checkpoint_average_m') * 100, 1, ' cm')}"
            )
        lines.append("")
        for label, path in (
            ("正射影像 DOM", outputs.dom),
            ("数字表面模型 DSM", outputs.dsm),
            ("数字地面模型 DTM", outputs.dtm),
            ("密集点云", outputs.point_cloud),
            ("质检报告", outputs.report),
        ):
            if path:
                lines.append(f"{label}：{path}")
        if outputs.warnings:
            lines.append("")
            lines.extend(f"⚠ {warning}" for warning in outputs.warnings[:5])
        self.result_label.setText("\n".join(lines))
        self.result_label.setStyleSheet("color: #2e7d32;")
        self._report_path = outputs.report
        self.open_dir_button.setVisible(True)
        self.open_report_button.setText("打开质检报告")
        self.open_report_button.setVisible(bool(outputs.report))

    def show_errors(self, issues) -> None:
        lines = [f"✘ {issue.title}：{issue.detail}" for issue in issues]
        if not lines:
            lines = ["预检未通过"]
        self.result_label.setText("预检未通过：\n" + "\n".join(lines))
        self.result_label.setStyleSheet("color: #d9534f;")
        self.open_dir_button.setVisible(False)
        self.open_report_button.setVisible(False)


__all__ = [
    "AlignmentStep",
    "DenseStep",
    "ExportStep",
    "OrthoStep",
    "ProjectStep",
    "StepPage",
    "SurfaceStep",
]
