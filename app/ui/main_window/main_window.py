"""主窗口。

旧 :class:`main.QGISMapWindow` 700+ 行，拆成 6 个子组件 + 业务回调：

- :class:`MenuBar`     - 菜单
- :class:`ToolBar`     - 工具条
- :class:`StatusBar`   - 状态栏
- :class:`LayerDock`   - 图层树
- :class:`BrowserDock` - 数据浏览器
- :class:`LogDock`     - 日志面板

业务逻辑（打开文件、空间查询、质检等）通过 callback 注入；窗口本身只负责
"装配 + 事件分发"。
"""

from __future__ import annotations

import os
import sys
from typing import Optional

from PyQt5.QtCore import Qt, QPoint
from PyQt5.QtGui import QCloseEvent, QIcon
from PyQt5.QtWidgets import (
    QAction,
    QListWidget,
    QMainWindow,
    QMenu,
    QMessageBox,
    QSizePolicy,
    QTreeWidget,
    QVBoxLayout,
    QWidget,
)

from qgis.core import (
    QgsApplication,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsLayerTreeGroup,
    QgsMapLayer,
    QgsProject,
    QgsRectangle,
    QgsVectorLayer,
)
from qgis.core import QgsStyle
from qgis.gui import (
    QgsLayerTreeMapCanvasBridge,
    QgsMapCanvas,
    QgsMapToolPan,
    QgsMapToolZoom,
    QgsMessageBar,
    QgsRendererPropertiesDialog,
    QgsVectorLayerProperties,
    QgsVectorLayerSaveAsDialog,
)

from app.core.domain.editing import LayerEditService
from app.core.domain.query import QueryType, SpaceQueryService
from app.infra.fs.paths import Paths
from app.ui.common.canvas_zoom import CanvasZoomTool
from app.ui.common.icons import Icons
from app.ui.docks.attribute_dock import AttributeDock
from app.ui.docks.feature_info_dock import FeatureInfoDock
from app.ui.docks.log_dock import LogDock
from app.ui.docks.tools_dock import ToolsDock
from app.ui.main_window.browser_dock import BrowserDock
from app.ui.docks.project_dock import ProjectDock
from app.ui.main_window.layer_dock import LayerDock
from app.ui.main_window.menu_bar import MenuBar
from app.ui.main_window.status_bar import StatusBar
from app.ui.main_window.tool_bar import ToolBar
from app.ui.main_window.draw_toolbar import DigitizingToolbar
from app.ui.main_window.qgis_digitizing_toolbar import QgisDigitizingToolbar

# 让组件行为兼容旧 `component.SpaceQuery` 等可选的 legacy 模块
# （这些模块在阶段 4 后期会从 legacy 重新启用，或被新 service 替换）


