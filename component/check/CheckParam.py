import string

from qgis._core import QgsVectorLayer


class CheckParam:
    def __init__(self, layer: QgsVectorLayer = None, check_name: string = None, field_name: [] = None,
                 where: string = None,
                 meta_source_layer_name: string = None, check_id=None):
        self.layer = layer
        self.check_name = check_name
        self.field_name = field_name
        self.where = where
        self.meta_source_layer_name = meta_source_layer_name
        self.check_id = check_id


class NullValueParam(CheckParam):
    def __init__(self, layer: QgsVectorLayer = None, check_name: string = None, field_name: [] = None,
                 where: string = None, meta_source_layer_name: string = None, check_id=None):
        super().__init__(layer, check_name, field_name, where, meta_source_layer_name, check_id)

