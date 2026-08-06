"""自定义图层属性 Dialog（占位实现）。"""

from __future__ import annotations

from app.ui.common.base_dialog import BaseDialog


class CustomLayerPropertiesDialog(BaseDialog):
    """自定义图层属性 Dialog（占位）。"""

    def __init__(self, layer, parent=None) -> None:
        super().__init__(parent, layer.name() if layer else "图层属性")
        self.layer = layer
