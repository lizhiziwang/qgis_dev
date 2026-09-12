"""自建图层属性对话框（替代 QGIS 原生 ``QgsVectorLayerProperties``）。"""

from __future__ import annotations

from app.ui.dialogs.layer_properties.base import LayerPropertyTab, PlaceholderTab
from app.ui.dialogs.layer_properties.dialog import LayerPropertiesDialog

__all__ = ["LayerPropertiesDialog", "LayerPropertyTab", "PlaceholderTab"]
