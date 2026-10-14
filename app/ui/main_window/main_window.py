"""主窗口。

旧 :class:`main.QGISMapWindow` 700+ 行，拆成 6 个子组件 + 业务回调：

- :class:`ToolBar`      - 工具条（顶部 chrome 用 Tab 切换器切换各功能工具条）
- :class:`StatusBar`    - 状态栏
- :class:`LayerDock`    - 图层树
- :class:`BrowserDock`  - 数据浏览器
- :class:`LogDock`      - 日志面板

业务逻辑（打开文件、空间查询、质检等）通过 callback 注入；窗口本身只负责
"装配 + 事件分发"。

``MainWindow`` 本体进一步只保留装配顺序与 Qt 生命周期钩子，其余按功能拆到
:mod:`app.ui.main_window.mixins` 下的 mixin，由这里多继承组合：

- :class:`~app.ui.main_window.mixins.shell.WindowShellMixin`           - 画布/中心控件/停靠区/悬浮层
- :class:`~app.ui.main_window.mixins.chrome.WindowChromeMixin`         - 顶部 chrome/状态栏/主题
- :class:`~app.ui.main_window.mixins.edit_stack.EditStackMixin`        - 撤销重做栈
- :class:`~app.ui.main_window.mixins.map_tools.MapToolsMixin`          - 地图工具与选择
- :class:`~app.ui.main_window.mixins.app_actions.AppActionsMixin`      - 各功能入口
- :class:`~app.ui.main_window.mixins.project_ops.ProjectOpsMixin`      - 工程操作
- :class:`~app.ui.main_window.mixins.online_basemap.OnlineBasemapMixin` - 在线底图
- :class:`~app.ui.main_window.mixins.file_browser.FileBrowserMixin`    - 打开文件/数据浏览器
- :class:`~app.ui.main_window.mixins.layer_actions.LayerActionsMixin`  - 图层动作与右键菜单
- :class:`~app.ui.main_window.mixins.layer_tree_ops.LayerTreeOpsMixin` - 图层树操作
- :class:`~app.ui.main_window.mixins.layer_dialogs.LayerDialogsMixin`  - 图层相关对话框
- :class:`~app.ui.main_window.mixins.gpkg_alias.GpkgAliasMixin`        - gpkg 别名清扫

各 mixin 只依赖 ``self`` 上的共享状态（``self.canvas`` / ``self.project`` /
``self._paths`` ...），以 ``super()`` 沿 MRO 协同。
"""

from __future__ import annotations

import sys
from typing import Optional

from qgis.core import (
    QgsApplication,
    QgsProject,
    QgsVectorLayer,
)
from qgis.gui import QgsLayerTreeMapCanvasBridge
from qgis.PyQt.QtGui import QCloseEvent, QShowEvent
from qgis.PyQt.QtWidgets import QMainWindow

from app.core.auth import AuthResult
from app.core.domain.editing import LayerEditService
from app.core.domain.query import SpaceQueryService
from app.core.domain.tile_prefetch import (
    TilePrefetcher,
    ZoomTileGuard,
)
from app.core.util.local_layer_loader import LocalLayerLoader
from app.infra.fs.paths import Paths
from app.infra.logging import get_logger
from app.ui.common.frameless import FramelessMixin
from app.ui.common.icons import Icons
from app.ui.main_window.mixins.app_actions import AppActionsMixin
from app.ui.main_window.mixins.chrome import WindowChromeMixin
from app.ui.main_window.mixins.edit_stack import EditStackMixin
from app.ui.main_window.mixins.file_browser import FileBrowserMixin
from app.ui.main_window.mixins.gpkg_alias import GpkgAliasMixin
from app.ui.main_window.mixins.layer_actions import LayerActionsMixin
from app.ui.main_window.mixins.layer_dialogs import LayerDialogsMixin
from app.ui.main_window.mixins.layer_tree_ops import LayerTreeOpsMixin
from app.ui.main_window.mixins.map_tools import MapToolsMixin
from app.ui.main_window.mixins.online_basemap import OnlineBasemapMixin
from app.ui.main_window.mixins.project_ops import ProjectOpsMixin
from app.ui.main_window.mixins.shell import WindowShellMixin

logger = get_logger(__name__)


