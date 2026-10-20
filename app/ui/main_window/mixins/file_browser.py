"""打开文件与数据浏览器。

本地文件打开、按类别装载图层，以及数据浏览器目录树的双击/右键菜单
（新建矢量图层、添加 GDB 图层）。

由 :class:`~app.ui.main_window.main_window.MainWindow` 多继承组合，方法只依赖
``self`` 上的共享状态。
"""

from __future__ import annotations

import os
from contextlib import suppress

from qgis.core import Qgis
from qgis.PyQt import sip
from qgis.PyQt.QtWidgets import QMessageBox

from app.infra.logging import get_logger
from app.ui.common.widgets import open_file_dir

logger = get_logger(__name__)


#: 浏览器目录右键"新建"子菜单支持的矢量格式：(显示名, 扩展名, OGR 驱动)
_NEW_VECTOR_FORMATS = (
    ("Shapefile 文件（点/线/面）", "shp", "ESRI Shapefile"),
    ("GeoPackage 数据库", "gpkg", "GPKG"),
    ("File Geodatabase 数据库", "gdb", "OpenFileGDB"),
    ("GeoJSON 文件", "geojson", "GeoJSON"),
    ("GML 文件", "gml", "GML"),
    ("KML 文件", "kml", "KML"),
    ("SQLite 数据库", "sqlite", "SQLite"),
    ("CSV 表格", "csv", "CSV"),
)

#: 「添加数据文件」认的栅格扩展名（按扩展名走 GDAL 栅格 provider）
_RASTER_EXTS = frozenset({
    ".tif", ".tiff", ".img", ".jpg", ".jpeg", ".png", ".bmp", ".gif",
    ".dem", ".asc", ".vrt", ".jp2", ".ecw", ".hdf", ".nc", ".mrf",
})

#: 重设数据源时按「原图层的数据源类型」给的过滤器（同类型文件）
_RASTER_FILE_FILTER = (
    "栅格数据 (*.tif *.tiff *.img *.jpg *.jpeg *.png *.bmp *.gif "
    "*.dem *.asc *.vrt *.jp2 *.ecw *.hdf *.nc *.mrf)"
)

_VECTOR_FILE_FILTERS: dict[str, str] = {
    ".shp": "Shapefile (*.shp)",
    ".geojson": "GeoJSON (*.geojson *.json)",
    ".json": "GeoJSON (*.geojson *.json)",
    ".kml": "KML (*.kml)",
    ".gml": "GML (*.gml)",
    ".gpkg": "GeoPackage (*.gpkg)",
    ".sqlite": "SQLite 数据库 (*.sqlite *.sqlite3 *.db)",
    ".sqlite3": "SQLite 数据库 (*.sqlite *.sqlite3 *.db)",
    ".db": "SQLite 数据库 (*.sqlite *.sqlite3 *.db)",
    ".csv": "CSV 表格 (*.csv)",
    ".tab": "MapInfo TAB (*.tab)",
    ".mif": "MapInfo MIF (*.mif)",
    ".fgb": "FlatGeobuf (*.fgb)",
    ".gpx": "GPX (*.gpx)",
}

_VECTOR_ANY_FILTER = (
    "矢量数据 (*.shp *.geojson *.json *.gpkg *.gml *.kml *.sqlite *.sqlite3 "
    "*.db *.csv *.tab *.mif *.fgb *.gpx)"
)

#: 已知的矢量扩展名。**不在这里、也不在栅格表里的**扩展名（比如 .txt/.dat）：
#: 先试矢量、再试栅格，都不成才报错
_VECTOR_HINT_EXTS = frozenset({
    ".shp", ".geojson", ".json", ".gpkg", ".gml", ".kml", ".sqlite",
    ".csv", ".tab", ".mif", ".fgb", ".gpx", ".dxf",
})

#: 多图层容器（一个文件装多个图层）：加载前先把里面的图层列出来让用户挑。
#: ``.gdb`` 是目录，走 ``load_file`` 的目录分支，不在这里。
_CONTAINER_VECTOR_EXTS = frozenset({".gpkg", ".sqlite", ".sqlite3", ".db"})

#: 文本 / XML 矢量格式：超过该大小加载可能卡顿甚至 OOM（KML 常见），加载前先提示
_LARGE_TEXT_VECTOR_EXTS = frozenset({".geojson", ".json", ".kml", ".gml"})
_LARGE_TEXT_VECTOR_BYTES = 50 * 1024 * 1024

