"""各功能入口（把工具条/Dock 的点击接到具体功能）。

空间条件查询、数据质检、离线下载、分析工具、数据管理、卷帘与分屏对比、
流程编辑器、日志与属性面板等入口的装配。

由 :class:`~app.ui.main_window.main_window.MainWindow` 多继承组合，方法只依赖
``self`` 上的共享状态。
"""

from __future__ import annotations

import threading

from qgis.core import (
    Qgis,
    QgsRasterLayer,
)
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (
    QDialog,
    QMessageBox,
)

from app.core.util.local_db_util import _CATEGORY_LABEL
from app.core.util.local_layer_loader import (
    LocalLayerError,
    LocalLayerUnsupportedCategoryError,
)
from app.infra.logging import get_logger
from app.server.cli import main as server_main
from app.ui.dialogs.add_connection import AddConnectionDialog
from app.ui.dialogs.check_viewer import CheckViewerDialog
from app.ui.dialogs.offline_download import OfflineDownloadDialog
from app.ui.dialogs.qmap_compare import QMapCompareDialog
from app.ui.dialogs.query_builder import QueryBuilderDialog
from app.ui.dialogs.swipe_compare import SwipeCompareDialog
from app.ui.docks.attribute_dock import AttributeDock
from app.ui.docks.data_manage_dock import DataManageDock
from app.ui.docks.log_dock import LogDock
from app.ui.docks.tools_dock import ToolsDock
from app.ui.flow_editor import FlowEditorWindow
from app.ui.tools.qmap_compare import (
    CompareMask,
    MirrorCompare,
)
from app.ui.tools.swipe import SwipeTool


