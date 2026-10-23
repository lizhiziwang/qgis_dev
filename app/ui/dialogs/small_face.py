"""细碎面处理 Dialog（质检 →「细碎面处理」）。

对应后端质检类型 ``space_small_area``
（:class:`app.core.domain.check.builtin.space_small_area.SpaceSmallAreaCheck`）：
按面积判定面要素是否「细碎」——面积小于「最小面积」阈值、或周长 / 面积比超过
「周长面积比」阈值、或开启「多部件检查」且几何为多部件，命中任一即判为细碎面。

界面沿用「质检 → 狭长面处理」窗口的样式
（:class:`app.ui.dialogs.sliver_face.SliverFaceDialog`）：左侧参数面板（输入图层 /
过滤条件（可点「配置」用表达式构建器拼写）/ 类型选择（几何面积 / 属性面积 + 面积字段）/
最小面积 / 周长面积比大于 / 多部件检查 / 输出图层（放最后，可点「浏览…」选落盘路径）），
右侧「参考提示」栏（说明文字 + 示意图），底部处理进度条 + 取消 / 确认。

「最小面积」单位是平方米、「周长面积比大于」单位是米/平方米：几何面积与周长
不在地理坐标系上直接量，而是由后端先把要素转到**江西区域 Albers 等积投影**
（``CGCS2000 / Jiangxi_Albers_Equal_Area``）后取平面值 —— 等积投影的平面面积
即真实面积，与国土 / 林业面积统计口径一致。

点「确认」在**输入图层**上执行检查，命中要素另存到「输出图层」：填了输出路径
就写盘并从文件加载，留空则落临时 memory 图层；两种情况都自动加入工程并选中 +
打开属性表。

为避免大图层卡死界面，检查不是一次性同步跑完：后端 check 提供
:meth:`~app.core.domain.check.builtin.space_small_area.SpaceSmallAreaCheck.iter_failed`
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
from qgis.PyQt.QtGui import QBrush, QColor, QDoubleValidator, QPainter, QPen
from qgis.PyQt.QtWidgets import (
    QButtonGroup,
    QComboBox,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QProgressBar,
    QPushButton,
    QRadioButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from app.core.domain.check import CheckParam, CheckService
from app.core.domain.check.result_layer import build_result_layer
from app.infra.logging import get_logger
from app.infra.messagebar import send
from app.ui.common.base_dialog import BaseDialog
from app.ui.common.field_combo import make_field_combo_searchable

logger = get_logger(__name__)

#: 后端质检类型注册 id
CHECK_TYPE_ID = "space_small_area"

#: 最小面积默认阈值（平方米）
DEFAULT_MIN_AREA = "20"

#: 周长面积比默认阈值（米/平方米）
DEFAULT_PA_RATIO = "10"

#: 参考提示说明文案
DESC_TEXT = "检查源图层中面积小于设置值的面要素"

#: 输出图层名兜底（输出路径为空时用，落临时 memory 图层）
DEFAULT_OUTPUT_NAME = "细碎面结果"

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


def _criteria_text(min_area: float, pa_ratio: float, check_multipart: bool) -> str:
    """把生效的判定条件拼成一句人话，用于提示 / 结果说明。"""
    parts = []
    if min_area > 0:
        parts.append(f"面积小于 {min_area:g} 平方米")
    if pa_ratio > 0:
        parts.append(f"周长面积比大于 {pa_ratio:g} 米/平方米")
    if check_multipart:
        parts.append("多部件")
    return " 或 ".join(parts) if parts else "未启用任何条件"


class HintDiagram(QWidget):
    """「参考提示」右侧的示意图（按设计稿 ``resource/reeee/image.png`` 矢量重绘）。

    设计稿这里不是文字，而是一张小图：三个图斑（一红两蓝）配几道黄色标注线，
    用来示意"碎、小"的面要素。设计稿只给了位图，这里按测得坐标等比重绘成矢量，
    随控件大小自适应且不引入额外图片资源。
    """

    #: 设计稿图形区尺寸（测得），下面的坐标都按这块画布归一化
    _W = 270.0
    _H = 70.0
    #: 图斑（椭圆）：``(cx, cy, rx, ry, color)``
    _DOTS = (
        (36.0, 40.0, 21.0, 18.0, "#ff0004"),
        (135.5, 40.0, 29.5, 26.0, "#98ccfe"),
        (233.5, 38.0, 34.5, 29.0, "#98ccfe"),
    )
    #: 黄色标注线：``(x, y1, y2)``
    _BARS = (
        (1.0, 14.0, 40.0),
        (70.0, 14.0, 40.0),
        (100.0, 6.0, 31.0),
        (169.0, 6.0, 31.0),
        (201.5, 2.0, 25.0),
        (264.5, 2.0, 25.0),
    )
    #: 取色自设计稿（示意图是插画，不跟随主题换色）
    _BAR = "#f4f618"

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setMinimumHeight(74)
        self.setMinimumWidth(200)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        # 等比缩放 + 居中，保证不同尺寸下图形不变形
        scale = min(self.width() / self._W, self.height() / self._H)
        painter.translate((self.width() - self._W * scale) / 2, (self.height() - self._H * scale) / 2)
        painter.scale(scale, scale)

        # 标注线先画，图斑压在上面（设计稿里线被图斑遮住的部分不可见）
        pen = QPen(QColor(self._BAR))
        pen.setWidthF(3.0)
        pen.setCapStyle(Qt.PenCapStyle.FlatCap)
        painter.setPen(pen)
        for x, y1, y2 in self._BARS:
            painter.drawLine(QPointF(x, y1), QPointF(x, y2))

        for cx, cy, rx, ry, color in self._DOTS:
            fill = QColor(color)
            painter.setBrush(QBrush(fill))
            pen = QPen(fill.darker(140))
            pen.setWidthF(1.5)
            painter.setPen(pen)
            painter.drawEllipse(QPointF(cx, cy), rx, ry)
        painter.end()


class SmallFaceDialog(BaseDialog):
    """细碎面处理（面积过小 / 形状细碎）配置 / 执行 Dialog。"""

    def __init__(self, parent=None, title: str = "细碎面处理") -> None:
        super().__init__(parent, title)
        # 尺寸与「质检 → 狭长面处理」窗口同构（左参数 + 右说明两栏，参数更多故略高）
        self.setMinimumSize(760, 620)
        self.check_service = CheckService()
        self._cancelled = False
        self._build()
        self._populate_layers()

    # ------------------------------------------------------------ 构建

    def _build(self) -> None:
        root = QVBoxLayout()

        # ---- 左右两部分：左参数 + 右说明（同狭长面窗口；右侧「参考提示」
        #      相当于算法窗口里的算法说明栏）----
        main = QHBoxLayout()
        main.setSpacing(12)

        param_panel = QWidget()
        self._init_parameters_panel(param_panel)
        main.addWidget(param_panel, stretch=3)
        main.addWidget(self._make_hint_panel(), stretch=2)
        root.addLayout(main, stretch=1)

        # ---- 处理进度 + 按钮（同狭长面窗口：进度条在上、按钮右对齐）----
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
        """左侧参数面板：字段名在上、控件在下（与狭长面窗口写法一致）。"""
        layout = QVBoxLayout(parent)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        # 1. 输入图层（只收矢量图层）
        layout.addWidget(QLabel("输入图层"))
        self.input_combo = QComboBox()
        self.input_combo.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self.input_combo.currentIndexChanged.connect(self._on_input_changed)
        layout.addWidget(self.input_combo)
        layout.addSpacing(4)

        # 2. 过滤条件 + 「配置」（表达式构建器）：行内按钮，同狭长面窗口的「浏览…」
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

        # 3. 类型选择：几何面积 / 属性面积；选「属性面积」时用下面的面积字段取值
        layout.addWidget(QLabel("类型选择"))
        type_row = QHBoxLayout()
        self.geom_radio = QRadioButton("几何面积")
        self.attr_radio = QRadioButton("属性面积")
        self.geom_radio.setChecked(True)
        self.area_group = QButtonGroup(self)
        self.area_group.addButton(self.geom_radio)
        self.area_group.addButton(self.attr_radio)
        self.geom_radio.toggled.connect(self._sync_area_field)
        type_row.addWidget(self.geom_radio)
        type_row.addWidget(self.attr_radio)
        type_row.addSpacing(12)
        layout.addLayout(type_row)

        # 4. 面积字段（仅「属性面积」时可用）
        layout.addWidget(QLabel("面积字段"))
        self.area_field_combo = QComboBox()
        make_field_combo_searchable(self.area_field_combo)
        self.area_field_combo.setEnabled(False)
        layout.addWidget(self.area_field_combo)
        layout.addSpacing(4)

        # 5. 最小面积阈值 + 单位（比上面几个输入框窄一些）
        layout.addWidget(QLabel("最小面积"))
        min_row = QHBoxLayout()
        self.min_area_input = QLineEdit(DEFAULT_MIN_AREA)
        self.min_area_input.setValidator(QDoubleValidator(0.0, 1e15, 6, self))
        self.min_area_input.setFixedWidth(140)
        min_row.addWidget(self.min_area_input)
        min_row.addWidget(self._unit_label("平方米"))
        min_row.addStretch(1)
        layout.addLayout(min_row)
        layout.addSpacing(4)

        # 6. 周长面积比阈值 + 单位
        layout.addWidget(QLabel("周长面积比大于"))
        pa_row = QHBoxLayout()
        self.pa_ratio_input = QLineEdit(DEFAULT_PA_RATIO)
        self.pa_ratio_input.setValidator(QDoubleValidator(0.0, 1e15, 6, self))
        self.pa_ratio_input.setFixedWidth(140)
        pa_row.addWidget(self.pa_ratio_input)
        pa_row.addWidget(self._unit_label("米/平方米"))
        pa_row.addStretch(1)
        layout.addLayout(pa_row)
        layout.addSpacing(4)

        # 7. 多部件检查：面要素是否允许多部件（否 = 多部件视为细碎面）
        layout.addWidget(QLabel("多部件检查"))
        self.multi_combo = QComboBox()
        self.multi_combo.addItem("否", False)
        self.multi_combo.addItem("是", True)
        self.multi_combo.setFixedWidth(140)
        layout.addWidget(self.multi_combo)
        layout.addSpacing(4)

        # 8. 输出图层：放最后；文件选择器控制落盘路径，留空则落临时 memory 图层
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

    @staticmethod
    def _unit_label(text: str) -> QLabel:
        """参数行末尾的单位文字（浅色小字）。"""
        lbl = QLabel(text)
        lbl.setObjectName("PropHint")
        return lbl

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
        """用工程里的矢量图层填输入图层下拉框，默认选中上下文图层。"""
        layers = [
            layer
            for layer in QgsProject.instance().mapLayers().values()
            if isinstance(layer, QgsVectorLayer) and layer.isValid()
        ]
        # 面积判定只对面要素有意义，面图层排前面（不过滤，源数据可能是复合类型）
        layers.sort(key=lambda layer: layer.geometryType() != QgsWkbTypes.PolygonGeometry)
        context = self._current_layer()
        context_id = context.id() if context is not None else None

        self.input_combo.blockSignals(True)
        self.input_combo.clear()
        for layer in layers:
            self.input_combo.addItem(layer.name(), layer)
        index = 0 if layers else -1
        for i in range(self.input_combo.count()):
            item = self.input_combo.itemData(i)
            if context_id is not None and item is not None and item.id() == context_id:
                index = i
                break
        self.input_combo.setCurrentIndex(index)
        self.input_combo.blockSignals(False)
        self._populate_fields()

    def _populate_fields(self) -> None:
        """用输入图层的数值字段填「面积字段」下拉框。"""
        self.area_field_combo.clear()
        layer = self._input_layer()
        if layer is not None:
            for field in layer.fields():
                if field.isNumeric():
                    self.area_field_combo.addItem(field.name(), field.name())
        self._sync_area_field()

    def _sync_area_field(self) -> None:
        """仅「属性面积」时面积字段可选。"""
        self.area_field_combo.setEnabled(self.attr_radio.isChecked())

    # ------------------------------------------------------------ 交互

    def _on_input_changed(self) -> None:
        self._populate_fields()

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

    def _read_number(self, edit: QLineEdit, label: str) -> float | None:
        """读一个阈值输入框；空串按 0（不启用该条件），非法值返回 ``None``。"""
        text = edit.text().strip()
        if not text:
            return 0.0
        try:
            value = float(text)
        except (TypeError, ValueError):
            send("warning", f"{label}必须是数字", duration=3)
            edit.setFocus()
            return None
        if value < 0:
            send("warning", f"{label}不能为负数", duration=3)
            edit.setFocus()
            return None
        return value

    def _on_confirm(self) -> None:
        """「确认」：校验参数后排队执行（不留在 clicked 信号栈里跑）。"""
        layer = self._input_layer()
        if layer is None:
            send("warning", "请先在图层列表选择要质检的矢量图层", duration=3)
            self.input_combo.setFocus()
            return

        min_area = self._read_number(self.min_area_input, "最小面积")
        if min_area is None:
            return
        pa_ratio = self._read_number(self.pa_ratio_input, "周长面积比")
        if pa_ratio is None:
            return
        check_multipart = bool(self.multi_combo.currentData())
        if min_area <= 0 and pa_ratio <= 0 and not check_multipart:
            send("warning", "请至少启用一个条件（最小面积 / 周长面积比 / 多部件检查）", duration=4)
            self.min_area_input.setFocus()
            return

        area_type = "attr" if self.attr_radio.isChecked() else "geom"
        area_field = self.area_field_combo.currentData() if area_type == "attr" else None
        if area_type == "attr" and not area_field:
            send("warning", "请为「属性面积」选择面积字段", duration=3)
            self.area_field_combo.setFocus()
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
            extra={
                "type_id": CHECK_TYPE_ID,
                "min_area": min_area,
                "pa_ratio": pa_ratio,
                "check_multipart": check_multipart,
                "area_type": area_type,
                "area_field": area_field,
            },
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
            logger.error("细碎面检查执行失败：%s", exc, exc_info=True)
            send("critical", f"细碎面检查执行失败：{exc}", duration=5)
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
                seen, fid, _area = next(self._iterator)
                self._seen = seen
                self._failed.append(fid)
        except StopIteration:
            # 落层 / addMapLayer 再推一拍，不和检查循环挤在同一个事件里
            QTimer.singleShot(0, self._on_check_done)
            return
        except Exception as exc:  # 同上：已记录完整栈
            logger.error("细碎面检查执行失败：%s", exc, exc_info=True)
            send("critical", f"细碎面检查执行失败：{exc}", duration=5)
            self._finish_failed()
            return

        pct = min(int(90 * self._seen / self._total), 90) if self._total else self.progress.value()
        self._set_progress(pct, f"正在检查…（已处理 {self._seen}）")
        QTimer.singleShot(0, self._step)

    def _on_check_done(self) -> None:
        """检查迭代结束：无命中直接收尾，有命中则落「输出图层」。"""
        if self._cancelled:
            return
        param = self._param
        layer = param.layer
        min_area = float(param.extra.get("min_area", 0) or 0)
        pa_ratio = float(param.extra.get("pa_ratio", 0) or 0)
        check_multipart = bool(param.extra.get("check_multipart"))
        criteria = _criteria_text(min_area, pa_ratio, check_multipart)
        failed = self._failed
        if not failed:
            self._set_progress(100, "检查完成")
            send("success", f"未发现{criteria}的细碎面要素", duration=3)
            self.accept()
            return

        self._set_progress(90, "正在输出图层…")
        output_path = self._output_path
        output_name = (
            os.path.splitext(os.path.basename(output_path))[0]
            if output_path
            else DEFAULT_OUTPUT_NAME
        )
        try:
            output = build_result_layer(layer, failed, output_name)
            if output_path:
                _write_vector_layer(output, output_path)
                output = QgsVectorLayer(output_path, output_name, "ogr")
                if not output.isValid():
                    raise RuntimeError(f"输出文件加载失败：{output_path}")
        except Exception as exc:  # 同上：已记录完整栈
            logger.error("细碎面输出图层生成失败：%s", exc, exc_info=True)
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
            f"发现 {len(failed)} 个{criteria}的细碎面要素，已写入「{output_name}」{dest}",
            duration=5,
        )
        self.accept()

    def _finish_failed(self) -> None:
        """执行失败后恢复按钮，让用户能改参数重试。"""
        self.progress_label.setText("执行失败")
        self.confirm_btn.setEnabled(True)


__all__ = [
    "CHECK_TYPE_ID",
    "DEFAULT_MIN_AREA",
    "DEFAULT_OUTPUT_NAME",
    "DEFAULT_PA_RATIO",
    "HintDiagram",
    "SmallFaceDialog",
]
