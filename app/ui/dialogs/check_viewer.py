"""质检 JSON 树状列表 Dialog。

迁自 :class:`component.check_dia.JsonTreeView`。
"""

from __future__ import annotations

import json
import os
import uuid
from typing import Any, Optional

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
    QAbstractItemView,
)

from app.core.domain.check import CheckParam, CheckService
from app.infra.logging import get_logger
from app.ui.common.base_dialog import BaseDialog


logger = get_logger(__name__)


class CheckViewerDialog(BaseDialog):
    """质检 JSON 配置 → 树状列表 → 跑 CheckService。"""

    def __init__(self, parent=None, json_data: Optional[dict] = None, file_path: Optional[str] = None, title: str = "质检") -> None:
        super().__init__(parent, title)
        self.parent = parent
        self.display_items: list[dict] = []
        self.all_items: list[dict] = []
        self.json_data: Optional[dict] = json_data
        self.file_path = file_path or r"D:\系统默认\桌面\质检2.json"
        self.check_service = CheckService()
        self._setup_ui()
        self._load_default_data("resource/质检2.json")

    def _setup_ui(self) -> None:
        self.setWindowTitle("数据质检：林草湿更新成果")
        self.resize(900, 600)
        main = QVBoxLayout()

        # 文件选择
        file_layout = QHBoxLayout()
        file_layout.addWidget(QLabel("质检文件："))
        self.file_input = QLineEdit()
        file_layout.addWidget(self.file_input)
        but = QPushButton("选择文件")
        but.clicked.connect(lambda: self._open_file())
        file_layout.addWidget(but)
        main.addLayout(file_layout)

        # 数据源
        ds_layout = QHBoxLayout()
        ds_layout.addWidget(QLabel("数据源："))
        self.data_source_input = QComboBox()
        if self.parent is not None and hasattr(self.parent, "project"):
            self.data_source_input.addItems([l.name() for l in self.parent.project.mapLayers().values()])
        self.data_source_input.setCurrentIndex(-1)
        ds_layout.addWidget(self.data_source_input)
        main.addLayout(ds_layout)

        # 控制
        ctrl = QHBoxLayout()
        self.show_detail_cb = QCheckBox("显示详情")
        self.show_detail_cb.setChecked(True)
        self.show_failed_cb = QCheckBox("显示未通过项")
        self.def_but = QPushButton("加载默认")
        self.def_but.clicked.connect(lambda: self._load_default_data("resource/质检2.json"))
        self.show_detail_cb.stateChanged.connect(self._refresh_display)
        self.show_failed_cb.stateChanged.connect(self._refresh_display)
        ctrl.addWidget(self.show_detail_cb)
        ctrl.addWidget(self.show_failed_cb)
        ctrl.addWidget(self.def_but)
        ctrl.addStretch()
        main.addLayout(ctrl)

        # 表格
        self.table = QTableWidget()
        self.table.setColumnCount(3)
        self.table.setHorizontalHeaderLabels(["质检名称", "质检状态", "错误数"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._show_context_menu)
        main.addWidget(self.table)

        # 底部
        btns = QHBoxLayout()
        export_but = QPushButton("导出")
        export_but.clicked.connect(self._export_data)
        btns.addWidget(export_but)
        confirm_but = QPushButton("确认")
        confirm_but.clicked.connect(self._start_check)
        btns.addWidget(confirm_but)
        cancel_but = QPushButton("取消")
        cancel_but.clicked.connect(self.close)
        btns.addWidget(cancel_but)
        btns.addStretch()
        main.addLayout(btns)

        self.setLayout(main)

    def _load_default_data(self, path: str) -> None:
        if not os.path.exists(path):
            return
        with open(path, "r", encoding="utf-8") as f:
            self.json_data = json.load(f)
        self.file_input.setText(path)
        self._load_data()

    def _open_file(self) -> None:
        fd = QFileDialog(self, "选择质检文件", os.path.dirname(self.file_path), "json(*.json)")
        if fd.exec() == QFileDialog.DialogCode.Accepted:
            self.file_path = fd.selectedFiles()[0]
            self.file_input.setText(self.file_path)
            with open(self.file_path, "r", encoding="utf-8") as f:
                self.json_data = json.load(f)
            self._load_data()

    def _load_data(self) -> None:
        if not self.json_data:
            return
        self.all_items = []
        self.all_items.append({
            "name": self.json_data["checkItemName"],
            "level": 0,
            "checked": self.json_data.get("isApply", 0) == 1,
            "has_children": bool(self.json_data.get("items")),
            "original_item": self.json_data,
            "pass": self.json_data.get("isPass", True),
            "noPassCount": self.json_data.get("noPassCount", 0),
            "isCheckItem": "items" not in self.json_data,
        })
        self._parse(self.json_data.get("items", []), 1)
        self._refresh_display()

    def _parse(self, items, level: int) -> None:
        for item in items:
            self.all_items.append({
                "name": item["checkItemName"],
                "level": level,
                "checked": item.get("isApply", 0) == 1,
                "has_children": bool(item.get("items")),
                "original_item": item,
                "pass": item.get("isPass", True),
                "noPassCount": item.get("noPassCount", 0),
                "isCheckItem": "items" not in item,
            })
            if item.get("items"):
                self._parse(item["items"], level + 1)

    def _refresh_display(self) -> None:
        show_detail = self.show_detail_cb.isChecked()
        show_failed = self.show_failed_cb.isChecked()
        if not self.all_items:
            return
        self.display_items = list(self.all_items)
        if show_failed:
            self.display_items = [
                it for it in self.display_items
                if (it.get("pass") is False and it["isCheckItem"]) or not it["isCheckItem"]
            ]
        self.table.setRowCount(len(self.display_items))
        for row, item in enumerate(self.display_items):
            name_w = QWidget()
            name_lay = QHBoxLayout(name_w)
            name_lay.setContentsMargins(5, 0, 5, 0)
            cb = QCheckBox()
            cb.setChecked(item["checked"])
            cb.setEnabled(False)
            indent = "　　" * item["level"]
            lbl = QLabel(f"{indent}{item['name']}")
            name_lay.addWidget(cb)
            name_lay.addWidget(lbl)
            name_lay.addStretch()
            self.table.setCellWidget(row, 0, name_w)
            status = "通过" if item.get("pass", True) else "不通过"
            s_item = QTableWidgetItem(status)
            s_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.table.setItem(row, 1, s_item)
            err = QTableWidgetItem(str(item["noPassCount"]))
            err.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.table.setItem(row, 2, err)

    def _start_check(self) -> None:
        if not self.data_source_input.currentText():
            QMessageBox.warning(self, "提示", "请先选择数据源")
            return
        layer = self.parent.project.mapLayersByName(self.data_source_input.currentText())[0]
        params: list[CheckParam] = []
        self._collect_params(params, self.json_data.get("items", []))
        for p in params:
            res = self.check_service.run(p)
            self._update_item_status(p.check_id, res)
        self._refresh_display()

    def _collect_params(self, params: list, items) -> None:
        for it in items:
            if "items" not in it:
                layer = None
                if self.data_source_input.currentText() and self.parent:
                    layer = self.parent.project.mapLayersByName(self.data_source_input.currentText())[0]
                params.append(self.check_service.from_dict(it, layer=layer))
            else:
                self._collect_params(params, it["items"])

    def _update_item_status(self, check_id: str, result) -> None:
        if check_id is None or not self.json_data:
            return
        def _walk(node):
            if node.get("id") == check_id:
                node["isPass"] = result.is_pass
                node["noPassCount"] = result.failed_count
                return True
            for ch in node.get("items", []) or []:
                if _walk(ch):
                    return True
            return False
        _walk(self.json_data)

    def _show_context_menu(self, pos) -> None:
        idx = self.table.indexAt(pos)
        menu = QMenu(self.table)
        from PyQt6.QtGui import QAction
        if idx.isValid():
            row = idx.row()
            current = self.display_items[row]
            edit = QMenu("编辑当前规则", menu)
            edit_a = QAction("编辑", menu)
            edit_a.triggered.connect(lambda: self._create_check_item(False, item=current))
            menu.addAction(edit_a)
            if not current["pass"]:
                show = QAction("查看错误数据", menu)
                show.triggered.connect(lambda: self._show_error_data(current))
                menu.addAction(show)
            menu.addSeparator()
        else:
            new_a = QAction("新建规则", menu)
            new_a.triggered.connect(lambda: self._on_add_new())
            menu.addAction(new_a)
        menu.exec(self.table.viewport().mapToGlobal(pos))

    def _create_check_item(self, is_add: bool, item=None) -> None:
        from app.ui.dialogs.check_editor import CheckConfigDialog, CheckVo
        if item is None:
            return
        original = item["original_item"]
        vo = CheckVo(
            checkItemName=original["checkItemName"],
            checkType=original["checkType"],
            id=original["id"],
            isApply=original.get("isApply", 0),
            items=original.get("items"),
            checkParam=original.get("checkParam"),
            pId=None,
            isPass=original.get("isPass"),
            noPassCount=original.get("noPassCount"),
        )
        dlg = CheckConfigDialog(is_add, self.parent, columns=self._field_names(), check_vo=vo)
        dlg.save_edit.connect(self._on_save_edit)
        dlg.exec()

    def _field_names(self) -> list[str]:
        if not (self.parent and self.data_source_input.currentText()):
            return []
        layer = self.parent.project.mapLayersByName(self.data_source_input.currentText())[0]
        return [f.name() for f in layer.fields()]

    def _on_save_edit(self, vo) -> None:
        def _walk(node):
            if node.get("id") == vo.id:
                node["checkItemName"] = vo.checkItemName
                node["checkType"] = vo.checkType
                node["isApply"] = vo.isApply
                node["checkParam"] = vo.checkParam
                return True
            for ch in node.get("items", []) or []:
                if _walk(ch):
                    return True
            return False
        if self.json_data:
            _walk(self.json_data)
        self._load_data()

    def _show_error_data(self, current) -> None:
        original = current["original_item"]
        layer = self.parent.project.mapLayersByName(self.data_source_input.currentText())[0]
        p = self.check_service.from_dict(original, layer=layer)
        if self.parent:
            self.parent._show_attribute_dock(layer, filter_str=p.where)

    def _on_add_new(self) -> None:
        from app.infra.messagebar import send
        send("warning", "功能待开发！", duration=5)
        self.close()

    def _export_data(self) -> None:
        from app.ui.common.widgets import open_file_dir
        path = open_file_dir(self.parent, is_dir=True)
        if not path:
            return
        file_path = os.path.join(path, f"导出结果-{uuid.uuid4().hex}.json")
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(self.json_data, f, ensure_ascii=False, indent=4)
