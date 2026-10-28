"""控制点刺点对话框。

左侧影像列表、中间可缩放影像、右侧控制点表；左键在影像上点击即为
当前控制点刺一个点，保存到 ``<控制点文件>.marks.json``。
"""

from __future__ import annotations

import copy
from pathlib import Path

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QColor
from qgis.PyQt.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.core.uav.gcp import load_marks, save_marks
from app.core.uav.models import GcpPoint, GcpSet
from app.ui.common.base_dialog import BaseDialog
from app.ui.common.widgets import make_table
from app.ui.dialogs.uav.mark_view import GcpMarkView

_ROLE_NAME = "控制点"
_ROLE_CHECK = "检查点"


class _AddGcpPointDialog(BaseDialog):
    """手动新增一个控制点（点号 / 东 / 北 / 高程 / 类型）。"""

    def __init__(self, parent=None) -> None:
        super().__init__(parent, title="新增控制点", show_min_button=False, show_max_button=False)
        self.setMinimumSize(0, 0)
        self.resize(460, 300)
        self.setModal(True)
        self._build_ui()

    def _build_ui(self) -> None:
        layout = QVBoxLayout()
        form = QFormLayout()
        form.setSpacing(10)
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("例如 P01")
        self.east_edit = QLineEdit()
        self.east_edit.setPlaceholderText("东坐标（与输出坐标系一致）")
        self.north_edit = QLineEdit()
        self.north_edit.setPlaceholderText("北坐标")
        self.height_edit = QLineEdit("0")
        self.role_combo = QComboBox()
        self.role_combo.addItems([_ROLE_NAME, _ROLE_CHECK])
        form.addRow("点号", self.name_edit)
        form.addRow("东坐标", self.east_edit)
        form.addRow("北坐标", self.north_edit)
        form.addRow("高程", self.height_edit)
        form.addRow("类型", self.role_combo)
        layout.addLayout(form)
        layout.addStretch(1)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.setLayout(layout)

    def accept(self) -> None:
        if not self.name_edit.text().strip():
            QMessageBox.warning(self, "新增控制点", "请填写点号")
            self.name_edit.setFocus()
            return
        for edit, label in ((self.east_edit, "东坐标"), (self.north_edit, "北坐标")):
            try:
                float(edit.text())
            except ValueError:
                QMessageBox.warning(self, "新增控制点", f"{label}必须是数字")
                edit.setFocus()
                return
        super().accept()

    def values(self) -> tuple[str, float, float, float, str]:
        height_text = self.height_edit.text().strip() or "0"
        return (
            self.name_edit.text().strip(),
            float(self.east_edit.text()),
            float(self.north_edit.text()),
            float(height_text),
            self.role_combo.currentText(),
        )


