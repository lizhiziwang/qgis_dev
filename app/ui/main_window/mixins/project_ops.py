"""工程操作。

新建 / 打开工程、工程名询问、图层加入工程分组，以及地图配置（app.yaml）
的读取与基础底图装载。

由 :class:`~app.ui.main_window.main_window.MainWindow` 多继承组合，方法只依赖
``self`` 上的共享状态。
"""

from __future__ import annotations

import os
from pathlib import Path

from qgis.core import (
    Qgis,
    QgsCoordinateReferenceSystem,
    QgsLayerTreeGroup,
)
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import QMessageBox

from app.core.domain.basemap import add_base_tile_layer
from app.env.config import AppConfig
from app.infra.fs.paths import Paths
from app.infra.logging import get_logger
from app.ui.common.widgets import open_file_dir
from app.ui.docks.project_dock import ProjectDock

logger = get_logger(__name__)


class ProjectOpsMixin:
    """ProjectOpsMixin：见模块 docstring。"""

    def _on_new_project(self) -> None:
        """新建工程：先弹窗输入工程名，创建后把名字带到右侧工程 Dock 显示。"""
        # 已有图层时先确认清空（避免输入完名字又取消）
        if self.project.mapLayers():
            ret = QMessageBox.question(
                self,
                "新建工程",
                "将清空当前所有图层并新建工程，是否继续？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if ret != QMessageBox.StandardButton.Yes:
                return
        name = self._ask_project_name()
        if name is None:
            return
        # 确保 Dock 存在
        if getattr(self, "project_dock", None) is None:
            self.project_dock = ProjectDock(self)
            self.project_dock.render_requested.connect(self._show_style_editor)
            self.project_dock.label_requested.connect(self._show_label_settings)
            self.project_dock.layer_context_menu_requested.connect(self._on_layer_context_menu)
            self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.project_dock)
        self._on_project_create(name)
        self.project_dock.show()
        self.project_dock.raise_()

    def _ask_project_name(self):
        """弹窗输入工程名；返回工程名，取消或为空则返回 ``None``。"""
        from qgis.PyQt.QtWidgets import QInputDialog

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

        # 重建分组：参考图层（底图）+ 数据图层（用户数据）。
        # **顺序敏感**：QGIS 按 layer tree 索引升序渲染——索引 0 先画、最后索引
        # 画在最上面。所以参考图层（底图）必须建在根分组**底部**（先渲染），
        # 数据图层建在**顶部**（后渲染、盖在底图上）。若顺序反了，缩放/平移时
        # 底图会被排到最后渲染，慢速矢量图层会拖住它——表现为"等矢量画完才出
        # 底图"。``addGroup`` 是 append，所以建组顺序直接决定渲染顺序。
        root = self.project.layerTreeRoot()
        self._ref_group = root.addGroup("参考图层")
        self._data_group = root.addGroup("数据图层")

        # 工程名 + 坐标系（project.clear() 会把 CRS 重置回默认）
        self.project.setTitle(name)
        self.setWindowTitle(f"{self._app_name} - {name}")
        self.project.setCrs(QgsCoordinateReferenceSystem("EPSG:4490"))

        # 底图进参考分组
        self._load_config_base_tile_into_ref_group()

        self.project_dock.set_project(name, self._data_group, self._ref_group)
        self.canvas.refreshAllLayers()
        self.messageBar.pushMessage(f"已创建工程：{name}", level=Qgis.MessageLevel.Info, duration=3)

    def _add_layer_to_project(self, layer) -> None:
        """把图层加进项目：默认路由到"数据图层"分组（未创建工程时平铺挂根）。

        用 ``insertLayer(0, ...)`` 把新图层插到分组最顶层——QGIS 面板顶部即渲染
        最上层，后加的图层直接盖在最上面。
        """
        self.project.addMapLayer(layer, False)
        group = getattr(self, "_data_group", None) or self.project.layerTreeRoot()
        group.insertLayer(0, layer)
        # 所有用户导入（打开文件 / 浏览器双击 / 拖拽）都走这里，统一记日志
        src = getattr(layer, "path_c", None) or layer.source()
        logger.info("导入图层：%s（来源=%s）", layer.name(), src)

    def _load_config_base_tile_into_ref_group(self) -> None:
        """加载配置底图并移入"参考图层"分组（底图已平铺挂根，需移动）。"""
        layer = self._load_config_base_tile()
        if layer is not None and getattr(self, "_ref_group", None) is not None:
            self._move_layer_to_group(layer, self._ref_group)

    def _on_open_project(self) -> None:
        """打开工程：读入 ``.qgs`` / ``.qgz`` 工程文件并刷新画布。"""

        path = open_file_dir(
            self,
            is_dir=False,
            title="打开工程",
            filter_="QGIS 工程 (*.qgs *.qgz);;所有文件 (*)",
        )
        if not path:
            return
        # QgsProject.read 会同步读入工程里所有图层（大工程较慢）→ 期间亮起加载指示
        self._begin_layer_loading(f"正在打开图层：{os.path.basename(path)}")
        try:
            opened = self.project.read(path)
        finally:
            self._end_layer_loading()
        if opened:
            self.canvas.setDestinationCrs(self.project.crs())
            self.canvas.refreshAllLayers()
            self.setWindowTitle(f"{self._app_name} - {self.project.title()}")
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
            # 老工程可能把"参考图层"建在数据图层之上（索引更大、后渲染），
            # 缩放时底图会被慢速矢量图层拖住。强制把参考图层挪到根分组最底，
            # 与 ``_on_project_create`` 的建组顺序一致。``insertChildNode`` 在
            # 同父节点上移动已存在的子节点时只重排不重发 willRemoveChildren 给
            # bridge（group 节点不挂 layer，bridge 不关心）。
            if self._ref_group is not None:
                root = self.project.layerTreeRoot()
                if root.children().index(self._ref_group) != 0:
                    root.insertChildNode(0, self._ref_group)
            if getattr(self, "project_dock", None) is not None:
                if self._data_group is not None and self._ref_group is not None:
                    self.project_dock.set_project(self.project.title(), self._data_group, self._ref_group)
                else:
                    self.project_dock.reset()
            self.messageBar.pushMessage(f"已打开工程：{path}", level=Qgis.MessageLevel.Info, duration=3)
        else:
            QMessageBox.warning(self, "打开失败", f"无法打开工程：{path}")

    def _map_config_path(self):
        """``config/app.yaml`` 实际路径（``QGIS_DEV_CONFIG`` 可覆盖），读/写共用。"""


        return Path(os.environ.get("QGIS_DEV_CONFIG") or Paths.default().config_file("app.yaml"))

    def _load_map_config(self):
        """读取并解析 ``config/app.yaml``；失败返回 ``None``（不抛、只记日志）。"""
        try:

            return AppConfig.from_file(self._map_config_path())
        except Exception as exc:  # noqa: BLE001
            logger.warning("读取配置文件失败：%s", exc, exc_info=True)
            return None

    def _load_config_base_tile(self):
        """读取配置并加载默认底图（新建工程时恢复天地图底图）。返回图层或 ``None``。"""
        self._begin_layer_loading("正在加载默认底图…")
        try:

            cfg = self._load_map_config()
            if cfg is None:
                return None
            layer = add_base_tile_layer(cfg.map, project=self.project)
            if layer is not None:
                self.messageBar.pushMessage(f"已加载底图：{layer.name()}", level=Qgis.MessageLevel.Info, duration=2)
            return layer
        except Exception as exc:  # noqa: BLE001
            self.messageBar.pushMessage(f"底图加载失败：{exc}", level=Qgis.MessageLevel.Critical, duration=5)
            return None
        finally:
            self._end_layer_loading()
