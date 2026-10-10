"""质检项配置 Dialog。"""

from __future__ import annotations

import json
from typing import List, Optional

from qgis.PyQt.QtCore import Qt, pyqtSignal
from qgis.PyQt.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QRadioButton,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
)

from app.infra.logging import get_logger
from app.ui.common.base_dialog import BaseDialog
from app.ui.common.widgets import CenteredCheckDelegate


logger = get_logger(__name__)


_CHECK_TYPES = ["值域范围", "非空（必填）校验", "逻辑校验", "属性质检"]


class CheckVo:
    """质检项数据载体。"""

    def __init__(self, **kwargs) -> None:
        self.checkItemName = kwargs["checkItemName"]
        self.checkType = kwargs["checkType"]
        self.id = kwargs["id"]
        self.isApply = kwargs["isApply"]
        self.items = kwargs["items"]
        self.checkParam = kwargs["checkParam"]
        self.pId = kwargs["pId"]
        self.isPass = kwargs["isPass"]
        self.noPassCount = kwargs["noPassCount"]


class CheckConfigDialog(BaseDialog):
    """质检项配置 Dialog。"""

    save_edit = pyqtSignal(object)

    def __init__(self, is_add: bool, parent, columns: Optional[List[str]] = None, check_vo: Optional[CheckVo] = None) -> None:
        super().__init__(parent, "新增质检配置" if is_add else "编辑质检配置")
        self.is_add = is_add
        self.check_vo = check_vo or CheckVo(
            checkItemName=None, checkType=None, id=None,
            isApply=None, items=None, checkParam=None, pId=None,
            isPass=None, noPassCount=None,
        )
        self.columns = columns or []
        self._init_ui()

    def _init_ui(self) -> None:
        main = QVBoxLayout()
        main.setSpacing(15)
        main.setContentsMargins(20, 20, 20, 20)

        title = QLabel("质检项配置信息")
        title.setObjectName("DialogTitle")  # 标题样式（字号/颜色）由 app_qss 提供
        main.addWidget(title)

        # 名称
        row1 = QHBoxLayout()
        lbl = QLabel("名称:")
        lbl.setFixedWidth(60)
        self.name_input = QLineEdit()
        self.name_input.textEdited.connect(self._on_name_change)
        if self.check_vo.checkItemName is not None:
            self.name_input.setText(self.check_vo.checkItemName)
        self.name_input.setPlaceholderText("请输入质检项名称")
        self.name_input.setMinimumWidth(200)
        row1.addWidget(lbl)
        row1.addWidget(self.name_input)
        row1.addStretch()
        main.addLayout(row1)

        # 选项
        row2 = QHBoxLayout()
        row2.setSpacing(15)
        self.apply_radio = QRadioButton("是否应用")
        self.apply_radio.clicked.connect(self._on_apply_change)
        if self.check_vo.isApply is not None:
            self.apply_radio.setChecked(bool(self.check_vo.isApply))
        else:
            self.apply_radio.setChecked(True)
        opt_lbl = QLabel("质检选项:")
        opt_lbl.setFixedWidth(60)
        self.option_combo = QComboBox()
        self.option_combo.addItems(_CHECK_TYPES)
        self.option_combo.setMinimumWidth(150)
        row2.addWidget(self.apply_radio)
        row2.addWidget(opt_lbl)
        row2.addWidget(self.option_combo)
        row2.addStretch()
        self.option_combo.currentIndexChanged.connect(self._on_type_change)
        main.addLayout(row2)

        # 过滤条件
        f_lay = QVBoxLayout()
        self.filter_label = QLabel("过滤条件（选填）:")
        self.filter_input = QTextEdit()
        self.filter_input.setFixedHeight(50)
        if self.check_vo.checkParam:
            try:
                cp = json.loads(self.check_vo.checkParam)
                self.filter_input.setText(str(cp.get("where", "")))
            except Exception:  # noqa: BLE001
                pass
        else:
            self.filter_input.setPlaceholderText("支持表达式，如 field > 10")
        self.filter_input.textChanged.connect(self._on_where_change)
        f_lay.addWidget(self.filter_label)
        f_lay.addWidget(self.filter_input)
        f_lay.addStretch()
        main.addLayout(f_lay)

        # 字段选择
        self._build_field_table(main)

        main.addStretch(1)

        # 底部
        if self.check_vo.checkType is not None:
            self.option_combo.setCurrentIndex(int(self.check_vo.checkType))
            self._on_type_change()

        btns = QHBoxLayout()
        confirm = QPushButton("确认")
        confirm.clicked.connect(lambda: self.save_edit.emit(self.check_vo))
        cancel = QPushButton("取消")
        cancel.clicked.connect(self.close)
        btns.addWidget(confirm)
        btns.addWidget(cancel)
        btns.addStretch()
        main.addLayout(btns)
        self.main_layout = main
        self.setLayout(main)

    def _build_field_table(self, parent_lay) -> None:
        self.table = QTableWidget()
        self.table.setItemDelegate(CenteredCheckDelegate(self.table))
        self.table.setItemDelegate(CenteredCheckDelegate(self.table))
        self.table.setRowCount(len(self.columns))
        self.table.setColumnCount(2)
        self.table.setHorizontalHeaderLabels(["是否应用", "质检字段"])
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        try:
            j = json.loads(self.check_vo.checkParam) if self.check_vo.checkParam else {"fieldName": []}
        except Exception:  # noqa: BLE001
            j = {"fieldName": []}
        for r, col in enumerate(self.columns):
            check_item = QTableWidgetItem()
            check_item.setCheckState(Qt.CheckState.Checked if col in j.get("fieldName", []) else Qt.CheckState.Unchecked)
            check_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.table.setItem(r, 0, check_item)
            name_item = QTableWidgetItem(col)
            self.table.setItem(r, 1, name_item)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.table.cellClicked.connect(self._on_field_clicked)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setFixedHeight(300)
        v = QVBoxLayout()
        v.addWidget(QLabel("选择字段"))
        v.addWidget(self.table)
        v.addStretch(1)
        parent_lay.addLayout(v)

    def _on_field_clicked(self, row: int, col: int) -> None:
        if col == 0:
            try:
                j = json.loads(self.check_vo.checkParam) if self.check_vo.checkParam else {"fieldName": []}
            except Exception:  # noqa: BLE001
                j = {"fieldName": []}
            check_item = self.table.item(row, 0)
            field_item = self.table.item(row, 1)
            if check_item.checkState() == Qt.CheckState.Checked:
                j["fieldName"].append(field_item.text())
            else:
                j["fieldName"].remove(field_item.text())
            self.check_vo.checkParam = json.dumps(j)

    def _on_name_change(self) -> None:
        self.check_vo.checkItemName = self.name_input.text()

    def _on_apply_change(self) -> None:
        self.check_vo.isApply = 1 if self.apply_radio.isChecked() else 0

    def _on_type_change(self) -> None:
        idx = self.option_combo.currentIndex()
        self.check_vo.checkType = idx
        if idx == 0:
            self.filter_label.setText("值域数据（使用,分割）:")
            self.filter_input.setPlaceholderText("'value1','value2','value3'......")
        else:
            self.filter_label.setText("过滤条件（选填）:")
            self.filter_input.setPlaceholderText("支持表达式，如 field > 10")

    def _on_where_change(self) -> None:
        try:
            j = json.loads(self.check_vo.checkParam) if self.check_vo.checkParam else {}
        except Exception:  # noqa: BLE001
            j = {}
        j["where"] = self.filter_input.toPlainText()
        self.check_vo.checkParam = json.dumps(j)