class GcpMarkDialog(BaseDialog):
    """控制点刺点。"""

    def __init__(
        self,
        parent=None,
        gcp: GcpSet | None = None,
        image_paths: list[str] | None = None,
    ) -> None:
        super().__init__(parent, title="控制点刺点", show_min_button=False)
        self.setMinimumSize(0, 0)
        self.resize(1180, 760)
        self.setModal(True)
        self._gcp = copy.deepcopy(gcp) if gcp is not None else GcpSet()
        self._image_paths = list(image_paths or [])
        self._current_image_index = -1
        self._current_point_row = -1
        if self._gcp.source_path:
            load_marks(self._gcp)
        self._build_ui()
        self._populate_images()
        self._refresh_point_table()
        self._update_summary()

    # ------------------------------------------------------------ UI

    def _build_ui(self) -> None:
        layout = QVBoxLayout()
        layout.setSpacing(8)

        top = QHBoxLayout()
        self.path_label = QLabel(self._gcp.source_path or "（未指定控制点文件）")
        self.path_label.setObjectName("PropHint")
        self.summary_label = QLabel()
        self.summary_label.setObjectName("SectionLabel")
        top.addWidget(self.path_label, 1)
        top.addWidget(self.summary_label)
        layout.addLayout(top)

        splitter = QSplitter(Qt.Orientation.Horizontal)

        # 左：影像列表
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.addWidget(QLabel("影像"))
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("搜索影像…")
        self.search_edit.textChanged.connect(self._filter_images)
        left_layout.addWidget(self.search_edit)
        self.image_list = QListWidget()
        self.image_list.currentRowChanged.connect(self._on_image_changed)
        left_layout.addWidget(self.image_list, 1)
        splitter.addWidget(left)

        # 中：影像视图
        center = QWidget()
        center_layout = QVBoxLayout(center)
        center_layout.setContentsMargins(0, 0, 0, 0)
        toolbar = QHBoxLayout()
        zoom_in = QPushButton("放大")
        zoom_out = QPushButton("缩小")
        fit = QPushButton("适应窗口")
        zoom_in.clicked.connect(lambda: self.view.zoom_in())
        zoom_out.clicked.connect(lambda: self.view.zoom_out())
        fit.clicked.connect(lambda: self.view.fit_to_view())
        toolbar.addWidget(zoom_in)
        toolbar.addWidget(zoom_out)
        toolbar.addWidget(fit)
        toolbar.addStretch(1)
        self.view_hint = QLabel("滚轮缩放 · 中键拖动 · 左键刺点")
        self.view_hint.setObjectName("PropHint")
        toolbar.addWidget(self.view_hint)
        center_layout.addLayout(toolbar)
        self.view = GcpMarkView()
        self.view.image_clicked.connect(self._on_view_clicked)
        center_layout.addWidget(self.view, 1)
        splitter.addWidget(center)

        # 右：控制点表
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.addWidget(QLabel("控制点"))
        self.point_table = make_table(["点号", "类型", "刺点影像"])
        self.point_table.itemSelectionChanged.connect(self._on_point_selected)
        right_layout.addWidget(self.point_table, 1)
        point_buttons = QHBoxLayout()
        add_button = QPushButton("新增点")
        delete_button = QPushButton("删除点")
        add_button.clicked.connect(self._add_point)
        delete_button.clicked.connect(self._delete_point)
        point_buttons.addWidget(add_button)
        point_buttons.addWidget(delete_button)
        right_layout.addLayout(point_buttons)
        delete_mark_button = QPushButton("删除本张刺点")
        delete_mark_button.clicked.connect(self._delete_current_mark)
        right_layout.addWidget(delete_mark_button)
        splitter.addWidget(right)

        splitter.setSizes([240, 660, 300])
        layout.addWidget(splitter, 1)

        self.hint_label = QLabel(
            "先在右侧选中一个控制点，再在影像上点击同名位置；"
            "每点至少刺 2 张影像，建议 4–6 张。"
        )
        self.hint_label.setWordWrap(True)
        self.hint_label.setObjectName("PropHint")
        layout.addWidget(self.hint_label)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("保存")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.setLayout(layout)

    # ------------------------------------------------------------ 数据

    def result_gcp(self) -> GcpSet:
        return self._gcp

    @property
    def current_image_name(self) -> str:
        if 0 <= self._current_image_index < len(self._image_paths):
            return Path(self._image_paths[self._current_image_index]).name
        return ""

    def _populate_images(self) -> None:
        self.image_list.blockSignals(True)
        self.image_list.clear()
        for path in self._image_paths:
            item = QListWidgetItem(Path(path).name)
            item.setData(Qt.ItemDataRole.UserRole, path)
            self.image_list.addItem(item)
        self.image_list.blockSignals(False)
        self._update_image_item_texts()
        if self._image_paths:
            self.image_list.setCurrentRow(0)

    def _update_image_item_texts(self) -> None:
        for row in range(self.image_list.count()):
            item = self.image_list.item(row)
            name = Path(str(item.data(Qt.ItemDataRole.UserRole))).name
            count = sum(1 for point in self._gcp.points if name in point.marks)
            item.setText(f"{name}  ●{count}" if count else name)

    def _filter_images(self, text: str) -> None:
        needle = text.strip().lower()
        for row in range(self.image_list.count()):
            item = self.image_list.item(row)
            item.setHidden(bool(needle) and needle not in item.text().lower())

    def _refresh_point_table(self, select_row: int | None = None) -> None:
        row_to_select = self._current_point_row if select_row is None else select_row
        self.point_table.blockSignals(True)
        self.point_table.setRowCount(len(self._gcp.points))
        for row, point in enumerate(self._gcp.points):
            self.point_table.setItem(row, 0, QTableWidgetItem(point.name))
            self.point_table.setItem(row, 1, QTableWidgetItem(point.role))
            count_item = QTableWidgetItem(str(len(point.marks)))
            if not point.marks:
                count_item.setForeground(QColor("#d9534f"))
            self.point_table.setItem(row, 2, count_item)
        self.point_table.blockSignals(False)
        if 0 <= row_to_select < self.point_table.rowCount():
            self.point_table.selectRow(row_to_select)
            self._current_point_row = row_to_select
        elif self.point_table.rowCount():
            self.point_table.selectRow(0)
            self._current_point_row = 0
        else:
            self._current_point_row = -1

    def _update_summary(self) -> None:
        self.summary_label.setText(
            f"已刺 {self._gcp.marked_point_count}/{len(self._gcp.points)} 点，"
            f"共 {self._gcp.total_marks} 个刺点"
        )

    def _refresh_view_marks(self) -> None:
        image_name = self.current_image_name
        if not image_name:
            self.view.set_marks([])
            return
        marks = [
            (point.name, point.marks[image_name][0], point.marks[image_name][1])
            for point in self._gcp.points
            if image_name in point.marks
        ]
        self.view.set_marks(marks)

    # ------------------------------------------------------------ 交互

    def _on_image_changed(self, row: int) -> None:
        self._current_image_index = row
        if row < 0 or row >= len(self._image_paths):
            self.view.clear()
            return
        path = self._image_paths[row]
        if not self.view.load_image(path):
            self.hint_label.setText(f"无法读取影像：{path}")
            return
        self._refresh_view_marks()
        self.hint_label.setText(
            "先在右侧选中一个控制点，再在影像上点击同名位置；"
            "每点至少刺 2 张影像，建议 4–6 张。"
        )

    def _on_point_selected(self) -> None:
        rows = self.point_table.selectionModel().selectedRows()
        self._current_point_row = rows[0].row() if rows else -1

    def _on_view_clicked(self, point) -> None:
        if not (0 <= self._current_point_row < len(self._gcp.points)):
            self.hint_label.setText("请先在右侧控制点表里选中一个点，再在影像上点击。")
            return
        image_name = self.current_image_name
        if not image_name:
            return
        gcp_point = self._gcp.points[self._current_point_row]
        gcp_point.marks[image_name] = (round(float(point.x()), 2), round(float(point.y()), 2))
        self._refresh_view_marks()
        self._update_image_item_texts()
        self._refresh_point_table(select_row=self._current_point_row)
        self._update_summary()

    def _delete_current_mark(self) -> None:
        if not (0 <= self._current_point_row < len(self._gcp.points)):
            return
        image_name = self.current_image_name
        point = self._gcp.points[self._current_point_row]
        if image_name in point.marks:
            del point.marks[image_name]
            self._refresh_view_marks()
            self._update_image_item_texts()
            self._refresh_point_table(select_row=self._current_point_row)
            self._update_summary()

    def _add_point(self) -> None:
        dialog = _AddGcpPointDialog(self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        name, east, north, height, role = dialog.values()
        if self._gcp.get(name) is not None:
            QMessageBox.warning(self, "新增控制点", f"点号已存在：{name}")
            return
        self._gcp.points.append(
            GcpPoint(name=name, east=east, north=north, height=height, role=role)
        )
        self._refresh_point_table(select_row=len(self._gcp.points) - 1)
        self._update_summary()

    def _delete_point(self) -> None:
        if not (0 <= self._current_point_row < len(self._gcp.points)):
            return
        point = self._gcp.points[self._current_point_row]
        answer = QMessageBox.question(
            self,
            "删除控制点",
            f"确定删除 {point.name} 及其全部刺点吗？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        del self._gcp.points[self._current_point_row]
        self._refresh_point_table(select_row=min(self._current_point_row, len(self._gcp.points) - 1))
        self._update_image_item_texts()
        self._refresh_view_marks()
        self._update_summary()

    # ------------------------------------------------------------ 保存

    def accept(self) -> None:
        if not self._gcp.source_path:
            QMessageBox.warning(self, "控制点刺点", "没有可保存的控制点文件路径")
            return
        if self._gcp.total_marks == 0:
            answer = QMessageBox.question(
                self,
                "控制点刺点",
                "还没有刺任何点，仍要保存吗？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        try:
            save_marks(self._gcp)
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, "控制点刺点", f"保存失败：{exc}")
            return
        super().accept()


__all__ = ["GcpMarkDialog"]
