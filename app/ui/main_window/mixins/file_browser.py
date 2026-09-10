"""打开文件与数据浏览器。

本地文件打开、按类别装载图层，以及数据浏览器目录树的双击/右键菜单
（新建矢量图层、添加 GDB 图层）。

由 :class:`~app.ui.main_window.main_window.MainWindow` 多继承组合，方法只依赖
``self`` 上的共享状态。
"""

from __future__ import annotations

import os

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


class FileBrowserMixin:
    """FileBrowserMixin：见模块 docstring。"""

    def _on_open_file(self) -> None:

        path = open_file_dir(
            self,
            is_dir=False,
            title="选择矢量文件",
            filter_="矢量文件 (*.shp *.geojson *.gpkg *.gdb);;所有文件 (*)",
        )
        if path:
            self.load_file(path)

    def load_file(self, file_path: str) -> None:
        """加载文件到项目（支持 shp / gdb / geojson / gpkg）。"""
        from osgeo import ogr
        from qgis.core import QgsVectorLayer

        ext = os.path.splitext(file_path)[1].lower()
        self._begin_layer_loading(f"正在加载图层：{os.path.basename(file_path)}")
        try:
            if ext == ".gdb":
                ogr.RegisterAll()
                ds = ogr.Open(file_path)
                if not ds:
                    QMessageBox.warning(self, "失败", f"无法打开 GDB: {file_path}")
                    return
                for i in range(ds.GetLayerCount()):
                    fc = ds.GetLayerByIndex(i)
                    name = fc.GetName()
                    source = f"{file_path}|layername={name}"
                    layer = QgsVectorLayer(source, name, "ogr")
                    layer.path_c = file_path
                    if layer.isValid():
                        self._add_layer_to_project(layer)
                        self.canvas.setExtent(layer.extent())
                ds.Destroy()
            else:
                name = os.path.basename(file_path)
                layer = QgsVectorLayer(file_path, name, "ogr")
                layer.path_c = file_path
                self._add_layer_to_project(layer)
                self.canvas.setExtent(layer.extent())
            self.canvas.refreshAllLayers()
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(self, "加载错误", f"文件加载失败：{e}")
        finally:
            self._end_layer_loading()

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
