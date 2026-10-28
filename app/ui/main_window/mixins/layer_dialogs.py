"""图层相关对话框。

属性表、执行 SQL、样式编辑器、标注设置、样式导入导出、图层导出与图层属性。

由 :class:`~app.ui.main_window.main_window.MainWindow` 多继承组合，方法只依赖
``self`` 上的共享状态。
"""

from __future__ import annotations

import math
from contextlib import suppress

from qgis.core import (
    Qgis,
    QgsCategorizedSymbolRenderer,
    QgsPalettedRasterRenderer,
    QgsRasterLayer,
    QgsRendererCategory,
    QgsSingleSymbolRenderer,
    QgsVectorLayer,
)
from qgis.PyQt.QtGui import QColor
from qgis.PyQt.QtWidgets import (
    QDialog,
    QListWidget,
    QTreeWidget,
)

from app.infra.logging import get_logger
from app.ui.common.icons import Icons
from app.ui.common.message_box import MessageBox
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

    def _open_raster_attribute_table(self) -> None:
        """打开栅格属性表（RAT）。

        用自建的 :class:`RasterAttributeTableDialog`（跟随应用的无边框标题栏与
        主题）只读展示字段 / 行 / 颜色。窗口按图层单实例复用，图层从工程移除时
        自动关闭。
        """
        layer = self._selected_layer
        if not isinstance(layer, QgsRasterLayer):
            return
        if layer.attributeTableCount() <= 0:
            MessageBox.warning(self, "打开属性表", "该图层没有栅格属性表。")
            return
        try:
            from app.ui.dialogs.raster_attribute_table import (
                RasterAttributeTableDialog,
            )

            show_modeless(
                self,
                ("raster_attribute_table", layer.id()),
                lambda: RasterAttributeTableDialog(layer, parent=self),
                layer=layer,
            )
        except Exception as e:  # 交给消息框，别让右键菜单崩掉
            logger.error("打开栅格属性表失败: %s", e, exc_info=True)
            MessageBox.warning(self, "打开属性表失败", str(e))

    def _create_raster_attribute_table(self) -> None:
        """按当前渲染器生成栅格属性表，并写回数据源。

        ``createFromRaster`` 只支持调色板 / 单波段伪彩色渲染器（与 QGIS 一致）。
        ``writeNativeAttributeTable`` 对不支持的格式不一定报错（例如 GPKG 会
        静默丢弃），所以写完必须 ``readNativeAttributeTable`` 回读验证；失败
        就回滚 provider 里的表，避免出现“本次会话能打开、下次打开就没了”。
        """
        layer = self._selected_layer
        if not isinstance(layer, QgsRasterLayer):
            return
        if layer.attributeTableCount() > 0:
            self._open_raster_attribute_table()
            return
        if not layer.canCreateRasterAttributeTable():
            MessageBox.warning(
                self,
                "创建栅格属性表",
                "当前渲染方式无法生成栅格属性表。",
                informative_text="请先使用「调色板」或「单波段伪彩色」渲染，再创建属性表。",
            )
            return

        ret = MessageBox.question(
            self,
            "创建栅格属性表",
            f"将按「{layer.name()}」的当前渲染生成属性表，并写入数据源。",
            MessageBox.StandardButton.Yes | MessageBox.StandardButton.No,
            MessageBox.StandardButton.No,
            informative_text="此操作会修改原始数据文件，是否继续？",
        )
        if ret != MessageBox.StandardButton.Yes:
            return

        from app.core.util.raster_rat import build_raster_attribute_table

        table, band = build_raster_attribute_table(layer)
        if table is None or band <= 0:
            MessageBox.warning(self, "创建栅格属性表", "当前渲染方式无法生成属性表。")
            return

        provider = layer.dataProvider()
        try:
            provider.setAttributeTable(band, table)
        except Exception as e:  # 非 GDAL 数据源可能根本没实现这个方法
            logger.error("设置栅格属性表失败: %s", e, exc_info=True)
            MessageBox.warning(
                self,
                "创建栅格属性表",
                "当前数据源不支持保存属性表。",
                informative_text=str(e),
            )
            return
        try:
            ok, err = provider.writeNativeAttributeTable()
            # 不支持的格式可能返回成功但实际上没写进去（GPKG），回读才是准的。
            written = False
            if ok:
                written, _ = provider.readNativeAttributeTable()
        except Exception as e:  # 写回失败要走回滚，不让异常冒到菜单槽
            ok, written, err = False, False, str(e)
            logger.error("写入栅格属性表失败: %s", e, exc_info=True)

        if not ok or not written:
            with suppress(Exception):
                provider.removeAttributeTable(band)
            MessageBox.warning(
                self,
                "创建栅格属性表",
                "当前栅格格式不支持保存属性表。",
                informative_text=err
                or "可改用 GeoTIFF、Erdas Imagine（.img）或 VRT 等支持 RAT 的格式。",
            )
            return

        self.messageBar.pushMessage(
            f"已创建栅格属性表：{layer.name()}",
            level=Qgis.MessageLevel.Success,
            duration=3,
        )

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
        """新建图层属性窗口时接上「已应用」信号：点「应用」也刷新行首色块。

        上次关窗时停在哪个页签，这次右键打开就优先进入哪个页签（页签被禁用时跳过）。
        """
        try:
            dlg.applied.connect(self.layer_dock.refresh_symbol_icons)
        except (AttributeError, TypeError):
            pass
        last = getattr(self, "_last_properties_tab", "")
        if last:
            self._select_tab_widget_page(dlg, (last,))

    def _on_layer_properties_finished(self, dlg) -> None:
        """图层属性关闭（确定 / 取消都算）→ 记住当前页签并刷新图层树。"""
        try:
            tabs = getattr(dlg, "tabs", None)
            if tabs is not None and tabs.count():
                self._last_properties_tab = tabs.tabText(tabs.currentIndex())
        except (RuntimeError, AttributeError):
            pass
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
            # 栅格没有“符号”，双击图例直接进渲染页（RGB / 伪彩色 / 山体阴影…）
            if isinstance(layer, QgsRasterLayer):
                self._show_render_tab(layer)
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
    def _find_palette_class(classes, text: str):
        """按图例文本找调色板类的下标：先匹配标签，再按数值匹配。"""
        text = str(text or "").strip()
        if not text:
            return None
        labeled = [
            index
            for index, cls in enumerate(classes)
            if str(cls.label or "").strip() == text
        ]
        if len(labeled) == 1:
            return labeled[0]
        try:
            value = float(text)
        except ValueError:
            return None
        hits = [
            index
            for index, cls in enumerate(classes)
            if math.isclose(float(cls.value), value, rel_tol=0.0, abs_tol=1e-9)
        ]
        return hits[0] if len(hits) == 1 else None

    def _edit_raster_legend_color(self, layer, text: str) -> None:
        """双击栅格调色板图例的色块：改对应类的颜色，重建渲染器后刷新图例。

        与矢量分类图例一样重建渲染器而不是原地改类：本构建里渲染器返回的
        类拷贝与渲染器共享颜色 / 符号指针，原地写回有双重释放风险。
        """
        from app.ui.dialogs.layer_properties.widgets import pick_color

        if not isinstance(layer, QgsRasterLayer):
            return
        try:
            renderer = layer.renderer()
        except RuntimeError:
            return
        if not isinstance(renderer, QgsPalettedRasterRenderer):
            # 伪彩色 / 灰度等没有逐个类的图例，退回渲染设置页
            self._show_render_tab(layer)
            return
        try:
            classes = list(renderer.classes())
            band = int(renderer.inputBand())
        except (RuntimeError, AttributeError):
            return
        index = self._find_palette_class(classes, text)
        if index is None:
            self._show_render_tab(layer)
            return
        target = classes[index]
        chosen = pick_color(self, QColor(target.color))
        if chosen is None:
            return
        try:
            updated = [
                QgsPalettedRasterRenderer.Class(
                    cls.value,
                    chosen if position == index else QColor(cls.color),
                    str(cls.label or ""),
                )
                for position, cls in enumerate(classes)
            ]
            new_renderer = QgsPalettedRasterRenderer(
                layer.dataProvider(), band, updated
            )
            with suppress(Exception):
                new_renderer.setOpacity(renderer.opacity())
            layer.setRenderer(new_renderer)
            layer.triggerRepaint()
            self.layer_dock.refresh_symbol_icons()
        except Exception as exc:  # noqa: BLE001
            logger.warning("编辑栅格图例颜色失败：%s", exc, exc_info=True)

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
            MessageBox.warning(self, "失败", str(e))

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
                if tabs.tabText(index) in texts and tabs.isTabEnabled(index):
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
                # 图层列表右键：筛选数据（导出全部 / 选中数据 / 自定义条件）
                filter_modes=("all", "selected", "expression"),
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
            MessageBox.warning(self, "失败", str(e))
