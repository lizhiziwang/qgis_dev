"""流程编排：运行前参数填写对话框。

运行整条流程前，把所有「未绑定上游」的输入参数列出来，让用户填写 / 覆盖
本次运行的值，返回 ``{node_id: {input_id: 原始字符串}}``。

覆盖值只影响本次运行（执行器临时覆盖默认值），**不写回图模型、不持久化到
JSON**；下次运行仍用节点里保存的默认值 / 绑定。
"""

from __future__ import annotations

from PyQt6.QtWidgets import (
    QCheckBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from app.core.domain.flow.model import InputSpec, unbound_inputs
from app.core.domain.flow.types import PortType
from app.ui.common.base_dialog import BaseDialog

#: 文件类端口 → 浏览对话框文件过滤器
_FILE_FILTERS: dict[PortType, str] = {
    PortType.VECTOR: "矢量文件 (*.shp *.gpkg *.geojson *.gdb);;所有文件 (*)",
    PortType.RASTER: "栅格文件 (*.tif *.img);;所有文件 (*)",
    PortType.FILE: "所有文件 (*)",
    PortType.TABLE: "所有文件 (*)",
}


class RunParamsDialog(BaseDialog):
    """运行前参数填写对话框。"""

    def __init__(self, graph, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("填写本次运行的输入参数")
        self.setMinimumWidth(520)
        #: (node_id, input_id, InputSpec, 控件)
        self._rows: list[tuple[str, str, InputSpec, QCheckBox | QLineEdit]] = []
        self._build(graph)

    # ------------------------------------------------------------ 构建

    def _build(self, graph) -> None:
        root = QVBoxLayout()

        hint = QLabel("以下输入未绑定上游，请填写本次运行使用的值（带 * 为必填）：")
        hint.setWordWrap(True)
        root.addWidget(hint)

        container = QWidget()
        v = QVBoxLayout(container)
        v.setContentsMargins(0, 0, 0, 0)

        last_node: str | None = None
        for node_id, node_name, ins in unbound_inputs(graph):
            if node_id != last_node:
                hdr = QLabel(f"▎{node_name}")
                hdr.setStyleSheet("font-weight: bold;")
                v.addWidget(hdr)
                last_node = node_id
            self._add_row(v, node_id, ins)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(container)
        root.addWidget(scroll, 1)

        btns = QHBoxLayout()
        btns.addStretch(1)
        run_btn = QPushButton("运行")
        run_btn.setDefault(True)
        run_btn.clicked.connect(self.accept)
        cancel_btn = QPushButton("取消")
        cancel_btn.clicked.connect(self.reject)
        btns.addWidget(run_btn)
        btns.addWidget(cancel_btn)
        root.addLayout(btns)
        self.setLayout(root)

    def _add_row(self, layout: QVBoxLayout, node_id: str, ins: InputSpec) -> None:
        star = " *" if ins.required else ""
        label = QLabel(f"{ins.name}（{ins.type.value}）{star}")
        label.setMinimumWidth(160)

        if ins.type is PortType.BOOLEAN:
            widget: QCheckBox | QLineEdit = QCheckBox()
            widget.setChecked(str(ins.default).strip().lower() in ("1", "true", "是", "yes"))
            widget.setToolTip("勾选 = true，不勾选 = false")
            row = QHBoxLayout()
            row.addWidget(label)
            row.addWidget(widget)
        else:
            widget = QLineEdit(ins.default)
            widget.setPlaceholderText(ins.default or "（未设置）")
            row = QHBoxLayout()
            row.addWidget(label)
            row.addWidget(widget, 1)
            if ins.type in _FILE_FILTERS:
                # clicked 信号会传 bool 参数，用 _ 吞掉避免误绑到 edit
                browse = self._make_browse_button(
                    lambda _=False, w=widget, t=ins.type: self._browse_file(w, t)
                )
                row.addWidget(browse)
            elif ins.type is PortType.FOLDER:
                browse = self._make_browse_button(
                    lambda _=False, w=widget: self._browse_folder(w)
                )
                row.addWidget(browse)

        self._rows.append((node_id, ins.id, ins, widget))
        layout.addLayout(row)

    def _make_browse_button(self, slot) -> QPushButton:
        btn = QPushButton("…")
        btn.setFixedWidth(32)
        btn.setToolTip("浏览选择")
        btn.clicked.connect(slot)
        return btn

    def _browse_file(self, edit: QLineEdit, ptype: PortType) -> None:
        flt = _FILE_FILTERS.get(ptype, "所有文件 (*)")
        path, _ = QFileDialog.getOpenFileName(self, "选择文件", edit.text() or "", flt)
        if path:
            edit.setText(path)

    def _browse_folder(self, edit: QLineEdit) -> None:
        path = QFileDialog.getExistingDirectory(self, "选择文件夹", edit.text() or "")
        if path:
            edit.setText(path)

    # ------------------------------------------------------------ 校验 / 取值

    def accept(self) -> None:  # noqa: N802 —— Qt 命名
        problem = self._validate()
        if problem is not None:
            QMessageBox.warning(self, "参数不完整", problem)
            return
        super().accept()

    def _validate(self) -> str | None:
        for _node_id, _port_id, ins, widget in self._rows:
            if isinstance(widget, QCheckBox):
                continue
            text = widget.text().strip()
            if ins.required and not text:
                return f"「{ins.name}」是必填参数，不能为空。"
            if text and ins.type is PortType.NUMBER:
                try:
                    float(text)
                except ValueError:
                    return f"「{ins.name}」需要填数值，当前输入：{text}"
        return None

    def overrides(self) -> dict[str, dict[str, str]]:
        """本次运行覆盖值 ``{node_id: {input_id: 原始字符串}}``。"""
        out: dict[str, dict[str, str]] = {}
        for node_id, port_id, _ins, widget in self._rows:
            if isinstance(widget, QCheckBox):
                value = "1" if widget.isChecked() else "0"
            else:
                value = widget.text()
            out.setdefault(node_id, {})[port_id] = value
        return out
