"""矢量瓦片「高级」设置弹窗。

当前矢量瓦片算法（``native:writevectortiles_xyz``）本身只有输出目录 / 模板 /
图层 / 缩放 / 范围这几个参数，没有更多算法级选项。这里提供的是**工具层**的
三个实用开关：

- **文件模板**：``{z}/{x}/{y}.pbf``，可改成带子目录的模板；
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

DEFAULT_TEMPLATE = "{z}/{x}/{y}.pbf"


@dataclass
class VectorTileAdvancedOptions:
    """矢量瓦片工具层高级选项。"""

    template: str = DEFAULT_TEMPLATE
    clear_output: bool = False
    viewer: bool = False


def template_is_valid(template: str) -> bool:
    """模板必须同时包含 ``{z}`` / ``{x}`` / ``{y}`` 三个占位符。"""
    text = str(template or "")
    return all(token in text for token in ("{z}", "{x}", "{y}"))


class VectorTileAdvancedDialog(BaseDialog):
    """编辑矢量瓦片高级选项。"""

    def __init__(self, parent, options: VectorTileAdvancedOptions) -> None:
        super().__init__(
            parent, title="矢量瓦片高级设置", show_min_button=False, show_max_button=False
        )
        self.setMinimumSize(0, 0)
        self.resize(560, 330)

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
        )

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
