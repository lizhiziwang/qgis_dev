
from .check_dia import BaseDialog

class LayerPropertiesDialog(BaseDialog):
    def __init__(self, layer, parent):
        super().__init__(parent,title=layer.name())
        self.layer = layer

