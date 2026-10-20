"""字段结构检查结果对话框。"""

from __future__ import annotations

from app.ui.common.base_dialog import BaseDialog
from qgis.PyQt.QtGui import QColor
from qgis.PyQt.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)


class FieldCheckDialog(BaseDialog):
    """展示字段规范检查的错误 / 警告列表。"""

    def __init__(self, parent, table: str, issues: list) -> None:
        super().__init__(parent, title=f"字段结构检查 - {table}")
        self.setMinimumSize(0, 0)
        self.resize(960, 560)
        errors = [item for item in issues if item.level == "错误"]
        warnings = [item for item in issues if item.level != "错误"]
        layout = QVBoxLayout()
        summary = QLabel(
            f"检查结果：错误 {len(errors)} 个，警告 {len(warnings)} 个"
            + ("（先处理错误项）" if errors else "")
        )
        font = summary.font()
        font.setBold(True)
        summary.setFont(font)
        filter_row = QHBoxLayout()
        filter_row.addWidget(summary, 1)
        filter_row.addWidget(QLabel("级别："))
        self.level_combo = QComboBox()
        self.level_combo.addItems(["全部", "错误", "警告"])
        self.level_combo.currentIndexChanged.connect(self._apply_filter)
        filter_row.addWidget(self.level_combo)
        layout.addLayout(filter_row)

        table_widget = QTableWidget(0, 3)
        table_widget.setHorizontalHeaderLabels(["级别", "字段", "问题"])
        table_widget.verticalHeader().setVisible(False)
        table_widget.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table_widget.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table_widget.setWordWrap(True)
        header = table_widget.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        table_widget.setRowCount(len(issues))
        for row, issue in enumerate(issues):
            level_item = QTableWidgetItem(issue.level)
            if issue.level == "错误":
                level_item.setForeground(QColor("#c0392b"))
            table_widget.setItem(row, 0, level_item)
            table_widget.setItem(row, 1, QTableWidgetItem(issue.field or "—"))
            table_widget.setItem(row, 2, QTableWidgetItem(issue.message))
        self._table = table_widget
        layout.addWidget(table_widget, 1)

        footer = QHBoxLayout()
        footer.addStretch(1)
        close_btn = QPushButton("关闭")
        close_btn.clicked.connect(self.accept)
        footer.addWidget(close_btn)
        layout.addLayout(footer)
        self.setLayout(layout)

    def _apply_filter(self, _index: int = 0) -> None:
        """按级别筛选（全部 / 错误 / 警告）。"""
        level = self.level_combo.currentText()
        for row in range(self._table.rowCount()):
            item = self._table.item(row, 0)
            row_level = item.text() if item is not None else ""
            self._table.setRowHidden(row, level != "全部" and row_level != level)
