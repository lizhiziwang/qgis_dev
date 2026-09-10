"""质检 JSON 树状列表 Dialog。

迁自 :class:`component.check_dia.JsonTreeView`。
"""

from __future__ import annotations

import json
import os
import uuid
from typing import Any, Optional

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.core.domain.check import CheckParam, CheckService
from app.infra.logging import get_logger
from app.infra.messagebar import send
from app.ui.common.base_dialog import BaseDialog
from app.ui.common.widgets import open_file_dir
from app.ui.dialogs.check_editor import CheckConfigDialog, CheckVo

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

        # 进度条：跑质检时按质检项数步进（每项一个单位），空闲置 0
        self.loading = QProgressBar()
        self.loading.setRange(0, 100)
        self.loading.setValue(0)
        main.addWidget(self.loading)

        # 底部
        btns = QHBoxLayout()
        export_but = QPushButton("导出")
        export_but.clicked.connect(self._export_data)
        btns.addWidget(export_but)
        self.confirm_but = QPushButton("确认")
        self.confirm_but.clicked.connect(self._start_check)
        btns.addWidget(self.confirm_but)
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
            # 勾选 ↔ JSON 节点的 isApply：勾上 = 该质检项参与执行
            cb.stateChanged.connect(
                lambda state, it=item: self._on_toggle_apply(it, state)
            )
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

    def _on_toggle_apply(self, item: dict, state: int) -> None:
        """勾选/取消勾选 → 同步 JSON 树的 ``isApply``（勾上=参与执行）。

        树形联动：勾上叶子会把所有祖先（含根）一并勾上，否则 :meth:`_collect_params`
        按 ``isApply==1 且祖先勾选`` 判定，根/中间节点 isApply=0 时勾了也白勾；
        取消勾选会把所有子孙一并取消。
        """
        is_apply = 1 if state == Qt.CheckState.Checked.value else 0
        node = item.get("original_item")
        if node is not None:
            node["isApply"] = is_apply
            if is_apply:
                # 祖先（含根）联动勾上
                for anc in self._find_ancestors(node):
                    anc["isApply"] = 1
                if self.json_data is not None:
                    self.json_data["isApply"] = 1
            else:
                # 取消 → 子孙联动取消
                self._set_descendants_apply(node, 0)
        self._sync_checked_flags()
        self._refresh_display()

    def _find_ancestors(self, node: dict) -> list[dict]:
        """返回从根到 ``node`` 直接父级的祖先节点链（不含 node 自身）。"""
        if not self.json_data:
            return []
        ancestors: list[dict] = []

        def _dfs(items: list, trail: list) -> bool:
            for it in items:
                if it is node:
                    ancestors.extend(trail)
                    return True
                if it.get("items") and _dfs(it["items"], trail + [it]):
                    return True
            return False

        _dfs(self.json_data.get("items", []), [])
        return ancestors

    def _set_descendants_apply(self, node: dict, val: int) -> None:
        """把 ``node`` 下所有子孙节点的 ``isApply`` 设为 ``val``。"""

        def _walk(n: dict) -> None:
            for ch in n.get("items", []) or []:
                ch["isApply"] = val
                _walk(ch)

        _walk(node)

    def _sync_checked_flags(self) -> None:
        """把表格 ``all_items`` 的 checked 从 JSON 树重新对齐（联动后刷新显示）。"""
        if not self.json_data:
            return
        nodes: list[dict] = [self.json_data]

        def _collect(items: list) -> None:
            for it in items:
                nodes.append(it)
                if it.get("items"):
                    _collect(it["items"])

        _collect(self.json_data.get("items", []))
        for entry in self.all_items:
            node = entry.get("original_item")
            entry["checked"] = node in nodes and node.get("isApply", 0) == 1

    def _start_check(self) -> None:
        if not self.data_source_input.currentText():
            QMessageBox.warning(self, "提示", "请先选择数据源")
            return
        layer = self.parent.project.mapLayersByName(self.data_source_input.currentText())[0]
        params: list[CheckParam] = []
        # 只跑勾选（isApply==1 且所有祖先勾选）的质检项
        self._collect_params(
            params,
            self.json_data.get("items", []),
            enabled=self.json_data.get("isApply", 0) == 1,
        )
        if not params:
            QMessageBox.warning(self, "提示", "请先勾选要执行的质检项")
            return

        # 开始日志 + 进度条按项数步进
        total = len(params)
        logger.info("数据质检开始：数据源=%s，共 %d 项质检", layer.name(), total)
        self.confirm_but.setEnabled(False)
        self.loading.setRange(0, total)
        self.loading.setValue(0)

        passed = failed = 0
        for i, p in enumerate(params, start=1):
            logger.info("质检执行 %d/%d：%s", i, total, p.check_name or p.check_id)
            try:
                res = self.check_service.run(p)
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "质检执行失败：%s（%s）：%s",
                    p.check_name or p.check_id,
                    p.check_id,
                    exc,
                    exc_info=True,
                )
                failed += 1
            else:
                self._update_item_status(p.check_id, res)
                if res.is_pass:
                    passed += 1
                    logger.info("质检通过：%s（不通过 %d 条）", p.check_name or p.check_id, res.failed_count)
                else:
                    failed += 1
                    logger.info("质检不通过：%s（不通过 %d 条）", p.check_name or p.check_id, res.failed_count)
            self.loading.setValue(i)
            # 同步阻塞跑质检期间也要让进度条 / 日志面板实时刷新（splash 同款修法）
            QApplication.processEvents()

        logger.info("数据质检完成：共 %d 项，通过 %d，不通过 %d", total, passed, failed)
        send("success", f"数据质检完成：共 {total} 项，通过 {passed}，不通过 {failed}", duration=3)
        self.confirm_but.setEnabled(True)
        self.loading.setValue(0)
        self._aggregate_results()
        self._refresh_display()

    def _collect_params(self, params: list, items, enabled: bool = True) -> None:
        """收集勾选（``enabled`` 为 True 且该节点 ``isApply==1``）的叶子质检参数。"""
        for it in items:
            node_enabled = enabled and (it.get("isApply", 0) == 1)
            if "items" not in it:
                if node_enabled:
                    layer = None
                    if self.data_source_input.currentText() and self.parent:
                        layer = self.parent.project.mapLayersByName(self.data_source_input.currentText())[0]
                    params.append(self.check_service.from_dict(it, layer=layer))
            else:
                self._collect_params(params, it["items"], node_enabled)

    def _update_item_status(self, check_id: str, result) -> None:
        if check_id is None or not self.json_data:
            return
        node = self._find_json_node(check_id)
        if node is None:
            return
        node["isPass"] = result.is_pass
        node["noPassCount"] = result.failed_count
        # 同步表格 all_items 快照：_refresh_display / 显示未通过项过滤都读快照，
        # 不同步的话跑完结果不会回显到表格（一直是加载时的旧值）。
        for entry in self.all_items:
            if entry.get("original_item") is node:
                entry["pass"] = result.is_pass
                entry["noPassCount"] = result.failed_count
                break

    def _find_json_node(self, check_id: str):
        """按 ``check_id`` 在 JSON 树里找节点；JSON 的 id 是 int、check_id 是 str，
        统一转 ``str`` 比较。"""
        if not self.json_data:
            return None

        def _walk(node):
            # JSON 的 id 是 int，CheckParam.check_id 是 str，统一转 str 比较
            if str(node.get("id")) == str(check_id):
                return node
            for ch in node.get("items", []) or []:
                found = _walk(ch)
                if found is not None:
                    return found
            return None

        return _walk(self.json_data)

    def _aggregate_results(self) -> None:
        """质检跑完后把叶子结果向上聚合，让树状表格整体反映结果。

        中间节点（属性质检/值域范围/根）的 ``isPass`` = 全部子项通过，
        ``noPassCount`` = 子孙叶子错误数之和；未勾选/未执行的叶子按默认通过
        参与聚合。聚合后同步 all_items 快照，供表格回显。
        """
        if not self.json_data:
            return

        def _walk(node) -> None:
            children = node.get("items") or []
            if not children:
                return
            for ch in children:
                _walk(ch)
            all_pass = all(ch.get("isPass", True) for ch in children)
            total = sum(ch.get("noPassCount", 0) or 0 for ch in children)
            node["isPass"] = all_pass
            node["noPassCount"] = total

        _walk(self.json_data)
        for entry in self.all_items:
            node = entry.get("original_item")
            if node is not None and node.get("items"):
                entry["pass"] = node.get("isPass", True)
                entry["noPassCount"] = node.get("noPassCount", 0)

    def _show_context_menu(self, pos) -> None:
        idx = self.table.indexAt(pos)
        menu = QMenu(self.table)
        from qgis.PyQt.QtGui import QAction
        if idx.isValid():
            row = idx.row()
            current = self.display_items[row]
            edit = QMenu("编辑当前规则", menu)
            edit_a = QAction("编辑", menu)
            edit_a.triggered.connect(lambda: self._create_check_item(False, item=current))
            menu.addAction(edit_a)
            # 只有叶子质检项能看错误数据；聚合出"不通过"的中间节点没有 checkParam
            if not current["pass"] and current["isCheckItem"]:
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
        send("warning", "功能待开发！", duration=5)
        self.close()

    def _export_data(self) -> None:
        path = open_file_dir(self.parent, is_dir=True)
        if not path:
            return
        file_path = os.path.join(path, f"导出结果-{uuid.uuid4().hex}.json")
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(self.json_data, f, ensure_ascii=False, indent=4)
