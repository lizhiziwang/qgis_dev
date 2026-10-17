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

from qgis.core import (
    Qgis,
    QgsProject,
    QgsRasterLayer,
)
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (
    QApplication,
    QDialog,
    QMessageBox,
)

from app.core.domain.delete_data import (
    DeleteDataError,
    delete_all_registered_data,
    delete_registered_data,
)
from app.core.domain.import_layer import (
    ImportLayerError,
    import_vector_layer_to_db,
)
from app.core.util.local_db_util import _CATEGORY_LABEL, describe_local_db
from app.core.util.local_layer_loader import (
    LocalLayerError,
    LocalLayerUnsupportedCategoryError,
    layer_source_container,
    layer_source_table,
)
from app.infra.fs.state import load_state, save_state
from app.infra.logging import get_logger
from app.server.cli import main as server_main
from app.ui.dialogs.add_connection import AddConnectionDialog
from app.ui.dialogs.check_viewer import CheckViewerDialog
from app.ui.dialogs.create_data_dialog import CreateDataDialog, default_output_dir
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

logger = get_logger(__name__)

#: 数据管理连接清单在系统级状态文件里的名字（见 app.infra.fs.state）。
#: **它不跟工程走**：用户绑定好库之后，即使当前工程没保存，下次打开也得还在。
_DATA_MANAGE_STATE = "data_manage"


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
            self._analyse_dock = ToolsDock(self)
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
                on_remove_connection=self._on_dm_remove_connection,
                on_add_layer=self._on_dm_add_layer,
                on_delete_layer=self._on_dm_delete_layer,
                on_remove_all_layers=self._on_dm_remove_all_layers,
                on_import_data=self._on_dm_import_data,
                on_export_layer=self._on_dm_export_layer,
                on_create_data=self._on_create_data,
                on_import=self._on_import,
                on_refresh=self._on_refresh,
            )
            self._data_manage_dock = dock
            self.place_bottom_dock(dock)
            # 连接清单是**系统级**配置（不属于某个工程）：建 Dock 时从状态文件读回，
            # 之后一有变动就落盘 —— 用户要的是「绑定好之后即使当前工程没保存，
            # 下次打开也还在」（见 app.infra.fs.state）。先读后连，免得读回来又存一遍。
            dock.restore_state(load_state(_DATA_MANAGE_STATE))
            dock.databasesChanged.connect(self._save_data_manage_state)
        dock.show()
        dock.raise_()

    def _save_data_manage_state(self) -> None:
        """把数据管理里登记的连接写进系统级状态文件（加库 / 改库时调，失败只记日志）。"""
        dock = getattr(self, "_data_manage_dock", None)
        if dock is None:
            return
        save_state(_DATA_MANAGE_STATE, dock.export_state())

    def _on_dm_add_connection(self, scope: str = "local") -> None:
        """数据管理「+ 新建连接」/ 根节点右键：弹窗选本地库文件或填服务器连接并加入树。

        ``scope`` 是右键那个根节点（``local`` / ``server``）：弹窗**默认停在该页签**。
        """
        dlg = AddConnectionDialog(self, default_kind=scope or "local")
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        dock = getattr(self, "_data_manage_dock", None)
        if dock is not None:
            dock.add_connection(dlg.connection())

    def _on_dm_remove_connection(self, db: dict) -> None:
        """数据管理库节点右键「删除连接」：确认后**只从列表里解绑，不删库文件**。"""
        path = str(db.get("path") or "")
        name = str(db.get("name") or os.path.basename(path) or "该连接")
        if db.get("kind") == "server":
            message = f"从数据管理里移除服务器连接「{name}」？\n（只解绑，不影响服务器上的数据）"
        else:
            message = (
                f"从数据管理里移除「{name}」？\n"
                f"库文件：{path}\n\n"
                "只取消绑定，**不会删除库文件**；以后还可以重新添加。"
            )
        ret = QMessageBox.question(
            self,
            "删除连接",
            message,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if ret != QMessageBox.StandardButton.Yes:
            return
        dock = getattr(self, "_data_manage_dock", None)
        if dock is not None and dock.remove_connection(db):
            self.messageBar.pushMessage(
                f"已移除连接：{name}", level=Qgis.MessageLevel.Info, duration=3
            )

    def _on_create_data(self, default_db: dict | None = None) -> None:
        """创建数据：弹窗收集参数 → 落盘 → 按三类结果分别收尾。

        两个入口共用：数据管理 Dock 的「创建数据」按钮与主工具条「数据」组的
        「新增数据」（``tool_bar`` 的 ``create_data`` key）。落盘都是领域服务的事
        （对话框在 ``accept()`` / ``apply_now()`` 里调），这里只管把结果放进
        工程 / 数据管理：

        - **建空库**（「本地数据库」页）→ 登记进「数据管理」，**不上地图**（没有图层可上）；
        - **建独立图层文件** → 加入工程（原来的行为）；
        - **往已有库里建图层** → 加入工程，并让「数据管理」重扫该库（新图层进树）。

        Args:
            default_db: 从某个库节点右键进来时传该库 → 目标库下拉**预选它**。
                也可能是 Qt 信号带来的 ``False``（按钮 checked 参数），这里会忽略。

        「确定」与「应用」两条路共用 :meth:`_handle_create_outcome`：前者在 ``exec()``
        返回后取 :meth:`CreateDataDialog.outcome`，后者由 :attr:`CreateDataDialog.applied`
        信号**当场**送过来（窗口不关，可以连着建好几个）。

        向导第二步的「结构来源 → 外部数据集」要一份**工程里已加载的矢量图层**清单，
        跟「导入」那条路用的是同一份（``_on_import`` 里现算），这里照旧注入 ——
        对话框不自己去问 ``QgsProject``。
        """
        from qgis.core import QgsVectorLayer

        vector_layers = [
            lyr for lyr in self.project.mapLayers().values()
            if isinstance(lyr, QgsVectorLayer)
        ]
        # 从某个库节点右键「创建数据」进来时，目标库预选到它（Qt 信号会给 False，忽略）
        preferred = default_db if isinstance(default_db, dict) else None
        dlg = CreateDataDialog(
            self,
            default_crs=self._default_crs_authid(),
            local_dbs=self._local_db_entries(getattr(self, "_data_manage_dock", None)),
            vector_layers=vector_layers,
            default_target_db=str((preferred or {}).get("path") or ""),
        )
        dlg.applied.connect(self._handle_create_outcome)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        outcome = dlg.outcome()
        if outcome is not None:
            # 「应用」过的那些已经在信号里处理过了：``accept()`` 在这一情形下会把
            # outcome 置空，所以这里拿到的只可能是最后这次「确定」要建的东西
            self._handle_create_outcome(outcome)

    def _handle_create_outcome(self, outcome) -> None:
        """创建成功后的收尾（「确定」与「应用」共用）。

        Dock 每次都现取：对话框可能刚从「本地数据库」页建了个库，那条路会把 Dock
        懒建出来，不能沿用进对话框之前拿到的那份引用。
        """
        dock = getattr(self, "_data_manage_dock", None)
        if outcome.kind == "container":
            self._finish_container_creation(outcome, dock)
            return

        layer = outcome.layer
        if layer is None:
            return
        # 容器路径挂到图层上（供"执行SQL"按文件打开）：创建数据的回读图层不是经
        # 数据管理加载的，没有这一步，`source()` 解析会按 |layername= 形态去切
        # SpatiaLite 的 URI 数据源（见 layer_source_container）。
        layer.path_c = layer_source_container(layer)
        self.project.addMapLayer(layer)
        self.messageBar.pushMessage(
            f"已创建数据「{layer.name()}」并加入工程：{layer.source()}",
            level=Qgis.MessageLevel.Success,
            duration=4,
        )
        if outcome.kind == "into_db":
            self._sync_created_db_layer(outcome.db_path, layer.name(), dock)

    def _local_db_entries(self, dock) -> list[dict]:
        """「数据管理」里已登记的本地库连接（注入给创建数据对话框当目标候选）。

        Dock 没开过就返回空表（对话框里只剩「浏览…」）。
        """
        if dock is None:
            return []
        return [db for db in getattr(dock, "_databases", []) if db.get("kind") == "local"]

    def _finish_container_creation(self, outcome, dock) -> None:
        """建完空库：登记进「数据管理」并提示（不往地图上加东西）。"""
        path = outcome.container_path
        if dock is None:
            # Dock 没开过就建出来并显示：库刚建好，「已登记进数据管理」得让用户看得见
            self._on_data_manage()
            dock = getattr(self, "_data_manage_dock", None)
        if dock is not None:
            self._register_local_db(dock, path)
        self.messageBar.pushMessage(
            f"已创建空库：{path}",
            level=Qgis.MessageLevel.Success,
            duration=4,
        )

    def _sync_created_db_layer(self, db_path: str, layer_name: str, dock) -> None:
        """往库里建完图层后，让「数据管理」跟上：已登记的库重扫，其余整库登记。"""
        if dock is None:
            return
        entry = self._dock_db_entry(dock, db_path)
        if entry is not None:
            # 库里多了一张表：重扫 user_relation 后新图层才进树（同导入链路的收尾）
            dock.refresh_database(entry)
            return
        # 目标库不在数据管理里（「浏览…」选的外部库）：整库登记一次
        self._register_local_db(dock, db_path)

    @staticmethod
    def _dock_db_entry(dock, path: str) -> dict | None:
        """Dock 里指向 ``path`` 的本地库连接（活的原 dict）；没有返回 ``None``。

        **必须按路径取活对象**：连接 dict 经 Qt 的 ``itemData`` 传一圈会变成深拷
        副本，而 ``refresh_database`` 是按值在 ``_databases`` 里找连接的 ——
        副本一旦与实时缓存不一致就静默 no-op（见 :class:`CreateOutcome` 的说明）。
        """
        for db in getattr(dock, "_databases", []):
            if db.get("kind") == "local" and str(db.get("path") or "") == str(path):
                return db
        return None

    def _register_local_db(self, dock, path: str) -> dict | None:
        """把 ``path`` 处的本地库登记进「数据管理」；登记不了只提示、不抛。

        建完空库 / 往库外文件里建完图层都走这里。``describe_local_db`` 只认
        sqlite 家族，FileGDB 或结构不符的文件会报错 —— 那也只是「登记不上」，
        刚创建出来的东西照旧可用，所以这里只记一条 warning。
        """
        try:
            catalog = describe_local_db(path)
        except Exception as exc:  # noqa: BLE001 —— 登记失败不该盖住「已创建」这件事
            get_logger(__name__).warning("登记本地库到数据管理失败：%s（%s）", path, exc)
            self.messageBar.pushMessage(
                f"已创建，但没能登记到「数据管理」：{exc}",
                level=Qgis.MessageLevel.Warning,
                duration=6,
            )
            return None
        db = {
            "kind": "local",
            "name": Path(path).stem,
            "path": str(path),
            "categories": catalog["categories"],
            "counts": catalog["counts"],
            "meta": catalog["meta"],
        }
        dock.add_connection(db)
        # 返回 Dock 里的**活对象**：add_connection 存进 _databases 的是 ``dict(db)``
        # 浅拷贝，把手上这份副本交出去，拿去 refresh_database 会因「不在 _databases
        # 里」静默失效（见 :meth:`_dock_db_entry`）
        return self._dock_db_entry(dock, str(path))

    def _on_import(self, default_db: dict | None = None, locked: bool = False) -> None:
        """数据管理「导入」按钮 / 库节点右键「导入图层」：把一份或多份数据导入进库。

        数据源在弹窗里用「选择」挑：工程里的图层，或电脑上的数据文件
        （GDB / 库会先列出里面的图层再选），可多选。**不再选目标坐标系** ——
        每份数据按**自己的坐标系**入库。

        Args:
            default_db: 从某个库节点右键进来时传该库 → 目标数据库预选它；
                不传时沿用「数据管理里当前选中的那个库」。Qt 信号会给 ``False``，忽略。
            locked: ``True`` = 目标库锁定（从左侧库节点右键进来：用户点的是那个库，
                不能再切）；从右侧面板的「导入」按钮进来是 ``False``，可自由选。
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

        # **工程里没有图层也照样能进**：弹窗里可以用「选择」从本地文件挑。
        # 只有「一个库都没有」才是真的导不进去。
        if not databases:
            self.messageBar.pushMessage(
                "导入失败：请先在「数据管理」Dock 添加本地数据库",
                level=Qgis.MessageLevel.Warning,
                duration=3,
            )
            return

        dlg = ImportLayerDialog(
            self,
            vector_layers=vector_layers,
            databases=databases,
            # 右键点的是哪个库就预选哪个；否则沿用「数据管理里当前选中的库」
            default_db=(
                default_db
                if isinstance(default_db, dict)
                else getattr(dock, "_current_db", None)
            ),
            lock_db=bool(locked),
        )
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        layers, db = dlg.result_payload()
        if not layers:
            return
        # 防御：图层对象可能已被别的入口销毁。后面要用 layer.name() 排重名表，
        # 直接把坏掉的 QObject 递进去会在槽里抛异常（进程直接没了，用户只看到
        # "点导入没反应、也没提示"）。坏掉的滤掉并明说，剩下照常导入。
        valid_layers: list[object] = []
        stale = 0
        for layer in layers:
            try:
                layer.name()
            except (AttributeError, RuntimeError):
                stale += 1
                continue
            valid_layers.append(layer)
        if stale:
            self.messageBar.pushMessage(
                f"{stale} 个待导入图层已失效（数据源可能已被关闭），已跳过",
                level=Qgis.MessageLevel.Warning,
                duration=6,
            )
        layers = valid_layers
        if not layers:
            return
        # 进度反馈：弹个可取消的无模态 QProgressDialog，用 QGIS 标准
        # QGIS 3.x / 4.x 都支持的 :class:`QgsFeedback` 联动。用户点取消
        # 调 ``feedback.cancel()`` → service 内部 ``isCanceled()`` 赶在
        # 下一次主循环迭代里跳出、回滚已建表。
        from qgis.core import QgsFeedback
        from qgis.PyQt.QtWidgets import QApplication, QProgressDialog

        from app.core.domain.import_layer import (
            _phase_text,
            unique_table_name,
        )

        db_path = str(db.get("path") or "")
        # 库里已有同名表时**自动按序号改名**（名称_1、名称_2…），不报错让用户改。
        # taken 记本批已经用掉的名字：同一次导入里的几层也要互相避让。
        planned: list[tuple[object, str | None]] = []
        taken: set[str] = set()
        for layer in layers:
            table_name: str | None = None
            if db.get("kind") == "local" and db_path:
                table_name = unique_table_name(db_path, layer.name(), taken=taken)
                taken.add(table_name)
            planned.append((layer, table_name))

        # **一次可导入多个图层**（每层一张新表）：进度按"第几层 / 共几层"推进
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

        current = ["", 0, len(layers)]  # 当前图层名 / 序号 / 总数

        # progressChanged 只报数字，文字阶段名由 _phase_text 按进度区间映射
        # （QgsFeedback 没有 progressTextChanged 这个信号，避免踩坑）。
        # QGIS 4.x 的 setProgress 参数实际是 double（emit 出来是 float），
        # QProgressDialog.setValue 强类型 int 必须显式转。
        def _on_progress_changed(value) -> None:
            progress.setValue(int(value))
            progress.setLabelText(
                f"正在导入图层「{current[0]}」（第 {current[1]} / {current[2]} 个）\n"
                f"{_phase_text(int(value))}"
            )

        feedback.progressChanged.connect(_on_progress_changed)

        imported: list[tuple[str, str, int]] = []  # (图层名, 表名, 条数)
        failures: list[tuple[str, str]] = []       # (图层名, 原因)
        try:
            for index, (layer, table_name) in enumerate(planned, start=1):
                if feedback.isCanceled():
                    break
                current[0], current[1] = layer.name(), index
                # 每份数据按**它自己的坐标系**入库（弹窗不再问目标坐标系）。
                # 图层没有有效 CRS 时退回工程 CRS；两个都没有就报出来。
                target_crs = ""
                try:
                    target_crs = layer.crs().authid()
                except RuntimeError:  # 图层已销毁
                    target_crs = ""
                if not target_crs:
                    target_crs = self._default_crs_authid()
                try:
                    result = import_vector_layer_to_db(
                        layer,
                        db,
                        target_crs,
                        feedback=feedback,
                        table_name=table_name,
                    )
                except ImportLayerError as exc:
                    # 单个图层失败不中断其余的：把原因记下来，最后一起报
                    logger.error("导入图层「%s」失败：%s", layer.name(), exc, exc_info=True)
                    failures.append((layer.name(), str(exc)))
                    feedback.setProgress(0)
                    continue
                except Exception as exc:  # noqa: BLE001 —— 意外错误也要报到汇总里
                    logger.error("导入图层「%s」异常：%s", layer.name(), exc, exc_info=True)
                    failures.append((layer.name(), f"{type(exc).__name__}: {exc}"))
                    feedback.setProgress(0)
                    continue
                imported.append((layer.name(), result.table_name, result.inserted))
                feedback.setProgress(0)
        finally:
            progress.close()
            # 关闭 QProgressDialog 后偶发关键事件未处理（PyQt5 / PyQt6 都需
            # 主动 pump），不调会留下「窗口隐藏但 C++ 对象未释放」的中间态
            # 导致下一个 dialog.exec() 阻塞。直接 processEvents 一下。
            QApplication.processEvents()

        if imported:
            brief = "、".join(
                (
                    f"「{table}」({count} 条)"
                    if table == layer
                    else f"「{table}」({count} 条，原名「{layer}」)"
                )
                for layer, table, count in imported
            )
            self.messageBar.pushMessage(
                f"已导入 {len(imported)} 个图层：{brief}",
                level=Qgis.MessageLevel.Success,
                duration=6,
            )
        if failures:
            detail = "；".join(f"「{name}」：{why}" for name, why in failures)
            self.messageBar.pushMessage(
                f"{len(failures)} 个图层没导进去：{detail}",
                level=Qgis.MessageLevel.Critical,
                duration=8,
            )
        # 落库后刷新右树：让新建表进入"本地数据库 → 该库 → 分类"列表
        if imported and dock is not None and db.get("kind") == "local":
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

    def _on_dm_import_data(self, db: dict, category: str, name: str) -> None:
        """数据管理图层右键「导入数据」：把数据源的要素**追加**进这个图层，或**替换**它。

        数据源可以是工程里的图层，也可以是电脑上的文件（GDB / 库会先列出里面的图层），
        而且**可以一次选多个**；导入方式在同一个弹窗里选（默认「追加」，不动原有数据）。
        字段按**同名匹配**、按目标表 SRID 自动投影，结果里把"没跟上的字段"如实报出来。
        """
        from app.core.domain.append_layer import AppendLayerError, import_features_into_table
        from app.ui.dialogs.create_data_fields_step import ExternalDatasetDialog

        path = str(db.get("path") or "")
        if not path or db.get("kind") != "local":
            self.messageBar.pushMessage(
                "导入数据：只支持本地库（GeoPackage / SpatiaLite）",
                level=Qgis.MessageLevel.Warning,
                duration=4,
            )
            return

        from qgis.core import QgsVectorLayer

        from app.core.util.local_layer_loader import open_local_vector_table
        from app.core.util.sublayers import geometry_kind_of_layer

        vector_layers = [
            lyr for lyr in self.project.mapLayers().values()
            if isinstance(lyr, QgsVectorLayer)
        ]
        # 只列与目标图层**同几何类型**的数据源：往面图层里塞点数据没有意义
        # （库那边也会按列类型拒掉）。类型从目标图层本身读。
        target_layer, target_error = open_local_vector_table(path, name)
        if target_layer is None:
            self.messageBar.pushMessage(
                f"导入失败：打不开目标图层「{name}」\n{target_error}",
                level=Qgis.MessageLevel.Warning,
                duration=5,
            )
            return
        geometry_kind = geometry_kind_of_layer(target_layer)
        dialog = ExternalDatasetDialog(
            self,
            vector_layers=vector_layers,
            geometry_kind=geometry_kind,
            multi=True,
            allow_replace=True,
        )
        accepted = dialog.exec()
        sources, error = dialog.resolve_all() if accepted else ([], "")
        replace = accepted and dialog.mode() == "replace"
        dialog.deleteLater()
        if error:
            # 部分失败：能打开的照常导入，打不开的在消息条里说清楚（别静默少几份）
            self.messageBar.pushMessage(error, level=Qgis.MessageLevel.Warning, duration=6)
        if not sources:
            return

        target_label = f"{os.path.basename(path)} · 「{name}」"
        if replace:
            existing = target_layer.featureCount()
            existing_text = f"{existing} 个要素" if existing >= 0 else "全部要素"
            message = (
                f"将**清空** {target_label} 现有的 {existing_text}，\n"
                f"然后写入 {len(sources)} 份数据里的要素。\n\n"
                "此操作不可撤销，确定替换？"
            )
        else:
            message = (
                f"把 {len(sources)} 份数据的要素追加到\n{target_label}？\n\n"
                "字段按同名匹配，坐标系会自动转换；原有要素不受影响。"
            )
        ret = QMessageBox.question(
            self,
            "导入数据（替换）" if replace else "导入数据（追加）",
            message,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No if replace else QMessageBox.StandardButton.Yes,
        )
        if ret != QMessageBox.StandardButton.Yes:
            return

        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            result = import_features_into_table(sources, path, name, replace=replace)
        except AppendLayerError as exc:
            logger.warning("导入数据失败：%s", exc)
            self.messageBar.pushMessage(
                f"导入失败：{exc}", level=Qgis.MessageLevel.Critical, duration=8
            )
            return
        except Exception as exc:  # noqa: BLE001 —— 任何意外都要让用户看到，不能静默
            logger.error("导入数据异常：%s", exc, exc_info=True)
            self.messageBar.pushMessage(
                f"导入失败（{type(exc).__name__}）：{exc}",
                level=Qgis.MessageLevel.Critical,
                duration=8,
            )
            return
        finally:
            QApplication.restoreOverrideCursor()

        note = f"已{'替换为' if replace else '追加'} {result.inserted} 个要素（「{name}」）"
        if replace:
            note += f"；原有 {result.replaced} 个要素已清空"
        if result.skipped_fields:
            note += f"；目标表没有这些字段、未导入：{'、'.join(result.skipped_fields)}"
        if result.skipped_multipart:
            note += (
                f"；{result.skipped_multipart} 个多部件要素被跳过"
                "（目标表是单部件类型，转换会丢部件）"
            )
        if result.failed_sources:
            failed = "；".join(f"{src}：{why}" for src, why in result.failed_sources)
            note += f"；这些数据源没导入：{failed}"
        self.messageBar.pushMessage(note, level=Qgis.MessageLevel.Info, duration=8)
        # 行数变了：让 Dock 重扫这个库，左树 / 右表的计数跟着更新
        dock = getattr(self, "_data_manage_dock", None)
        if dock is not None:
            live = self._dock_db_entry(dock, path)
            if live is not None:
                dock.refresh_database(live)

    def _on_dm_export_layer(self, db: dict, category: str, name: str) -> None:
        """数据管理图层右键「导出数据」：导出成 shp / gdb / gpkg / sqlite，可带条件筛选。"""
        from app.core.domain.export_layer import ExportLayerError, export_vector_layer
        from app.core.util.local_layer_loader import open_local_vector_table
        from app.ui.dialogs.export_layer_dialog import ExportLayerDialog

        path = str(db.get("path") or "")
        if not path or db.get("kind") != "local":
            self.messageBar.pushMessage(
                "导出数据：只支持本地库（GeoPackage / SpatiaLite）",
                level=Qgis.MessageLevel.Warning,
                duration=4,
            )
            return
        layer, error = open_local_vector_table(path, name)
        if layer is None:
            self.messageBar.pushMessage(
                f"导出失败：打不开「{name}」\n{error}",
                level=Qgis.MessageLevel.Warning,
                duration=5,
            )
            return
        dialog = ExportLayerDialog(self, layer=layer, default_dir=default_output_dir())
        accepted = dialog.exec()
        spec = dialog.spec() if accepted else None
        dialog.deleteLater()
        if spec is None:
            return
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            target = export_vector_layer(layer, spec)
        except ExportLayerError as exc:
            logger.warning("导出数据失败：%s", exc)
            self.messageBar.pushMessage(
                f"导出失败：{exc}", level=Qgis.MessageLevel.Critical, duration=6
            )
            return
        except Exception as exc:  # noqa: BLE001 —— 任何意外都要让用户看到，不能静默
            logger.error("导出数据异常：%s", exc, exc_info=True)
            self.messageBar.pushMessage(
                f"导出失败（{type(exc).__name__}）：{exc}",
                level=Qgis.MessageLevel.Critical,
                duration=8,
            )
            return
        finally:
            QApplication.restoreOverrideCursor()
        self.messageBar.pushMessage(
            f"已导出：{target}", level=Qgis.MessageLevel.Info, duration=6
        )

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

    def _on_dm_remove_all_layers(self, db: dict) -> None:
        """数据管理左树右键库连接「移除全部图层」：一次移除该库里注册的所有图层。

        与「删除图层」同一口径：先把工程里指向这些表的图层移出（释放文件句柄 /
        gpkg 别名），再批量删库内数据表与 ``user_relation`` 注册行。删除不可恢复，
        所以**先弹一次确认**，把要删的图层数写清楚。

        单张表删除失败不会中断整批（见 :func:`delete_all_registered_data`）：
        最后把"删了几个、哪几个没删掉"一起报出来，不留静默的半成品状态。
        """
        path = str(db.get("path") or "")
        if db.get("kind") == "server":
            QMessageBox.information(
                self,
                "移除全部图层",
                "服务器库删除尚未实现，请选用本地库（SpatiaLite / GeoPackage）。",
            )
            return
        if not path:
            QMessageBox.warning(
                self, "移除全部图层", "这个库缺少文件路径，无法移除图层。"
            )
            return

        dock = getattr(self, "_data_manage_dock", None)
        # 右键拿到的常常是树节点 payload 的副本；换成 Dock 缓存里的原件，
        # 批量删完 refresh_database 才能把新清单写回同一个对象。
        live = self._dock_db_entry(dock, path) if dock is not None else None
        source = live if live is not None else db
        categories = source.get("categories") or {}
        targets = [
            (str(category), str(name))
            for category, names in categories.items()
            for name in (names or [])
            if name
        ]
        if not targets:
            self.messageBar.pushMessage(
                "这个库里没有已注册的图层",
                level=Qgis.MessageLevel.Info,
                duration=3,
            )
            return

        name = str(source.get("name") or os.path.basename(path))
        ret = QMessageBox.question(
            self,
            "移除全部图层",
            f"确定移除本地库「{name}」里的全部 {len(targets)} 个图层吗？\n\n"
            "将删除库内数据表与注册信息，并把工程中已打开的对应图层移出地图。\n"
            "此操作不可恢复。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if ret != QMessageBox.StandardButton.Yes:
            return

        # 1) 先移出工程里指向这些表的图层（释放文件句柄 / gpkg 别名）
        removed_layers = 0
        seen: set[int] = set()
        for _category, table in targets:
            for layer in self._layers_backed_by(path, table):
                if id(layer) in seen:
                    continue
                seen.add(id(layer))
                self._remove_layer(layer)
                removed_layers += 1

        # 2) 删库内数据表 + user_relation 注册行（单表失败不中断整批）
        try:
            deleted, failures = delete_all_registered_data(source)
        except DeleteDataError as exc:
            logger.error("移除全部图层失败：%s", exc, exc_info=True)
            self.messageBar.pushMessage(
                f"移除全部图层失败：{exc}",
                level=Qgis.MessageLevel.Critical,
                duration=8,
            )
            return
        except Exception as exc:  # noqa: BLE001 —— 槽里抛异常会带走进程
            logger.error("移除全部图层异常：%s", exc, exc_info=True)
            self.messageBar.pushMessage(
                f"移除全部图层失败（{type(exc).__name__}）：{exc}",
                level=Qgis.MessageLevel.Critical,
                duration=8,
            )
            return

        # 3) 重扫该库：左树 / 右表的清单与计数跟着更新（失败的表也重扫回来）
        if dock is not None and live is not None:
            dock.refresh_database(live)

        layer_suffix = f"，并移出工程 {removed_layers} 个图层" if removed_layers else ""
        if failures:
            detail = "；".join(f"「{table}」：{why}" for _c, table, why in failures)
            self.messageBar.pushMessage(
                f"已移除 {deleted} 个图层{layer_suffix}；"
                f"{len(failures)} 个没删掉：{detail}",
                level=Qgis.MessageLevel.Warning,
                duration=10,
            )
            return
        logger.info(
            "已移除本地库 %s 的全部 %d 个图层%s", path, deleted, layer_suffix
        )
        self.messageBar.pushMessage(
            f"已移除「{name}」的全部 {deleted} 个图层{layer_suffix}",
            level=Qgis.MessageLevel.Success,
            duration=5,
        )

    def _layers_backed_by(self, db_path: str, table: str) -> list:
        """工程里数据源指向「``db_path`` 库中的 ``table``」的图层（删除时一并移出）。

        两条线索都命中才算同一份数据（都走
        :mod:`app.core.util.local_layer_loader` 里的数据源解析 —— SpatiaLite
        图层的源是 ``dbname='…'`` URI，别再自己按 ``|`` 切）：

        - **容器路径**：加载时挂在图层上的 ``layer.path_c``，没有则从数据源里取
          （见 :func:`layer_source_container`）；
        - **表名**：数据源里的子图层名（见 :func:`layer_source_table`），栅格等
          没有该概念的回落图层显示名（本地库图层加载时就是用表名命名的）。

        路径比较先按规范化字符串（大小写不敏感），再退 ``os.path.samefile``
        —— 老 ``.db`` 后缀 gpkg 库的图层数据源指向同 inode 的 ``.gpkg`` 硬
        链接别名，字符串对不上但其实是同一个文件。
        """
        out: list = []
        for layer in self.project.mapLayers().values():
            try:
                layer_name = layer.name()
                container = layer_source_container(layer)
            except RuntimeError:  # C++ 对象已销毁（极端时序），跳过
                continue
            if not self._same_db_path(container, db_path):
                continue
            src_table = layer_source_table(layer)
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
