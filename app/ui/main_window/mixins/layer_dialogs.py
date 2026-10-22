"""图层相关对话框。

属性表、执行 SQL、样式编辑器、标注设置、样式导入导出、图层导出与图层属性。

由 :class:`~app.ui.main_window.main_window.MainWindow` 多继承组合，方法只依赖
``self`` 上的共享状态。
"""

from __future__ import annotations

from qgis.core import (
    Qgis,
    QgsCategorizedSymbolRenderer,
    QgsRendererCategory,
    QgsSingleSymbolRenderer,
    QgsVectorLayer,
)
from qgis.PyQt.QtWidgets import (
    QDialog,
    QListWidget,
    QMessageBox,
    QTreeWidget,
)

from app.infra.logging import get_logger
from app.ui.common.icons import Icons
from app.ui.common.modeless import show_modeless
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

    def _show_render_tab(self, layer) -> None:
        """打开图层渲染设置：弹自建 :class:`LayerPropertiesDialog` 并跳到「渲染」tab。

        统一使用项目自建的 :class:`LayerPropertiesDialog`（跟随 :class:`ThemeManager`
        与无边框标题栏），**入口默认跳到「渲染」页**——避免用户在多个 tab 间再翻，
        也不再使用 QGIS 原生的 ``QgsRendererPropertiesDialog``。

        tab 跳转按 tab 文本"渲染"匹配（:class:`LayerPropertiesDialog._TAB_SPECS` 表里
        第 5 个就是「渲染」→ :class:`RenderTab`）。点「确定 / 应用」后由 dialog 内部
        :meth:`_apply_all` 触发 ``triggerRepaint`` + ``canvas.refresh``，本方法不重复触发
        （避免在信号栈内重入画布，参见 :meth:`_show_label_settings` 的同型注释）。
        """
        if layer is None:
            return
        from app.ui.dialogs.layer_properties.dialog import LayerPropertiesDialog

        dlg = show_modeless(
            self,
            ("layer_properties", layer.id()),
            lambda: LayerPropertiesDialog(layer, canvas=self.canvas, parent=self),
            on_finished=self._on_layer_properties_finished,
            configure=self._configure_layer_properties,
            layer=layer,
        )
        self._select_properties_page(dlg, ("渲染", "Render"))
        self._select_tab_widget_page(dlg, ("渲染", "Render"))

    def _configure_layer_properties(self, dlg) -> None:
        """新建图层属性窗口时接上「已应用」信号：点「应用」也刷新行首色块。"""
        try:
            dlg.applied.connect(self.layer_dock.refresh_symbol_icons)
        except (AttributeError, TypeError):
            pass

    def _on_layer_properties_finished(self, _dlg) -> None:
        """图层属性关闭（确定 / 取消都算）→ 刷新行尾标注开关与行首符号色块。"""
        try:
            self.layer_dock.refresh_label_state()
            self.layer_dock.refresh_symbol_icons()
        except Exception:  # noqa: BLE001 —— 刷新失败不影响已完成的修改
            pass

    # --------------------------------------------------- 双击符号编辑

    def _open_symbol_selector(self, symbol, layer):
        """共用符号选择器：返回选中的符号拷贝，取消返回 ``None``。"""
        from app.ui.dialogs.layer_properties.render import _geom_kind
        from app.ui.dialogs.layer_properties.symbol_selector import (
            SymbolSelectorDialog,
        )

        kind = _geom_kind(layer)
        if kind is None or symbol is None:
            return None
        dlg = SymbolSelectorDialog(symbol.clone(), kind, self)
        try:
            if dlg.exec() != QDialog.DialogCode.Accepted:
                return None
            return dlg.selected_symbol()
        finally:
            dlg.deleteLater()

    def _edit_layer_symbol(self, layer) -> None:
        """双击图层行的符号图标：单一符号弹符号选择器，其它渲染方式打开渲染页。"""
        if not isinstance(layer, QgsVectorLayer):
            return
        renderer = layer.renderer()
        if not isinstance(renderer, QgsSingleSymbolRenderer):
            self._show_render_tab(layer)
            return
        try:
            symbol = renderer.symbol()
        except (RuntimeError, AttributeError):
            return
        chosen = self._open_symbol_selector(symbol, layer)
        if chosen is None:
            return
        try:
            # 原地换符号：保留渲染器与其它配置
            renderer.setSymbol(chosen.clone())
            layer.triggerRepaint()
            self.layer_dock.refresh_symbol_icons()
        except Exception as exc:  # noqa: BLE001
            logger.warning("双击编辑图层符号失败：%s", exc, exc_info=True)

    def _edit_legend_symbol(self, layer, row: int) -> None:
        """双击分类图例子项：编辑该类别符号（重建分类渲染器，避免所有权问题）。"""
        if not isinstance(layer, QgsVectorLayer):
            return
        renderer = layer.renderer()
        if not isinstance(renderer, QgsCategorizedSymbolRenderer):
            # 分级的图例子项暂不支持直接改，退回渲染页
            self._show_render_tab(layer)
            return
        try:
            categories = renderer.categories()
            if not (0 <= row < len(categories)):
                return
            category = categories[row]
            symbol = category.symbol()
        except Exception:  # noqa: BLE001
            return
        chosen = self._open_symbol_selector(symbol, layer)
        if chosen is None:
            return
        try:
            self._replace_category_symbol(layer, renderer, row, chosen)
            layer.triggerRepaint()
            self.layer_dock.refresh_symbol_icons()
        except Exception as exc:  # noqa: BLE001
            logger.warning("编辑类别符号失败：%s", exc, exc_info=True)

    @staticmethod
    def _replace_category_symbol(layer, renderer, row: int, chosen) -> None:
        """用 ``chosen`` 替换第 ``row`` 个类别的符号，重建渲染器后换给图层。

        不调用 ``updateCategorySymbol``：本构建里类别拷贝与渲染器共享符号指针，
        传进去会双重释放（见 render.py 模块 docstring 的所有权说明）。
        """
        copies = renderer.categories()
        categories = []
        for index in range(len(copies)):
            category = copies[index]
            symbol = category.symbol()
            if symbol is None:
                continue
            categories.append(
                QgsRendererCategory(
                    category.value(),
                    chosen.clone() if index == row else symbol.clone(),
                    category.label(),
                    category.renderState(),
                )
            )
        new_renderer = QgsCategorizedSymbolRenderer(
            renderer.classAttribute(), categories
        )
        source = renderer.sourceSymbol()
        if source is not None:
            new_renderer.setSourceSymbol(source.clone())
        layer.setRenderer(new_renderer)

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

    @staticmethod
    def _select_tab_widget_page(dlg, texts: tuple) -> None:
        """自建图层属性对话框用 QTabWidget 分页：按 tab 文本切页。"""
        try:
            tabs = getattr(dlg, "tabs", None)
            if tabs is None:
                return
            for index in range(tabs.count()):
                if tabs.tabText(index) in texts:
                    tabs.setCurrentIndex(index)
                    return
        except Exception:  # noqa: BLE001
            pass

    def _open_style_file(self, layer) -> None:

        dlg = StyleOpenDialog(layer, self)
        dlg.show()

    def _save_style(self, layer) -> None:

        show_modeless(
            self,
            ("style_save", getattr(layer, "id", lambda: id(layer))()),
            lambda: StyleSaveDialog(layer, self),
            layer=layer,
        )

    def _export_layer(self, layer) -> None:
        """图层列表右键「导出」：用自建导出对话框（与数据管理一致，多支持四种格式）。"""
        from app.ui.dialogs.create_data_dialog import default_output_dir
        from app.ui.dialogs.export_layer_dialog import (
            ExportLayerDialog,
            export_with_progress,
        )

        if layer is None:
            return

        def _export(dlg) -> None:
            spec = dlg.spec()
            if spec is not None:
                export_with_progress(
                    self, layer, spec, load_after=dlg.load_after_export()
                )
            else:
                logger.warning("导出图层：表单不合法，未导出")

        show_modeless(
            self,
            ("export_layer", layer.id()),
            lambda: ExportLayerDialog(
                self,
                layer=layer,
                default_dir=default_output_dir(),
                extra_formats=True,
            ),
            on_accepted=_export,
            layer=layer,
        )

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

            show_modeless(
                self,
                ("layer_properties", layer.id()),
                lambda: LayerPropertiesDialog(layer, canvas=self.canvas, parent=self),
                on_finished=self._on_layer_properties_finished,
                configure=self._configure_layer_properties,
                layer=layer,
            )
        except Exception as e:  # noqa: BLE001
            logger.error("打开图层属性失败: %s", e, exc_info=True)
            QMessageBox.warning(self, "失败", str(e))
