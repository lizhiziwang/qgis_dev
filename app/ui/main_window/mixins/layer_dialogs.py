"""图层相关对话框。

属性表、执行 SQL、样式编辑器、标注设置、样式导入导出、图层导出与图层属性。

由 :class:`~app.ui.main_window.main_window.MainWindow` 多继承组合，方法只依赖
``self`` 上的共享状态。
"""

from __future__ import annotations

from qgis.core import (
    QgsStyle,
    QgsVectorLayer,
)
from qgis.gui import (
    QgsRendererPropertiesDialog,
    QgsVectorLayerSaveAsDialog,
)
from qgis.PyQt.QtGui import QAction
from qgis.PyQt.QtWidgets import (
    QDialog,
    QListWidget,
    QMessageBox,
    QTreeWidget,
)

from app.infra.logging import get_logger
from app.ui.common.icons import Icons
from app.ui.dialogs.execute_sql import ExecuteSqlDialog
from app.ui.dialogs.style_io import (
    StyleOpenDialog,
    StyleSaveDialog,
)

logger = get_logger(__name__)


class LayerDialogsMixin:
    """LayerDialogsMixin：见模块 docstring。"""

    def _open_attribute_table(self) -> None:
        if self._selected_layer is None:
            return
        self._show_attribute_dock(self._selected_layer)

    def _open_execute_sql(self) -> None:

        if self._selected_layer is None:
            return
        dlg = ExecuteSqlDialog(self._selected_layer, self)
        dlg.show()

    def _show_style_editor(self, layer) -> None:
        dlg = QgsRendererPropertiesDialog(layer=layer, style=QgsStyle.defaultStyle(), embedded=False, parent=self)
        dlg.setWindowTitle(f"图层样式 - {layer.name()}")
        if dlg.exec():
            layer.triggerRepaint()
            self.canvas.refresh()

    def _show_label_settings(self, layer) -> None:
        """打开标注设置。

        本 QGIS 构建没有绑定独立的 ``QgsLabelingWidget``，改用图层属性对话框
        并尽力跳到"标注"页（失败则停留在默认页，用户可手动点"标注"）。
        """
        if not isinstance(layer, QgsVectorLayer):
            return
        try:
            from qgis._gui import QgsVectorLayerProperties

            dlg = QgsVectorLayerProperties(canvas=self.canvas, lyr=layer, messageBar=self.messageBar, parent=self)
            dlg.setWindowIcon(Icons.app())
            dlg.setWindowTitle(f"标注设置 - {layer.name()}")
            self._select_properties_page(dlg, ("标注", "Labels"))
            dlg.exec()
            layer.triggerRepaint()
            self.canvas.refresh()
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "失败", str(e))

    def _select_properties_page(self, dlg, texts: tuple) -> None:
        """尽力跳到图层属性对话框的指定页（按左侧选项列表项文本匹配，失败则忽略）。"""
        try:
            for w in dlg.findChildren(QListWidget):
                for i in range(w.count()):
                    item = w.item(i)
                    if item and item.text() in texts:
                        w.setCurrentItem(item)
                        return
            for w in dlg.findChildren(QTreeWidget):
                root = w.invisibleRootItem()
                for i in range(root.childCount()):
                    item = root.child(i)
                    if item and item.text(0) in texts:
                        w.setCurrentItem(item)
                        return
        except Exception:  # noqa: BLE001
            pass

    def _open_style_file(self, layer) -> None:

        dlg = StyleOpenDialog(layer, self)
        dlg.show()

    def _save_style(self, layer) -> None:

        dlg = StyleSaveDialog(layer, self)
        dlg.exec()

    def _export_layer(self, layer) -> None:
        dlg = QgsVectorLayerSaveAsDialog(layer)
        for btn in dlg.findChildren(QAction):
            t = btn.text().lower()
            if t in ("ok", "save", "确定", "保存"):
                btn.setText("确定")
            elif t in ("cancel", "取消"):
                btn.setText("取消")
            elif t in ("help", "帮助"):
                btn.hide()
        result = dlg.exec()
        if result == QDialog.DialogCode.Accepted:
            # 对话框内部已把图层写出（exec 的 OK 槽触发保存）；记日志
            out = dlg.fileName() or ""
            logger.info("导出图层：%s → %s", layer.name(), out)

    def _open_layer_properties(self, layer) -> None:
        try:
            from qgis._gui import QgsVectorLayerProperties

            dlg = QgsVectorLayerProperties(canvas=self.canvas, lyr=layer, messageBar=self.messageBar, parent=self)
            dlg.setWindowIcon(Icons.app())
            dlg.exec()
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "失败", str(e))