class MainWindow(
    WindowShellMixin,
    WindowChromeMixin,
    EditStackMixin,
    MapToolsMixin,
    AppActionsMixin,
    ProjectOpsMixin,
    OnlineBasemapMixin,
    FileBrowserMixin,
    LayerActionsMixin,
    LayerTreeOpsMixin,
    LayerDialogsMixin,
    GpkgAliasMixin,
    FramelessMixin,
    QMainWindow,
):
    """主窗口：只负责装配顺序、Qt 生命周期钩子与各功能 mixin 的组合。"""

    def __init__(
        self,
        project_root: Optional[str] = None,
        app_name: str = "qgis_dev",
        auth_result: AuthResult | None = None,
    ) -> None:
        super().__init__()
        #: 本次启动登录的结果（用户名 / token / 有效期）；标题栏账号信息据此显示。
        #: 登录已不再落盘（无本地凭证），必须由 bootstrap 传进来。
        self._auth_result = auth_result
        # 无边框窗口：标题栏 / 拖动 / 缩放 / 最大化统一由 FramelessMixin 装配。
        # 顶部 chrome 用 QTabBar 取代菜单栏（点 tab 切换下方工具条）。
        self.title_bar = self.enable_frameless(with_tab_bar=True)
        self._app_name = app_name
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

        self.setWindowTitle(self._app_name)
        self.setGeometry(100, 100, 1200, 800)
        # 最小宽高 500×300：无边框窗口边缘拖拽缩放下限
        self.setMinimumSize(500, 300)
        self.setWindowIcon(Icons.app())

        # ---- 项目 + 图层树桥（先建好，_build_central_widget 要用 self.project）----
        self.project = QgsProject.instance()
        self.tree_root = self.project.layerTreeRoot()
        #: 当前工程关联的文件路径（打开 / 保存为工程后记录）；关闭或再次保存
        #: 时据此判断“更新已有工程”还是走“保存为工程”另存。
        self._project_path: Optional[str] = None

        # ---- 地图画布 ----
        self.canvas = self._build_canvas()
        self.bridge = QgsLayerTreeMapCanvasBridge(self.tree_root, self.canvas)
        # **不自动缩放到第一个图层**：加了数据就把视图甩到图层范围上，用户正在看的
        # 地方就丢了（用户报「添加完数据后视图被切到错误位置」）。关掉之后图层照样
        # 上屏（实测 canvas.layers() 有它），要定位自己右键 → 「缩放至图层」。
        self.bridge.setAutoSetupOnFirstLayer(False)
        # 读工程后必须显式把图层树同步进画布：QgsProject.read() 读入的图层不会
        # 驱动 bridge 更新（实测 read 完成后 canvas.layers() 仍为 0），画布空渲染
        # ⇒ 打开工程白屏，且不触发任何渲染；直到用户手动切换任一图层可见性，
        # bridge 才收到信号并 setCanvasLayers()，看起来像“必须点一下就恢复”。
        # 挂 readProject 信号，覆盖所有读工程入口（启动自动打开 / 菜单打开）。
        self.project.readProject.connect(self._sync_canvas_layers_on_project_read)
        # 后台瓦片预热：天地图 XYZ 底图拖动/放大后白屏的修复（渲染完成 →
        # 后台离屏预取四周 8 个相邻视口的瓦片进缓存，平移时命中缓存不白屏）

        self._tile_prefetcher = TilePrefetcher(self.canvas, project=self.project, parent=self)
        # 缩放白屏防护：extentsChanged 时冻结画布 + 预取当前视口瓦片 + 解冻重绘，
        # 消除"缩放后总要白一下"的卡顿感（与 TilePrefetcher 互补：TilePrefetcher
        # 负责邻居预热用于平移，ZoomTileGuard 负责当前缩放不白屏）
        self._zoom_tile_guard = ZoomTileGuard(self.canvas, project=self.project, parent=self)
        self._build_central_widget()

        # ---- 画布左上角缩放工具（覆盖层）----
        # self.zoom_tool = CanvasZoomTool(self.canvas)

        # ---- service 注入 ----
        self.editing_service = LayerEditService()
        self.space_query_service = SpaceQueryService()
        self.draw_manager = None
        self.edit_layer: Optional[QgsVectorLayer] = None
        #: 当前选中的图层（图层树点击 / 右键菜单选中）。必须在 __init__ 里初始化，
        #: 否则未选中图层时直接访问会 AttributeError —— 该异常在 Qt 槽内未捕获
        #: 会 native 崩溃（0xC0000409）。
        self._selected_layer: Optional[QgsVectorLayer] = None
        #: 样式剪贴板：复制样式时存 xmlData()，粘贴样式时读（QGIS 图层菜单习惯）
        self._style_clipboard: Optional[str] = None
        #: 图层 / 组剪贴板：复制组时存节点快照（图层存 clone()），组右键「粘贴」读它
        self._layer_clipboard: Optional[list] = None
        #: 流程节点编辑器窗口（懒建单实例，见 _open_flow_editor）
        self._flow_editor_window = None
        #: 离线下载弹窗（懒建单实例，保留用户勾选/进度状态）
        self._offline_download_dialog = None
        #: 「选择集」面板（懒建单实例，保留窗口位置，见 MapToolsMixin._on_box_select_complete）
        self._selection_set_dialog = None
        #: 可用离线任务列表（平台任务接口就绪后由此注入 TaskInfo，见 TaskInfo.from_dict）
        self.offline_tasks: list = []
        #: 数据管理 Dock 加载策略(image/spatial/dict 分流,见 app.core.util.local_layer_loader)
        self._layer_loader = LocalLayerLoader(self)

        # ---- dock ----
        self._build_docks()

        # ---- 自定义标题栏（标题栏 + 菜单栏整体作为顶部 menuWidget）----
        self._build_chrome()

        # ---- 工具 / Tab 切换器 / 状态栏 ----
        self._build_tool_bar()
        self._build_tabs()
        self._build_status_bar()

        # ---- 应用快捷键（含用户在「快捷键」面板里的自定义）----
        # 放在最后：撤销 / 重做的 QShortcut、文件/查看工具条的 QAction 都已建好
        self._init_shortcuts()

        # ---- 默认激活平移 ----
        self.canvas.setMapTool(self._pan_tool)

        # ---- 启动清扫: 删上一会话残留的 .gpkg 别名(仅老 .db 兼容路径; 新规
        #      .gpkg 本体库无别名, 不会命中清扫的 _is_our_alias 闸门) ----
        from qgis.PyQt.QtCore import QTimer as _SweepTimer

        _SweepTimer.singleShot(2000, self._sweep_startup_gpkg_aliases)

    # ===================================================== 关闭

    #: 关闭窗口时是否弹窗询问“是否保存工程”。默认开启；无头 / 测试场景可置
    #: ``False`` 以避免模态弹窗阻塞（见 ``tests/ui/conftest.py`` 的 autouse fixture）。
    prompt_save_on_close: bool = True

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        # 窗口显示之后再把左侧页签停在「图层列表」（__init__ 里 raise 会被
        # 随后显示的浏览器抢过去，实测）。只做一次，之后由用户自己切页签。
        self._raise_left_dock_once()

    def closeEvent(self, event: QCloseEvent) -> None:
        # 先问正在编辑的图层有没有未提交改动（保存工程不会提交它们），
        # 用户确定后再问是否保存工程；任一环节选“取消”都放弃关闭
        if not self._confirm_editing_layers_on_close():
            event.ignore()
            return
        # 关闭前询问是否保存工程；用户选“取消”/保存失败则放弃关闭
        if not self._confirm_save_project_on_close():
            event.ignore()
            return
        try:
            # ZoomTileGuard 先停：冻结期间若直接关窗，worker 线程还在跑离屏渲染，
            # 关窗口会触发 QPainter/QImage 析构，可能撞上 native 崩溃。
            guard = getattr(self, "_zoom_tile_guard", None)
            if guard is not None:
                try:
                    guard.shutdown()
                except Exception:  # noqa: BLE001
                    pass
            self.project.clear()
        finally:
            self._clear_global_messagebar()
            event.accept()

    def _sync_canvas_layers_on_project_read(self, *_args) -> None:
        """工程读入后把图层树同步到画布（原因见 __init__ 中 readProject 注释）。

        ``QgsProject.read()`` 不会触发 ``QgsLayerTreeMapCanvasBridge`` 更新，
        必须显式同步一次；失败不应打断打开工程流程。
        """
        try:
            self.bridge.setCanvasLayers()
        except Exception:  # noqa: BLE001
            logger.exception("同步画布图层失败")

    def _clear_global_messagebar(self, *args) -> None:
        """清理全局 ``messageBar`` 属性（防悬空指针 native 崩溃）。

        构造时把 ``self.messageBar`` 塞进 ``QgsApplication`` 全局属性（兼容旧
        ``sendMessageToTopParent``）。如果窗口销毁而不清理，属性里残留的
        ``QgsMessageBar*`` 就是悬空指针——之后任何 ``send()`` 访问该属性都会
        access violation（实测：测试里 MainWindow 被 GC 后，reproject 的
        ``send("success", ...)`` 在 ``property("messageBar")`` 处崩溃）。

        ``closeEvent`` 与 ``destroyed`` 信号都走这里；两者分别覆盖「用户主动
        关闭」和「被 GC / deleteLater 销毁」两条路径。
        """
        qgis_app = QgsApplication.instance()
        if qgis_app is None:
            return
        try:
            qgis_app.setProperty("messageBar", None)
        except RuntimeError:
            pass  # 对象已部分销毁，属性残留交给 Qt 自身兜底
