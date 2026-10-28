"""跨边界面不相交处理 Dialog（质检 →「跨边界面不相交处理」）。

对应后端质检类型 ``space_contain``
（:class:`app.core.domain.check.builtin.space_contain.SpaceContainCheck`）：
检查**源图层**（输入图层）的每个面要素是否都被**参考图层**完全包含 —— 只要要素
有一部分伸到参考图层所有要素拼起来的范围之外，就算「跨过参考图层边界」判失败。

界面沿用「质检 → 面自相交处理」窗口的样式
（:class:`app.ui.dialogs.self_intersect.SelfIntersectDialog`）：左侧参数面板
（输入图层 / 过滤条件（可点「配置」用表达式构建器拼写）/ 参考图层 / 参考图层
过滤条件（可点「配置」）/ 输出图层（放最后，可点「浏览…」选落盘路径）），右侧
「参考提示」栏（说明文字 + 示意图），底部处理进度条 + 取消 / 确认。本检查没有
数值阈值（包含与否是几何之间的相对位置关系），但比面自相交多一行「参考图层」
下拉框和一行「参考图层过滤条件」—— 两个图层各筛各的：输入图层的过滤条件决定
“查哪些源要素”，参考图层的过滤条件决定“拿哪些参考要素拼参考范围”。

点「确认」在**输入图层**上执行检查，命中要素另存到「输出图层」：填了输出路径
就写盘并从文件加载，留空则落临时 memory 图层；两种情况都自动加入工程并选中 +
打开属性表。

为避免大图层卡死界面，检查不是一次性同步跑完：后端 check 提供
:meth:`~app.core.domain.check.builtin.space_contain.SpaceContainCheck.iter_failed`
流式接口，Dialog 用 QTimer 按时间片分批消费（每批 :data:`_CHECK_BUDGET_S` 秒），
跑完立即把控制权交回事件循环。
"""

from __future__ import annotations

import os
import time

