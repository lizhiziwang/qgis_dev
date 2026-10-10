"""节点列表交互工具。

编辑工具条「节点列表」按钮激活的交互工具：**点击当前编辑图层上的图形 → 弹出该
图形的「节点列表」弹窗**（:class:`app.ui.dialogs.node_list.NodeListDialog`），
在弹窗里逐个查看 / 前插 / 后插 / 删除节点，并可让节点在图上闪烁、把视图平移或缩放
到它。

与「属性录入」（:class:`app.ui.tools.attribute_entry.AttributeEntryTool`）同一套
交互形状，所以直接**继承它、只覆写 `canvasReleaseEvent`** —— 命中测试（12px 容差、
多要素取最上层）、像素↔坐标换算、Esc 的推迟退出、`deactivate` 复位这些容易写错的
部分全都在父类里已经写对了，没必要再抄一遍。

与父类的三处差异：

- **不改图层选中集**：父类命中后会 ``selectByIds([fid])`` 把选中集换成该要素（属性
  录入一次只针对一个要素，需要那个高亮）。节点列表与选中集无关，改用户的选中集只会
  让人困惑；被检查的图形由弹窗自己的描边带标出。
- **必须已处于编辑态才可用**（用户明确约束）：没有编辑目标图层、或目标图层未开编辑，
  都只提示、不弹窗。
- **点空 / 点到别的图层时提示得更具体**：用户点了一个不在编辑图层上的图形时，明确
  告诉他是哪个图层的图形，比一句「未命中」有用得多。
"""

from __future__ import annotations

from collections.abc import Callable

from qgis.core import QgsVectorLayer
from qgis.PyQt.QtCore import Qt

from app.infra.logging import get_logger
from app.ui.tools.attribute_entry import AttributeEntryTool

logger = get_logger(__name__)


class NodeListTool(AttributeEntryTool):
    """点击要素 → 弹出其「节点列表」弹窗（只作用于当前编辑图层）。"""

    def __init__(
        self,
        canvas,
        editing_layer_getter: Callable[[], QgsVectorLayer | None] = lambda: None,
        on_feature: Callable[[QgsVectorLayer, int], None] | None = None,
        hint: Callable[[str], None] | None = None,
    ) -> None:
        super().__init__(
            canvas,
            editing_layer_getter,
            on_feature=on_feature,
            hint=hint,
        )
        # 与其它拾取工具一致：十字光标提示「正在选图形」
        self.setCursor(Qt.CursorShape.CrossCursor)

    # -------------------------------------------------- 交互

    def canvasReleaseEvent(self, event) -> None:  # noqa: N802 —— QGIS 命名
        """左键单击：命中编辑图层上的要素 → 回调 ``on_feature(layer, fid)``。"""
        if event.button() != Qt.MouseButton.LeftButton:
            return
        layer = self._current_layer()
        if layer is None or layer.type() != layer.VectorLayer:
            self._hint("没有编辑图层：请先点「开始编辑」选择图层")
            return
        if not layer.isEditable():
            self._hint(f"图层「{layer.name()}」未处于编辑态，无法查看节点列表")
            return

        fid = self._pick_fid(event, layer)
        if fid is None:
            other = self._pick_other_layer(event, layer)
            if other is not None:
                self._hint(
                    f"只能查看当前编辑图层「{layer.name()}」上的图形，"
                    f"当前点击的是「{other.name()}」"
                )
            else:
                self._hint(f"未点击到图形：请点击当前编辑图层「{layer.name()}」上的图形")
            return
        # 与父类不同：**不动**图层选中集，直接把 (图层, fid) 交给主窗口开弹窗
        self._on_feature(layer, fid)

    # -------------------------------------------------- 内部

    def _pick_other_layer(self, event, edit_layer: QgsVectorLayer):
        """在其它可见矢量图层上做同一套小范围求交，**只用于把提示说清楚**。

        没命中编辑图层时才有这一次扫描（每次最多几个可见图层各求交一次），
        换来的是「你点的是别的图层的图形」这句能让用户知道该切哪一层。
        """
        try:
            layers = self.canvas.layers()
        except RuntimeError:
            return None
        for other in layers:
            if other is edit_layer or not isinstance(other, QgsVectorLayer):
                continue
            try:
                if not other.isValid():
                    continue
            except RuntimeError:
                continue
            if self._pick_fid(event, other) is not None:
                return other
        return None


__all__ = ["NodeListTool"]
