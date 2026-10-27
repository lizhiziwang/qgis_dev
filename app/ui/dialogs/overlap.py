"""面重叠处理 Dialog（质检 →「面重叠」）。

对应后端质检类型 ``space_overlap``
（:class:`app.core.domain.check.builtin.space_overlap.SpaceOverlapCheck`）：
把源图层要素两两比对，``相交但不只是邻接``（共用边/点算邻接）即判为**重叠**；
命中的要素另存到输出图层。

界面沿用「质检 → 面自相交处理」窗口的样式
（:class:`app.ui.dialogs.self_intersect.SelfIntersectDialog`）：左侧参数面板
（输入图层 / 过滤条件（可点「配置」用表达式构建器拼写）/ 输出图层（放最后，可点
「浏览…」选落盘路径）），右侧「参考提示」栏（说明文字 + 示意图），底部处理进度条 +
取消 / 确认。重叠判定没有数值阈值（重叠与否是要素间的关系），参数与面自相交窗口一致。

点「确认」在**输入图层**上执行检查，命中要素另存到「输出图层」：填了输出路径
就写盘并从文件加载，留空则落临时 memory 图层；两种情况都自动加入工程并选中 +
打开属性表。

为避免大图层卡死界面，检查不是一次性同步跑完：后端 check 提供
:meth:`~app.core.domain.check.builtin.space_overlap.SpaceOverlapCheck.iter_failed`
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
CHECK_TYPE_ID = "space_overlap"

#: 参考提示说明文案（取自设计稿 ``resource/reeee/面重叠.png``）
DESC_TEXT = "检查源图层中重叠的面要素"

#: 输出图层名兜底（输出路径为空时用，落临时 memory 图层）
DEFAULT_OUTPUT_NAME = "面重叠结果"

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
    """「参考提示」右侧的示意图（按设计稿 ``resource/reeee/面重叠.png`` 矢量重绘）。

    设计稿这里是一张小图：三组上下相邻的面要素（上黄下蓝），

    - 左边两组**有重叠** —— 两块的公共部分用红色标出，会被判为失败；
    - 右边一组只共边、不重叠 —— 作对照，说明合格的情形长什么样。

    设计稿只给了位图，这里把各个图形的轮廓按 1:1 从图上描出来（轮廓点即图形区内的
    像素坐标），随控件大小等比缩放，不引入额外图片资源。
    """

    #: 设计稿图形区尺寸（描点坐标的参考画布 = 图形外接框 + 留白）
    _W = 239.0
    _H = 71.0
    #: 上排的三个面要素轮廓（三个"地块"，最上面一排）
    _TOP = (
        (
            (10.0, 2.0),
            (2.0, 16.0),
            (2.0, 21.0),
            (8.0, 29.0),
            (9.0, 33.0),
            (12.0, 33.0),
            (13.0, 31.0),
            (28.0, 31.0),
            (29.0, 33.0),
            (32.0, 33.0),
            (33.0, 29.0),
            (39.0, 21.0),
            (39.0, 16.0),
            (33.0, 8.0),
            (31.0, 2.0),
            (10.0, 2.0),
        ),
        (
            (109.0, 2.0),
            (101.0, 16.0),
            (101.0, 21.0),
            (107.0, 29.0),
            (108.0, 33.0),
            (131.0, 33.0),
            (132.0, 29.0),
            (138.0, 21.0),
            (138.0, 16.0),
            (132.0, 8.0),
            (130.0, 2.0),
            (109.0, 2.0),
        ),
        (
            (209.0, 2.0),
            (201.0, 15.0),
            (201.0, 20.0),
            (209.0, 33.0),
            (228.0, 33.0),
            (230.0, 31.0),
            (232.0, 25.0),
            (236.0, 20.0),
            (236.0, 15.0),
            (228.0, 2.0),
            (209.0, 2.0),
        ),
    )
    #: 下排的三个面要素轮廓（与上排贴身相邻）
    _BOTTOM = (
        (
            (7.0, 42.0),
            (5.0, 43.0),
            (2.0, 49.0),
            (2.0, 55.0),
            (8.0, 63.0),
            (9.0, 67.0),
            (31.0, 68.0),
            (39.0, 55.0),
            (39.0, 49.0),
            (36.0, 43.0),
            (34.0, 42.0),
            (33.0, 38.0),
            (9.0, 37.0),
            (7.0, 42.0),
        ),
        (
            (104.0, 43.0),
            (101.0, 49.0),
            (101.0, 55.0),
            (109.0, 68.0),
            (131.0, 67.0),
            (132.0, 63.0),
            (138.0, 55.0),
            (138.0, 49.0),
            (130.0, 35.0),
            (109.0, 35.0),
            (106.0, 42.0),
            (104.0, 43.0),
        ),
        (
            (209.0, 36.0),
            (202.0, 47.0),
            (200.0, 54.0),
            (204.0, 59.0),
            (205.0, 63.0),
            (207.0, 64.0),
            (208.0, 68.0),
            (228.0, 69.0),
            (237.0, 54.0),
            (237.0, 51.0),
            (233.0, 46.0),
            (232.0, 42.0),
            (230.0, 41.0),
            (229.0, 37.0),
            (209.0, 36.0),
        ),
    )
    #: 两组重叠的公共部分轮廓（红色标出，其余一组只共边所以没有）
    _OVERLAP = (
        (
            (12.0, 31.0),
            (11.0, 37.0),
            (31.0, 37.0),
            (30.0, 31.0),
            (12.0, 31.0),
        ),
        (
            (130.0, 36.0),
            (130.0, 33.0),
            (110.0, 33.0),
            (110.0, 36.0),
            (130.0, 36.0),
        ),
    )
    #: 取色自设计稿（示意图是插画，不跟随主题换色）
    _TOP_FILL = "#ffff01"
    _BOTTOM_FILL = "#98ccfe"
    _OVERLAP_FILL = "#ff0004"
    _EDGE = "#3d4346"

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setMinimumHeight(71)
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
        # 重叠部分最后画：设计稿里红色压在两个面的公共边上
        shapes = [(points, self._TOP_FILL) for points in self._TOP]
        shapes += [(points, self._BOTTOM_FILL) for points in self._BOTTOM]
        shapes += [(points, self._OVERLAP_FILL) for points in self._OVERLAP]
        for points, color in shapes:
            painter.setBrush(QBrush(QColor(color)))
            painter.drawPolygon(QPolygonF([QPointF(x, y) for x, y in points]))
        painter.end()


class OverlapDialog(CheckDialogRunStateMixin, BaseDialog):
    """面重叠处理（同图层内要素互相重叠）配置 / 执行 Dialog。"""

    def __init__(self, parent=None, title: str = "面重叠处理") -> None:
        super().__init__(parent, title)
        # 尺寸与「质检 → 面自相交处理」窗口同构（左参数 + 右说明两栏）
        self.setMinimumSize(760, 520)
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

        # 1. 输入图层（只收矢量图层）
        layout.addWidget(QLabel("输入图层"))
        self.input_combo = QComboBox()
        self.input_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.input_combo.currentIndexChanged.connect(self._on_input_changed)
        layout.addWidget(self.input_combo)
        layout.addSpacing(4)

        # 2. 过滤条件 + 「配置」（表达式构建器）：行内按钮，同面自相交窗口
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

        # 3. 输出图层：放最后；文件选择器控制落盘路径，留空则落临时 memory 图层
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

    def _populate_layers(self) -> None:
        """用工程里的矢量图层填输入图层下拉框；**再次打开时保留上次的选择**。"""
        # 与图层面板从上到下的顺序一致（``mapLayers()`` 按图层 UUID 排，看着是乱序）
        layers = vector_layers_in_tree_order()
        context = self._current_layer()
        # 上次选中的图层仍在工程里就用它；否则退回上下文图层
        self._fill_layer_combo(self.input_combo, layers, self._layer_id(context))

    # ------------------------------------------------------------ 交互

    def _on_input_changed(self) -> None:
        """输入图层变化：本窗口没有随图层变化的参数，占位保持信号连接一致。"""

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
        """「配置」：用表达式构建器拼写过条件（需要图层提供字段）。"""
        layer = self._input_layer()
        if layer is None:
            send("warning", "请先选择输入图层，再配置过滤条件", duration=3)
            return
        from app.ui.dialogs.expression_builder import (  # noqa: PLC0415 - 懒 import：避免弹窗联动
            ExpressionBuilderDialog,
        )

        dlg = ExpressionBuilderDialog(layer, self.filter_input.text().strip(), self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self.filter_input.setText(dlg.expression_text())

    def _on_confirm(self) -> None:
        """「确认」：校验参数后排队执行（不留在 clicked 信号栈里跑）。"""
        layer = self._input_layer()
        if layer is None:
            send("warning", "请先在图层列表选择要质检的矢量图层", duration=3)
            self.input_combo.setFocus()
            return

        where = self.filter_input.text().strip() or "1=1"
        expr = QgsExpression(where)
        if expr.hasParserError():
            send("warning", f"过滤条件语法错误：{expr.parserErrorString()}", duration=5)
            self.filter_input.setFocus()
            return

        param = CheckParam(
            layer=layer,
            check_name=None,
            where=where,
            extra={"type_id": CHECK_TYPE_ID},
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
            logger.error("面重叠检查执行失败：%s", exc, exc_info=True)
            send("critical", f"面重叠检查执行失败：{exc}", duration=5)
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
                seen, fid, _msg = next(self._iterator)
                self._seen = seen
                if fid is not None:
                    self._failed.append(fid)
        except StopIteration:
            # 落层 / addMapLayer 再推一拍，不和检查循环挤在同一个事件里
            QTimer.singleShot(0, self._on_check_done)
            return
        except Exception as exc:  # 同上：已记录完整栈
            logger.error("面重叠检查执行失败：%s", exc, exc_info=True)
            send("critical", f"面重叠检查执行失败：{exc}", duration=5)
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
            send("success", "未发现重叠的面要素", duration=3)
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
            logger.error("面重叠输出图层生成失败：%s", exc, exc_info=True)
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
            f"发现 {len(failed)} 个重叠的面要素，已写入「{output_name}」{dest}",
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
    "HintDiagram",
    "OverlapDialog",
]