from qgis.core import (
    QgsCoordinateTransformContext,
    QgsExpression,
    QgsProject,
    QgsVectorFileWriter,
    QgsVectorLayer,
    QgsWkbTypes,
)
from qgis.PyQt.QtCore import QPointF, Qt, QTimer
from qgis.PyQt.QtGui import QBrush, QColor, QPainter, QPen, QPolygonF
from qgis.PyQt.QtWidgets import (
    QComboBox,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from app.core.domain.check import CheckParam, CheckService
from app.core.domain.check.result_layer import build_result_layer
from app.infra.logging import get_logger
from app.infra.messagebar import send
from app.ui.common.base_dialog import BaseDialog
from app.ui.common.layer_order import vector_layers_in_tree_order
from app.ui.dialogs.check_dialog_state import CheckDialogRunStateMixin

logger = get_logger(__name__)

#: 后端质检类型注册 id
CHECK_TYPE_ID = "space_contain"

#: 参考提示说明文案
DESC_TEXT = "检查源图层中超出参考图层范围的面要素"

#: 输出图层名兜底（输出路径为空时用，落临时 memory 图层）
DEFAULT_OUTPUT_NAME = "跨边界面不相交结果"

#: 进度条上方状态文字的初始文案
_STATUS_READY = "就绪：点「确认」开始检查"

#: 每个事件循环批次的处理时间预算（秒）：跑完一批就还给事件循环，UI 不卡死
_CHECK_BUDGET_S = 0.03


def _write_vector_layer(layer: QgsVectorLayer, path: str) -> None:
    """把结果图层写盘（按扩展名选驱动：shp / geojson，其余按 GPKG）。"""
    ext = os.path.splitext(path)[1].lower().lstrip(".")
    options = QgsVectorFileWriter.SaveVectorOptions()
    options.driverName = {"shp": "ESRI Shapefile", "geojson": "GeoJSON"}.get(ext, "GPKG")
    error, errmsg, _new, _saved = QgsVectorFileWriter.writeAsVectorFormatV3(
        layer, path, QgsCoordinateTransformContext(), options
    )
    if error != QgsVectorFileWriter.WriterError.NoError:
        raise RuntimeError(f"输出文件写入失败：{errmsg}")


class HintDiagram(QWidget):
    """「参考提示」右侧的示意图（按设计稿 ``resource/reeee/跨边界面不相交.png`` 矢量重绘）。

    设计稿这里是一组面要素叠在一起的对照图，三层含义：

    - **黄色**（3 个面）：参考图层给出的范围；
    - **红色**（2 个面，压在黄色之上）：跨出参考范围的面要素 —— 有一部分伸到黄色
      边界外面，所以要报出来；
    - **浅蓝**（1 个面，压在黄色之上）：完全落在参考范围内的面要素 —— 合格。

    设计稿只给了位图，这里把三层图形的轮廓按 1:1 从图上描出来（轮廓点即图形区内的
    像素坐标），随控件大小等比缩放，不引入额外图片资源；绘制顺序与设计稿一致
    （黄 → 红 → 浅蓝），后画的图形自然盖住先画的，叠压关系才不会丢。
    """

    #: 设计稿图形区尺寸（描点坐标的参考画布）
    _W = 266.0
    _H = 71.0
    #: 参考图层范围（黄色，右中左三个面）
    _REFERENCE = (
        (
            (227.0, 5.0),
            (198.0, 26.0),
            (198.0, 30.0),
            (210.0, 66.0),
            (252.0, 66.0),
            (264.0, 26.0),
            (233.0, 4.0),
            (227.0, 5.0),
        ),
        (
            (108.0, 38.0),
            (117.0, 67.0),
            (155.0, 67.0),
            (166.0, 31.0),
            (138.0, 11.0),
            (134.0, 11.0),
            (134.0, 18.0),
            (127.0, 38.0),
            (108.0, 38.0),
        ),
        (
            (37.0, 27.0),
            (18.0, 41.0),
            (18.0, 45.0),
            (26.0, 69.0),
            (56.0, 68.0),
            (60.0, 51.0),
            (63.0, 46.0),
            (62.0, 40.0),
            (43.0, 26.0),
            (37.0, 27.0),
        ),
    )
    #: 跨出参考范围的面要素（红色，左、中两个面）
    _OUTSIDE = (
        (
            (114.0, 3.0),
            (98.0, 15.0),
            (98.0, 20.0),
            (104.0, 38.0),
            (128.0, 38.0),
            (134.0, 15.0),
            (118.0, 3.0),
            (114.0, 3.0),
        ),
        (
            (14.0, 3.0),
            (2.0, 12.0),
            (2.0, 15.0),
            (7.0, 30.0),
            (25.0, 30.0),
            (30.0, 16.0),
            (30.0, 12.0),
            (27.0, 9.0),
            (18.0, 3.0),
            (14.0, 3.0),
        ),
    )
    #: 完全落在参考范围内的面要素（浅蓝，压在最右那个黄面上）
    _INSIDE = (
        (
            (213.0, 30.0),
            (212.0, 35.0),
            (219.0, 54.0),
            (244.0, 53.0),
            (250.0, 35.0),
            (250.0, 31.0),
            (244.0, 26.0),
            (236.0, 22.0),
            (233.0, 18.0),
            (229.0, 18.0),
            (226.0, 22.0),
            (213.0, 30.0),
        ),
    )
    #: 取色自设计稿（示意图是插画，不跟随主题换色）
    _REFERENCE_FILL = "#ffff01"
    _OUTSIDE_FILL = "#ff0004"
    _INSIDE_FILL = "#a6caee"
    _EDGE = "#3d4346"

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setMinimumHeight(77)
        self.setMinimumWidth(200)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        # 等比缩放 + 居中，保证不同尺寸下图形不变形
        scale = min(self.width() / self._W, self.height() / self._H)
        painter.translate((self.width() - self._W * scale) / 2, (self.height() - self._H * scale) / 2)
        painter.scale(scale, scale)

        pen = QPen(QColor(self._EDGE))
        pen.setWidthF(1.5)
        painter.setPen(pen)
        layers = (
            (self._REFERENCE, self._REFERENCE_FILL),
            (self._OUTSIDE, self._OUTSIDE_FILL),
            (self._INSIDE, self._INSIDE_FILL),
        )
        for polygons, color in layers:
            painter.setBrush(QBrush(QColor(color)))
            for points in polygons:
                painter.drawPolygon(QPolygonF([QPointF(x, y) for x, y in points]))
        painter.end()


class CrossBoundaryDialog(CheckDialogRunStateMixin, BaseDialog):
    """跨边界面不相交（面要素须被参考图层完全包含）配置 / 执行 Dialog。"""

    def __init__(self, parent=None, title: str = "跨边界面不相交处理") -> None:
        super().__init__(parent, title)
        # 尺寸与「质检 → 面自相交处理」窗口同构（左参数 + 右说明两栏，多一行参考图层）
        self.setMinimumSize(760, 560)
        self.check_service = CheckService()
        self._cancelled = False
        self._build()
        self._populate_layers()

    # ------------------------------------------------------------ 构建

    def _build(self) -> None:
        root = QVBoxLayout()

        # ---- 左右两部分：左参数 + 右说明（右侧「参考提示」相当于算法说明栏）----
        main = QHBoxLayout()
        main.setSpacing(12)

        param_panel = QWidget()
        self._init_parameters_panel(param_panel)
        main.addWidget(param_panel, stretch=3)
        main.addWidget(self._make_hint_panel(), stretch=2)
        root.addLayout(main, stretch=1)

        # ---- 处理进度 + 按钮（进度条在上、按钮右对齐）----
        b_l = QVBoxLayout()
        b_l.setContentsMargins(0, 0, 0, 0)

        # 状态文字单独一行、进度条整宽铺满：两者同处一行时，空文字的状态标签
        # 会占位把进度条整体右推。
        self.progress_label = QLabel(_STATUS_READY)
        self.progress_label.setObjectName("PropHint")
        b_l.addWidget(self.progress_label)
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        b_l.addWidget(self.progress)

        btns = QHBoxLayout()
        btns.addStretch(1)
        cancel_btn = QPushButton("取消")
        cancel_btn.clicked.connect(self.reject)
        btns.addWidget(cancel_btn)
        self.confirm_btn = QPushButton("确认")
        self.confirm_btn.setObjectName("DialogPrimary")
        self.confirm_btn.setDefault(True)
        self.confirm_btn.clicked.connect(self._on_confirm)
        btns.addWidget(self.confirm_btn)
        b_l.addLayout(btns)
        root.addLayout(b_l)

        self.setLayout(root)

    def _init_parameters_panel(self, parent) -> None:
        """左侧参数面板：字段名在上、控件在下（与面自相交窗口写法一致）。"""
        layout = QVBoxLayout(parent)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        # 1. 输入图层（源图层，只收矢量图层）
        layout.addWidget(QLabel("输入图层"))
        self.input_combo = QComboBox()
        self.input_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.input_combo.currentIndexChanged.connect(self._on_input_changed)
        layout.addWidget(self.input_combo)
        layout.addSpacing(4)

        # 2. 输入图层自己的过滤条件 + 「配置」（表达式构建器）：行内按钮
        layout.addWidget(QLabel("过滤条件"))
        filter_row = QHBoxLayout()
        self.filter_input = QLineEdit()
        self.filter_input.setPlaceholderText("请输入过滤条件")
        self.config_btn = QPushButton("配置")
        self.config_btn.clicked.connect(self._on_config)
        filter_row.addWidget(self.filter_input, 1)
        filter_row.addWidget(self.config_btn)
        layout.addLayout(filter_row)
        layout.addSpacing(4)

        # 3. 参考图层：它的要素并集就是“参考范围”，所以也有自己的过滤条件（选哪些
        #    参考要素参与拼范围），行布局与上面一致
        layout.addWidget(QLabel("参考图层"))
        self.ref_combo = QComboBox()
        self.ref_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.ref_combo.currentIndexChanged.connect(self._on_ref_changed)
        layout.addWidget(self.ref_combo)
        layout.addSpacing(4)

        layout.addWidget(QLabel("参考图层过滤条件"))
        ref_filter_row = QHBoxLayout()
        self.ref_filter_input = QLineEdit()
        self.ref_filter_input.setPlaceholderText("留空则用参考图层全部要素")
        self.ref_config_btn = QPushButton("配置")
        self.ref_config_btn.clicked.connect(self._on_ref_config)
        ref_filter_row.addWidget(self.ref_filter_input, 1)
        ref_filter_row.addWidget(self.ref_config_btn)
        layout.addLayout(ref_filter_row)
        layout.addSpacing(4)

        # 4. 输出图层：放最后；文件选择器控制落盘路径，留空则落临时 memory 图层
        layout.addWidget(QLabel("输出图层"))
        out_row = QHBoxLayout()
        self.output_input = QLineEdit()
        self.output_input.setPlaceholderText("留空则生成临时图层")
        out_row.addWidget(self.output_input, 1)
        self.output_browse_btn = QPushButton("浏览…")
        self.output_browse_btn.clicked.connect(self._browse_output_path)
        out_row.addWidget(self.output_browse_btn)
        layout.addLayout(out_row)

        layout.addStretch()

    def _make_hint_panel(self) -> QWidget:
        """右侧「参考提示」栏：说明文字框 + 示意图（对应算法窗口的算法说明栏）。"""
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        tip_lbl = QLabel("参考提示")
        tip_lbl.setObjectName("SectionLabel")
        layout.addWidget(tip_lbl)

        # 说明框高度贴合文字（不给 stretch）：拉伸成一整列高时框内大片空白。
        self.desc = QLabel(DESC_TEXT)
        self.desc.setObjectName("CheckDesc")
        self.desc.setWordWrap(True)
        self.desc.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        layout.addWidget(self.desc)

        self.diagram = HintDiagram()
        layout.addWidget(self.diagram)
        layout.addStretch()
        return panel

    # ------------------------------------------------------------ 上下文

    def _current_layer(self) -> QgsVectorLayer | None:
        """取当前操作图层：主窗口选中图层 → 编辑图层 → 画布当前图层。"""
        parent = self.parent()
        for attr in ("_selected_layer", "edit_layer"):
            layer = getattr(parent, attr, None)
            if isinstance(layer, QgsVectorLayer) and layer.isValid():
                return layer
        canvas = getattr(parent, "canvas", None)
        layer = canvas.currentLayer() if canvas is not None else None
        if isinstance(layer, QgsVectorLayer) and layer.isValid():
            return layer
        return None

    def _input_layer(self) -> QgsVectorLayer | None:
        """输入图层：下拉框当前项，取不到再回退到上下文图层。"""
        layer = self.input_combo.currentData()
        if isinstance(layer, QgsVectorLayer) and layer.isValid():
            return layer
        return self._current_layer()

    def _reference_layer(self) -> QgsVectorLayer | None:
        """参考图层：下拉框当前项（没有回退 —— 参考图层必须显式选）。"""
        layer = self.ref_combo.currentData()
        if isinstance(layer, QgsVectorLayer) and layer.isValid():
            return layer
        return None

    def _populate_layers(self) -> None:
        """用工程里的矢量图层填两个下拉框；**再次打开时保留上次的选择**。

        首次打开默认：输入选上下文图层，参考挑一个**跟输入不同**的图层。
        再次打开时优先沿用上次输入 / 参考图层（只有图层已从工程移除才退回默认）。
        """
        # 与图层面板从上到下的顺序一致（``mapLayers()`` 按图层 UUID 排，看着是乱序）
        layers = vector_layers_in_tree_order()
        context = self._current_layer()
        context_id = self._layer_id(context)
        previous_input_id = self._layer_id(self.input_combo.currentData())
        previous_ref_id = self._layer_id(self.ref_combo.currentData())

        for combo in (self.input_combo, self.ref_combo):
            combo.blockSignals(True)
            combo.clear()
            for layer in layers:
                combo.addItem(layer.name(), layer)

        # 输入：上次选择 > 上下文图层 > 第一个
        index = 0 if layers else -1
        for i in range(self.input_combo.count()):
            if previous_input_id is not None and self._layer_id(
                self.input_combo.itemData(i)
            ) == previous_input_id:
                index = i
                break
        else:
            for i in range(self.input_combo.count()):
                if context_id is not None and self._layer_id(
                    self.input_combo.itemData(i)
                ) == context_id:
                    index = i
                    break
        self.input_combo.setCurrentIndex(index)

        # 参考：上次选择 > 第一个与输入不同的 > 第一个
        ref_index = -1
        for i in range(self.ref_combo.count()):
            if previous_ref_id is not None and self._layer_id(
                self.ref_combo.itemData(i)
            ) == previous_ref_id:
                ref_index = i
                break
        if ref_index < 0:
            ref_index = 0 if layers else -1
            for i in range(self.ref_combo.count()):
                if i != index:
                    ref_index = i
                    break
        self.ref_combo.setCurrentIndex(ref_index)

        for combo in (self.input_combo, self.ref_combo):
            combo.blockSignals(False)

    # ------------------------------------------------------------ 交互

    def _on_input_changed(self) -> None:
        """输入图层变化：本窗口没有随图层变化的参数，占位保持信号连接一致。"""

    def _on_ref_changed(self) -> None:
        """参考图层变化：同上，无需联动其它控件。"""

    def _browse_output_path(self) -> None:
        """文件选择器；无扩展名时补 ``.gpkg``。"""
        path, _ = QFileDialog.getSaveFileName(
            self,
            "选择输出文件",
            self.output_input.text(),
            "GeoPackage (*.gpkg);;Shapefile (*.shp);;GeoJSON (*.geojson)",
        )
        if path and not os.path.splitext(path)[1]:
            path += ".gpkg"
        if path:
            self.output_input.setText(path)

    def _on_config(self) -> None:
        """「配置」：用表达式构建器拼写**输入图层**的过滤条件。"""
        self._configure_filter(self.filter_input, self._input_layer(), "输入图层")

    def _on_ref_config(self) -> None:
        """「配置」：用表达式构建器拼写**参考图层**的过滤条件。"""
        self._configure_filter(self.ref_filter_input, self._reference_layer(), "参考图层")

    def _configure_filter(self, line_edit: QLineEdit, layer: QgsVectorLayer | None, what: str) -> None:
        """打开表达式构建器，把拼好的表达式写回 ``line_edit``（需要图层提供字段）。"""
        if layer is None:
            send("warning", f"请先选择{what}，再配置过滤条件", duration=3)
            return
        from app.ui.dialogs.expression_builder import (  # noqa: PLC0415 - 懒 import：避免弹窗联动
            ExpressionBuilderDialog,
        )

        dlg = ExpressionBuilderDialog(layer, line_edit.text().strip(), self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            line_edit.setText(dlg.expression_text())

    def _on_confirm(self) -> None:
        """「确认」：校验参数后排队执行（不留在 clicked 信号栈里跑）。"""
        layer = self._input_layer()
        if layer is None:
            send("warning", "请先在图层列表选择要质检的矢量图层", duration=3)
            self.input_combo.setFocus()
            return

        ref = self._reference_layer()
        if ref is None:
            send("warning", "请选择用于判定范围的参考图层", duration=3)
            self.ref_combo.setFocus()
            return

        where = self.filter_input.text().strip() or "1=1"
        expr = QgsExpression(where)
        if expr.hasParserError():
            send("warning", f"过滤条件语法错误：{expr.parserErrorString()}", duration=5)
            self.filter_input.setFocus()
            return

        ref_where = self.ref_filter_input.text().strip() or "1=1"
        ref_expr = QgsExpression(ref_where)
        if ref_expr.hasParserError():
            send("warning", f"参考图层过滤条件语法错误：{ref_expr.parserErrorString()}", duration=5)
            self.ref_filter_input.setFocus()
            return

        param = CheckParam(
            layer=layer,
            check_name=None,
            where=where,
            extra={"type_id": CHECK_TYPE_ID, "reference_layer": ref, "reference_where": ref_where},
        )
        output_path = self.output_input.text().strip() or None
        self.confirm_btn.setEnabled(False)
        self._set_progress(0, "正在准备…")
        # 跑质检 + addMapLayer 不能留在 clicked 信号栈里同步执行（原生栈里加图层会崩），
        # 排队到事件循环再跑。
        QTimer.singleShot(0, lambda: self._run(param, output_path))

    # ------------------------------------------------------------ 执行

    def reject(self) -> None:
        """取消时停掉分批循环，避免已关闭的窗口继续跑 QTimer 回调。"""
        self._cancelled = True
        super().reject()

    def _set_progress(self, value: int, text: str) -> None:
        """更新进度条 + 阶段文字（重绘交给事件循环，不再手动 processEvents）。"""
        self.progress.setValue(value)
        self.progress_label.setText(text)

    def _run(self, param: CheckParam, output_path: str | None) -> None:
        """启动**分批**检查：每批只跑一个时间片，跑完还给事件循环，UI 不阻塞。"""
        self._param = param
        self._output_path = output_path
        self._failed: list[int] = []
        self._cancelled = False
        self._seen = 0
        try:
            total = param.layer.featureCount()
        except Exception:  # noqa: BLE001 - 某些 provider 不支持计数，降级为不确定进度
            total = 0
        self._total = total if total and total > 0 else 0

        try:
            self._iterator = self.check_service.iter_failed(param, type_id=CHECK_TYPE_ID)
        except Exception as exc:  # 用 exc_info 记录完整栈，等价于 BLE001 的“非盲捕”处理
            logger.error("跨边界面不相交检查执行失败：%s", exc, exc_info=True)
            send("critical", f"跨边界面不相交检查执行失败：{exc}", duration=5)
            self._finish_failed()
            return

        self._set_progress(1, "正在检查…")
        QTimer.singleShot(0, self._step)

    def _step(self) -> None:
        """消费一个时间片的要素；跑完交给事件循环，避免长循环卡死主线程。"""
        if self._cancelled:
            return
        deadline = time.monotonic() + _CHECK_BUDGET_S
        try:
            while time.monotonic() < deadline:
                seen, fid, _excess = next(self._iterator)
                self._seen = seen
                if fid is not None:
                    self._failed.append(fid)
        except StopIteration:
            # 落层 / addMapLayer 再推一拍，不和检查循环挤在同一个事件里
            QTimer.singleShot(0, self._on_check_done)
            return
        except Exception as exc:  # 同上：已记录完整栈
            logger.error("跨边界面不相交检查执行失败：%s", exc, exc_info=True)
            send("critical", f"跨边界面不相交检查执行失败：{exc}", duration=5)
            self._finish_failed()
            return

        pct = max(1, min(int(90 * self._seen / self._total), 90)) if self._total else self.progress.value()
        self._set_progress(pct, f"正在检查…（已处理 {self._seen}）")
        QTimer.singleShot(0, self._step)

    def _on_check_done(self) -> None:
        """检查迭代结束：无命中直接收尾，有命中则落「输出图层」。"""
        if self._cancelled:
            return
        param = self._param
        layer = param.layer
        failed = self._failed
        if not failed:
            self._set_progress(100, "检查完成")
            send("success", "未发现超出参考图层范围的面要素", duration=3)
            self.accept()
            return

        self._set_progress(90, "正在输出图层…")
        output_path = self._output_path
        output_name = os.path.splitext(os.path.basename(output_path))[0] if output_path else DEFAULT_OUTPUT_NAME
        try:
            output = build_result_layer(layer, failed, output_name)
            if output_path:
                _write_vector_layer(output, output_path)
                output = QgsVectorLayer(output_path, output_name, "ogr")
                if not output.isValid():
                    raise RuntimeError(f"输出文件加载失败：{output_path}")
        except Exception as exc:  # 同上：已记录完整栈
            logger.error("跨边界面不相交输出图层生成失败：%s", exc, exc_info=True)
            send("critical", f"输出图层生成失败：{exc}", duration=5)
            self._finish_failed()
            return

        QgsProject.instance().addMapLayer(output)
        self._set_progress(100, f"完成：{len(failed)} 个")
        layer.selectByIds(failed)
        parent = self.parent()
        if hasattr(parent, "_show_attribute_dock"):
            parent._show_attribute_dock(layer, show_ids=failed)
        dest = f" → {output_path}" if output_path else ""
        send(
            "warning",
            f"发现 {len(failed)} 个超出参考图层范围的面要素，已写入「{output_name}」{dest}",
            duration=5,
        )
        self.accept()

    def _finish_failed(self) -> None:
        """执行失败后恢复按钮，让用户能改参数重试。"""
        self.progress_label.setText("执行失败")
        self.confirm_btn.setEnabled(True)


__all__ = [
    "CHECK_TYPE_ID",
    "DEFAULT_OUTPUT_NAME",
    "CrossBoundaryDialog",
    "HintDiagram",
]