class AppActionsMixin:
    """AppActionsMixin：见模块 docstring。"""

    def _on_condition_query(self) -> None:
        """条件查询：对当前选中 / 编辑的矢量图层打开 SQL 条件查询构建器。

        未选中图层时提示先选层（与属性表「筛选」同一对话框，见
        :class:`app.ui.dialogs.query_builder.QueryBuilderDialog`）。
        """
        from qgis.core import QgsVectorLayer

        layer = self._selected_layer or self.edit_layer
        if layer is None or not isinstance(layer, QgsVectorLayer):
            self.messageBar.pushMessage(
                "请先在图层列表选择要查询的矢量图层",
                level=Qgis.MessageLevel.Warning,
                duration=3,
            )
            return

        dlg = QueryBuilderDialog(layer, parent=self)
        dlg.exec()

    def _on_quality(self) -> None:

        dlg = CheckViewerDialog(self, "")
        dlg.show()

    def _on_offline_download(self) -> None:
        """打开「下载离线图层」弹窗（懒建单实例，保留勾选与进度状态）。"""

        if self._offline_download_dialog is None:
            self._offline_download_dialog = OfflineDownloadDialog(
                self, canvas=self.canvas, tasks=self.offline_tasks or []
            )
        dlg = self._offline_download_dialog
        dlg.set_tasks(self.offline_tasks or dlg.tasks)
        # 最小化状态下 show()/raise_() 不还原窗口 → 显式 showNormal 再激活
        if dlg.isMinimized():
            dlg.showNormal()
        else:
            dlg.show()
        dlg.raise_()
        dlg.activateWindow()

    def _on_analyse(self) -> None:
        # 复用单个"分析工具"dock，避免每次点"分析"都新建一个
        if getattr(self, "_analyse_dock", None) is None:
            self._analyse_dock = ToolsDock(self)
            self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self._analyse_dock)
        self._analyse_dock.show()
        self._analyse_dock.raise_()

    def _on_data_manage(self) -> None:
        """数据管理：懒建底部 Dock（本地数据库注册目录分级展示）。

        「+ 新建连接」打开选库弹窗（本地数据库 Tab，见
        :mod:`app.ui.dialogs.add_connection`），成功后把新连接追加进左树并
        联动右侧数据表；创建数据 / 导入 / 刷新由 Dock 内部占位记日志
        （后续版本接入真实功能）。
        """

        dock = getattr(self, "_data_manage_dock", None)
        if dock is None:
            dock = DataManageDock(
                self,
                on_add_connection=self._on_dm_add_connection,
                on_add_layer=self._on_dm_add_layer,
            )
            self._data_manage_dock = dock
            self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, dock)
            # 打开工程时若数据管理还没开过，连接状态暂存在 MainWindow，
            # 这里首次建 Dock 时应用上去
            apply_pending = getattr(self, "_apply_pending_data_manage_state", None)
            if apply_pending is not None:
                apply_pending(dock)
        dock.show()
        dock.raise_()

    def _on_dm_add_connection(self) -> None:
        """数据管理「+ 新建连接」：弹窗选本地库文件或填服务器连接并加入树。"""

        dlg = AddConnectionDialog(self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        dock = getattr(self, "_data_manage_dock", None)
        if dock is not None:
            dock.add_connection(dlg.connection())

    def _on_dm_add_layer(self, db: dict, category: str, name: str) -> None:
        """数据管理表格行双击：把注册表作为图层加入工程。

        加载策略(image / spatial / dict 分流)统一委托
        :class:`app.core.util.local_layer_loader.LocalLayerLoader`。本方法
        只负责 UX(spinner / 模态警告 / 信息弹窗 / messageBar Success / 入
        工程 / 缩放),业务路径与原 MainWindow 行为一致:

        - ``image``:gpkg 内容 → GDAL/GPKG 栅格直开(gdal provider, 普通文件
          路径, 经免 .aux.xml 边车的打开); 直开失败(非 GeoPackage 内容)直接
          模态提示改用 .gpkg 库 —— 无本地 XYZ 回落(服务已移除);
        - ``spatial``:OGR provider 直开(``路径|layername=表名``)。新规 gpkg
          内容库本体即 ``.gpkg`` 后缀 → GPKG 驱动直开, 无别名; 仅老 ``.db``
          后缀 gpkg 库才经同 inode 的 ``.gpkg`` 硬链接别名打开(GDAL 3.11
          按后缀裁决驱动, .db 会被 SQLite 抢先成纯属性表; 别名仅图层存续
          期间存在, 移除/退出即删); 老 spatialite 库 OGR 失败时才有回退;
        - ``dict`` 等无几何数据:沿用原 ``QMessageBox.information`` 提示。
        """

        path = str(db.get("path") or "")
        label = _CATEGORY_LABEL.get(category, category)
        if not path:
            QMessageBox.warning(self, "数据管理", f"「{name}」缺少库文件路径, 无法加载。")
            return
        kind_zh = "影像" if category == "image" else "图层"
        self._begin_layer_loading(f"正在加载{kind_zh}：{name}")
        try:
            try:
                layer = self._layer_loader.load(db, category, name, parent=self)
            except LocalLayerUnsupportedCategoryError:
                QMessageBox.information(
                    self,
                    "数据管理",
                    f"「{name}」为{label}数据(无空间几何), 无法加入地图。\n"
                    f"可在「执行SQL」中按表查看记录。",
                )
                return
            except LocalLayerError as exc:
                QMessageBox.warning(self, "数据管理", f"「{name}」无法打开: {exc}")
                return
            if layer is None:
                QMessageBox.warning(
                    self,
                    "数据管理",
                    f"「{name}」无法作为地图图层打开\n(文件: {path})",
                )
                return
            layer.path_c = path  # 容器路径, 供"执行SQL"按文件打开
            self._add_layer_to_project(layer)
            self._zoom_to_layer(layer)
            self.messageBar.pushMessage(
                f"已加载{kind_zh}：{name}",
                level=Qgis.MessageLevel.Success,
                duration=3,
            )
        finally:
            self._end_layer_loading()

    def _on_swipe(self) -> None:
        """卷帘对比：分屏会话已存续则直接恢复（不弹窗）；否则弹窗选栅格激活。

        分屏是视图状态常驻：切到任意工具（平移/缩放/画要素/查询）后覆盖层仍在，
        再点「卷帘对比」即恢复分隔条拖拽；Esc 才真正退出会话。
        """
        tool = getattr(self, "_swipe_tool", None)
        if tool is not None and tool.is_alive():
            self.canvas.setMapTool(tool)
            self.tool_bar_builder.set_active("swipe")
            return
        if not any(isinstance(l, QgsRasterLayer) for l in self.project.mapLayers().values()):
            QMessageBox.information(self, "提示", "请先加载至少一个栅格图层")
            return

        dlg = SwipeCompareDialog(self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        base, compare, orientation = dlg.selected_params()
        if base is None or compare is None:
            return
        self._swipe_tool = SwipeTool(self.canvas, base, compare, orientation, parent=self)
        self.canvas.setMapTool(self._swipe_tool)
        self.tool_bar_builder.set_active("swipe")

    def _on_qmap_compare(self) -> None:
        """地图对比：会话激活时再点即停止；否则弹窗选择图层 + 模式后启动。

        支持三种模式（参照 qmapcompare 插件）：
        - 垂直/水平分割、放大镜：mask 图层组方案（CompareMask）
        - 镜像分屏：右侧 dock 第二画布（MirrorCompare）
        """
        session = getattr(self, "_qmap_compare_session", None)
        if session is not None and session.is_active:
            session.stop()
            self._set_qmap_compare_active(False)
            self.messageBar.pushMessage("已停止地图对比", level=Qgis.MessageLevel.Info, duration=3)
            return
        if not self.project.mapLayers():
            QMessageBox.information(self, "提示", "请先加载至少一个图层")
            self._set_qmap_compare_active(False)
            return

        dlg = QMapCompareDialog(self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            self._set_qmap_compare_active(False)
            return
        layers, mode, lens_shape, lens_rate = dlg.selected()
        if not layers:
            self._set_qmap_compare_active(False)
            return
        if mode == "mirror":

            session = MirrorCompare(self, self.canvas, parent=self)
            session.start(layers)
        else:

            session = CompareMask(self.project, parent=self, canvas=self.canvas)
            session.start(layers, mode, lens_shape, lens_rate)
        self._qmap_compare_session = session
        # 地图对比无地图工具：会话存活期间顶部 / 左侧工具条按钮独立常亮（再点停止）
        self._set_qmap_compare_active(True)
        self.messageBar.pushMessage(
            "地图对比已启动，再点「地图对比」停止",
            level=Qgis.MessageLevel.Info,
            duration=3,
        )

    def _set_qmap_compare_active(self, active: bool) -> None:
        """同步「地图对比 / 分屏」会话高亮到顶部与左侧两个工具条。"""
        self.tool_bar_builder.set_session_active("qmap_compare", active)
        side = getattr(self, "side_toolbar", None)
        if side is not None:
            side.set_session_active("qmap_compare", active)

    def _on_test(self) -> None:
        """测试按钮：启动独立 FastAPI 服务（在新进程中）。"""
        try:


            threading.Thread(target=server_main, daemon=True).start()
            self.messageBar.pushMessage("已启动服务（8000）", level=Qgis.MessageLevel.Info, duration=3)
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "失败", str(e))

    def _show_log_dock(self) -> None:
        if self.log_dock is None:
            self.log_dock = LogDock(self)
            self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self.log_dock)
        self.log_dock.show()
        self.log_dock.raise_()

    def _open_flow_editor(self) -> None:
        """打开流程节点编辑器（懒建单实例，重复调用只置前）。"""
        try:
            if self._flow_editor_window is None:

                self._flow_editor_window = FlowEditorWindow()
            win = self._flow_editor_window
            win.show()
            win.raise_()
            win.activateWindow()
        except Exception as exc:  # noqa: BLE001
            # 槽内异常会 native 崩溃，务必兜住（见 qgis-ui-integration-gotchas）

            get_logger(__name__).error("打开流程编辑器失败：%s", exc, exc_info=True)
            if hasattr(self, "messageBar"):
                self.messageBar.pushWarning("流程编辑器", f"打开失败：{exc}")

    def _show_attribute_dock(self, layer, filter_str: str = "1=1", show_ids: list = None) -> None:
        if not hasattr(self, "current_attribute_dock") or self.current_attribute_dock is None:
            self.current_attribute_dock = AttributeDock(
                layer, page_size=50, parent=self, filter_str=filter_str, show_ids=show_ids
            )
            self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self.current_attribute_dock)
            self.current_attribute_dock.show()
        else:
            self.current_attribute_dock.add_new_layer(layer, self, filter_str, show_ids, 50, f"属性表 - {layer.name()}")
