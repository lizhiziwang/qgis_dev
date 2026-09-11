"""图层属性对话框的标签页协议与占位页。

每个标签页都是一个独立 ``QWidget``，只实现两个方法：

- :meth:`LayerPropertyTab.load` —— 从图层读值填控件（构造时调一次）
- :meth:`LayerPropertyTab.apply` —— 把控件值写回图层；校验不通过返回 ``False``
  并自己弹 ``QMessageBox``（对话框外壳据此中止「确定」并保持打开）

这样后续补「字段 / 选择 / 标注 / 渲染 / 条件」各页时，只要写一个新类塞进
:data:`~app.ui.dialogs.layer_properties.dialog.LayerPropertiesDialog._TAB_SPECS`
即可，外壳完全不用改。
"""

from __future__ import annotations

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import QLabel, QVBoxLayout, QWidget


class LayerPropertyTab(QWidget):
    """图层属性标签页基类。"""

    def load(self, layer) -> None:
        """从 ``layer`` 读值填充控件。子类重写。"""

    def apply(self, layer) -> bool:
        """把控件值写回 ``layer``。

        返回 ``False`` 表示校验未通过（子类须自己提示用户），外壳会中止
        「确定 / 应用」并让对话框保持打开。默认实现什么都不做。
        """
        return True


class PlaceholderTab(LayerPropertyTab):
    """尚未实现的标签页占位内容。"""

    def __init__(self, title: str = "", parent=None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        tip = QLabel(f"「{title}」页内容待补充" if title else "内容待补充", self)
        tip.setObjectName("PropHint")
        tip.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(tip)
