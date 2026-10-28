"""矢量瓦片「高级」设置弹窗。

当前矢量瓦片算法（``native:writevectortiles_xyz``）本身只有输出目录 / 模板 /
图层 / 缩放 / 范围这几个参数，没有更多算法级选项。这里提供的是**工具层**的
三个实用开关：

- **文件模板**：``{z}/{x}/{y}.pbf``，可改成带子目录的模板；
- **输出字段**：只把网页展示需要的字段写进 pbf。全部字段都导出时，一条要素
  可能带几百个属性，单个瓦片能到几十 MB，网页查看器打开/缩放都很慢；
- **执行前清空输出目录**：避免上一次残留的高缩放级别瓦片混进新结果；
- **生成网页查看器**：在输出目录写 ``index.html``（Leaflet + VectorGrid），
  可直接双击浏览（依赖网络加载前端脚本）。
"""

from __future__ import annotations

from dataclasses import dataclass

from qgis.PyQt.QtWidgets import (
    QCheckBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
)

from app.ui.common.base_dialog import BaseDialog
from app.ui.dialogs.organize_columns import OrganizeColumnsDialog

DEFAULT_TEMPLATE = "{z}/{x}/{y}.pbf"


@dataclass
class VectorTileAdvancedOptions:
    """矢量瓦片工具层高级选项。"""

    template: str = DEFAULT_TEMPLATE
    clear_output: bool = False
    viewer: bool = False
    #: 要写入 pbf 的字段名；``None`` = 全部字段
    fields: tuple[str, ...] | None = None


def template_is_valid(template: str) -> bool:
    """模板必须同时包含 ``{z}`` / ``{x}`` / ``{y}`` 三个占位符。"""
    text = str(template or "")
    return all(token in text for token in ("{z}", "{x}", "{y}"))


class VectorTileAdvancedDialog(BaseDialog):
    """编辑矢量瓦片高级选项。"""

    def __init__(
        self,
        parent,
        options: VectorTileAdvancedOptions,
        layers: list | None = None,
    ) -> None:
        super().__init__(
            parent, title="矢量瓦片高级设置", show_min_button=False, show_max_button=False
        )
        self.setMinimumSize(0, 0)
        self.resize(560, 400)
        self._layers = list(layers or [])
        self._fields = options.fields
        self._field_source_layer = self._pick_source_layer()

        layout = QVBoxLayout()
        hint = QLabel(
            "以下为工具层选项，不改变 QGIS 矢量瓦片算法的输出格式。"
        )
        hint.setWordWrap(True)
        layout.addWidget(hint)
        layout.addSpacing(6)

        layout.addWidget(QLabel("文件模板"))
        self.template_edit = QLineEdit(options.template or DEFAULT_TEMPLATE)
        self.template_edit.setToolTip(
            "瓦片相对输出目录的路径模板，必须包含 {z} / {x} / {y}；"
            "例如 {z}/{x}/{y}.pbf"
        )
        layout.addWidget(self.template_edit)

        layout.addWidget(QLabel("输出字段"))
        fields_row = QHBoxLayout()
        self.fields_label = QLabel()
        fields_row.addWidget(self.fields_label, 1)
        fields_btn = QPushButton("选择字段…")
        fields_btn.setToolTip(
            "只把勾选的字段写进 pbf。字段越多瓦片越大，网页查看器越慢；"
            "默认导出全部字段。"
        )
        fields_btn.clicked.connect(self._choose_fields)
        fields_row.addWidget(fields_btn)
        layout.addLayout(fields_row)
        self._update_fields_label()
        self.clear_check = QCheckBox("执行前清空输出目录（删除其中的旧瓦片与文件）")
        self.clear_check.setChecked(bool(options.clear_output))
        self.clear_check.setToolTip(
            "输出目录存在时，先删除目录内的全部内容再切片；"
            "批量处理只清空每个图层自己的子目录。"
        )
        layout.addWidget(self.clear_check)

        self.viewer_check = QCheckBox("生成网页查看器（index.html）")
        self.viewer_check.setChecked(bool(options.viewer))
        self.viewer_check.setToolTip(
            "写一个 Leaflet + VectorGrid 页面，用相对路径读取 {z}/{x}/{y}.pbf；"
            "需要联网加载前端脚本，建议用本地 HTTP 服务打开。"
        )
        layout.addWidget(self.viewer_check)
        layout.addStretch(1)

        bottom = QHBoxLayout()
        bottom.addStretch(1)
        cancel = QPushButton("取消")
        cancel.clicked.connect(self.reject)
        bottom.addWidget(cancel)
        ok = QPushButton("确定")
        ok.setDefault(True)
        ok.clicked.connect(self._validate)
        bottom.addWidget(ok)
        layout.addLayout(bottom)

        self.setLayout(layout)

    def options(self) -> VectorTileAdvancedOptions:
        """当前编辑结果。"""
        return VectorTileAdvancedOptions(
            template=self.template_edit.text().strip() or DEFAULT_TEMPLATE,
            clear_output=self.clear_check.isChecked(),
            viewer=self.viewer_check.isChecked(),
            fields=self._fields,
        )

    def _pick_source_layer(self):
        """找一个可用的图层作为字段清单来源（字段选择只按字段名应用）。"""
        for layer in self._layers:
            try:
                if layer is not None and layer.isValid():
                    return layer
            except (AttributeError, RuntimeError):
                continue
        return None

    def _update_fields_label(self) -> None:
        if self._fields is None:
            self.fields_label.setText("全部字段（字段多时瓦片会很大）")
            return
        self.fields_label.setText(f"已选 {len(self._fields)} 个字段")

    def _choose_fields(self) -> None:
        layer = self._field_source_layer
        if layer is None:
            QMessageBox.warning(
                self,
                "矢量瓦片高级设置",
                "当前没有可用的矢量图层，无法选择输出字段。",
            )
            return
        all_fields = [field.name() for field in layer.fields()]
        visible = all_fields if self._fields is None else list(self._fields)
        dialog = OrganizeColumnsDialog(
            layer,
            visible_names=visible,
            parent=self,
            title="选择矢量瓦片输出字段",
            hint="勾选要写入 pbf 的字段（字段越少，瓦片越小、查看器越快）：",
        )
        accepted = dialog.exec()
        chosen = dialog.checked_field_names() if accepted else []
        dialog.deleteLater()
        if not accepted:
            return
        if not chosen:
            QMessageBox.warning(
                self, "矢量瓦片高级设置", "请至少保留一个字段。"
            )
            return
        # 全选时存 None，表示“全部字段”，避免后续图层新增字段被漏掉
        self._fields = None if set(chosen) == set(all_fields) else tuple(chosen)
        self._update_fields_label()

    def _validate(self) -> None:
        if not template_is_valid(self.template_edit.text()):
            QMessageBox.warning(
                self,
                "矢量瓦片高级设置",
                "文件模板必须包含 {z}、{x}、{y} 三个占位符，例如 {z}/{x}/{y}.pbf。",
            )
            return
        self.accept()


__all__ = [
    "DEFAULT_TEMPLATE",
    "VectorTileAdvancedDialog",
    "VectorTileAdvancedOptions",
    "template_is_valid",
]
