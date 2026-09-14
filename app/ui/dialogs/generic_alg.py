"""通用算法 Dialog（按 ``QgsProcessingAlgorithm`` 参数描述动态生成表单）。

为 tools_dock 上挂的 57 个 ``gdal:*`` 算法（以及任意 processing 算法）复用
同一个对话框：构造时收 ``algo_id`` → ``QgsGui.processingGuiRegistry().createParameterWidgetWrapper``
按 ``alg.parameterDefinitions()`` 逐个造控件 → 收齐 ``value()`` 后调
:class:`app.core.domain.generic_alg.GenericAlgService` 跑算法。

复用 :class:`BufferDialog` 的 layout（左参数 + 右算法描述 + 下按钮 / 进度条），
但不写死任何参数名。Advanced flag 的参数折叠到「高级」``QGroupBox`` 内，与
QGIS 自家工具箱保持一致。

执行在**后台线程**（``QgsProcessingAlgRunnerTask``，与 QGIS processing 对话框
同一机制）跑，主线程事件循环不阻塞——GDAL 大栅格处理不再卡死 UI；带进度条与
取消按钮，结果经 ``executed`` 信号回主线程后再装配图层。
"""

from __future__ import annotations

import os
from typing import Optional

from qgis.PyQt.QtCore import QTimer
from qgis.PyQt.QtGui import QGuiApplication
from qgis.PyQt.QtWidgets import (
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from qgis.core import (
    Qgis,
    QgsApplication,
    QgsMapLayer,
    QgsProcessingParameterDefinition,
    QgsProject,
)
from qgis.gui import (
    QgsAbstractProcessingParameterWidgetWrapper,
    QgsGui,
    QgsProcessingContextGenerator,
    QgsProcessingGui,
    QgsProcessingParameterWidgetContext,
)

from app.core.domain.generic_alg import (
    RESULT_IS_PATH,
    GenericAlgJob,
    GenericAlgService,
)
from app.infra.logging import get_logger
from app.infra.messagebar import send
from app.ui.common.algo_desc import make_description_browser
from app.ui.common.base_dialog import BaseDialog

logger = get_logger(__name__)


class _ContextGenerator(QgsProcessingContextGenerator):
    """固定返回一个 ``QgsProcessingContext``（项目级）的生成器。

    QGIS processing GUI 注册表生成的 widget wrapper 需要这个接口才能解析
    当前项目 / 上下文。我们的 service 仍用单独 ``QgsProcessingContext`` 跑
    ``processing.run``，这里只是为了给 widget 提供上下文。
    """

    def __init__(self, project):
        from qgis.core import QgsProcessingContext

        super().__init__()
        self._ctx = QgsProcessingContext()
        self._ctx.setProject(project)

    def processingContext(self):
        return self._ctx


class GenericAlgDialog(BaseDialog):
    """任意 processing 算法的通用对话框。

    构造参数：
    :param parent: 父窗口
    :param algo_id: 算法 id（``gdal:warpreproject`` / ``native:buffer`` 等）
    :param title: 可选；为空时取 ``alg.displayName()``
    """

    def __init__(self, parent=None, algo_id: str = "", title: str = "") -> None:
        self._algo_id = algo_id
        self._alg = self._resolve_algorithm(algo_id)
        if not title:
            title = self._alg.displayName() if self._alg else algo_id
        super().__init__(parent, title)
        self.setMinimumSize(760, 620)
        # 最大高度对齐主窗口：避免 57 个 GDAL 算法参数面板太长时
        # 超出主窗口范围；保留父级 QMainWindow 的实际可用高度，
        # 没父级时退到当前屏幕可用高度减 80px 留出任务栏空间。
        max_h = self._resolve_max_height(parent)
        if max_h:
            self.setMaximumHeight(max_h)
        self.project = QgsProject.instance()
        self.service = GenericAlgService()

        #: 当前后台任务及其反馈 / 上下文 / 任务参数。任务结束后 _task 立即置 None；
        #: context 与 feedback 继续保留（QgsProcessingAlgRunnerTask 内部保存的是裸
        #: 指针，任务收尾阶段仍会使用），下次执行时被新实例覆盖。
        self._task = None
        self._context = None
        self._feedback = None
        self._job: Optional[GenericAlgJob] = None
        self._primary_output = None
        #: 当前任务清洗后的参数（含临时 memory 输入图层）。任务结束后仍保留到下次
        #: 执行，避免任务收尾阶段临时图层被 GC 导致悬垂。
        self._prepared_params: dict = {}
        #: 最近一条 ERROR 级算法日志（任务失败时展示到 messagebar）
        self._last_error = ""

        # 维持 wrapper 不被 Python GC（C++ 持有指针）
        self._wrappers: dict[str, "QgsAbstractProcessingParameterWidgetWrapper"] = {}
        # 复用一个上下文生成器 + widget context，所有 wrapper 共用
        self._ctx_gen = _ContextGenerator(self.project)
        self._widget_ctx = self._build_widget_context()

        self._init_ui()

    # ----------------------------------------------------------- helpers

    def _resolve_max_height(self, parent) -> Optional[int]:
        """算出 dialog 最大高度：与父 QMainWindow 高度对齐，无父级退到屏幕。

        用户反馈：「gdal所有的算法的最大高度改为和mainwindow一致」——很多
        GDAL 算法参数面板行数多（gdal:warpreproject 10+ 行），如果不限制，
        dialog sizeHint 会撑到 900+ px（实测 927），比 mainwindow 800 还高。
        走父链找 QMainWindow 拿真实高度；找不到再退到 QScreen.availableGeometry
        减 80（任务栏）。
        """
        node = parent
        while node is not None:
            if isinstance(node, QMainWindow) and node.height() > 0:
                return node.height()
            node = node.parentWidget() if hasattr(node, "parentWidget") else None
        screen = QGuiApplication.primaryScreen()
        if screen is not None:
            return max(400, screen.availableGeometry().height() - 80)
        return None

    def _resolve_algorithm(self, algo_id: str):
        if not algo_id:
            return None
        return QgsApplication.processingRegistry().algorithmById(algo_id)

    def _build_widget_context(self) -> "QgsProcessingParameterWidgetContext":
        """构造 ``QgsProcessingParameterWidgetContext``。

        设置项目 / 活动图层，wrapper 内置的下拉框（layer / band / field 等）
        才能正确列出可选值。messageBar 等可选上下文拿不到就跳过，不阻塞。
        """
        ctx = QgsProcessingParameterWidgetContext()
        ctx.setProject(self.project)
        iface = self.project.property("qgis_iface") if self.project else None
        try:
            if iface is not None:
                canvas = getattr(iface, "mapCanvas", None)
                if canvas is not None:
                    ctx.setMapCanvas(canvas)
                active = iface.activeLayer() if hasattr(iface, "activeLayer") else None
                if active is not None:
                    ctx.setActiveLayer(active)
        except Exception:  # noqa: BLE001
            logger.debug("widgetContext 可选字段未装配，不影响主流程", exc_info=True)
        return ctx

    # ----------------------------------------------------------- UI

    def _init_ui(self) -> None:
        layout = QVBoxLayout()

        main = QHBoxLayout()

        # 参数面板装进 QScrollArea：当算法参数行数过多（如栅格计算器 13+ 行）
        # 内容总高度超过 MainWindow 可用高度时，由滚动条承载，而不是撑爆
        # dialog 或压扁 wrapper 控件。outer QScrollArea 的高度由 mainwindow
        # 高度上限决定（``_resolve_max_height``），超过部分滚动
        param_panel = QWidget()
        self._init_parameters_panel(param_panel)
        param_scroll = QScrollArea()
        param_scroll.setWidgetResizable(True)
        param_scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        param_scroll.setWidget(param_panel)
        main.addWidget(param_scroll, stretch=3)

        main.addWidget(make_description_browser(self._algo_id), stretch=2)
        layout.addLayout(main, stretch=1)

        b_l = QVBoxLayout()
        b_l.setContentsMargins(0, 0, 0, 0)

        # 输出控件（仅在算法有 destination 时显示）
        self._has_destination = bool(self._alg.destinationParameterDefinitions())
        if self._has_destination:
            out_widget = QWidget()
            self._init_output_panel(out_widget)
            b_l.addWidget(out_widget)

        self.loading = QProgressBar()
        self.loading.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.loading.setRange(0, 100)
        # 重要：与 BufferDialog 同原因，不接 feedback.progressChanged。
        b_l.addWidget(self.loading, stretch=2)

        bottom = QHBoxLayout()
        bottom.addStretch()
        self.execute_btn = QPushButton("执行")
        self.execute_btn.clicked.connect(self._on_execute)
        bottom.addWidget(self.execute_btn)
        self.cancel_btn = QPushButton("取消")
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.clicked.connect(self._on_cancel)
        bottom.addWidget(self.cancel_btn)
        close = QPushButton("关闭")
        close.clicked.connect(self.close)
        bottom.addWidget(close)
        b_l.addLayout(bottom)
        layout.addLayout(b_l)

        self.setLayout(layout)

    def _init_parameters_panel(self, parent) -> None:
        """按 ``alg.parameterDefinitions()`` 走表生成表单。

        - 跳过 hidden flag（0x04，``FlagHidden``）
        - 跳过 destination 输出参数（统一走底部输出面板）
        - Advanced flag（0x02，``FlagAdvanced``）→ 折叠到「高级」分组
        - 字段增长策略用 :attr:`QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow`
          —— QGIS wrapper 默认 size policy 是 ``Preferred``，若用
          ``ExpandingFieldsGrow`` 只会拉伸 ``Expanding`` 子控件，下拉框会卡
          在 ``minimumSizeHint()`` 宽度（实测 26px），形如被压扁
        """
        layout = QVBoxLayout(parent)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        normal_form = QFormLayout()
        normal_form.setFieldGrowthPolicy(
            QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow
        )
        normal_form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.DontWrapRows)
        normal_form.setSpacing(6)

        advanced_form = QFormLayout()
        advanced_form.setFieldGrowthPolicy(
            QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow
        )
        advanced_form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.DontWrapRows)
        advanced_form.setSpacing(6)
        advanced_box = QGroupBox("高级")
        advanced_box.setCheckable(True)
        advanced_box.setChecked(False)
        advanced_box.setLayout(advanced_form)

        normal_count = advanced_count = 0
        for pdef in self._alg.parameterDefinitions():
            name = pdef.name()
            if pdef.flags() & QgsProcessingParameterDefinition.Flag.FlagHidden:
                continue
            wrapper = self._build_wrapper(pdef)
            if wrapper is None:
                continue
            self._wrappers[name] = wrapper

            target_form = advanced_form if (
                pdef.flags() & QgsProcessingParameterDefinition.Flag.FlagAdvanced
            ) else normal_form

            label = wrapper.createWrappedLabel()
            widget = wrapper.createWrappedWidget(self._ctx_gen.processingContext())
            if widget is None:
                continue
            widget.setToolTip(pdef.toolTip() or pdef.description())
            # 兜底：显式把横向 size policy 设为 Expanding，与上面增长策略配对
            # —— 防止某些 wrapper 子控件（多套一层 QWidget 容器）默认 Preferred
            # 在 QGroupBox 内仍被压成最小宽度
            widget.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
            if label is None:
                desc = pdef.description() or pdef.name()
                if isinstance(pdef, __import__("qgis.core", fromlist=["QgsProcessingParameterExtent"]).QgsProcessingParameterExtent):
                    desc += "（xmin, xmax, ymin, ymax）"
                if pdef.flags() & QgsProcessingParameterDefinition.Flag.FlagOptional:
                    desc += " [可选]"
                label_widget = QLabel(desc)
                label_widget.setSizePolicy(
                    QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred
                )
                target_form.addRow(label_widget, widget)
            else:
                target_form.addRow(label, widget)

            if pdef.flags() & QgsProcessingParameterDefinition.Flag.FlagAdvanced:
                advanced_count += 1
            else:
                normal_count += 1

        # 高级框仅在确实有 advanced 参数时显示
        if advanced_count:
            layout.addWidget(advanced_box)
        # normal_form 必须有至少一行才挂（QFormLayout 行为）
        layout.addLayout(normal_form)
        layout.addStretch()

        # 工程图层增删后刷新 wrapper（layer/band/field 等下拉内容）
        self.project.layersAdded.connect(self._refresh_wrappers)
        self.project.layersWillBeRemoved.connect(self._refresh_wrappers)
        # dialog 关闭时清掉连接，避免 dangling
        self.destroyed.connect(self._disconnect_project_signals)

    def _build_wrapper(self, pdef) -> Optional["QgsAbstractProcessingParameterWidgetWrapper"]:
        """调 QGIS 注册表工厂创建单个参数的 widget wrapper。"""
        try:
            wrapper = QgsGui.processingGuiRegistry().createParameterWidgetWrapper(
                pdef, QgsProcessingGui.WidgetType.Standard
            )
            wrapper.setWidgetContext(self._widget_ctx)
            wrapper.registerProcessingContextGenerator(self._ctx_gen)
            return wrapper
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "无法为参数 %s (%s) 创建 widget wrapper：%s",
                pdef.name(), type(pdef).__name__, exc,
            )
            return None

    def _refresh_wrappers(self, *args) -> None:
        """图层增删后通知所有 wrapper 刷新下拉内容。"""
        for wrapper in self._wrappers.values():
            try:
                wrapper.refresh()
            except AttributeError:
                pass
            except Exception:  # noqa: BLE001
                logger.debug("wrapper refresh 失败：%s", type(wrapper).__name__, exc_info=True)

    def _disconnect_project_signals(self, *args) -> None:
        try:
            self.project.layersAdded.disconnect(self._refresh_wrappers)
            self.project.layersWillBeRemoved.disconnect(self._refresh_wrappers)
        except (TypeError, RuntimeError):
            pass

    def _init_output_panel(self, parent) -> None:
        layout = QFormLayout(parent)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        self.output_edit = QLineEdit(self._alg.displayName() + " 结果")
        layout.addRow("输出图层名", self.output_edit)

        self.output_path_edit = QLineEdit()
        self.output_path_edit.setPlaceholderText(
            self._output_placeholder()
        )
        path_row = QHBoxLayout()
        path_row.addWidget(self.output_path_edit, stretch=1)
        browse_btn = QPushButton("浏览…")
        browse_btn.clicked.connect(self._browse_output_path)
        path_row.addWidget(browse_btn)
        layout.addRow("输出路径（留空为临时缓存）", path_row)

    def _output_placeholder(self) -> str:
        """按 destination 类型给出不同的 placeholder。"""
        from qgis.core import (
            QgsProcessingParameterFileDestination,
            QgsProcessingParameterFolderDestination,
            QgsProcessingParameterRasterLayer,
            QgsProcessingParameterVectorLayer,
        )

        for odef in self._alg.destinationParameterDefinitions():
            if isinstance(odef, (QgsProcessingParameterFolderDestination,)):
                return "留空则使用临时目录"
            if isinstance(odef, (QgsProcessingParameterFileDestination,)):
                return "留空则使用临时文件"
        return "留空则生成临时缓存图层"

    def _browse_output_path(self) -> None:
        """按 destination 类型弹文件 / 目录选择器。"""
        from qgis.core import (
            QgsProcessingParameterFileDestination,
            QgsProcessingParameterFolderDestination,
            QgsProcessingParameterRasterDestination,
            QgsProcessingParameterVectorDestination,
        )

        is_folder = any(
            isinstance(o, (QgsProcessingParameterFolderDestination,))
            for o in self._alg.destinationParameterDefinitions()
        )
        is_raster = any(
            isinstance(o, (QgsProcessingParameterRasterDestination,))
            for o in self._alg.destinationParameterDefinitions()
        )

        path = ""
        if is_folder:
            path = QFileDialog.getExistingDirectory(
                self, "选择输出目录", self.output_path_edit.text()
            )
        else:
            # 矢量 .gpkg / 栅格 .tif 默认；用户可改后缀
            if is_raster:
                filt = "GeoTIFF (*.tif *.tiff);;Erdas Imagine (*.img);;所有文件 (*.*)"
                default = ".tif"
            else:
                filt = "GeoPackage (*.gpkg);;Shapefile (*.shp);;所有文件 (*.*)"
                default = ".gpkg"
            path, _ = QFileDialog.getSaveFileName(
                self, "选择输出文件", self.output_path_edit.text(), filt
            )
            if path and not os.path.splitext(path)[1]:
                path += default
        if path:
            self.output_path_edit.setText(path)

    # ----------------------------------------------------------- execute

    def _collect_params(self) -> dict:
        """遍历 wrapper 收集所有参数值（缺省走 ``parameterDefaultValue``）。"""
        params: dict = {}
        for name, wrapper in self._wrappers.items():
            try:
                value = wrapper.parameterValue()
            except Exception:  # noqa: BLE001
                logger.debug("wrapper %s 取值失败，用默认值", name, exc_info=True)
                try:
                    value = wrapper.parameterDefaultValue()
                except Exception:  # noqa: BLE001
                    value = None
            params[name] = value
        return params

    def _on_execute(self) -> None:
        if self._alg is None:
            send("critical", "算法未注册或已被卸载", duration=3)
            return
        if self._task is not None:
            send("warning", "已有任务正在执行，请等待完成或取消", duration=3)
            return

        params = self._collect_params()
        output_name = (
            self.output_edit.text().strip()
            if self._has_destination and hasattr(self, "output_edit")
            else (self._alg.displayName() or self._algo_id)
        )
        output_path = (
            self.output_path_edit.text().strip()
            if self._has_destination and hasattr(self, "output_path_edit")
            else None
        ) or None

        job = GenericAlgJob(
            algo_id=self._algo_id,
            params=params,
            output_name=output_name,
            output_path=output_path,
        )

        self.execute_btn.setEnabled(False)
        self.loading.setValue(0)
        self._last_error = ""
        # 上一个任务已结束（_task 为 None），可安全释放其临时输入图层
        self._prepared_params = {}

        # 算法声明 NoThreading → 只能在主线程跑（与 QGIS processing 对话框一致）。
        # 仍推迟到事件栈外，避免按钮点击处理过程中直接跑 processing.run。
        if self._alg.flags() & Qgis.ProcessingAlgorithmFlag.NoThreading:
            QTimer.singleShot(0, lambda: self._run(job))
            return

        # 后台线程执行（QgsProcessingAlgRunnerTask）：算法在 worker 线程跑，
        # 主线程事件循环不阻塞 → 窗口保持响应（GDAL 大栅格曾经卡死 UI）。
        try:
            task, context, feedback, primary_output = self.service.build_task(job)
        except Exception as exc:
            logger.error("%s 任务准备失败：%s", job.algo_id, exc, exc_info=True)
            self.execute_btn.setEnabled(True)
            send("critical", f"{job.algo_id} 任务准备失败：{exc}", duration=5)
            return

        # context / feedback 必须持有到任务结束（task 内部是裸指针，被 GC 会悬垂）
        self._task = task
        self._context = context
        self._feedback = feedback
        self._job = job
        self._primary_output = primary_output
        self._prepared_params = task.prepared_params
        self.cancel_btn.setEnabled(True)
        self.cancel_btn.setText("取消")

        task.progress_updated.connect(self._on_task_progress)
        # executed(successful, results) 在主线程回调里装配结果（见 _on_task_executed）
        task.executed.connect(self._on_task_executed)
        # 取消 / 异常终止时 executed 可能不发，用 taskTerminated 兜底复位按钮
        task.taskTerminated.connect(self._on_task_terminated)
        # 算法日志（worker 线程发出）跨线程投递到普通 Python 方法（安全）
        feedback.log_line.connect(self._on_alg_log)
        QgsApplication.taskManager().addTask(task)

    def _run(self, job: GenericAlgJob) -> None:
        """主线程同步执行（``NoThreading`` 算法 / 兜底路径）。

        注意：会阻塞事件循环，仅用于确实无法后台化的算法；常规 GDAL 算法走
        :meth:`_on_execute` 的 ``QgsProcessingAlgRunnerTask`` 后台路径。
        """
        self.loading.setValue(0)
        try:
            result = self.service.run(job)
        except Exception as exc:
            logger.error("%s 执行失败：%s", job.algo_id, exc, exc_info=True)
            self.loading.setValue(0)
            send("critical", f"{job.algo_id} 执行失败：{exc}", duration=5)
            return
        finally:
            self.execute_btn.setEnabled(True)
        self.loading.setValue(100)
        self._apply_result(job, result)

    # ------------------------------------------------------ 后台任务回调

    def _on_task_progress(self, value: int) -> None:
        """worker 线程进度跨线程投递到这里（普通 Python 方法，投递安全）。"""
        self.loading.setValue(int(value))

    def _on_alg_log(self, level: str, message: str) -> None:
        """worker 线程算法日志跨线程投递到这里。

        记录最近一条 ERROR 供任务失败时展示；完整日志写应用日志。不弹 UI 日志
        面板（GDAL 控制台输出量大，messagebar + 日志文件足够）。
        """
        if level == "ERROR":
            self._last_error = message
        if level in ("ERROR", "WARNING"):
            logger.warning("[%s] %s", level, message)
        else:
            logger.debug("[%s] %s", level, message)

    def _on_task_executed(self, ok: bool, results: dict) -> None:
        """后台任务结束（``QgsProcessingAlgRunnerTask.executed``，主线程回调）。

        先快照再复位：任务随时可能被 ``QgsTaskManager`` 删除，不能再访问任务成员；
        ``context`` / ``feedback`` 继续保留到下次执行（任务收尾阶段仍会用到它们，
        且这里无法确定 ``executed`` 与任务析构的先后顺序）。
        """
        if self._task is None:
            return  # 已由 _on_task_terminated 处理（取消 / 异常终止）
        job = self._job
        primary_output = self._primary_output
        feedback = self._feedback
        error = self._last_error
        if job is None:
            return
        self._reset_task_ui()

        canceled = feedback is not None and feedback.isCanceled()
        command_failed = bool(getattr(feedback, "failed", False))

        if canceled:
            self.loading.setValue(0)
            send("warning", f"已取消 {job.algo_id}", duration=3)
            return
        if not ok or command_failed:
            self.loading.setValue(0)
            detail = f"：{error}" if error else ""
            logger.error("%s 执行失败%s", job.algo_id, detail)
            send("critical", f"{job.algo_id} 执行失败{detail}", duration=5)
            return

        self.loading.setValue(100)
        try:
            result = self.service.wrap_results(job, results or {}, primary_output)
        except Exception as exc:
            logger.error("%s 结果装配失败：%s", job.algo_id, exc, exc_info=True)
            send("critical", f"{job.algo_id} 结果装配失败：{exc}", duration=5)
            return
        self._apply_result(job, result)

    def _on_task_terminated(self) -> None:
        """任务异常终止 / 取消（``executed`` 未送达时的兜底）。

        ``QgsProcessingAlgRunnerTask`` 被取消时可能直接终止而不发 ``executed``，
        必须在此复位按钮状态，否则对话框会卡在「执行中」。
        """
        if self._task is None:
            return  # 已由 _on_task_executed 处理
        job = self._job
        feedback = self._feedback
        error = self._last_error
        self._reset_task_ui()
        self.loading.setValue(0)
        if job is None:
            return
        if feedback is not None and feedback.isCanceled():
            send("warning", f"已取消 {job.algo_id}", duration=3)
            return
        detail = f"：{error}" if error else ""
        logger.error("%s 执行失败%s", job.algo_id, detail)
        send("critical", f"{job.algo_id} 执行失败{detail}", duration=5)

    def _reset_task_ui(self) -> None:
        """复位「执行中」状态（按钮 / 任务引用），保留 context / feedback。"""
        self._task = None
        self._job = None
        self._primary_output = None
        self._last_error = ""
        self.execute_btn.setEnabled(True)
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.setText("取消")

    def _on_cancel(self) -> None:
        """请求取消后台任务（转发到 ``feedback.cancel()``，算法尽早停止）。"""
        task = self._task
        if task is None:
            return
        try:
            task.cancel()
        except RuntimeError:
            # 任务已完成，C++ 对象已被 QgsTaskManager 删除
            self._task = None
            return
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.setText("取消中…")

    def closeEvent(self, event) -> None:
        """关闭对话框时取消在跑的后台任务（实例被工具树复用）。"""
        self._on_cancel()
        super().closeEvent(event)

    def _apply_result(self, job: GenericAlgJob, result) -> None:
        """按 service 返回类型装配地图 / messagebar（主线程）。"""
        if isinstance(result, dict) and result.get(RESULT_IS_PATH):
            value = result.get("value", "")
            name = result.get("name", job.output_name)
            send("success", f"{job.algo_id} 完成：{name} → {value}", duration=5)
            return

        if isinstance(result, QgsMapLayer):
            self.project.addMapLayer(result)
            fc = ""
            try:
                fc = f"，{result.featureCount()} 个要素"
            except Exception:  # noqa: BLE001
                fc = ""
            send(
                "success",
                f"{job.algo_id} 完成：{result.name()}{fc}",
                duration=3,
            )
            QTimer.singleShot(0, self._refresh_canvas)
            return

        # 兜底：未知返回类型
        send("success", f"{job.algo_id} 已完成", duration=3)

    def _refresh_canvas(self) -> None:
        canvas = (
            self.parent().canvas
            if self.parent() and hasattr(self.parent(), "canvas")
            else None
        )
        if canvas is not None:
            canvas.refresh()