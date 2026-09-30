"""工程操作。

新建 / 保存为工程 / 打开工程、工程名询问、图层加入工程分组，以及地图配置
（app.yaml）的读取与基础底图装载。保存为工程时额外记录画布当前显示范围，
下次打开自动恢复。关闭应用前询问是否保存工程：已是工程文件（打开过 / 另存过）
则直接更新该文件，否则走“保存为工程”另存。

由 :class:`~app.ui.main_window.main_window.MainWindow` 多继承组合，方法只依赖
``self`` 上的共享状态。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from qgis.core import (
    Qgis,
    QgsCoordinateReferenceSystem,
    QgsLayerTreeGroup,
)
from qgis.PyQt.QtWidgets import QMessageBox

from app.core.domain.basemap import add_base_tile_layer
from app.env.config import AppConfig
from app.infra.fs.paths import Paths
from app.infra.logging import get_logger
from app.ui.common.recent_projects import add_recent
from app.ui.common.widgets import open_file_dir, save_file
from app.ui.docks.project_dock import ProjectDock

logger = get_logger(__name__)

#: 工程文件的保存 / 打开过滤器
_PROJECT_FILTER = "工程文件 (*.pgs);;所有文件 (*)"

#: 应用私有数据在工程文件里的存放位置（QgsProject.writeEntry 的 scope/key）
_DM_SCOPE = "qgis_dev"
_DM_KEY = "data_manage"


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
            self.place_right_dock(self.project_dock)
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

        # 重建分组：数据图层（用户数据，画在最上）+ 参考图层（底图，垫在最下）。
        # **顺序敏感**：QGIS 画布 ``layers()`` 是自顶向下存储——索引 0 画在最上
        # 层，末尾索引垫在最底（见 QgsMapSettings.setLayers 文档）。``addGroup``
        # 是 append，所以先建“数据图层”（索引 0，最上），再建“参考图层”（末尾，
        # 最底）。若反过来，底图分组会压在数据图层之上、把数据盖住。
        root = self.project.layerTreeRoot()
        self._data_group = root.addGroup("数据图层")
        self._ref_group = root.addGroup("参考图层")

        # 工程名 + 坐标系（project.clear() 会把 CRS 重置回默认）
        self.project.setTitle(name)
        self.setWindowTitle(f"{self._app_name} - {name}")
        self.project.setCrs(QgsCoordinateReferenceSystem("EPSG:4490"))

        # 新建工程 ⇒ 尚未关联工程文件，关闭/保存时需另存
        self._project_path = None
        # 数据管理连接不属于新工程：清空 Dock / 待恢复状态
        self._reset_data_manage_state()

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

    # ------------------------------------------------------------ 保存为工程

    def _save_project_to(self, path: str) -> bool:
        """把当前工程写入 ``path``（已存在的工程文件即“更新”）。返回是否成功。

        QGIS 工程天然序列化图层树、数据源、渲染器与标注设置（本工程文件后缀
        ``.pgs``，内容即 QGIS 工程 XML）；画布当前显示范围额外写入 “工程视图
        设置”，下次打开时由 :meth:`_restore_canvas_extent` 恢复。
        """
        # 把画布当前显示范围写进工程视图设置，供下次打开恢复
        self._store_canvas_extent()
        # 把数据管理里添加的本地库 / 服务器连接一并写进工程
        self._store_data_manage_state()
        self._begin_layer_loading(f"正在保存工程：{os.path.basename(path)}")
        try:
            saved = self.project.write(path)
        except Exception as exc:  # noqa: BLE001
            logger.warning("保存工程失败：%s", exc, exc_info=True)
            QMessageBox.warning(self, "保存失败", f"无法保存工程：{path}\n{exc}")
            return False
        finally:
            self._end_layer_loading()

        if not saved:
            QMessageBox.warning(self, "保存失败", f"无法保存工程：{path}")
            return False
        title = self.project.title() or Path(path).stem
        self.project.setTitle(title)
        self.setWindowTitle(f"{self._app_name} - {title}")
        # 记住当前工程文件：下次保存 / 关闭时直接更新该文件，不再弹另存
        self._project_path = path
        logger.info("保存工程：%s", path)
        add_recent(path)
        self.messageBar.pushMessage(
            f"已保存工程：{path}", level=Qgis.MessageLevel.Info, duration=3
        )
        return True

    def _on_save_project_as(self) -> bool:
        """另存为工程：选路径后写入。返回是否已保存。"""
        if not self.project.mapLayers():
            QMessageBox.information(self, "提示", "当前没有可保存的图层")
            return False
        path = save_file(
            self,
            title="另存为工程",
            init_path=str(getattr(self, "_paths", Paths.default()).project_root),
            filter_=_PROJECT_FILTER,
        )
        if not path:
            return False
        if not Path(path).suffix:
            path += ".pgs"
        return self._save_project_to(path)

    def _save_current_project(self) -> bool:
        """保存工程：**已经打开着某个工程文件就直接覆盖保存**，否则走「另存为」。

        返回是否已保存（用户取消另存对话框 / 写盘失败均返回 ``False``）。
        """
        path = getattr(self, "_project_path", None)
        if path:
            return self._save_project_to(path)
        return self._on_save_project_as()

    def _confirm_save_project_on_close(self) -> bool:
        """关闭应用前询问是否保存工程；返回 ``True`` 表示可以关闭。

        - 无图层 / 提示被禁用（``prompt_save_on_close=False``）→ 直接可关闭；
        - 「是（保存）」→ 已有工程文件则更新，否则走“保存为工程”；保存失败即
          不关闭（返回 ``False``）；
        - 「否（不保存）」→ 直接关闭；
        - 「取消」→ 返回 ``False``，调用方应 ``event.ignore()``。
        """
        if not getattr(self, "prompt_save_on_close", True):
            return True
        if not self.project.mapLayers():
            return True
        path = getattr(self, "_project_path", None)
        hint = f"将保存到：{path}" if path else "当前工程尚未保存到文件。"
        ret = QMessageBox.question(
            self,
            "关闭应用",
            f"是否保存当前工程？\n{hint}",
            QMessageBox.StandardButton.Yes
            | QMessageBox.StandardButton.No
            | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Yes,
        )
        if ret == QMessageBox.StandardButton.Cancel:
            return False
        if ret == QMessageBox.StandardButton.Yes:
            return self._save_current_project()
        return True  # 否（不保存）

    def _store_data_manage_state(self) -> None:
        """把数据管理 Dock 里的本地库 / 服务器连接写入工程文件。

        Dock 尚未创建（本次会话没开过数据管理）时，若有从工程恢复的待应用
        状态则原样写回，避免重存工程丢掉连接信息。失败只记日志、不阻断保存。
        """
        try:
            dock = getattr(self, "_data_manage_dock", None)
            if dock is not None:
                state = dock.export_state()
            else:
                state = getattr(self, "_dm_pending_state", None)
                if not state:
                    return
            self.project.writeEntry(
                _DM_SCOPE, _DM_KEY, json.dumps(state, ensure_ascii=False)
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("保存数据管理连接失败：%s", exc, exc_info=True)

    def _restore_data_manage_state(self) -> None:
        """从工程文件恢复数据管理里的本地库 / 服务器连接。

        工程里没有该项时按“空”处理（新工程式工作区，清空残留连接）。Dock 已
        创建（用户开过数据管理）→ 立即应用；尚未创建 → 暂存到
        ``_dm_pending_state``，等 Dock 首次创建时再由
        :meth:`_apply_pending_data_manage_state` 应用。
        """
        state: dict = {}
        try:
            raw, ok = self.project.readEntry(_DM_SCOPE, _DM_KEY, "")
            if ok and raw:
                parsed = json.loads(raw) if isinstance(raw, str) else raw
                if isinstance(parsed, dict):
                    state = parsed
        except Exception as exc:  # noqa: BLE001
            logger.warning("恢复数据管理连接失败：%s", exc, exc_info=True)
        dock = getattr(self, "_data_manage_dock", None)
        if dock is not None:
            dock.restore_state(state)
        else:
            self._dm_pending_state = state

    def _apply_pending_data_manage_state(self, dock) -> None:
        """把打开工程时暂存的连接状态应用到刚创建的 Dock（无暂存则不动）。"""
        state = getattr(self, "_dm_pending_state", None)
        self._dm_pending_state = None
        if state:
            dock.restore_state(state)

    def _reset_data_manage_state(self) -> None:
        """新建工程：丢掉待恢复状态，并把已存在的 Dock 清空成空树。"""
        self._dm_pending_state = None
        dock = getattr(self, "_data_manage_dock", None)
        if dock is not None:
            dock.restore_state({})

    def _store_canvas_extent(self) -> None:
        """把画布当前显示范围写入工程视图设置（保存工程时调用）。"""
        try:
            from qgis.core import QgsReferencedRectangle

            extent = self.canvas.extent()
            crs = self.canvas.mapSettings().destinationCrs()
            if extent is None or extent.isNull() or extent.isEmpty():
                return
            settings = self.project.viewSettings()
            settings.setDefaultViewExtent(QgsReferencedRectangle(extent, crs))
            settings.setRestoreProjectExtentOnProjectLoad(True)
        except Exception as exc:  # noqa: BLE001
            logger.warning("记录画布范围失败：%s", exc, exc_info=True)

    def _restore_canvas_extent(self) -> bool:
        """恢复工程里保存的画布范围；无有效范围或失败返回 ``False``。"""
        try:
            from qgis.core import QgsCoordinateTransform

            extent = self.project.viewSettings().defaultViewExtent()
            if extent is None or extent.isNull() or extent.isEmpty():
                return False
            crs = extent.crs()
            canvas_crs = self.canvas.mapSettings().destinationCrs()
            rect = extent
            if crs.isValid() and canvas_crs.isValid() and crs != canvas_crs:
                transform = QgsCoordinateTransform(crs, canvas_crs, self.project)
                rect = transform.transformBoundingBox(extent)
            if rect is None or rect.isNull() or rect.isEmpty():
                return False
            self.canvas.setExtent(rect)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("恢复画布范围失败：%s", exc, exc_info=True)
            return False

    def _on_open_project(self) -> None:
        """打开工程：弹文件框选 ``.pgs`` 后读入并刷新画布。"""

        path = open_file_dir(
            self,
            is_dir=False,
            title="打开工程",
            filter_=_PROJECT_FILTER,
        )
        if not path:
            return
        self.open_project_path(path)

    def open_project_path(self, path: str) -> bool:
        """读入 ``path`` 指定的工程文件并刷新画布；返回是否成功。

        打开工程动作（菜单 / 工具栏）与启动“打开地图文档”对话框共用：对话框
        只负责选路径，读盘、bridge 同步、分组重排都在这里。
        """
        if not path:
            return False
        # QgsProject.read 会同步读入工程里所有图层（大工程较慢）→ 期间亮起加载指示
        self._begin_layer_loading(f"正在打开图层：{os.path.basename(path)}")
        try:
            opened = self.project.read(path)
        finally:
            self._end_layer_loading()
        if opened:
            self.canvas.setDestinationCrs(self.project.crs())
            # 记住工程文件：关闭 / 保存时直接更新该文件
            self._project_path = path
            # 恢复保存工程时的画布范围（无记录时保持默认全图）
            self._restore_canvas_extent()
            # 恢复工程里保存的数据管理连接（本地库 / 服务器）
            self._restore_data_manage_state()
            self.canvas.refreshAllLayers()
            title = self.project.title() or Path(path).stem
            self.setWindowTitle(f"{self._app_name} - {title}")
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
            # 老工程可能把“参考图层”排在数据图层之上（树里索引更小 = 画在更上
            # 层），底图会盖住数据。这里把参考图层挪到根分组末尾（最底层），与
            # ``_on_project_create`` 的建组顺序一致。
            #
            # 注意：**不能**直接 ``insertChildNode(index, ref_group)``——实测对
            # 已在树里的子节点，该调用会在同一父节点下插入一份重复节点（树里
            # 出现两个“参考图层”）。必须用 clone + insert + remove 三步（与
            # ``_on_move_layer_node`` 一致）：bridge 收到 willRemoveChildren 时
            # 看到图层仍挂在克隆节点上，不会把底图从工程删掉。
            if self._ref_group is not None:
                root = self.project.layerTreeRoot()
                children = root.children()
                ref_idx = children.index(self._ref_group)
                data_idx = (
                    children.index(self._data_group) if self._data_group is not None else -1
                )
                if data_idx < 0 or ref_idx < data_idx:
                    self._clear_view_current()
                    cloned = self._ref_group.clone()
                    root.insertChildNode(len(children), cloned)
                    self._remap_group_refs(self._ref_group, cloned)
                    root.removeChildNode(self._ref_group)
                    # 重排后 bridge 未必收到通知（与 read 同理），显式再同步
                    # 一次，保证画布按纠正后的顺序重绘。
                    self.bridge.setCanvasLayers()
            if getattr(self, "project_dock", None) is not None:
                if self._data_group is not None and self._ref_group is not None:
                    self.project_dock.set_project(self.project.title(), self._data_group, self._ref_group)
                else:
                    self.project_dock.reset()
            self.messageBar.pushMessage(f"已打开工程：{path}", level=Qgis.MessageLevel.Info, duration=3)
            add_recent(path)
            return True
        QMessageBox.warning(self, "打开失败", f"无法打开工程：{path}")
        return False

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
