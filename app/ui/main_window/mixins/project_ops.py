"""工程操作。

新建 / 保存为工程 / 打开工程、工程名询问、图层加入工程分组，以及地图配置
（app.yaml）的读取与基础底图装载。保存为工程时额外记录画布当前显示范围，
下次打开自动恢复。关闭应用或打开其它工程前询问是否保存当前工程：已是工程
文件（打开过 / 另存过）则直接更新该文件，否则走“保存为工程”另存。

由 :class:`~app.ui.main_window.main_window.MainWindow` 多继承组合，方法只依赖
``self`` 上的共享状态。
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

from qgis.core import (
    Qgis,
    QgsCoordinateReferenceSystem,
    QgsLayerTreeGroup,
    QgsReadWriteContext,
    QgsVectorLayer,
)
from qgis.PyQt.QtCore import QTimer
from qgis.PyQt.QtWidgets import QDialog
from qgis.PyQt.QtXml import QDomDocument

from app.core.domain.basemap import add_base_tile_layer
from app.env.config import AppConfig
from app.infra.fs.paths import Paths
from app.infra.logging import get_logger
from app.ui.common.message_box import MessageBox
from app.ui.common.recent_projects import add_recent
from app.ui.common.widgets import save_file
from app.ui.dialogs.startup_project import StartupProjectDialog
from app.ui.docks.project_dock import ProjectDock

logger = get_logger(__name__)

#: 工程文件的保存 / 打开过滤器
_PROJECT_FILTER = "工程文件 (*.pgs);;所有文件 (*)"

#: 默认坐标系兜底（配置读不到时用）：CGCS2000 / 中国 2000，见 :meth:`ProjectOpsMixin._default_crs_authid`
_DEFAULT_CRS_AUTHID = "EPSG:4490"


class ProjectOpsMixin:
    """ProjectOpsMixin：见模块 docstring。"""

    #: 工程内容自上次保存 / 打开 / 新建后是否发生过改动。关闭时据此决定是否
    #: 询问保存；由 :meth:`_init_project_dirty_tracking` 挂的信号维护。
    _project_modified: bool = False

    #: 上次干净基线时各图层的配置 XML 指纹（图层 id → sha1；取不到为 None）。
    #: 标注 / 表达式 / 子集串等改动在 QGIS 4.2 里不发任何信号，只能靠指纹补判，
    #: 见 :meth:`_layer_state_fingerprint`。
    _layer_state_hashes: dict[str, str | None] = {}

    #: 自上次保存 / 打开 / 新建以来画布范围是否变过。视图范围会随工程保存，
    #: 用户平移 / 缩放后退出也要询问；窗口 / 停靠面板尺寸变化不算。
    _view_modified: bool = False

    #: 上次记录时的画布尺寸（宽, 高）。用于把“尺寸变化引起的范围重算”与
    #: 用户平移 / 缩放区分开。
    _last_canvas_size: tuple[int, int] | None = None

    #: 上次记录时的视图中心（地图坐标）。中心变了必是用户平移 / 定位；
    #: 只有比例尺变化时才需要结合画布尺寸判断是缩放还是窗口尺寸变化。
    _last_view_center: tuple[float, float] | None = None

    # ------------------------------------------------------------ 脏标记

    def _init_project_dirty_tracking(self) -> None:
        """挂工程 / 图层树 / 图层 / 画布范围信号，维护未保存标记。

        **不能只看 ``QgsProject.isDirty()``**：实测 QGIS 4.2 里加 / 删图层、
        切换可见性、改样式 / 透明度都不会把它置脏（只有标题、CRS 等少数信号
        会），拿它判断会在用户改完内容后静默关闭丢改动。这里把会写进工程文件
        的内容信号统一转成自己的标志；保存 / 打开 / 新建完成后再复位。画布
        范围单独记在 :attr:`_view_modified`（视图范围随工程保存）。
        """
        self._project_modified = False
        #: 已挂过信号的实际图层 id（避免重复连接）；图层移除后丢弃
        self._watched_layer_ids: set[str] = set()
        self._layer_state_hashes = {}
        self._view_modified = False
        self._last_canvas_size = None
        self._last_view_center = None
        project = self.project
        # 画布范围：平移 / 缩放会随工程保存，属于用户改动。挂在这里（而不是
        # shell 的视图历史槽）是为了同时管脏标记；尺寸变化由槽内过滤。
        canvas = getattr(self, "canvas", None)
        if canvas is not None:
            canvas.extentsChanged.connect(self._on_canvas_view_changed)
        # 工程级属性（不包含数据编辑信号：要素改动落在数据源里，不需要保存工程）
        for name in (
            "crsChanged",
            "crs3DChanged",
            "verticalCrsChanged",
            "titleChanged",
            "homePathChanged",
            "metadataChanged",
            "snappingConfigChanged",
            "avoidIntersectionsModeChanged",
            "customVariablesChanged",
            "ellipsoidChanged",
            "distanceUnitsChanged",
            "areaUnitsChanged",
            "scaleMethodChanged",
            "transformContextChanged",
            "transactionModeChanged",
            "topologicalEditingChanged",
            "avoidIntersectionsLayersChanged",
            "labelingEngineSettingsChanged",
            "projectColorsChanged",
            "backgroundColorChanged",
            "selectionColorChanged",
            "mapScalesChanged",
            "nonIdentifiableLayersChanged",
        ):
            signal = getattr(project, name, None)
            if signal is not None:
                signal.connect(self._mark_project_modified)
        project.layersAdded.connect(self._on_project_layers_added)
        project.layersRemoved.connect(self._on_project_layers_removed)
        # QGIS 自己的 dirty 标记也并入（兜住本类没逐个列出的工程级改动）
        project.isDirtyChanged.connect(self._on_project_is_dirty_changed)

        # 图层树：增删 / 移动 / 改名 / 显隐 / 节点自定义属性都会写进工程文件
        root = project.layerTreeRoot()
        for name in (
            "addedChildren",
            "removedChildren",
            "visibilityChanged",
            "nameChanged",
            "customPropertyChanged",
        ):
            signal = getattr(root, name, None)
            if signal is not None:
                signal.connect(self._mark_project_modified)

        for layer in project.mapLayers().values():
            self._watch_project_layer(layer)
        self._mark_project_clean()

    def _mark_project_modified(self, *_args) -> None:
        """标记工程内容已改动（信号槽，参数形态随信号不同而忽略）。"""
        self._project_modified = True

    def _on_project_is_dirty_changed(self, dirty: bool) -> None:
        if dirty:
            self._project_modified = True

    def _mark_project_clean(self) -> None:
        """把当前状态记为“已保存 / 刚打开”的基线，关闭时不再询问保存。"""
        self._project_modified = False
        self._capture_layer_state_baseline()
        self._mark_view_clean()
        try:
            self.project.setDirty(False)
        except (AttributeError, RuntimeError):
            pass

    def _mark_view_clean(self) -> None:
        """只复位“画布范围改过”标记，并记下当前画布尺寸 / 视图中心。

        程序化定位（启动底图、新建工程自动缩放）会触发 ``extentsChanged``；
        定位完成后调这里，避免用户什么都没动也被问是否保存。
        """
        self._view_modified = False
        try:
            self._last_canvas_size = (self.canvas.width(), self.canvas.height())
            extent = self.canvas.extent()
            center = extent.center()
            self._last_view_center = (
                None if extent.isNull() or extent.isEmpty()
                else (center.x(), center.y())
            )
        except (AttributeError, RuntimeError):
            self._last_canvas_size = None
            self._last_view_center = None

    def _on_canvas_view_changed(self, *_args) -> None:
        """画布范围变化 → 工程有未保存改动（视图范围会写进工程文件）。

        **窗口 / 停靠面板尺寸变化不算**：尺寸一变，QGIS 会按宽高比重算范围，
        这只是布局结果，不是用户平移 / 缩放。判断顺序：

        - 中心变了 → 平移 / 定位，算改动（即使同时发生了尺寸变化）；
        - 中心没变、尺寸变了 → 布局重算，更新尺寸基线后跳过；
        - 中心、尺寸都没变 → 原地缩放，算改动。
        """
        try:
            extent = self.canvas.extent()
        except RuntimeError:  # 画布已销毁
            return
        if extent.isNull() or extent.isEmpty():
            return
        center = extent.center()
        width = max(abs(extent.width()), 1e-12)
        height = max(abs(extent.height()), 1e-12)
        last_center = getattr(self, "_last_view_center", None)
        center_moved = last_center is not None and (
            abs(center.x() - last_center[0]) > width * 1e-9
            or abs(center.y() - last_center[1]) > height * 1e-9
        )
        size = (self.canvas.width(), self.canvas.height())
        size_changed = size != getattr(self, "_last_canvas_size", None)
        if size_changed:
            self._last_canvas_size = size
        if center_moved:
            self._view_modified = True
        elif not size_changed:
            self._view_modified = True

    def _capture_layer_state_baseline(self) -> None:
        """记录当前各图层配置的 XML 指纹，作为关闭时的对照基线。"""
        baseline: dict[str, str | None] = {}
        try:
            layers = self.project.mapLayers().values()
        except RuntimeError:  # 工程已销毁
            layers = ()
        for layer in layers:
            baseline[layer.id()] = self._layer_state_fingerprint(layer)
        self._layer_state_hashes = baseline

    def _layer_state_fingerprint(self, layer) -> str | None:
        """图层写入工程文件时的 XML 指纹（含标注 / 表达式 / 子集串等）。

        **为什么还要指纹**：本构建的 ``setLabeling`` / ``setLabelsEnabled``
        不发任何图层信号（见 ``layer_dock`` 里的同名实测说明），只挂信号会漏掉
        “改完标注直接退出”的情况；这里取图层真正落盘的那段 XML，能覆盖所有
        图层级配置。取不到指纹（图层已销毁 / 写入失败）返回 ``None``，不影响
        信号判定。
        """
        try:
            document = QDomDocument()
            node = document.createElement("maplayer")
            document.appendChild(node)
            context = QgsReadWriteContext()
            context.setPathResolver(self.project.pathResolver())
            if not layer.writeXml(node, document, context):
                return None
            text = document.toString()
        except (AttributeError, RuntimeError, TypeError):
            return None
        return hashlib.sha1(text.encode("utf-8")).hexdigest()

    def _on_project_layers_added(self, layers) -> None:
        self._mark_project_modified()
        for layer in layers:
            self._watch_project_layer(layer)

    def _on_project_layers_removed(self, layer_ids) -> None:
        self._mark_project_modified()
        for layer_id in layer_ids:
            self._watched_layer_ids.discard(layer_id)

    def _watch_project_layer(self, layer) -> None:
        """给图层挂“配置 / 样式变化”信号（图层增删由工程信号统一处理）。"""
        if layer is None:
            return
        layer_id = layer.id()
        if layer_id in self._watched_layer_ids:
            return
        self._watched_layer_ids.add(layer_id)
        for name in (
            "nameChanged",
            "crsChanged",
            "crs3DChanged",
            "verticalCrsChanged",
            "blendModeChanged",
            "opacityChanged",
            "rendererChanged",
            "styleChanged",
            "legendChanged",
            "configChanged",
            "dataSourceChanged",
            "metadataChanged",
            "customPropertyChanged",
            "displayExpressionChanged",
            "editFormConfigChanged",
            "subsetStringChanged",
            "flagsChanged",
            "readOnlyChanged",
            "renderer3DChanged",
            "mapTipTemplateChanged",
            "mapTipsEnabledChanged",
            "autoRefreshIntervalChanged",
        ):
            signal = getattr(layer, name, None)
            if signal is not None:
                signal.connect(self._mark_project_modified)

    def _project_has_unsaved_changes(self) -> bool:
        """工程内容相对上次保存 / 打开 / 新建是否有改动。"""
        if getattr(self, "_project_modified", False):
            return True
        if getattr(self, "_view_modified", False):
            return True
        try:
            if self.project.isDirty():
                return True
        except RuntimeError:  # 工程已销毁
            return False
        return self._layer_states_changed()

    def _layer_states_changed(self) -> bool:
        """对照干净基线，检查是否有图层配置变化没被信号捕获（如标注）。"""
        baseline = getattr(self, "_layer_state_hashes", None)
        if not baseline:
            return False
        try:
            layers = self.project.mapLayers()
        except RuntimeError:
            return False
        if set(layers) != set(baseline):
            # 增删图层本应已由 layersAdded / layersRemoved 置脏；这里兜底
            return True
        for layer_id, layer in layers.items():
            expected = baseline.get(layer_id)
            if not expected:
                continue
            current = self._layer_state_fingerprint(layer)
            if current is not None and current != expected:
                return True
        return False

    def _on_new_project(self) -> None:
        """新建工程：先弹窗输入工程名，创建后把名字带到右侧工程 Dock 显示。"""
        # 已有图层时先确认清空（避免输入完名字又取消）
        if self.project.mapLayers():
            ret = MessageBox.question(
                self,
                "新建工程",
                "将清空当前所有图层并新建工程，是否继续？",
                MessageBox.StandardButton.Yes | MessageBox.StandardButton.No,
                MessageBox.StandardButton.No,
            )
            if ret != MessageBox.StandardButton.Yes:
                return
        name = self._ask_project_name()
        if name is None:
            return
        # 确保 Dock 存在
        if getattr(self, "project_dock", None) is None:
            self.project_dock = ProjectDock(self)
            self.project_dock.render_requested.connect(self._show_render_tab)
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
            MessageBox.warning(self, "提示", "工程名不能为空")

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

        # 工程名 + 坐标系（project.clear() 会把 CRS 重置回默认）。
        # **画布一起设**：工程 CRS 与画布 destinationCrs 是两份状态，只改前者
        # 会让画布继续按旧 CRS 出图（用户在状态栏切过 CRS 再新建工程就会撞上）。
        self.project.setTitle(name)
        self.setWindowTitle(f"{self._app_name} - {name}")
        project_crs = QgsCoordinateReferenceSystem(self._default_crs_authid())
        self.project.setCrs(project_crs)
        self.canvas.setDestinationCrs(project_crs)

        # 新建工程 ⇒ 尚未关联工程文件，关闭/保存时需另存
        self._project_path = None
        # 注意：**数据管理的连接清单不跟着工程走**（它是系统级配置，存在
        # app.infra.fs.state 的状态文件里），所以新建工程不该清它。

        # 底图进参考分组
        self._load_config_base_tile_into_ref_group()

        self.project_dock.set_project(name, self._data_group, self._ref_group)
        self.canvas.refreshAllLayers()
        # 新建工程里的清空 / 建组 / 自动加载底图都是程序化初始化，不算用户改动：
        # 复位基线，避免用户什么都没做、退出时也弹“是否保存工程”。
        self._mark_project_clean()
        self.messageBar.pushMessage(f"已创建工程：{name}", level=Qgis.MessageLevel.Info, duration=3)

    def _add_layer_to_project(self, layer) -> None:
        """把图层加进项目：默认路由到"数据图层"分组（未创建工程时平铺挂根）。

        用 ``insertLayer(0, ...)`` 把新图层插到分组最顶层——QGIS 面板顶部即渲染
        最上层，后加的图层直接盖在最上面。

        **空工程的第一层顺手定位一下**：一个图层都没有时画布停在默认范围、看不出
        到底加没加上。已经有图层了就**绝不动视图**——用户正看着哪就是哪，要定位
        自己右键 →「缩放至图层」。
        """
        first_layer = not self.project.mapLayers()
        self.project.addMapLayer(layer, False)
        group = getattr(self, "_data_group", None) or self.project.layerTreeRoot()
        group.insertLayer(0, layer)
        # 所有用户导入（打开文件 / 浏览器双击 / 拖拽）都走这里，统一记日志
        src = getattr(layer, "path_c", None) or layer.source()
        logger.info("导入图层：%s（来源=%s）", layer.name(), src)
        if first_layer:
            # **推迟一拍**：图层树桥接是把新图层**异步**同步进画布的，当场 setExtent
            # 会被随后的同步覆盖。singleShot 也顺带满足仓库约束（不在 Qt 事件栈内
            # 直接动画布，见 swipe.py 的约定）。
            QTimer.singleShot(0, lambda lyr=layer: self._zoom_to_first_layer(lyr))

    def _zoom_to_first_layer(self, layer, *, mark_view_clean: bool = False) -> None:
        """空工程加第一个图层 / 新建工程加载默认底图时，把视图定位到它。

        **走 :meth:`_zoom_to_layer`**（带 CRS 变换）：默认底图是 EPSG:3857 的 XYZ
        瓦片、画布是 EPSG:4490，直接拿它的原始范围 ``setExtent`` 会把视图甩到
        几十万米外。图层已销毁 / 无效则安静跳过。

        :param mark_view_clean: 本次定位是程序化初始化（新建工程的默认底图），
            定位后把视图记成干净基线，用户直接退出不应被问是否保存。
        """
        try:
            if layer is None or not layer.isValid() or layer.extent().isEmpty():
                return
            self._zoom_to_layer(layer)
        except RuntimeError:  # 图层或画布已销毁（收尾流程）
            pass
        finally:
            if mark_view_clean:
                self._mark_view_clean()

    def _load_config_base_tile_into_ref_group(self) -> None:
        """加载配置底图并移入"参考图层"分组（底图已平铺挂根，需移动）。

        **顺手定位到它**：新建工程的第一步是 ``project.clear()``，画布范围会被清成
        空；以前这一步靠 ``QgsLayerTreeMapCanvasBridge`` 的"自动缩放到第一个图层"
        兜住，那个开关已按用户要求关掉（否则加数据时视图会乱跳），所以这里显式补
        一次 —— 不然新建完工程是一片空白画布。
        """
        layer = self._load_config_base_tile()
        if layer is not None and getattr(self, "_ref_group", None) is not None:
            self._move_layer_to_group(layer, self._ref_group)
        if layer is not None:
            # 推迟一拍：图层树桥接是异步把图层同步进画布的。定位完复位视图基线：
            # 这是新建工程的程序化初始化，不是用户平移 / 缩放。
            QTimer.singleShot(
                0,
                lambda lyr=layer: self._zoom_to_first_layer(
                    lyr, mark_view_clean=True
                ),
            )

    # ------------------------------------------------------------ 保存为工程

    def _save_project_to(self, path: str) -> bool:
        """把当前工程写入 ``path``（已存在的工程文件即“更新”）。返回是否成功。

        QGIS 工程天然序列化图层树、数据源、渲染器与标注设置（本工程文件后缀
        ``.pgs``，内容即 QGIS 工程 XML）；画布当前显示范围额外写入 “工程视图
        设置”，下次打开时由 :meth:`_restore_canvas_extent` 恢复。
        """
        # 把画布当前显示范围写进工程视图设置，供下次打开恢复
        self._store_canvas_extent()
        self._begin_layer_loading(f"正在保存工程：{os.path.basename(path)}")
        try:
            saved = self.project.write(path)
        except Exception as exc:  # noqa: BLE001
            logger.warning("保存工程失败：%s", exc, exc_info=True)
            MessageBox.warning(self, "保存失败", f"无法保存工程：{path}\n{exc}")
            return False
        finally:
            self._end_layer_loading()

        if not saved:
            MessageBox.warning(self, "保存失败", f"无法保存工程：{path}")
            return False
        title = self.project.title() or Path(path).stem
        self.project.setTitle(title)
        self.setWindowTitle(f"{self._app_name} - {title}")
        # 记住当前工程文件：下次保存 / 关闭时直接更新该文件，不再弹另存
        self._project_path = path
        # 保存成功 ⇒ 当前内容就是“已保存”基线；紧接着退出不应再提示保存
        self._mark_project_clean()
        logger.info("保存工程：%s", path)
        add_recent(path)
        self.messageBar.pushMessage(
            f"已保存工程：{path}", level=Qgis.MessageLevel.Info, duration=3
        )
        return True

    def _on_save_project_as(self) -> bool:
        """另存为工程：选路径后写入。返回是否已保存。"""
        if not self.project.mapLayers():
            MessageBox.information(self, "提示", "当前没有可保存的图层")
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

    def _confirm_slicing_on_close(self) -> bool:
        """影像切片进行中时先确认：关闭程序会终止这些切片。

        - 没有切片在跑 / 提示被禁用（``prompt_slices_on_close=False``）→ ``True``；
        - 「终止切片并退出」→ ``True``（真正的取消在 ``closeEvent`` 里做）；
        - 「取消」→ ``False``，调用方应 ``event.ignore()`` 中止关闭。

        必须排在「是否保存工程」之前：否则用户先答完保存、再被告知切片会
        中止，体验上是白回答一轮。
        """
        if not getattr(self, "prompt_slices_on_close", True):
            return True
        try:
            from app.core.domain.imagery_tiles.active_tasks import (  # noqa: PLC0415
                active_descriptions,
            )

            running = active_descriptions()
        except Exception:  # noqa: BLE001 - 提示失败不能挡住关闭流程
            logger.debug("读取影像切片任务状态失败", exc_info=True)
            return True
        if not running:
            return True
        listed = "\n".join(f"· {text}" for text in running[:5])
        if len(running) > 5:
            listed += f"\n…另有 {len(running) - 5} 个任务"
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("关闭应用")
        box.setText(f"还有 {len(running)} 个影像切片任务正在进行。")
        box.setInformativeText(
            "关闭程序会终止这些切片，尚未生成的瓦片不会继续生成"
            f"（已生成的瓦片保留在输出目录）。\n\n{listed}"
        )
        stop_btn = box.addButton("终止切片并退出", QMessageBox.ButtonRole.DestructiveRole)
        cancel_btn = box.addButton("取消", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(cancel_btn)
        box.exec()
        return box.clickedButton() is stop_btn

    def _confirm_editing_layers_on_close(self) -> bool:
        """关闭前逐个询问：处于编辑态且**有未提交改动**的图层是否保存。

        **必须排在"是否保存工程"之前**：保存工程只写 ``.qgs``，**不会**提交图层
        编辑缓冲里的改动 —— 先过这一轮，用户选保存才真的落盘。

        - 「保存」→ 提交该图层（``commitChanges(stopEditing=True)``）；
        - 「放弃」→ 回滚并停止编辑（丢弃缓冲里的改动）；
        - 「取消」→ 返回 ``False``，调用方应 ``event.ignore()`` 中止关闭；
        - 提交失败也返回 ``False``：宁可让用户看到错误，也不能静悄悄关掉丢数据。

        与 :meth:`_confirm_save_project_on_close` 共用 ``prompt_save_on_close``
        开关（无头 / 测试场景置 ``False``，避免模态弹窗阻塞）。
        """
        if not getattr(self, "prompt_save_on_close", True):
            return True
        dirty = [
            lyr
            for lyr in self.project.mapLayers().values()
            if isinstance(lyr, QgsVectorLayer) and lyr.isEditable() and lyr.isModified()
        ]
        for layer in dirty:
            ret = MessageBox.question(
                self,
                "关闭应用",
                f"图层「{layer.name()}」有未保存的修改，是否保存？",
                MessageBox.StandardButton.Save
                | MessageBox.StandardButton.Discard
                | MessageBox.StandardButton.Cancel,
                MessageBox.StandardButton.Save,
            )
            if ret == MessageBox.StandardButton.Cancel:
                return False
            if ret == MessageBox.StandardButton.Save:
                if not self.editing_service.stop(layer, save=True):
                    logger.error("关闭前保存图层「%s」失败，中止关闭", layer.name())
                    return False
            else:  # 放弃：回滚 + 停止编辑
                self.editing_service.stop(layer, save=False)
        return True

    def _confirm_save_project_on_close(self) -> bool:
        """关闭应用前询问是否保存工程；返回 ``True`` 表示可以关闭。

        - 无图层 / 提示被禁用（``prompt_save_on_close=False``）→ 直接可关闭；
        - **自上次保存 / 打开 / 新建后没有内容改动** → 直接关闭，不弹窗；
        - 「是（保存）」→ 已有工程文件则更新，否则走“保存为工程”；保存失败即
          不关闭（返回 ``False``）；
        - 「否（不保存）」→ 直接关闭；
        - 「取消」→ 返回 ``False``，调用方应 ``event.ignore()``。
        """
        if not getattr(self, "prompt_save_on_close", True):
            return True
        if not self.project.mapLayers():
            return True
        # 没有实际改动（或刚保存过）就不要打断用户；地图范围变化算改动，
        # 因为它会随工程保存（窗口尺寸变化不算，见 _on_canvas_view_changed）
        if not self._project_has_unsaved_changes():
            return True
        path = getattr(self, "_project_path", None)
        hint = f"将保存到：{path}" if path else "当前工程尚未保存到文件。"
        ret = MessageBox.question(
            self,
            "关闭应用",
            f"是否保存当前工程？\n{hint}",
            MessageBox.StandardButton.Yes
            | MessageBox.StandardButton.No
            | MessageBox.StandardButton.Cancel,
            MessageBox.StandardButton.Yes,
        )
        if ret == MessageBox.StandardButton.Cancel:
            return False
        if ret == MessageBox.StandardButton.Yes:
            return self._save_current_project()
        return True  # 否（不保存）

    def _has_open_project(self) -> bool:
        """当前是否已经存在工程（新建 / 打开 / 另存过）。

        启动时只加载了配置底图，此时 ``project.title()`` 为空、没有
        ``_project_path``、也没有「数据图层」分组，不算已有工程；用户从启动
        对话框打开工程时不应被“是否保存”打扰。
        """
        if getattr(self, "_project_path", None):
            return True
        if self.project.title():
            return True
        return getattr(self, "_data_group", None) is not None

    def _has_data(self) -> bool:
        """当前工程里是否有用户数据（排除启动时自动加载的配置底图）。

        没有新建 / 打开工程时也可以直接往地图里加数据，这时虽然
        :meth:`_has_open_project` 为假，但打开其它工程同样会丢掉这些数据，
        因此也要走保存确认。启动底图单独记了 id（见 bootstrap），不算数据。
        """
        startup_id = getattr(self, "_startup_base_layer_id", None)
        for layer in self.project.mapLayers().values():
            if startup_id is None or layer.id() != startup_id:
                return True
        return False

    def _confirm_save_project_before_open(self) -> bool:
        """打开其它工程前询问是否保存当前工程；返回 ``True`` 表示继续打开。

        - 没有工程且没有数据 / 没有图层 / 提示被禁用 → 直接继续；
        - 「是（保存）」→ 已有工程文件则更新，否则走“保存为工程”；保存失败或
          用户在另存对话框中取消即中止打开（返回 ``False``）；
        - 「否（不保存）」→ 放弃当前工程，继续打开目标工程；
        - 「取消」→ 返回 ``False``，不打开目标工程。
        """
        if not getattr(self, "prompt_save_on_close", True):
            return True
        if not self.project.mapLayers():
            return True
        if not self._has_open_project() and not self._has_data():
            return True
        path = getattr(self, "_project_path", None)
        hint = f"将保存到：{path}" if path else "当前工程尚未保存到文件。"
        ret = MessageBox.question(
            self,
            "打开工程",
            f"是否保存当前工程？\n{hint}",
            MessageBox.StandardButton.Yes
            | MessageBox.StandardButton.No
            | MessageBox.StandardButton.Cancel,
            MessageBox.StandardButton.Yes,
        )
        if ret == MessageBox.StandardButton.Cancel:
            return False
        if ret == MessageBox.StandardButton.Yes:
            return self._save_current_project()
        return True  # 否（不保存）

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
        """打开工程：弹与启动时相同的「打开地图文档」对话框（最近打开 / 浏览）。

        不再直接弹系统文件框：用户想打开的基本都是最近工程，启动对话框里有
        列表可点；需要找别的文件时走它左侧的「浏览…」。
        """
        try:
            dlg = StartupProjectDialog(
                self, app_name=getattr(self, "_app_name", None) or "qgis_dev"
            )
            if dlg.exec() != QDialog.DialogCode.Accepted:
                return
            if dlg.create_new():
                self._on_new_project()
                return
            path = dlg.selected_path()
            if path:
                self.open_project_path(path)
        except Exception as exc:  # noqa: BLE001 —— 槽内异常在本环境会原生崩溃
            logger.error("打开工程失败: %s", exc, exc_info=True)
            MessageBox.warning(self, "打开工程", f"打开工程失败：{exc}")

    def open_project_path(self, path: str) -> bool:
        """读入 ``path`` 指定的工程文件并刷新画布；返回是否成功。

        打开工程动作（菜单 / 工具栏）与启动“打开地图文档”对话框共用：对话框
        只负责选路径，读盘、bridge 同步、分组重排都在这里。
        """
        if not path:
            return False
        # 已有工程时先问是否保存当前工程；用户取消 / 保存失败则不打开目标工程
        if not self._confirm_save_project_before_open():
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
            # 读取 / 分组重排 / 恢复视图都是打开过程的一部分：以此为“无改动”基线，
            # 用户打开工程后直接退出不应再被询问保存。
            self._mark_project_clean()
            return True
        MessageBox.warning(self, "打开失败", f"无法打开工程：{path}")
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

    def _default_crs_authid(self) -> str:
        """工程 / 新建数据的默认坐标系 authid：``config/app.yaml`` 的 ``app.crs``。

        单一来源就是那份 yaml（:data:`_DEFAULT_CRS_AUTHID` 只是它读不到时的兜底），
        启动建画布、新建工程、创建数据对话框三处都走这里 —— 以前三处各写各的
        字面量，改一处忘一处就会出现「工程是 4490、创建数据默认给 4326」这种
        不一致（用户报过默认值跑偏）。
        """
        cfg = self._load_map_config()
        authid = getattr(cfg, "crs", "") if cfg is not None else ""
        return authid or _DEFAULT_CRS_AUTHID

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
