from PyQt5.QtWidgets import QDialog, QPushButton
from qgis._core import QgsVectorFileWriter, QgsVectorLayer
from qgis._gui import QgsVectorLayerSaveAsDialog
from .Logger import setup_logger

logger = setup_logger()



def save_as_export(layer, project):
    dialog = QgsVectorLayerSaveAsDialog(layer)
    # 遍历对话框所有子按钮
    for btn in dialog.findChildren(QPushButton):
        original_text = btn.text().lower()
        if original_text in ["ok", "Save", "确定", "保存"]:  # 匹配常见文本
            btn.setText("确定")
        elif original_text in ["cancel", "取消"]:
            btn.setText("取消")
        elif original_text in ["help", "帮助"]:
            # 移除帮助按钮
            btn.hide()
    if dialog.exec_() == QDialog.Accepted:
        output_filename = dialog.fileName()
        output_layer_name = dialog.layerName()
        output_crs = dialog.crs()
        output_format = dialog.format()
        add_to_canvas = dialog.addToCanvas()
        error, message = QgsVectorFileWriter.writeAsVectorFormat(
            layer,
            output_filename,
            "UTF-8",  # 编码
            output_crs,
            output_format,
            onlySelected=False,  # 是否只保存选中的要素
            layerOptions=["LAYER_NAME=" + output_layer_name]  # 设置图层名
        )

        if error == QgsVectorFileWriter.NoError:
            print(f"图层已成功保存到 {output_filename}")
            if add_to_canvas:
                new_layer = QgsVectorLayer(output_filename, output_layer_name, "ogr")
                if new_layer.isValid():
                    project.addMapLayer(new_layer)
                    logger.info(f"新图层 '{output_layer_name}' 已添加到地图")
                else:
                    logger.warn("无法加载新图层到地图")
        else:
            logger.warn(f"保存失败: {message}")
