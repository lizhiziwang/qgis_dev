"""各功能入口（把工具条/Dock 的点击接到具体功能）。

空间条件查询、数据质检、离线下载、分析工具、数据管理、卷帘与分屏对比、
流程编辑器、日志与属性面板等入口的装配。

由 :class:`~app.ui.main_window.main_window.MainWindow` 多继承组合，方法只依赖
``self`` 上的共享状态。
"""

from __future__ import annotations

import os
import threading
from pathlib import Path
from urllib.parse import unquote

from qgis.core import (
    Qgis,
    QgsProject,
    QgsRasterLayer,
)
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (
    QDialog,
    QMessageBox,
)

from app.core.domain.delete_data import (
    DeleteDataError,
    delete_registered_data,
)
from app.core.domain.import_layer import (
    ImportLayerError,
    import_vector_layer_to_db,
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
from app.ui.dialogs.create_data_dialog import CreateDataDialog
from app.ui.dialogs.import_layer_dialog import ImportLayerDialog
from app.ui.dialogs.offline_download import OfflineDownloadDialog
from app.ui.dialogs.qmap_compare import QMapCompareDialog
from app.ui.dialogs.query_builder import QueryBuilderDialog
from app.ui.dialogs.swipe_compare import SwipeCompareDialog
from app.ui.docks.attribute_dock import AttributeDock
from app.ui.docks.data_manage_dock import DataManageDock
from app.ui.docks.log_dock import LogDock
from app.ui.docks.quality_dock import QualityDock
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

    def _on_quality_dock(self) -> None:
        """质检面板：懒建单个「质检」dock（树里先挂一个「矢量质检」节点）。

        由顶部「质检」页签里的「质检」工具按钮触发，与 :meth:`_on_analyse`
        同一套「复用单实例 + 置前」逻辑；面板见
        :class:`app.ui.docks.quality_dock.QualityDock`。
        """
        dock = getattr(self, "_quality_dock", None)
        if dock is None:
            dock = QualityDock(self)
            self._quality_dock = dock
            self.place_right_dock(dock)
        dock.show()
        dock.raise_()

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
            self._analyse_dock = ToolsDock(
                self, deep_learning_toggle=self._on_deep_learning
            )
            self.place_right_dock(self._analyse_dock)
        self._analyse_dock.show()
        self._analyse_dock.raise_()

    def _on_data_manage(self) -> None:
        """数据管理：懒建底部 Dock（本地数据库注册目录分级展示）。

        「+ 新建连接」打开选库弹窗（本地数据库 Tab，见
        :mod:`app.ui.dialogs.add_connection`），成功后把新连接追加进左树并
        联动右侧数据表；创建数据 / 导入 / 刷新 / 树节点右键菜单（添连接 /
        导入 / 删除 / 刷新）都接到本类的 ``_on_*`` 实现上。
        """

        dock = getattr(self, "_data_manage_dock", None)
        if dock is None:
            dock = DataManageDock(
                self,
                on_add_connection=self._on_dm_add_connection,
                on_add_layer=self._on_dm_add_layer,
                on_delete_layer=self._on_dm_delete_layer,
                on_create_data=self._on_create_data,
                on_import=self._on_import,
                on_refresh=self._on_refresh,
            )
            self._data_manage_dock = dock
            self.place_bottom_dock(dock)
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

    def _on_create_data(self) -> None:
        """创建数据：弹窗收集参数 → 落盘建空矢量数据 → 加入工程。

        两个入口共用：数据管理 Dock 的「创建数据」按钮与主工具条「数据」组的
        「新增数据」（``tool_bar`` 的 ``create_data`` key）。建文件是
        :class:`~app.core.domain.create_data.CreateDataService` 的事（对话框在
        ``accept()`` 里调），这里只管把建出来的图层放进工程。
        """
        dlg = CreateDataDialog(self, default_crs=self._default_crs_authid())
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        layer = dlg.created_layer()
        if layer is None:
            return
        self.project.addMapLayer(layer)
        self.messageBar.pushMessage(
            f"已创建数据「{layer.name()}」并加入工程：{layer.source()}",
            level=Qgis.MessageLevel.Success,
            duration=4,
        )

    def _on_import(self) -> None:
        """数据管理「导入」按钮：弹 :class:`ImportLayerDialog` 收集图层/库/CRS
        → :func:`import_vector_layer_to_db` 落表 + 注册 ``user_relation``。

        工程中无矢量图层 / 已有连接里无 local + server 库 → 早期拒绝（弹警告），
        不弹空 dialog。服务器库目前未实现，dialog 中仍展示但确认后由 helper
        抛 :class:`ImportLayerError` → 弹 Critical 给用户。
        """
        from qgis.core import QgsVectorLayer

        vector_layers = [
            lyr for lyr in self.project.mapLayers().values()
            if isinstance(lyr, QgsVectorLayer)
        ]
        dock = getattr(self, "_data_manage_dock", None)
        databases: list[dict] = []
        if dock is not None:
            databases.extend(getattr(dock, "_databases", []))
            databases.extend(getattr(dock, "_server_connections", []))

        if not vector_layers:
            self.messageBar.pushMessage(
                "导入失败：当前工程中没有矢量图层",
                level=Qgis.MessageLevel.Warning,
                duration=3,
            )
            return
        if not databases:
            self.messageBar.pushMessage(
                "导入失败：请先在「数据管理」Dock 添加本地或服务器数据库",
                level=Qgis.MessageLevel.Warning,
                duration=3,
            )
            return

        dlg = ImportLayerDialog(self, vector_layers=vector_layers, databases=databases)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        layer, db, target_crs = dlg.result_payload()
        # 进度反馈：弹个可取消的无模态 QProgressDialog，用 QGIS 标准
        # QGIS 3.x / 4.x 都支持的 :class:`QgsFeedback` 联动。用户点取消
        # 调 ``feedback.cancel()`` → service 内部 ``isCanceled()`` 赶在
        # 下一次主循环迭代里跳出、回滚已建表。
        from qgis.core import QgsFeedback
        from qgis.PyQt.QtWidgets import QApplication, QProgressDialog

        from app.core.domain.import_layer import _phase_text

        feedback = QgsFeedback()
        progress = QProgressDialog(
            "准备导入…",
            "取消",
            0,
            100,
            self,
        )
        progress.setWindowTitle("导入图层到数据库")
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.setMinimumDuration(0)  # 弹窗立即出现
        progress.setValue(0)
        # 取消按钮 → feedback.cancel()；service 会尽快跳出 + 回滚
        progress.canceled.connect(feedback.cancel)
        # progressChanged 只报数字，文字阶段名由 _phase_text 按进度区间映射
        # （QgsFeedback 没有 progressTextChanged 这个信号，避免踩坑）。
        # QGIS 4.x 的 setProgress 参数实际是 double（emit 出来是 float），
        # QProgressDialog.setValue 强类型 int 必须显式转。
        def _on_progress_changed(value) -> None:
            progress.setValue(int(value))
            progress.setLabelText(
                f"正在导入图层「{layer.name()}」\n{_phase_text(int(value))}"
            )

        feedback.progressChanged.connect(_on_progress_changed)

        try:
            result = import_vector_layer_to_db(
                layer, db, target_crs, feedback=feedback
            )
        except ImportLayerError as exc:
            get_logger(__name__).error("导入图层失败：%s", exc, exc_info=True)
            self.messageBar.pushMessage(
                f"导入失败：{exc}",
                level=Qgis.MessageLevel.Critical,
                duration=5,
            )
            return
        finally:
            progress.close()
            # 关闭 QProgressDialog 后偶发关键事件未处理（PyQt5 / PyQt6 都需
            # 主动 pump），不调会留下「窗口隐藏但 C++ 对象未释放」的中间态
            # 导致下一个 dialog.exec() 阻塞。直接 processEvents 一下。
            QApplication.processEvents()
        self.messageBar.pushMessage(
            f"已导入图层「{layer.name()}」→ 表 {result.table_name}（{result.inserted} 条）",
            level=Qgis.MessageLevel.Success,
            duration=4,
        )
        # 落库后刷新右树：让新建表进入"本地数据库 → 该库 → 分类"列表
        if dock is not None and db.get("kind") == "local":
            dock.refresh_database(db)

    def _on_refresh(self) -> None:
        """数据管理「刷新」按钮：刷新所有已添加的本地库 + 服务器连接。

        - 本地库（:attr:`_databases`）：重新从 sqlite 端查表清单 / 分类 / 行数；
        - 服务器连接（:attr:`_server_connections`）：重画标题 + 摘要展示
          （不发起网络请求；服务器库表目录浏览待后续版本接入）。

        无连接 → 提示「无可刷新」，不发"已刷新 0 个"误导信息。失败回
        滚到刷新前的状态（数据读取失败时维持原表，不动 UI）。
        """
        dock = getattr(self, "_data_manage_dock", None)
        if dock is None:
            return
        n_local = len(getattr(dock, "_databases", []))
        n_server = len(getattr(dock, "_server_connections", []))
        total = n_local + n_server
        if total == 0:
            self.messageBar.pushMessage(
                "无可刷新的数据库连接——请先添加本地库或服务器连接",
                level=Qgis.MessageLevel.Warning,
                duration=3,
            )
            return
        try:
            refreshed = dock.refresh_all()
        except Exception as exc:  # noqa: BLE001
            get_logger(__name__).error("刷新数据库失败：%s", exc, exc_info=True)
            self.messageBar.pushMessage(
                f"刷新失败：{exc}",
                level=Qgis.MessageLevel.Critical,
                duration=5,
            )
            return
        get_logger(__name__).info("刷新 %d 个数据库连接", refreshed)
        self.messageBar.pushMessage(
            f"已刷新 {refreshed} 个数据库连接（{n_local} 本地 + {n_server} 服务器）",
            level=Qgis.MessageLevel.Success,
            duration=3,
        )

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

    def _on_dm_delete_layer(self, db: dict, category: str, name: str) -> None:
        """数据管理左树右键「删除图层」：删库内数据表 + 注册信息 + 移出工程图层。

        时序刻意是「先移出工程图层、再删库」：图层按 OGR / GDAL 持有库文件
        句柄（老 ``.db`` 后缀 gpkg 库还经同 inode 的 ``.gpkg`` 硬链接别名），
        先 :meth:`_remove_layer` 把句柄与别名放掉，``DROP TABLE`` 才不会撞上
        sqlite 锁。

        删除不可恢复 → 先弹确认框（默认 No）；库里删表 + ``user_relation``
        注册行走 :func:`~app.core.domain.delete_data.delete_registered_data`；
        最后让 Dock 摘掉该注册项并重画左树。
        """
        path = str(db.get("path") or "")
        label = _CATEGORY_LABEL.get(category, category)
        if db.get("kind") == "server":
            QMessageBox.information(
                self,
                "删除图层",
                "服务器库删除尚未实现，请选用本地库（SpatiaLite / GeoPackage）。",
            )
            return
        if not path:
            QMessageBox.warning(
                self, "删除图层", f"「{name}」缺少库文件路径, 无法删除。"
            )
            return

        ret = QMessageBox.question(
            self,
            "删除图层",
            f"确定删除{label}「{name}」吗？\n\n"
            "将删除库内数据表与注册信息，并把工程中已打开的对应图层移出地图。\n"
            "此操作不可恢复。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if ret != QMessageBox.StandardButton.Yes:
            return

        # 1) 先移出工程里指向这张表的图层（释放文件句柄 / gpkg 别名）
        removed = 0
        for layer in self._layers_backed_by(path, name):
            self._remove_layer(layer)
            removed += 1
        # 2) 删库内数据表 + user_relation 注册行
        try:
            delete_registered_data(db, category, name)
        except DeleteDataError as exc:
            get_logger(__name__).error("删除数据失败：%s", exc, exc_info=True)
            self.messageBar.pushMessage(
                f"删除失败：{exc}",
                level=Qgis.MessageLevel.Critical,
                duration=5,
            )
            return
        # 3) 左树摘掉该注册项（含类别计数 / 行数），右表跟着重画
        dock = getattr(self, "_data_manage_dock", None)
        if dock is not None:
            dock.remove_registered_node(db, category, name)
        suffix = f"，并移出工程 {removed} 个图层" if removed else ""
        get_logger(__name__).info(
            "已删除本地库数据：%s → %s(%s)%s", path, name, category, suffix
        )
        self.messageBar.pushMessage(
            f"已删除{label}「{name}」{suffix}",
            level=Qgis.MessageLevel.Success,
            duration=4,
        )

    def _layers_backed_by(self, db_path: str, table: str) -> list:
        """工程里数据源指向「``db_path`` 库中的 ``table``」的图层（删除时一并移出）。

        两条线索都命中才算同一份数据：

        - **容器路径**：加载时挂在图层上的 ``layer.path_c``（见
          :meth:`_on_dm_add_layer` / ``file_browser``），没有则退 OGR 数据源
          ``路径|layername=表名`` 里 ``|`` 之前那段；
        - **表名**：数据源 ``layername=`` 参数；栅格等非 OGR 数据源没有该参数，
          退图层显示名（加载时就是用表名命名的）。

        路径比较先按规范化字符串（大小写不敏感），再退 ``os.path.samefile``
        —— 老 ``.db`` 后缀 gpkg 库的图层数据源指向同 inode 的 ``.gpkg`` 硬
        链接别名，字符串对不上但其实是同一个文件。
        """
        out: list = []
        for layer in self.project.mapLayers().values():
            try:
                source = str(layer.source() or "")
                layer_name = layer.name()
            except RuntimeError:  # C++ 对象已销毁（极端时序），跳过
                continue
            container = str(getattr(layer, "path_c", None) or source.split("|", 1)[0])
            if not self._same_db_path(container, db_path):
                continue
            src_table = self._source_layername(source)
            if (src_table if src_table is not None else layer_name) != table:
                continue
            out.append(layer)
        return out

    @staticmethod
    def _same_db_path(container: str, db_path: str) -> bool:
        """两段路径是否指向同一个库文件（字符串规范化 → ``samefile`` 兜底）。"""
        if not container or not db_path:
            return False
        if container.replace("\\", "/").lower() == db_path.replace("\\", "/").lower():
            return True
        try:
            return os.path.samefile(Path(container), Path(db_path))
        except OSError:
            return False

    @staticmethod
    def _source_layername(source: str) -> str | None:
        """取 OGR 数据源里的 ``layername=`` 表名（没有该参数则 None）。

        值可能被 QGIS 做过 URI 转义（空格 / 中文），这里还原一次再比。
        """
        if "layername=" not in source:
            return None
        raw = source.split("layername=", 1)[1].split("|", 1)[0]
        return unquote(raw) or None

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


            # threading.Thread(target=server_main, daemon=True).start()
            self.messageBar.pushMessage("测试消息！", level=Qgis.MessageLevel.Info, duration=3)
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "失败", str(e))

    def _show_log_dock(self) -> None:
        if self.log_dock is None:
            self.log_dock = LogDock(self)
            self.place_bottom_dock(self.log_dock)
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

    def _open_annotation_page(self) -> None:
        """打开影像标注工作区（懒建单实例，嵌入主窗口中央区）。"""
        try:
            if self._annotation_page is None:
                from app.ui.deep_learning.annotation_page import AnnotationPage

                page = AnnotationPage(self)
                page.close_requested.connect(self._close_annotation_page)
                self._central_stack.addWidget(page)
                self._annotation_page = page
            self._annotation_page_open = True
            index = getattr(self, "_deep_learning_tab_index", -1)
            if index >= 0 and self._tab_bar.currentIndex() != index:
                self._tab_bar.setCurrentIndex(index)
            self._show_annotation_page()
        except Exception as exc:  # noqa: BLE001
            # 槽内异常会 native 崩溃，务必兜住（见 qgis-ui-integration-gotchas）
            get_logger(__name__).error("打开标注页面失败：%s", exc, exc_info=True)
            if hasattr(self, "messageBar"):
                self.messageBar.pushWarning("标注", f"打开失败：{exc}")

    def _show_annotation_page(self) -> None:
        """把标注页设为中央区当前页，并保证日志面板可见（用户要能看到刷新）。"""
        page = self._annotation_page
        if page is None:
            return
        self._central_stack.setCurrentWidget(page)
        self._annotation_page_visible = True
        self._training_page_visible = False
        self._inference_page_visible = False
        self._last_deep_learning_page = "annotation"
        self._set_annotation_shortcuts_blocked(True)
        self._show_log_dock()
        page.activate()

    def _close_annotation_page(self) -> None:
        """关闭标注页：确认未保存内容后切回地图（页面实例与状态保留）。"""
        page = self._annotation_page
        if page is None or not page.confirm_leave():
            return
        self._annotation_page_open = False
        self._annotation_page_visible = False
        if self._last_deep_learning_page == "annotation":
            self._last_deep_learning_page = ""
        if not self._training_page_visible and not self._inference_page_visible:
            self._set_annotation_shortcuts_blocked(False)
        general = getattr(self, "_general_tab_index", -1)
        if general >= 0 and self._tab_bar.currentIndex() != general:
            self._tab_bar.setCurrentIndex(general)
        else:
            self._central_stack.setCurrentWidget(self._map_page)
            self.canvas.setFocus(Qt.FocusReason.OtherFocusReason)

    def _open_training_page(self) -> None:
        """打开模型训练工作区（懒建单实例，嵌入主窗口中央区）。"""
        try:
            if self._training_page is None:
                from app.ui.deep_learning.training_page import TrainingPage

                page = TrainingPage(self)
                page.close_requested.connect(self._close_training_page)
                self._central_stack.addWidget(page)
                self._training_page = page
            self._training_page_open = True
            index = getattr(self, "_deep_learning_tab_index", -1)
            if index >= 0 and self._tab_bar.currentIndex() != index:
                self._tab_bar.setCurrentIndex(index)
            annotation = getattr(self, "_annotation_page", None)
            if annotation is not None:
                self._training_page.set_dataset_hint(annotation.dataset_hint_dir())
            self._show_training_page()
        except Exception as exc:  # noqa: BLE001
            # 槽内异常会 native 崩溃，务必兜住（见 qgis-ui-integration-gotchas）
            get_logger(__name__).error("打开训练页面失败：%s", exc, exc_info=True)
            if hasattr(self, "messageBar"):
                self.messageBar.pushWarning("训练", f"打开失败：{exc}")

    def _show_training_page(self) -> None:
        """把训练页设为中央区当前页，并保证日志面板可见。"""
        page = self._training_page
        if page is None:
            return
        self._central_stack.setCurrentWidget(page)
        self._training_page_visible = True
        self._annotation_page_visible = False
        self._inference_page_visible = False
        self._last_deep_learning_page = "training"
        self._set_annotation_shortcuts_blocked(True)
        self._show_log_dock()
        page.activate()

    def _close_training_page(self) -> None:
        """关闭训练页：确认仍在运行的训练后切回地图（页面实例保留）。"""
        page = self._training_page
        if page is None or not page.confirm_leave():
            return
        self._training_page_open = False
        self._training_page_visible = False
        if self._last_deep_learning_page == "training":
            self._last_deep_learning_page = ""
        if not self._annotation_page_visible and not self._inference_page_visible:
            self._set_annotation_shortcuts_blocked(False)
        general = getattr(self, "_general_tab_index", -1)
        if general >= 0 and self._tab_bar.currentIndex() != general:
            self._tab_bar.setCurrentIndex(general)
        else:
            self._central_stack.setCurrentWidget(self._map_page)
            self.canvas.setFocus(Qt.FocusReason.OtherFocusReason)

    def _open_inference_page(self) -> None:
        """打开模型推理工作区（懒建单实例，嵌入主窗口中央区）。"""
        try:
            if self._inference_page is None:
                from app.ui.deep_learning.inference_page import InferencePage

                page = InferencePage(self)
                page.close_requested.connect(self._close_inference_page)
                self._central_stack.addWidget(page)
                self._inference_page = page
            self._inference_page_open = True
            index = getattr(self, "_deep_learning_tab_index", -1)
            if index >= 0 and self._tab_bar.currentIndex() != index:
                self._tab_bar.setCurrentIndex(index)
            self._apply_training_checkpoint_hint()
            self._show_inference_page()
        except Exception as exc:  # noqa: BLE001
            # 槽内异常会 native 崩溃，务必兜住（见 qgis-ui-integration-gotchas）
            get_logger(__name__).error("打开推理页面失败：%s", exc, exc_info=True)
            if hasattr(self, "messageBar"):
                self.messageBar.pushWarning("推理", f"打开失败：{exc}")

    def _apply_training_checkpoint_hint(self) -> None:
        """把训练页最近一次 run 的 best.pt / last.pt 带入推理页。

        只在新出现的 run 上覆盖选择：用户手动换过模型后，重复打开页面不会被
        拉回旧 run；训练完成后 run_dir 变化才会再次自动带入。
        """
        training = getattr(self, "_training_page", None)
        run_dir = getattr(training, "run_dir", None)
        if run_dir is None:
            return
        page = getattr(self, "_inference_page", None)
        if page is None:
            return
        from app.core.inference.runs import checkpoint_in

        hint = checkpoint_in(run_dir)
        if not hint or hint == getattr(self, "_inference_applied_hint", ""):
            return
        page.set_checkpoint_hint(hint)
        self._inference_applied_hint = hint

    def _show_inference_page(self) -> None:
        """把推理页设为中央区当前页，并保证日志面板可见。"""
        page = self._inference_page
        if page is None:
            return
        self._central_stack.setCurrentWidget(page)
        self._inference_page_visible = True
        self._annotation_page_visible = False
        self._training_page_visible = False
        self._last_deep_learning_page = "inference"
        self._set_annotation_shortcuts_blocked(True)
        self._show_log_dock()
        page.activate()

    def _close_inference_page(self) -> None:
        """关闭推理页：确认仍在运行的推理后切回地图（页面实例保留）。"""
        page = self._inference_page
        if page is None or not page.confirm_leave():
            return
        self._inference_page_open = False
        self._inference_page_visible = False
        if self._last_deep_learning_page == "inference":
            self._last_deep_learning_page = ""
        if not self._annotation_page_visible and not self._training_page_visible:
            self._set_annotation_shortcuts_blocked(False)
        general = getattr(self, "_general_tab_index", -1)
        if general >= 0 and self._tab_bar.currentIndex() != general:
            self._tab_bar.setCurrentIndex(general)
        else:
            self._central_stack.setCurrentWidget(self._map_page)
            self.canvas.setFocus(Qt.FocusReason.OtherFocusReason)

    def _attribute_docks(self) -> dict:
        """图层 id → 该图层的属性表面板（同一图层复用同一个，不同图层各开一个）。"""
        docks = getattr(self, "_attr_docks", None)
        if docks is None:
            docks = self._attr_docks = {}
            # 图层删了就把登记清掉（面板自己会在图层删除时关闭，这里只是别留悬空条目）
            QgsProject.instance().layersRemoved.connect(
                lambda _ids: self._prune_attribute_docks()
            )
        return docks

    def _prune_attribute_docks(self) -> None:
        """清掉指向已删图层的登记条目。"""
        docks = getattr(self, "_attr_docks", None)
        if not docks:
            return
        for lid in [k for k in docks if QgsProject.instance().mapLayer(k) is None]:
            docks.pop(lid, None)

    def _show_attribute_dock(self, layer, filter_str: str = "1=1", show_ids: list = None) -> None:
        """打开某图层的属性表。

        **每个图层一个面板**，都停靠到底部、彼此**叠成标签页**（:meth:`place_bottom_dock`），
        所以可以像 QGIS 那样同时开着好几个图层的属性表。同一图层再次打开就复用原面板
        （位置记忆、标签页顺序都留着），只是把内容换成新的筛选 / 子集。
        """
        if layer is None:
            return
        docks = self._attribute_docks()
        self._prune_attribute_docks()       # 顺手清掉已删图层留下的条目
        dock = docks.get(layer.id())
        if dock is None:
            dock = AttributeDock(
                layer, page_size=50, parent=self, filter_str=filter_str, show_ids=show_ids
            )
            docks[layer.id()] = dock
            self.place_bottom_dock(dock)     # 底部已有别的面板时自动叠成标签页
            dock.show()
        else:
            # 复用同一个 Dock 实例（位置记忆留着），关过的就再显示出来
            dock.add_new_layer(layer, self, filter_str, show_ids, 50, f"属性表 - {layer.name()}")
            dock.show()
        dock.raise_()                        # 抬到当前标签页
        # 仍维护 current_attribute_dock：空间查询等入口拿到"刚打开的这个"
        self.current_attribute_dock = dock