class MainWindow(QMainWindow):
    """主窗口。"""

    def __init__(self, project_root: Optional[str] = None) -> None:
        super().__init__()
        self._paths = Paths.default()
        if project_root:
            self._paths = Paths(
                project_root=project_root,
                resources=self._paths.resources,
                logs=self._paths.logs,
                tmp=self._paths.tmp,
                config=self._paths.config,
            )
        # 把 project_root 加进 sys.path（兼容旧 component 旧 import）
        if str(self._paths.project_root) not in sys.path:
            sys.path.insert(0, str(self._paths.project_root))

        self.setWindowTitle("qgis_dev")
        self.setGeometry(100, 100, 1200, 800)
        self.setWindowIcon(Icons.app())

        # ---- 项目 + 图层树桥（先建好，_build_central_widget 要用 self.project）----
        self.project = QgsProject.instance()
        self.tree_root = self.project.layerTreeRoot()

        # ---- 地图画布 ----
        self.canvas = self._build_canvas()
        self.bridge = QgsLayerTreeMapCanvasBridge(self.tree_root, self.canvas)
        self.bridge.setAutoSetupOnFirstLayer(True)
        self._build_central_widget()

        # ---- 画布左上角缩放工具（覆盖层）----
        self.zoom_tool = CanvasZoomTool(self.canvas)

        # ---- service 注入 ----
        self.editing_service = LayerEditService()
        self.space_query_service = SpaceQueryService()
        self.draw_manager = None
        self.edit_layer: Optional[QgsVectorLayer] = None

        # ---- dock ----
        self._build_docks()

        # ---- 工具 / 菜单 / 状态栏 ----
        self._build_tool_bar()
        self._build_menu_bar()
        self._build_status_bar()

        # ---- 默认激活平移 ----
        self.canvas.setMapTool(self._pan_tool)

    # ===================================================== 装配

    def _build_canvas(self) -> QgsMapCanvas:
        canvas = QgsMapCanvas(self)
        canvas.setAcceptDrops(True)
        canvas.setCanvasColor(Qt.white)
        canvas.enableAntiAliasing(True)
        return canvas

    def _build_central_widget(self) -> None:
        central = QWidget(self)
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)

        # 消息栏
        self.messageBar = QgsMessageBar()
        self.messageBar.setSizePolicy(QSizePolicy.Minimum, QSizePolicy.Fixed)
        # 把 messageBar 放到全局属性（兼容旧 sendMessageToTopParent）
        qgis_app = QgsApplication.instance()
        if qgis_app is not None:
            qgis_app.setProperty("messageBar", self.messageBar)
        layout.addWidget(self.messageBar)

        layout.addWidget(self.canvas)

        crs = QgsCoordinateReferenceSystem("EPSG:4490")
        self.project.setCrs(crs)
        self.canvas.setDestinationCrs(crs)
        self.canvas.setProject(self.project)

    def _build_docks(self) -> None:
        # 1. 图层树
        self.layer_dock = LayerDock(self)
        self.layer_dock.context_menu_requested.connect(self._on_layer_context_menu)
        # 点击图层树即自动设为编辑目标（与 QGIS 桌面一致）：
        # 用户期望点完一层就能在工具条点"开始编辑"
        self.layer_dock.layer_clicked.connect(self._on_layer_clicked_in_dock)
        self.addDockWidget(Qt.LeftDockWidgetArea, self.layer_dock)

        # 2. 数据浏览器
        self.browser_dock = BrowserDock(self)
        self.browser_dock.double_clicked.connect(self._on_browser_double_clicked)
        self.browser_dock.context_menu_requested.connect(self._on_browser_context_menu)
        self.addDockWidget(Qt.LeftDockWidgetArea, self.browser_dock)

        # 3. 日志（默认隐藏，由菜单唤起）
        self.log_dock = None  # 懒加载

    def _build_tool_bar(self) -> None:
        callbacks = {
            "pan": self._on_pan,
            "identify": self._on_identify,
            "zoom_in": self._on_zoom_in,
            "zoom_out": self._on_zoom_out,
            "quality": self._on_quality,
            "analyse": self._on_analyse,
            "zoom_full": self._on_zoom_full,
            "refresh": self._on_refresh,
            "test": self._on_test,
        }
        self.tool_bar_builder = ToolBar(self)
        self.tool_bar = self.tool_bar_builder.build(callbacks)

        from app.infra.logging import get_logger
        _log = get_logger(__name__)
        _log.info("[probe] before DigitizingToolbar")
        # 追加绘图 / 编辑工具条（2 个下拉按钮，与旧 component.draw_tool.Draw 一致）
        self.digitizing_toolbar = DigitizingToolbar(
            self,
            self.canvas,
            editing_layer_getter=lambda: self.edit_layer,
        )
        _log.info("[probe] DigitizingToolbar OK")

        # DigitizingToolbar 编辑态切换 → 同步给 QGIS 复用版工具条
        self.digitizing_toolbar.editing_state_changed.connect(self._on_editing_state_changed)

        _log.info("[probe] before QgisDigitizingToolbar")
        # === 临时注释掉（验证 QgisDigitizingToolbar 是否为崩点）===
        # self.qgis_digitizing_toolbar = QgisDigitizingToolbar(
        #     self,
        #     self.canvas,
        #     editing_layer_getter=lambda: self.edit_layer,
        #     on_finished=self._on_qgis_draw_finished,
        # )
        self.qgis_digitizing_toolbar = None  # 占位，避免后续引用炸
        _log.info("[probe] QgisDigitizingToolbar SKIPPED")

        # 创建平移工具（保留引用避免被 GC）
        self._pan_tool = QgsMapToolPan(self.canvas)
        self._zoom_in_tool = QgsMapToolZoom(self.canvas, False)
        self._zoom_out_tool = QgsMapToolZoom(self.canvas, True)

    def _build_digitizing_toolbar(self) -> None:
        """兼容旧接口（占位）：由 :class:`DigitizingToolbar` 直接接管。"""
        return

    def _build_menu_bar(self) -> None:
        callbacks = {
            "open_file": self._on_open_file,
            "open_project": self._on_open_project,
            "new_project": self._on_new_project,
            "show_log": self._show_log_dock,
        }
        self.menu_bar_builder = MenuBar(self)
        self.menu_bar_builder.build(callbacks)

    def _build_status_bar(self) -> None:
        self.status_bar_builder = StatusBar(self)
        self.status_bar_builder.build()

    # ===================================================== 工具回调

    def _on_pan(self) -> None:
        self.canvas.setMapTool(self._pan_tool)

    def _on_identify(self) -> None:
        from app.ui.tools.find_feature import FindFeatureTool
        self._find_feature_tool = FindFeatureTool(self.canvas, parent=self)
        self.canvas.setMapTool(self._find_feature_tool)

    def show_feature_info(self, result, highlight=None) -> None:
        """点击识别后把要素详情显示到右侧 Dock（懒创建，内容就地刷新）。"""
        if not hasattr(self, "_feature_info_dock") or self._feature_info_dock is None:
            self._feature_info_dock = FeatureInfoDock(self)
            self.addDockWidget(Qt.RightDockWidgetArea, self._feature_info_dock)
        self._feature_info_dock.show_feature(result, highlight=highlight)

    def _on_zoom_in(self) -> None:
        self.canvas.setMapTool(self._zoom_in_tool)

    def _on_zoom_out(self) -> None:
        self.canvas.setMapTool(self._zoom_out_tool)

    def _on_quality(self) -> None:
        from app.ui.dialogs.check_viewer import CheckViewerDialog
        dlg = CheckViewerDialog(self, "")
        dlg.show()

    def _on_analyse(self) -> None:
        dock = ToolsDock(self)
        self.addDockWidget(Qt.RightDockWidgetArea, dock)
        dock.show()

    def _on_zoom_full(self) -> None:
        layers = [l for l in self.project.mapLayers().values() if l.isValid()]
        if not layers:
            QMessageBox.information(self, "提示", "当前无有效图层")
            return
        combined = layers[0].extent()
        for l in layers[1:]:
            combined.combineExtentWith(l.extent())
        self.canvas.setExtent(combined)
        self.canvas.refresh()

    def _on_refresh(self) -> None:
        self.canvas.refresh()

    def _on_test(self) -> None:
        """测试按钮：启动独立 FastAPI 服务（在新进程中）。"""
        try:
            from app.server.cli import main as server_main
            import threading
            threading.Thread(target=server_main, daemon=True).start()
            self.messageBar.pushMessage("已启动服务（8000）", level=0, duration=3)
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "失败", str(e))

    def _on_new_project(self) -> None:
        """新建工程：先弹窗输入工程名，创建后把名字带到右侧工程 Dock 显示。"""
        # 已有图层时先确认清空（避免输入完名字又取消）
        if self.project.mapLayers():
            ret = QMessageBox.question(
                self,
                "新建工程",
                "将清空当前所有图层并新建工程，是否继续？",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if ret != QMessageBox.Yes:
                return
        name = self._ask_project_name()
        if name is None:
            return
        # 确保 Dock 存在
        if getattr(self, "project_dock", None) is None:
            self.project_dock = ProjectDock(self)
            self.project_dock.render_requested.connect(self._show_style_editor)
            self.project_dock.label_requested.connect(self._show_label_settings)
            self.addDockWidget(Qt.RightDockWidgetArea, self.project_dock)
        self._on_project_create(name)
        self.project_dock.show()
        self.project_dock.raise_()

    def _ask_project_name(self):
        """弹窗输入工程名；返回工程名，取消或为空则返回 ``None``。"""
        from PyQt5.QtWidgets import QInputDialog

        while True:
            name, ok = QInputDialog.getText(self, "新建工程", "请输入工程名：")
            if not ok:
                return None
            name = name.strip()
            if name:
                return name
            QMessageBox.warning(self, "提示", "工程名不能为空")

    def _on_project_create(self, name: str) -> None:
        """真正创建工程：清空图层、建两个分组、设工程名（Dock 展示名字）。"""
        # 退出编辑态，避免遗留脏 buffer
        if self.edit_layer is not None:
            if self.edit_layer.isEditable():
                self.editing_service.stop(self.edit_layer, save=False)
            self.edit_layer = None
            if hasattr(self, "digitizing_toolbar"):
                self.digitizing_toolbar.set_edit_target(None)
        self.project.clear()

        # 重建分组：数据图层 + 参考图层
        root = self.project.layerTreeRoot()
        self._data_group = root.addGroup("数据图层")
        self._ref_group = root.addGroup("参考图层")

        # 工程名 + 坐标系（project.clear() 会把 CRS 重置回默认）
        self.project.setTitle(name)
        self.setWindowTitle(f"qgis_dev - {name}")
        self.project.setCrs(QgsCoordinateReferenceSystem("EPSG:4490"))

        # 底图进参考分组
        self._load_config_base_tile_into_ref_group()

        self.project_dock.set_project(name, self._data_group, self._ref_group)
        self.canvas.refreshAllLayers()
        self.messageBar.pushMessage(f"已创建工程：{name}", level=0, duration=3)

    def _add_layer_to_project(self, layer) -> None:
        """把图层加进项目：默认路由到"数据图层"分组（未创建工程时平铺挂根）。"""
        self.project.addMapLayer(layer, False)
        group = getattr(self, "_data_group", None) or self.project.layerTreeRoot()
        group.addLayer(layer)

    def _load_config_base_tile_into_ref_group(self) -> None:
        """加载配置底图并移入"参考图层"分组（底图已平铺挂根，需移动）。"""
        layer = self._load_config_base_tile()
        if layer is not None and getattr(self, "_ref_group", None) is not None:
            self._move_layer_to_group(layer, self._ref_group)

    def _on_open_project(self) -> None:
        """打开工程：读入 ``.qgs`` / ``.qgz`` 工程文件并刷新画布。"""
        from app.ui.common.widgets import open_file_dir

        path = open_file_dir(
            self,
            is_dir=False,
            title="打开工程",
            filter_="QGIS 工程 (*.qgs *.qgz);;所有文件 (*)",
        )
        if not path:
            return
        if self.project.read(path):
            self.canvas.setDestinationCrs(self.project.crs())
            self.canvas.refreshAllLayers()
            self.setWindowTitle(f"qgis_dev - {self.project.title()}")
            # 打开的工程里若带"数据图层/参考图层"分组则重绑 Dock，否则复位
            # （只匹配真正的分组，避免把同名图层节点误当分组）
            self._data_group = None
            self._ref_group = None
            for child in self.project.layerTreeRoot().children():
                if not isinstance(child, QgsLayerTreeGroup):
                    continue
                if child.name() == "数据图层":
                    self._data_group = child
                elif child.name() == "参考图层":
                    self._ref_group = child
            if getattr(self, "project_dock", None) is not None:
                if self._data_group is not None and self._ref_group is not None:
                    self.project_dock.set_project(
                        self.project.title(), self._data_group, self._ref_group
                    )
                else:
                    self.project_dock.reset()
            self.messageBar.pushMessage(f"已打开工程：{path}", level=0, duration=3)
        else:
            QMessageBox.warning(self, "打开失败", f"无法打开工程：{path}")

    def _load_config_base_tile(self):
        """读取配置并加载默认底图（新建工程时恢复天地图底图）。返回图层或 ``None``。"""
        import os
        from pathlib import Path

        try:
            from app.core.domain.basemap import add_base_tile_layer
            from app.env.config import AppConfig
            from app.infra.fs.paths import Paths

            cfg_path = Path(
                os.environ.get("QGIS_DEV_CONFIG")
                or Paths.default().config_file("app.yaml")
            )
            cfg = AppConfig.from_file(cfg_path)
            layer = add_base_tile_layer(cfg.map, project=self.project)
            if layer is not None:
                self.messageBar.pushMessage(
                    f"已加载底图：{layer.name()}", level=0, duration=2
                )
            return layer
        except Exception as exc:  # noqa: BLE001
            self.messageBar.pushMessage(f"底图加载失败：{exc}", level=2, duration=5)
            return None

    def _on_open_file(self) -> None:
        from app.ui.common.widgets import open_file_dir
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

    # ===================================================== 浏览器事件

    def _on_browser_double_clicked(self, index) -> None:
        from qgis.core import QgsLayerItem, QgsVectorLayer, QgsRasterLayer

        if not index.isValid():
            return
        item = self.browser_dock.model.dataItem(index)
        if not item or not isinstance(item, QgsLayerItem):
            return
        uri, name, provider = item.uri(), item.name(), item.providerKey()
        vlayer = QgsVectorLayer(uri, name, provider)
        if vlayer.isValid():
            vlayer.path_c = item.path()
            self._add_layer_to_project(vlayer)
            return
        rlayer = QgsRasterLayer(uri, name, provider)
        if rlayer.isValid():
            rlayer.path_c = item.path()
            self._add_layer_to_project(rlayer)

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
        # （没有 setProviderRegistry，调它会 AttributeError → PyQt5 槽内未捕获 → 崩溃）。
        # 包一层防御：任何异常只回退到"刷新"，不让菜单构建失败拖垮应用。
        try:
            from qgis.gui import QgsDataItemGuiContext, QgsGui

            registry = QgsGui.instance().dataItemGuiProviderRegistry()
            context = QgsDataItemGuiContext()
            context.setView(self.browser_dock.view)
            context.setMessageBar(self.messageBar)
            for provider in registry.providers():
                try:
                    provider.populateContextMenu(
                        item, menu, selectedItems=[item], context=context
                    )
                except Exception:  # noqa: BLE001
                    continue
        except Exception:  # noqa: BLE001
            from app.infra.logging import get_logger

            get_logger(__name__).exception("构建数据浏览器右键菜单失败，仅保留「刷新」")
        refresh = menu.addAction("刷新")
        refresh.triggered.connect(lambda: item.refresh())
        menu.exec_(self.browser_dock.view.viewport().mapToGlobal(pos))

    # ===================================================== 图层右键菜单

    def _on_layer_context_menu(self, pos: QPoint) -> None:
        view = self.layer_dock.view
        index = view.indexAt(pos)
        if not index.isValid():
            return
        model = index.model()
        source_index = model.mapToSource(index)
        source_model = source_index.model()
        layer_name = source_model.data(source_index, Qt.DisplayRole)
        if not layer_name:
            return
        layers = self.project.mapLayersByName(layer_name)
        if not layers:
            return
        self._selected_layer = layers[0]
        if not self._selected_layer:
            return
        menu = QMenu(view)
        # 缩放至图层
        act_zoom = QAction("缩放至图层", self)
        act_zoom.triggered.connect(lambda: self._zoom_to_layer(self._selected_layer))
        menu.addAction(act_zoom)
        # 设编辑
        act_edit = QAction("设为编辑图层", self)
        act_edit.triggered.connect(self._set_edit_layer)
        menu.addAction(act_edit)
        # 工程分组：移动图层归属（创建工程后才显示）
        if (
            getattr(self, "_data_group", None) is not None
            and getattr(self, "_ref_group", None) is not None
        ):
            menu.addSeparator()
            act_to_ref = QAction("移到参考图层", self)
            act_to_ref.triggered.connect(
                lambda: self._move_layer_to_group(self._selected_layer, self._ref_group)
            )
            menu.addAction(act_to_ref)
            act_to_data = QAction("移到数据图层", self)
            act_to_data.triggered.connect(
                lambda: self._move_layer_to_group(self._selected_layer, self._data_group)
            )
            menu.addAction(act_to_data)
        # 属性表
        act_attr = QAction("打开属性表", self)
        act_attr.triggered.connect(self._open_attribute_table)
        menu.addAction(act_attr)
        # SQL
        act_sql = QAction("执行SQL", self)
        act_sql.triggered.connect(self._open_execute_sql)
        menu.addAction(act_sql)
        # 样式
        sub_style = menu.addMenu("样式")
        edit_style = QAction("编辑样式", self)
        edit_style.triggered.connect(lambda: self._show_style_editor(self._selected_layer))
        sub_style.addAction(edit_style)
        open_style = QAction("打开样式配置", self)
        open_style.triggered.connect(lambda: self._open_style_file(self._selected_layer))
        sub_style.addAction(open_style)
        save_style = QAction("保存样式", self)
        save_style.triggered.connect(lambda: self._save_style(self._selected_layer))
        sub_style.addAction(save_style)
        # 移除
        act_remove = QAction("移除", self)
        act_remove.setIcon(Icons.remove())
        act_remove.triggered.connect(lambda: self._remove_layer(self._selected_layer))
        menu.addAction(act_remove)
        # 导出
        act_export = QAction("导出", self)
        act_export.triggered.connect(lambda: self._export_layer(self._selected_layer))
        menu.addAction(act_export)
        # 属性
        act_props = QAction("属性", self)
        act_props.triggered.connect(lambda: self._open_layer_properties(self._selected_layer))
        menu.addAction(act_props)
        menu.exec_(view.mapToGlobal(pos))

    def _zoom_to_layer(self, layer) -> None:
        if layer is None:
            return
        dest_crs = self.canvas.mapSettings().destinationCrs()
        if layer.crs() != dest_crs:
            xform = QgsCoordinateTransform(layer.crs(), dest_crs, self.project)
            ext = xform.transformBoundingBox(layer.extent())
        else:
            ext = layer.extent()
        self.canvas.setExtent(ext)
        self.canvas.refresh()

    def _move_layer_to_group(self, layer, group) -> None:
        """把图层节点移动到指定分组。

        不能只 ``takeChild`` 再 ``addLayer``：`QgsLayerTreeRegistryBridge`（由
        ``QgsProject`` 自动创建、负责树与图层注册表同步）会在图层**在树里没有任何
        节点**时把图层从工程删掉——事件循环一跑图层就没了，"移动"看起来没生效。
        所以先 `group.addLayer` 在目标组挂新节点（图层始终在树里），再摘原节点。
        """
        if layer is None or group is None:
            return
        node = self.project.layerTreeRoot().findLayer(layer)
        if node is not None and node.parent() is not group:
            group.addLayer(layer)          # 先挂新节点，保证图层仍在树里
            node.parent().takeChild(node)  # 再摘原节点（bridge 不会删图层）
            self.messageBar.pushMessage(
                f"已把 {layer.name()} 移到「{group.name()}」", level=0, duration=2
            )

    def _remove_layer(self, layer) -> None:
        """移除图层：清引用 + 刷新画布。

        ``project.removeMapLayer`` 只会把图层从 project 树摘掉，
        画布仍保留最后一次 render 缓存 → 必须显式 ``refresh()`` 否则看到
        残留图形。

        关键时序：``canvas.refresh()`` 不能在 ``removeMapLayer`` 返回后立刻
        调用——我们还在 ``QAction.triggered`` 信号栈里，QGIS 这时尝试
        render 会撞上半移除状态的 layer，导致 0xC0000409。
        用 :class:`QTimer.singleShot` 推到下一个事件循环 tick，等
        ``layersRemoved`` 信号链彻底走完再画。
        """
        from PyQt5.QtCore import QTimer

        if layer is None:
            return
        layer_id = layer.id()
        layer_name = layer.name()
        # 如果该图层正在编辑，先退出（避免脏 buffer 滞留）
        was_editing = layer.isEditable()
        if self.edit_layer is layer and was_editing:
            self.editing_service.stop(layer, save=False)
        # 清空本地引用（在 removeMapLayer 之前，避免后续回调引用悬空 layer）
        if self._selected_layer is layer:
            self._selected_layer = None
        if self.edit_layer is layer:
            self.edit_layer = None
            # 工具条也回滚到"无编辑图层"态
            if hasattr(self, "digitizing_toolbar"):
                try:
                    self.digitizing_toolbar.set_edit_target(None)
                except Exception:  # noqa: BLE001
                    pass
            qgis_tb = getattr(self, "qgis_digitizing_toolbar", None)
            if qgis_tb is not None:
                try:
                    qgis_tb.disable_all_buttons()
                except Exception:  # noqa: BLE001
                    pass

        # 真正从 project 摘掉
        self.project.removeMapLayer(layer_id)
        # 延迟刷新画布到下一个事件循环 tick，让 layersRemoved 信号链走完
        QTimer.singleShot(0, self._deferred_refresh_after_remove)
        self.messageBar.pushMessage(f"已移除图层：{layer_name}", level=0, duration=3)

    def _deferred_refresh_after_remove(self) -> None:
        """延迟到事件循环下一拍才刷新画布（避免半移除状态 native 崩溃）。"""
        try:
            self.canvas.refresh()
        except Exception:  # noqa: BLE001
            pass

    def _set_edit_layer(self) -> None:
        """仅"指定编辑图层"：不真正进入编辑态，让用户点工具条「开始编辑」。

        与工具条上的 :meth:`DigitizingToolbar.start_edit_fun` 拆分职责：

        - 本方法负责：记录 ``self.edit_layer``、刷新"开始编辑"按钮
        - 工具条的"开始编辑"才真正 ``layer.startEditing()``
        """
        if self._selected_layer and isinstance(self._selected_layer, QgsVectorLayer):
            # 如果之前有别的图层在编辑，先停掉（避免遗留脏 buffer）
            prev = self.edit_layer
            if prev is not None and prev is not self._selected_layer and prev.isEditable():
                self.editing_service.stop(prev, save=False)

            self.edit_layer = self._selected_layer
            # 切到"已指定编辑图层"态：开始编辑可用、停止/保存禁用、按图层类型启用添加要素
            if hasattr(self, "digitizing_toolbar"):
                self.digitizing_toolbar.set_edit_target(self.edit_layer)
            # QGIS 复用版按钮：进编辑态时才启用（与 drawing_state 联动）
            qgis_tb = getattr(self, "qgis_digitizing_toolbar", None)
            if qgis_tb is not None:
                qgis_tb.set_layer(self.edit_layer)  # 仅记录图层，不开按钮
            self.messageBar.pushMessage(f"已设 {self._selected_layer.name()} 为编辑图层，点击「开始编辑」生效", level=0, duration=3)

    def _on_layer_clicked_in_dock(self, index) -> None:
        """用户在图层树点击一层：自动同步 ``_selected_layer`` / ``edit_layer``。

        这样点击任意矢量图层后，工具条上的"开始编辑"立即可用，
        不必再走右键 → "设为编辑图层"。
        """
        try:
            node = self.layer_dock.view.indexToNode(index)
        except Exception:  # noqa: BLE001
            return
        if node is None:
            return
        # node 可能是 group / layer / legend node；只要带 ``layer()`` 方法的就拿
        layer_getter = getattr(node, "layer", None)
        if not callable(layer_getter):
            return
        layer = layer_getter()
        if layer is None:
            return

        self._selected_layer = layer
        # 非矢量图层（栅格 / mesh 等）也只做选择，不进入编辑目标
        if not isinstance(layer, QgsVectorLayer):
            return
        # 如果旧 edit_layer 还在 editing，先退出
        prev = self.edit_layer
        if prev is not None and prev is not layer and prev.isEditable():
            self.editing_service.stop(prev, save=False)

        self.edit_layer = layer
        if hasattr(self, "digitizing_toolbar"):
            self.digitizing_toolbar.set_edit_target(layer)
        qgis_tb = getattr(self, "qgis_digitizing_toolbar", None)
        if qgis_tb is not None:
            qgis_tb.set_layer(layer)

    # ---------------------------------------------------- 绘图工具回调（保留兼容）

    def _on_draw_finished(self, geometry) -> None:
        """DrawManager.finished 信号：跳到 DigitizingToolbar.finish_draw。"""
        if hasattr(self, "digitizing_toolbar"):
            self.digitizing_toolbar.finish_draw(geometry)

    def _on_qgis_draw_finished(self, geometry) -> None:
        """QgisDigitizingToolbar 走的是独立的 DrawManager，finished 信号也跳到同一处。"""
        if getattr(self, "digitizing_toolbar", None) is not None:
            self.digitizing_toolbar.finish_draw(geometry)

    def _on_editing_state_changed(self, editing: bool) -> None:
        """DigitizingToolbar 的开始/停止编辑切换 → 同步给 QGIS 复用版工具条。"""
        qgis_tb = getattr(self, "qgis_digitizing_toolbar", None)
        if qgis_tb is None:
            return
        if editing:
            qgis_tb.enable_buttons_for_layer(self.edit_layer)
        else:
            qgis_tb.disable_all_buttons()

    def _open_attribute_table(self) -> None:
        if self._selected_layer is None:
            return
        self._show_attribute_dock(self._selected_layer)

    def _open_execute_sql(self) -> None:
        from app.ui.dialogs.execute_sql import ExecuteSqlDialog
        if self._selected_layer is None:
            return
        dlg = ExecuteSqlDialog(self._selected_layer, self)
        dlg.show()

    def _show_style_editor(self, layer) -> None:
        dlg = QgsRendererPropertiesDialog(layer=layer, style=QgsStyle.defaultStyle(), embedded=False, parent=self)
        dlg.setWindowTitle(f"图层样式 - {layer.name()}")
        if dlg.exec_():
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

            dlg = QgsVectorLayerProperties(
                canvas=self.canvas, lyr=layer, messageBar=self.messageBar, parent=self
            )
            dlg.setWindowIcon(Icons.app())
            dlg.setWindowTitle(f"标注设置 - {layer.name()}")
            self._select_properties_page(dlg, ("标注", "Labels"))
            dlg.exec_()
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
        from app.ui.dialogs.style_io import StyleOpenDialog
        dlg = StyleOpenDialog(layer, self)
        dlg.show()

    def _save_style(self, layer) -> None:
        from app.ui.dialogs.style_io import StyleSaveDialog
        dlg = StyleSaveDialog(layer, self)
        dlg.exec_()

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
        dlg.exec_()

    def _open_layer_properties(self, layer) -> None:
        try:
            from qgis._gui import QgsVectorLayerProperties
            dlg = QgsVectorLayerProperties(canvas=self.canvas, lyr=layer, messageBar=self.messageBar, parent=self)
            dlg.setWindowIcon(Icons.app())
            dlg.exec_()
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "失败", str(e))

    def _show_log_dock(self) -> None:
        if self.log_dock is None:
            self.log_dock = LogDock(self)
            self.addDockWidget(Qt.BottomDockWidgetArea, self.log_dock)
        self.log_dock.show()
        self.log_dock.raise_()

    def _show_attribute_dock(self, layer, filter_str: str = "1=1", show_ids: list = None) -> None:
        if not hasattr(self, "current_attribute_dock") or self.current_attribute_dock is None:
            self.current_attribute_dock = AttributeDock(layer, page_size=50, parent=self, filter_str=filter_str, show_ids=show_ids)
            self.addDockWidget(Qt.BottomDockWidgetArea, self.current_attribute_dock)
            self.current_attribute_dock.show()
        else:
            self.current_attribute_dock.add_new_layer(layer, self, filter_str, show_ids, 50, f"属性表 - {layer.name()}")

    # ===================================================== 关闭

    def closeEvent(self, event: QCloseEvent) -> None:
        try:
            self.project.clear()
        finally:
            event.accept()