#: 文件对话框的过滤器（矢量 + 栅格）
_DATA_FILE_FILTER = (
    "矢量 / 栅格数据 (*.shp *.geojson *.json *.gpkg *.gml *.kml *.sqlite *.csv *.tab *.mif *.fgb *.gpx"
    " *.tif *.tiff *.img *.jpg *.jpeg *.png *.bmp *.dem *.asc *.vrt *.jp2);;"
    "矢量文件 (*.shp *.geojson *.json *.gpkg *.gml *.kml *.sqlite *.csv *.tab *.mif *.fgb *.gpx);;"
    "栅格文件 (*.tif *.tiff *.img *.jpg *.jpeg *.png *.bmp *.dem *.asc *.vrt *.jp2);;"
    "所有文件 (*)"
)


class FileBrowserMixin:
    """FileBrowserMixin：见模块 docstring。"""

    def _on_add_layer(self) -> None:
        """图层面板「添加图层」按钮：弹出「文件 / GDB」两个入口。

        **为什么要分两个入口**：Qt 的文件对话框**强制模式** —— 文件模式选不中目录
        （``.gdb`` 是目录），目录模式又选不中文件；实测两种模式的 ``accept()`` 都
        "只认自己那一类"（选错类型直接 Rejected），一个对话框兼收做不到。
        """
        from qgis.PyQt.QtGui import QCursor
        from qgis.PyQt.QtWidgets import QMenu

        menu = QMenu(self)
        act_file = menu.addAction("添加数据文件…（矢量 / 栅格）")
        act_file.triggered.connect(self._on_open_file)
        act_gdb = menu.addAction("添加 GDB 数据库…（.gdb 文件夹）")
        act_gdb.triggered.connect(self._on_open_gdb)
        menu.exec(QCursor.pos())

    def _on_open_file(self) -> None:
        path = open_file_dir(
            self,
            is_dir=False,
            title="选择数据文件",
            filter_=_DATA_FILE_FILTER,
        )
        if path:
            self.load_file(path)

    def _on_open_gdb(self) -> None:
        """选一个 File Geodatabase 文件夹（``.gdb`` 是目录，只能用目录选择器）。"""
        path = open_file_dir(
            self,
            is_dir=True,
            title="选择 File Geodatabase（选到 .gdb 文件夹本身）",
        )
        if path:
            self.load_file(path)

    def _on_repair_layer_source(self, layer) -> None:
        """图层列表红色感叹号：给一个数据源丢失的图层重设数据源。

        支持：
        - 单文件矢量 / 栅格（shp、tif…）；
        - 容器数据源（GDB / GeoPackage / SQLite）——先选里面的具体图层；
        - 保留原图层对象（名称 / 样式 / 树中位置都不变），只替换数据源。
        """
        from qgis.core import (
            QgsRasterLayer,
            QgsVectorLayer,
        )

        if layer is None:
            return
        is_vector = isinstance(layer, QgsVectorLayer)
        is_raster = isinstance(layer, QgsRasterLayer)
        if not (is_vector or is_raster):
            return

        # 当前数据源的类型 / 后缀：重设时只让用户选同类型的文件
        from app.core.util.local_layer_loader import layer_source_container

        try:
            container = str(layer_source_container(layer) or "")
        except Exception:  # noqa: BLE001
            container = str(layer.source() or "")
        if not container:
            container = str(layer.source() or "")
        old_ext = os.path.splitext(container.split("|", 1)[0])[1].lower()
        old_is_gdb = os.path.isdir(container) or old_ext == ".gdb"

        is_dir = False
        if is_raster:
            title = "选择新的栅格数据源（仅同类型栅格）"
            filter_ = _RASTER_FILE_FILTER
        elif old_is_gdb:
            is_dir = True
            title = "选择新的 GDB 数据库（仅 .gdb 文件夹）"
            filter_ = ""
        elif old_ext in _VECTOR_FILE_FILTERS:
            title = f"选择新的数据源（仅 {old_ext} 文件）"
            filter_ = _VECTOR_FILE_FILTERS[old_ext]
        else:
            title = "选择新的矢量数据源（仅矢量文件）"
            filter_ = _VECTOR_ANY_FILTER

        path = open_file_dir(
            self,
            is_dir=is_dir,
            title=title,
            filter_=filter_,
        )
        if not path:
            return

        if old_is_gdb and (
            not os.path.isdir(path) or not path.lower().endswith(".gdb")
        ):
            QMessageBox.warning(self, "重设数据源", "请选择 .gdb 文件夹。")
            return

        ext = os.path.splitext(path)[1].lower()
        # 单文件格式严格同后缀；容器（gpkg / sqlite / db）用子图层选择器再选具体层
        if (
            is_vector
            and not old_is_gdb
            and old_ext
            and old_ext not in _CONTAINER_VECTOR_EXTS
            and ext != old_ext
        ):
            QMessageBox.warning(
                self, "重设数据源", f"请选择同类型文件（{old_ext}）。"
            )
            return
        if is_raster:
            # 栅格不走 setDataSource：没有地理参考的 PNG / JPG 在 setDataSource 时
            # 会报 "Unable to compute a transformation..."，而和「添加图层」一样
            # 直接新建 QgsRasterLayer 是能正常打开的。用新图层替换旧图层对象，
            # 保留名称 / 样式 / 树中位置。
            from qgis.core import QgsRasterLayer

            new_layer = QgsRasterLayer(path, layer.name())
            if not new_layer.isValid():
                QMessageBox.warning(
                    self, "重设数据源", f"栅格数据源打不开：\n{path}"
                )
                return
            try:
                self._replace_layer_object(layer, new_layer)
            except Exception as exc:  # noqa: BLE001 —— 槽内异常不能冒出去
                logger.exception("重设栅格数据源失败")
                QMessageBox.warning(
                    self, "重设数据源", f"替换图层失败：{exc}"
                )
            return
        source = ""
        provider = ""
        #: 新数据源对应的图层对象：setDataSource 真的失败时用它整层替换
        #: （保留名称 / 样式 / 树中位置）。跨 provider（SpatiaLite ↔ OGR）在
        #: QGIS 4 里一般能直接换，换不了时这就是兜底。
        replacement = None
        if is_vector and (os.path.isdir(path) or ext in _CONTAINER_VECTOR_EXTS):
            from app.core.util.sublayers import open_sublayer
            from app.ui.dialogs.sublayer_pick_dialog import SublayerPickDialog

            dialog = SublayerPickDialog(self, path=path, title="选择新的数据源图层")
            accepted = dialog.exec()
            selected = dialog.selected_sublayers() if accepted else []
            dialog.deleteLater()
            if not selected:
                return
            new_layer, error = open_sublayer(path, selected[0])
            if new_layer is None:
                QMessageBox.warning(self, "重设数据源", f"打不开新数据源：\n{error}")
                return
            source = str(new_layer.source() or "")
            provider = str(new_layer.providerType() or "")
            replacement = new_layer
        elif is_raster or ext in _RASTER_EXTS:
            if not is_raster:
                QMessageBox.warning(
                    self, "重设数据源", "当前是矢量图层，不能改成栅格数据源。"
                )
                return
            probe = QgsRasterLayer(path, layer.name(), "gdal")
            if not probe.isValid():
                QMessageBox.warning(
                    self, "重设数据源", f"栅格数据源打不开：\n{path}"
                )
                return
            source = str(probe.source() or path)
            provider = str(probe.providerType() or "gdal")
            probe.deleteLater()
        else:
            if not is_vector:
                QMessageBox.warning(
                    self, "重设数据源", "当前是栅格图层，不能改成矢量数据源。"
                )
                return
            probe = QgsVectorLayer(path, layer.name(), "ogr")
            if not probe.isValid():
                QMessageBox.warning(
                    self, "重设数据源", f"矢量数据源打不开：\n{path}"
                )
                return
            source = str(probe.source() or path)
            provider = str(probe.providerType() or "ogr")
            replacement = probe

        # **setDataSource 的返回值不能当成败判据**：QGIS 4.x 里它是 void
        # （PyQt 拿到 None），但数据源已经换好了；照旧代码 `if not ok` 就会对
        # 所有格式都误报「设置数据源失败」（用户报的 sqlite / gpkg / gdb / shp 警告）。
        # 正确做法：调用后看图层是否有效、数据源是否真的变了。
        try:
            previous_source = str(layer.source() or "")
        except RuntimeError:
            previous_source = ""
        try:
            result = layer.setDataSource(source, layer.name(), provider)
        except Exception as exc:  # noqa: BLE001
            logger.error("重设图层数据源失败：%s", exc, exc_info=True)
            result = False
        if result is None:
            try:
                current_source = str(layer.source() or "")
                ok = bool(layer.isValid()) and (
                    current_source == source or current_source != previous_source
                )
            except RuntimeError:
                ok = False
        else:
            ok = bool(result)
        if not ok and replacement is not None and not sip.isdeleted(replacement):
            # 兜底：用刚打开的新图层整层替换（保留名称 / 样式 / 树中位置）。
            # provider 不同、或驱动拒绝了 setDataSource 时都能完成重设。
            try:
                self._replace_layer_object(layer, replacement)
                return
            except Exception as exc:  # noqa: BLE001 —— 槽内异常不能冒出去
                logger.exception("替换图层对象失败")
                QMessageBox.warning(
                    self, "重设数据源", f"设置数据源失败：\n{source}\n{exc}"
                )
                return
        if not ok:
            QMessageBox.warning(
                self, "重设数据源", f"设置数据源失败：\n{source}"
            )
            return
        # setDataSource 成功：把新数据源的字段别名 / 字典元数据抄回目标图层。
        # **只换 provider 不会带走这些** —— 实测重设后仍显示旧数据源的别名，
        # 新库里的 gpkg_data_columns / qgis_dev_column_alias 根本没生效。
        if replacement is not None and not sip.isdeleted(replacement):
            self._adopt_source_metadata(layer, replacement)
            replacement.deleteLater()
        with suppress(Exception):
            layer.dataProvider().reloadData()
        layer.triggerRepaint()
        with suppress(Exception):
            self.canvas.refreshAllLayers()
        with suppress(Exception):
            self.layer_dock.view.viewport().update()
        logger.info("已重设图层数据源：%s → %s", layer.name(), source)
        self.messageBar.pushMessage(
            f"已重设「{layer.name()}」的数据源",
            level=Qgis.MessageLevel.Success,
            duration=5,
        )

    def _adopt_source_metadata(self, layer, source_layer) -> None:
        """把新数据源的字段元数据抄回重设了数据源的图层。

        ``setDataSource`` 只替换 provider：字段别名、隐藏字段、字典关联这些
        **挂在图层/字段上的属性不会跟着换**，不抄的话重设后显示的还是旧数据源
        的别名与字典（用户报的「重设后别名没有更新」）。

        Args:
            layer: 被重设数据源的目标图层（字段已是新 provider 的）。
            source_layer: 从新数据源打开、已经套好别名 / 字典元数据的探测图层。
        """
        from app.core.util.field_meta import (
            DICT_DOMAIN_PROP,
            DICT_LOCAL_TABLE_PROP,
            DICT_TABLE_PROP,
        )

        prefixes = (DICT_TABLE_PROP, DICT_DOMAIN_PROP, DICT_LOCAL_TABLE_PROP)
        try:
            # QgsObjectCustomProperties 不是 dict（不能直接迭代），要走 keys()/value()
            source_props_obj = source_layer.customProperties()
            source_props = {
                key: source_props_obj.value(key) for key in source_props_obj.keys()
            }
            # 先清掉旧数据源留下的字典关联，再按新数据源补齐
            target_props = layer.customProperties()
            for key in list(target_props.keys()):
                if key.startswith(prefixes):
                    layer.removeCustomProperty(key)
            for key, value in source_props.items():
                if key.startswith(prefixes):
                    layer.setCustomProperty(key, value)

            source_fields = source_layer.fields()
            source_index = {
                source_fields.at(i).name().casefold(): i
                for i in range(source_fields.size())
            }
            fields = layer.fields()
            for index in range(fields.size()):
                name = fields.at(index).name()
                src_index = source_index.get(name.casefold())
                if src_index is None:
                    # 新数据源里没有同名字段：别名清空，别留旧数据源的
                    if fields.at(index).alias():
                        layer.setFieldAlias(index, "")
                    continue
                src_field = source_fields.at(src_index)
                alias = str(src_field.alias() or "")
                if fields.at(index).alias() != alias:
                    layer.setFieldAlias(index, alias)
                # 隐藏字段也跟着新数据源走（probe 里非隐藏的就恢复默认控件）
                setup = src_field.editorWidgetSetup()
                if setup.type() == "Hidden":
                    layer.setEditorWidgetSetup(index, setup)
                elif fields.at(index).editorWidgetSetup().type() == "Hidden":
                    from qgis.core import QgsEditorWidgetSetup

                    layer.setEditorWidgetSetup(index, QgsEditorWidgetSetup("", {}))
        except Exception:  # noqa: BLE001 —— 元数据抄不过来不影响数据源本身可用
            logger.warning("重设数据源后同步字段元数据失败", exc_info=True)

    def _replace_layer_object(self, old_layer, new_layer) -> None:
        """用新图层对象替换旧图层：保留名称、样式、可见性与图层树位置。

        栅格（尤其没有地理参考的 PNG / JPG）在 ``setDataSource`` 时会被 GDAL
        的坐标变换检查拦下；直接换成新建的 ``QgsRasterLayer`` 才是和「添加图层」
        一致的打开方式。
        """
        from qgis.core import QgsMapLayerStyle, QgsProject

        project = QgsProject.instance()
        root = project.layerTreeRoot()
        node = root.findLayer(old_layer.id())
        parent = node.parent() if node is not None else root
        index = parent.children().index(node) if node is not None else 0
        name = old_layer.name()
        # 可见性是**图层树节点**上的属性，QgsMapLayer 本身没有 isVisible()
        visible = True
        if node is not None:
            with suppress(Exception):
                visible = bool(node.itemVisibilityChecked())
        old_crs = None
        try:
            old_crs = old_layer.crs()
        except RuntimeError:
            old_crs = None
        style_xml = ""
        try:
            manager = old_layer.styleManager()
            current = manager.currentStyle() if manager is not None else ""
            if current:
                style_xml = manager.style(current).xmlData()
        except Exception:  # noqa: BLE001 —— 样式取不到不影响重设
            logger.debug("重设数据源：取旧图层样式失败", exc_info=True)
        custom = {}
        try:
            props = old_layer.customProperties()
            custom = {key: props.value(key) for key in props.keys()}
        except Exception:  # noqa: BLE001
            custom = {}
        project.removeMapLayer(old_layer.id())
        new_layer.setName(name)
        try:
            if (
                old_crs is not None
                and old_crs.isValid()
                and not new_layer.crs().isValid()
            ):
                new_layer.setCrs(old_crs)
        except Exception:  # noqa: BLE001
            pass
        if custom:
            from app.core.util.field_meta import (
                DICT_DOMAIN_PROP,
                DICT_LOCAL_TABLE_PROP,
                DICT_TABLE_PROP,
            )

            # 字典 / 字段元数据以**新数据源**为准（probe 上已经套好），别被旧图层覆盖；
            # 其它自定义属性（path_c、用户属性…）才从旧图层带过来
            prefixes = (DICT_TABLE_PROP, DICT_DOMAIN_PROP, DICT_LOCAL_TABLE_PROP)
            for key, value in custom.items():
                if key.startswith(prefixes):
                    continue
                with suppress(Exception):
                    new_layer.setCustomProperty(key, value)
        if style_xml:
            with suppress(Exception):
                manager = new_layer.styleManager()
                manager.addStyle("repaired", QgsMapLayerStyle(style_xml))
                manager.setCurrentStyle("repaired")
        project.addMapLayer(new_layer, False)
        tree_node = parent.insertLayer(index, new_layer)
        with suppress(Exception):
            tree_node.setItemVisibilityChecked(visible)
        # 旧图层对象已销毁：主窗口里指向它的引用要切到新对象
        with suppress(Exception):
            if getattr(self, "_selected_layer", None) is old_layer:
                self._selected_layer = new_layer
        with suppress(Exception):
            if getattr(self, "edit_layer", None) is old_layer:
                self.edit_layer = new_layer
        with suppress(Exception):
            tree_node.setSelected(True)
        with suppress(Exception):
            self.canvas.refreshAllLayers()
        with suppress(Exception):
            self.layer_dock.view.viewport().update()
        logger.info("已重设图层数据源：%s → %s", name, new_layer.source())
        self.messageBar.pushMessage(
            f"已重设「{name}」的数据源",
            level=Qgis.MessageLevel.Success,
            duration=5,
        )

    def load_file(self, file_path: str) -> None:
        """加载到项目：矢量（shp/gpkg/geojson/…）、栅格（tif/img/…）、或 GDB 目录。

        **容器型矢量数据源**（``.gdb`` 目录 / ``.gpkg`` / ``.sqlite``）会先弹出里面的
        图层列表，让用户挑一个或多个再加载 —— 以前 gpkg/sqlite 只开第一层、GDB 整库全加，
        都不是用户要的。
        """

        ext = os.path.splitext(file_path)[1].lower()
        if ext in _LARGE_TEXT_VECTOR_EXTS and not self._confirm_large_text_vector(file_path):
            return
        self._begin_layer_loading(f"正在加载图层：{os.path.basename(file_path)}")
        try:
            if os.path.isdir(file_path):
                # 目录 → 按 GDB 处理（.gdb 是数据集目录）
                self._load_gdb(file_path)
            elif ext in _RASTER_EXTS:
                self._load_raster(file_path)
            elif ext in _CONTAINER_VECTOR_EXTS:
                self._load_container(file_path)
            else:
                # 其余按矢量；扩展名不认识时就"矢量试一次、栅格试一次"
                if self._load_vector(file_path) is None and ext not in _VECTOR_HINT_EXTS:
                    self._load_raster(file_path)
            self.canvas.refreshAllLayers()
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(self, "加载错误", f"文件加载失败：{e}")
        finally:
            self._end_layer_loading()

    def _load_container(self, path: str, *, title: str = "选择要加载的图层") -> None:
        """容器数据源：**先列图层、再加载选中的那些**（可多选）。

        打开按 :func:`~app.core.util.sublayers.open_sublayer`（本地库沿用统一打开策略，
        ``.sqlite`` 不会凭空多出 ``rowid`` 列）。
        """
        from app.core.util.sublayers import open_sublayer
        from app.ui.dialogs.sublayer_pick_dialog import SublayerPickDialog

        dialog = SublayerPickDialog(self, path=path, title=title)
        accepted = dialog.exec()
        selected = dialog.selected_sublayers() if accepted else []
        dialog.deleteLater()
        if not selected:
            return
        added = 0
        failures: list[str] = []
        for sublayer in selected:
            layer, error = open_sublayer(path, sublayer)
            if layer is None:
                logger.warning("容器图层打不开，跳过：%s（%s）", sublayer.name, error)
                failures.append(f"{sublayer.name}：{error}")
                continue
            layer.path_c = path
            self._add_layer_to_project(layer)
            added += 1
        if added:
            logger.info("容器加载完成：%s（%d 个图层）", path, added)
        if failures:
            QMessageBox.warning(
                self,
                "部分图层没加载",
                "以下图层打不开：\n" + "\n".join(failures),
            )

    # ------------------------------------------------------- 各类型加载

    def _confirm_large_text_vector(self, file_path: str) -> bool:
        """大 GeoJSON / KML / GML 加载前提示改用 GeoPackage；取消返回 ``False``。"""
        try:
            size = os.path.getsize(file_path)
        except OSError:
            return True
        if size <= _LARGE_TEXT_VECTOR_BYTES:
            return True
        ext = os.path.splitext(file_path)[1].lower()
        format_name = {".geojson": "GeoJSON", ".json": "GeoJSON", ".kml": "KML", ".gml": "GML"}.get(
            ext, "文本矢量"
        )
        answer = QMessageBox.question(
            self,
            f"{format_name} 文件较大",
            f"{os.path.basename(file_path)} 约 {size / (1024 * 1024):.1f} MB。\n\n"
            f"{format_name} 是文本交换格式，QGIS 加载、修改字段和属性表定位都会很慢，"
            "部分驱动还会因整文件解析而内存不足。\n"
            "建议先用「矢量格式转换」转为 GeoPackage 再加载 / 编辑。\n\n仍要加载吗？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return answer == QMessageBox.StandardButton.Yes

    def _load_vector(self, file_path: str):
        """按矢量加载，成功返回图层，失败返回 ``None``。

        **不自动缩放地图**：加完就跳视图会把用户正在看的地方甩掉（多个图层时
        更是随便挑一个的范围就跳）。要定位自己右键 → 「缩放至图层」。
        """

        from qgis.core import QgsVectorLayer

        name = os.path.basename(file_path)
        layer = QgsVectorLayer(file_path, name, "ogr")
        if not layer.isValid():
            return None
        layer.path_c = file_path
        self._add_layer_to_project(layer)
        return layer

    def _load_raster(self, file_path: str):
        """按栅格加载（GDAL provider），成功返回图层，失败返回 ``None``（同样不缩放）。"""

        from qgis.core import QgsRasterLayer

        name = os.path.basename(file_path)
        # 不写死 provider：QgsRasterLayer 默认按 GDAL 打开，多波段 tif / img / dem 都走它
        layer = QgsRasterLayer(file_path, name)
        if not layer.isValid():
            QMessageBox.warning(self, "加载失败", f"无法识别为栅格数据：{name}")
            return None
        layer.path_c = file_path
        self._add_layer_to_project(layer)
        return layer

    def _load_gdb(self, gdb_path: str) -> None:
        """File Geodatabase：先诊断驱动问题，再让用户挑图层（可多选）加载。"""

        from osgeo import gdal, ogr

        ogr.RegisterAll()
        # 用 gdal 的错误栈拿到**具体原因**：GDB 打不开最常见的原因是这种库只有
        # ESRI 的 FileGDB 驱动能读（GDAL 自带的只读 OpenFileGDB 读不了），
        # 报原文比一句"无法打开 GDB"有用得多
        gdal.ErrorReset()
        ds = ogr.Open(gdb_path)
        error = gdal.GetLastErrorMsg() or ""
        if ds is None:
            name = os.path.basename(gdb_path.rstrip("/\\"))
            hint = ""
            if "FileGDB" in error:
                hint = (
                    "\n\n这个库需要 ESRI 的 FileGDB 驱动，当前 GDAL 里没有它"
                    "（只装了只读的 OpenFileGDB，打不开这类库）。\n"
                    "处理办法：用 ArcGIS 把该库导出成 GeoPackage / Shapefile，"
                    "或给 GDAL 装上 gdal-filegdb 插件后再试。"
                )
            elif not error:
                hint = "\n\n（该目录不是有效的 File Geodatabase：里面没有 .gdbtable 文件？）"
            QMessageBox.warning(self, "无法打开 GDB", f"{name}\n{error}{hint}")
            return
        ds.Destroy()
        # 能打开：列图层让用户挑（不再整库全加载）
        self._load_container(gdb_path, title="选择 GDB 里要加载的图层")

    # ===================================================== 浏览器事件

    def _on_browser_double_clicked(self, index) -> None:
        from qgis.core import QgsLayerItem, QgsRasterLayer, QgsVectorLayer

        if not index.isValid():
            return
        item = self.browser_dock.model.dataItem(index)
        if not item or not isinstance(item, QgsLayerItem):
            return
        uri, name, provider = item.uri(), item.name(), item.providerKey()
        self._begin_layer_loading(f"正在加载图层：{name}")
        try:
            vlayer = QgsVectorLayer(uri, name, provider)
            if vlayer.isValid():
                # path_c 存**容器路径**（供"执行SQL"按文件打开）：item.path()
                # 对 gdb/gpkg 子图层是 ``容器|layername=层名`` 的 QGIS URI，
                # ogr.Open 不认，剥掉 | 及之后所有 provider 选项
                vlayer.path_c = uri.split("|", 1)[0]
                self._add_layer_to_project(vlayer)
                return
            rlayer = QgsRasterLayer(uri, name, provider)
            if rlayer.isValid():
                rlayer.path_c = uri.split("|", 1)[0]
                self._add_layer_to_project(rlayer)
        finally:
            self._end_layer_loading()

    def _on_browser_context_menu(self, pos) -> None:
        from qgis.PyQt.QtWidgets import QMenu

        index = self.browser_dock.view.indexAt(pos)
        if not index.isValid():
            return
        item = self.browser_dock.model.dataItem(index)
        if not item:
            return
        menu = QMenu(self)
        # QGIS 3.40 起 QgsDataItemGuiContext 持有的是浏览器 view + 消息栏
        # （没有 setProviderRegistry，调它会 AttributeError → Qt 槽内未捕获 → 崩溃）。
        # 包一层防御：任何异常只回退到"刷新"，不让菜单构建失败拖垮应用。
        try:
            from qgis.gui import QgsDataItemGuiContext, QgsGui

            registry = QgsGui.instance().dataItemGuiProviderRegistry()
            context = QgsDataItemGuiContext()
            context.setView(self.browser_dock.view)
            context.setMessageBar(self.messageBar)
            for provider in registry.providers():
                try:
                    provider.populateContextMenu(item, menu, selectedItems=[item], context=context)
                except Exception:  # noqa: BLE001
                    continue
        except Exception:  # noqa: BLE001

            get_logger(__name__).exception("构建数据浏览器右键菜单失败，仅保留「刷新」")
        # 目录项：提供"新建"子菜单（各类矢量格式）
        try:
            from qgis.core import QgsDataCollectionItem, QgsDirectoryItem

            # QGIS 4 浏览器里普通文件夹是 QgsDirectoryItem，而 .gdb 等 OGR 数据集
            # 目录是 QgsDataCollectionItem（无 dirPath），统一取磁盘路径判定
            dir_path = getattr(item, "dirPath", None) or item.path()
            is_dir = isinstance(item, QgsDirectoryItem) or (
                isinstance(item, QgsDataCollectionItem) and os.path.isdir(dir_path)
            )
            if is_dir:
                is_gdb = str(dir_path).lower().endswith(".gdb")
                # .gdb 是数据集目录：只在库里新增图层，不在目录里塞零散文件
                if is_gdb:
                    act = menu.addAction("新增图层")
                    act.triggered.connect(lambda checked=False: self._on_browser_add_gdb_layer(item))
                else:
                    new_menu = menu.addMenu("新建")
                    for name, ext, driver in _NEW_VECTOR_FORMATS:
                        act = new_menu.addAction(name)
                        act.triggered.connect(
                            lambda checked=False, nm=name, ex=ext, dr=driver: self._on_browser_create_vector(
                                item, nm, ex, dr
                            )
                        )
                menu.addSeparator()
        except Exception:  # noqa: BLE001
            pass
        refresh = menu.addAction("刷新")
        refresh.triggered.connect(lambda: item.refresh())
        menu.exec(self.browser_dock.view.viewport().mapToGlobal(pos))

    def _on_browser_create_vector(self, item, name: str, ext: str, driver: str) -> None:
        """在浏览器目录项下新建空矢量数据文件（"新建"子菜单入口）。"""
        from qgis.core import (
            QgsCoordinateReferenceSystem,
            QgsCoordinateTransformContext,
            QgsVectorFileWriter,
            QgsVectorLayer,
        )
        from qgis.PyQt.QtWidgets import QFileDialog

        dir_path = getattr(item, "dirPath", None) or item.path()
        path, _ = QFileDialog.getSaveFileName(
            self,
            f"新建 {name}",
            os.path.join(dir_path, f"新建矢量.{ext}"),
            f"{name} (*.{ext})",
        )
        if not path:
            return
        crs = self.project.crs()
        if not crs.isValid():
            crs = QgsCoordinateReferenceSystem("EPSG:4490")
        layer = QgsVectorLayer(f"Point?crs={crs.authid()}", "new_layer", "memory")
        opts = QgsVectorFileWriter.SaveVectorOptions()
        opts.driverName = driver
        error, errmsg, _newfile, _saved = QgsVectorFileWriter.writeAsVectorFormatV3(
            layer, path, QgsCoordinateTransformContext(), opts
        )
        if error != QgsVectorFileWriter.WriterError.NoError:
            self.messageBar.pushCritical("新建图层", f"创建失败：{errmsg}")
            return
        item.refresh()
        self.messageBar.pushSuccess("新建图层", f"已创建 {os.path.basename(path)}")
        logger.info("新建矢量文件：%s（驱动=%s）", path, driver)

    def _on_browser_add_gdb_layer(self, item) -> None:
        """向已存在的 File Geodatabase（.gdb）中新增一个空图层。"""
        from qgis.core import (
            QgsCoordinateReferenceSystem,
            QgsCoordinateTransformContext,
            QgsVectorFileWriter,
            QgsVectorLayer,
        )
        from qgis.PyQt.QtWidgets import QInputDialog

        gdb = getattr(item, "dirPath", None) or item.path()
        layer_name, ok = QInputDialog.getText(self, "新增图层", "图层名称：")
        if not ok or not layer_name.strip():
            return
        layer_name = layer_name.strip()
        geom_names = ("点", "线", "面")
        geom_defs = {"点": "Point", "线": "LineString", "面": "Polygon"}
        geom_text, ok = QInputDialog.getItem(self, "新增图层", "几何类型：", geom_names, 0, False)
        if not ok:
            return
        crs = self.project.crs()
        if not crs.isValid():
            crs = QgsCoordinateReferenceSystem("EPSG:4490")
        layer = QgsVectorLayer(f"{geom_defs[geom_text]}?crs={crs.authid()}", layer_name, "memory")
        opts = QgsVectorFileWriter.SaveVectorOptions()
        opts.driverName = "OpenFileGDB"
        opts.layerName = layer_name
        error, errmsg, _newfile, _saved = QgsVectorFileWriter.writeAsVectorFormatV3(
            layer, gdb, QgsCoordinateTransformContext(), opts
        )
        if error != QgsVectorFileWriter.WriterError.NoError:
            self.messageBar.pushCritical("新增图层", f"创建失败：{errmsg}")
            return
        item.refresh()
        self.messageBar.pushSuccess("新增图层", f"已创建 {layer_name}")
        logger.info("GDB 新增图层：%s → %s", layer_name, gdb)
