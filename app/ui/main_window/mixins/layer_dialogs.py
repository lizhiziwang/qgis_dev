"""图层相关对话框。

属性表、执行 SQL、样式编辑器、标注设置、样式导入导出、图层导出与图层属性。

由 :class:`~app.ui.main_window.main_window.MainWindow` 多继承组合，方法只依赖
``self`` 上的共享状态。
"""

from __future__ import annotations

from qgis.core import (
    Qgis,
    QgsStyle,
    QgsVectorLayer,
)
from qgis.gui import (
    QgsRendererPropertiesDialog,
)
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

    def _show_render_tab(self, layer) -> None:
        """头部「打开图层渲染设置」：弹自建 :class:`LayerPropertiesDialog` 并跳到「渲染」tab。

        与 :meth:`_show_style_editor` 的区别：

        - 前者用 QGIS 原生 :class:`QgsRendererPropertiesDialog`（chrome 不随本应用主题）；
        - 本方法用项目自建的 :class:`LayerPropertiesDialog`（跟随 :class:`ThemeManager`
          与无边框标题栏），且**入口默认跳到「渲染」页**——避免用户在多个 tab 间再翻。

        tab 跳转按 tab 文本"渲染"匹配（:class:`LayerPropertiesDialog._TAB_SPECS` 表里
        第 5 个就是「渲染」→ :class:`RenderTab`）。点「确定 / 应用」后由 dialog 内部
        :meth:`_apply_all` 触发 ``triggerRepaint`` + ``canvas.refresh``，本方法不重复触发
        （避免在信号栈内重入画布，参见 :meth:`_show_label_settings` 的同型注释）。
        """
        from app.ui.dialogs.layer_properties.dialog import LayerPropertiesDialog

        dlg = LayerPropertiesDialog(layer, canvas=self.canvas, parent=self)
        for i in range(dlg.tabs.count()):
            if dlg.tabs.tabText(i) == "渲染":
                dlg.tabs.setCurrentIndex(i)
                break
        dlg.exec()

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
            # 对话框里可能改了标注启用状态；图层标注改动不发信号，手动刷新行尾「标注」开关
            self.layer_dock.refresh_label_state()
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "失败", str(e))

    def _on_layer_label_toggle(self, layer, enabled: bool) -> None:
        """图层树行尾的「标注」开关：开关该图层标注（每个图层各自独立，不互斥）。

        **不自动创建标注配置**——没配过标注的图层（``labeling() is None``）
        ``setLabelsEnabled`` 是静默空操作（实测调完 ``labelsEnabled()`` 仍为 False），
        所以这里如实提示去属性对话框配置，而不是假装打开。关闭时只关启用、保留
        已有配置，与「图层属性 → 标注」页的「无标注」语义一致。
        """
        if not isinstance(layer, QgsVectorLayer):
            return
        if enabled and layer.labeling() is None:
            self.messageBar.pushMessage(
                f"{layer.name()} 尚未配置标注，请先在「图层属性 → 标注」中配置",
                level=Qgis.MessageLevel.Info,
                duration=4,
            )
            return
        layer.setLabelsEnabled(bool(enabled))
        layer.triggerRepaint()
        self.canvas.refresh()
        self.layer_dock.refresh_label_state()

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
        """图层列表右键「导出」：用自建导出对话框（与数据管理一致，多支持四种格式）。"""
        from app.ui.dialogs.create_data_dialog import default_output_dir
        from app.ui.dialogs.export_layer_dialog import (
            ExportLayerDialog,
            export_with_progress,
        )

        if layer is None:
            return
        dlg = ExportLayerDialog(
            self,
            layer=layer,
            default_dir=default_output_dir(),
            extra_formats=True,
        )
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        spec = dlg.spec()
        if spec is not None:
            export_with_progress(self, layer, spec)
        else:
            logger.warning("导出图层：表单不合法，未导出")

    def _open_layer_properties(self, layer) -> None:
        """打开自建图层属性对话框（不再用 QGIS 原生 ``QgsVectorLayerProperties``）。

        原生窗口自带一套不跟随本应用无边框标题栏与主题的 chrome。自建版见
        ``app/ui/dialogs/layer_properties/``：目前「常规」「字段」「标注」页
        已实现，其余 3 页占位。
        """
        if layer is None:
            return
        try:
            from app.ui.dialogs.layer_properties import LayerPropertiesDialog

            dlg = LayerPropertiesDialog(layer, canvas=self.canvas, parent=self)
            dlg.exec()
            # 「标注」页可能改了启用状态；图层标注改动不发信号，手动刷新行尾「标注」开关
            self.layer_dock.refresh_label_state()
        except Exception as e:  # noqa: BLE001
            logger.error("打开图层属性失败: %s", e, exc_info=True)
            QMessageBox.warning(self, "失败", str(e))
