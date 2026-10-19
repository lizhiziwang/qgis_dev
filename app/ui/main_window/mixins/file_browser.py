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

#: 「添加数据文件」认的栅格扩展名（按扩展名走 GDAL 栅格 provider）
_RASTER_EXTS = frozenset({
    ".tif", ".tiff", ".img", ".jpg", ".jpeg", ".png", ".bmp", ".gif",
    ".dem", ".asc", ".vrt", ".jp2", ".ecw", ".hdf", ".nc", ".mrf",
})

#: 已知的矢量扩展名。**不在这里、也不在栅格表里的**扩展名（比如 .txt/.dat）：
#: 先试矢量、再试栅格，都不成才报错
_VECTOR_HINT_EXTS = frozenset({
    ".shp", ".geojson", ".json", ".gpkg", ".gml", ".kml", ".sqlite",
    ".csv", ".tab", ".mif", ".fgb", ".gpx", ".dxf",
})

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

    def load_file(self, file_path: str) -> None:
        """加载到项目：矢量（shp/gpkg/geojson/…）、栅格（tif/img/…）、或 GDB 目录。"""

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
            else:
                # 其余按矢量；扩展名不认识时就"矢量试一次、栅格试一次"
                if self._load_vector(file_path) is None and ext not in _VECTOR_HINT_EXTS:
                    self._load_raster(file_path)
            self.canvas.refreshAllLayers()
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(self, "加载错误", f"文件加载失败：{e}")
        finally:
            self._end_layer_loading()

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
        """加载 File Geodatabase 目录里的全部图层。"""

        from osgeo import gdal, ogr
        from qgis.core import QgsVectorLayer

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

        added = 0
        try:
            for i in range(ds.GetLayerCount()):
                fc = ds.GetLayerByIndex(i)
                name = fc.GetName()
                layer = QgsVectorLayer(f"{gdb_path}|layername={name}", name, "ogr")
                if not layer.isValid():
                    logger.warning("GDB 里的图层打不开，跳过：%s", name)
                    continue
                layer.path_c = gdb_path
                self._add_layer_to_project(layer)
                added += 1
            if added == 0:
                QMessageBox.warning(
                    self, "无法打开 GDB",
                    f"{os.path.basename(gdb_path)} 里没有可加载的图层。",
                )
        finally:
            ds.Destroy()
        logger.info("GDB 加载完成：%s（%d 个图层）", gdb_path, added)

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
