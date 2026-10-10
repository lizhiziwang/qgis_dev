"""单要素属性录入弹窗。

「属性录入」工具（见 :mod:`app.ui.tools.attribute_entry`）点击要素后，由
:class:`AttributeEntryDialog` 展示该要素的**全部属性字段**并提供就地编辑；
点「确认修改」把改动一次性写回图层的编辑缓冲（一个可整体撤销的编辑命令）。

原先是右侧的 ``QDockWidget``：字段多时要把右侧挤得很宽，而点要素录入时更需要
"就地把这一条改完"，所以改成**非模态弹窗**（与「节点列表」同一套做法：单实例
复用、``show()/raise_()``）。

编辑约束：

- 属性录入**不需要**用户先「开始编辑」：工具条激活该工具时会让目标图层
  自动进入编辑态；本弹窗载入 / 确认时仍校验 ``layer.isEditable()``，
  以防激活后图层被外部停止编辑（此时只提示、不动数据）；
- 写回走 QGIS 编辑缓冲 ``changeAttributeValue``（同一 fid 原地改属性，
  不新建要素），``beginEditCommand`` 把一次确认内的多个字段改动包成
  **一个**撤销步骤，可整体 Ctrl+Z 撤销；
- 载入表单时以图层**当前**状态为准：若该要素此前已被其它工具改过属性
  （编辑缓冲里的未提交修改），表单显示合并后的最新值，防止用旧值覆盖。

字段控件映射（实测 QGIS 4.2 中 ``feature.attribute(i)`` 的 Python 类型）：

- 文本（String 等）→ ``QLineEdit``（空文本 = 空值 NULL）；
- 整型 / 浮点 → ``QLineEdit`` + 数值校验（空文本 = NULL；解析失败拒绝保存）；
- 布尔 → 三态 ``QCheckBox``（半选 = NULL，避免把 NULL 悄悄改成 False）；
- 日期 / 时间 / 日期时间 → :class:`app.ui.common.date_field.DatePickerField`
  （文本框内嵌 × + ▼ 弹选择器；**空文本 = NULL**）。不用 ``QDateEdit`` 一族：
  它们在本应用主题下日历点不开，还带着上下微调箭头；
- 字段编辑器配置为 Hidden 的字段跳过不显示。
"""

from __future__ import annotations

from typing import Callable, Optional

from qgis.PyQt import sip
from qgis.PyQt.QtCore import QDate, QDateTime, QTime, Qt, QVariant
from qgis.PyQt.QtWidgets import (
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from qgis.core import QgsField, QgsVectorLayer

from app.infra.logging import get_logger
from app.ui.common.base_dialog import BaseDialog
from app.ui.common.date_field import make_date_field
from app.ui.common.theme import ThemeManager
from app.ui.tools.feature_attrs import (
    auto_id_field_index,
    field_alias,
    uid_field_index,
)

logger = get_logger(__name__)


class AttributeEntryDialog(BaseDialog):
    """属性录入**弹窗**：展示 + 编辑单个要素的全部属性，确认后写回编辑缓冲。

    原先是右侧的 ``QDockWidget``。改成弹窗是因为它天生与画布争地方：字段多时
    要把右侧挤得很宽，而用户点要素时更需要的是"就地把这一条改完"。与「节点列表」
    同一套做法：非模态、单实例复用、``show()/raise_()``。
    """

    def __init__(self, main_window, parent=None) -> None:
        super().__init__(parent or main_window, title="属性录入")
        self.setMinimumSize(420, 420)
        self.resize(460, 560)
        self.setModal(False)

        #: 画布（最小化小条要浮在它底部；测试桩可能没有 canvas）
        self._canvas = getattr(main_window, "canvas", None)
        #: 最小化小条（懒建）与最小化前的几何
        self._mini_bar = None
        self._restore_geo = None

        #: 正在编辑的目标图层（非 owner；可能随时被停止编辑）
        self._layer: QgsVectorLayer | None = None
        #: 正在编辑的要素 fid
        self._fid: int | None = None
        #: 载入时的字段原值快照 {fieldIdx: value}（含编辑缓冲已合并值），
        #: 供确认时比较"是否有改动"。
        self._orig: dict[int, object] = {}
        #: 表单行 [(fieldIdx, 编辑器 widget, 读值回调), ...]
        self._rows: list[tuple[int, QWidget, Callable[[], object]]] = []
        #: 确认后用于重新快照的读值回调
        self._readers: list[tuple[int, Callable[[], object]]] = []

        # 最小化按钮改成"缩成画布底部小条"，不是系统的 showMinimized
        # （无边框弹窗最小化后只剩任务栏一个条目，等于消失了）。FramelessMixin
        # 在 enable_frameless 里把它接到了 showMinimized，这里改接。
        try:
            self.title_bar.minimize_requested.disconnect()
        except TypeError:
            pass
        self.title_bar.minimize_requested.connect(self._minimize_to_bar)

        # 裸构造的 QVBoxLayout 再 setLayout —— BaseDialog 的约定（见其 docstring），
        # 用 QVBoxLayout(self) 内容会静默不显示
        root = QVBoxLayout()
        root.setContentsMargins(12, 10, 12, 10)

        self._head = QLabel("激活「属性录入」工具后，点击地图上的要素以载入其属性。")
        self._head.setWordWrap(True)
        self._head.setStyleSheet("font-weight: bold;")
        root.addWidget(self._head)

        # 字段表单（滚动）
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        self._body = QWidget()
        self._form = QVBoxLayout(self._body)
        self._form.setContentsMargins(4, 4, 4, 4)
        self._form.setSpacing(4)
        self._form.addStretch(1)
        scroll.setWidget(self._body)
        root.addWidget(scroll, 1)

        self._status = QLabel("")
        self._status.setWordWrap(True)
        root.addWidget(self._status)

        # 按钮行：右下角、按文字宽度（不加进布局拉伸，就不会铺满整行）
        self._apply_btn = QPushButton("确认修改")
        self._apply_btn.setEnabled(False)
        self._apply_btn.clicked.connect(self.apply_edits)
        self._cancel_btn = QPushButton("取消")
        self._cancel_btn.setToolTip("关闭窗口，不保存本次修改")
        self._cancel_btn.clicked.connect(self.reject)
        btns = QHBoxLayout()
        btns.addStretch(1)
        btns.addWidget(self._cancel_btn)
        btns.addWidget(self._apply_btn)
        root.addLayout(btns)

        self.setLayout(root)

    # ========================================================== 外部 API

    def load_feature(self, layer: QgsVectorLayer, fid: int) -> None:
        """载入要素属性到表单（工具点击要素后调用）。

        图层须处于编辑态；否则仅提示并清空表单。载入前会**丢弃**当前
        表单中尚未确认的改动（用户须先点「确认修改」再点其它要素）。
        """
        self._clear_form()
        self._layer = layer
        self._fid = fid
        if layer is None:
            self._set_status("请先「开始编辑」再点击要素。")
            return
        if layer.type() != layer.VectorLayer or not layer.isEditable():
            self._set_status(f"图层「{layer.name()}」未处于编辑态，无法录入属性。")
            return

        attrs = self._read_attrs(layer, fid)
        if attrs is None:
            self._set_status(f"要素 FID {fid} 已不存在（可能已被删除）。")
            return

        fields = layer.fields()
        self._head.setText(f"图层「{layer.name()}」 · 要素 FID {fid}：")
        # 全局唯一标识（MZGUID / FGUID / GlobalID…）**只给自动赋值**，不给手改：
        # 手工改这个值会破坏"一要素一标识"的约定，分割 / 分解 / 绘制的赋新值逻辑
        # 也会被绕过。这里仍展示（用户要看得见），但输入框只读。
        uid_idx = uid_field_index(fields)
        # objectid / ogc_fid 这类由数据源自动赋值的列同样不给手改（保存时自己会填）
        auto_id_idx = auto_id_field_index(fields)
        added = 0
        for i in range(fields.count()):
            fld = fields[i]
            # 隐藏字段（图层属性里配置为 Hidden 的编辑器）不展示
            try:
                if fld.editorWidgetSetup().type() == "Hidden":
                    continue
            except Exception:  # noqa: BLE001
                pass
            value = attrs.get(i)
            readonly = i in (uid_idx, auto_id_idx)
            row_widget, editor, read = self._make_editor(fld, value, readonly=readonly)
            if row_widget is None:
                continue
            # 显示**字段别名**（成果数据的别名才是中文可读的那个），没有别名才退回
            # 字段名；真实字段名放 tooltip 里备查。与属性表 / 属性查询同一口径。
            alias = field_alias(fld)
            name_label = QLabel(alias or fld.name())
            name_label.setToolTip(fld.name() if alias else "")
            # 标签别占太宽：太宽会把输入框挤出去、出现横向滚动条；但太窄又会把
            # 中文别名折成两三行
            name_label.setMinimumWidth(100)
            name_label.setMaximumWidth(140)
            name_label.setWordWrap(True)
            hb = QHBoxLayout()
            hb.setContentsMargins(0, 0, 0, 0)
            hb.addWidget(name_label)
            hb.addWidget(editor, 1)
            row_widget.setLayout(hb)
            self._form.insertWidget(self._form.count() - 1, row_widget)
            self._rows.append((i, editor, read))
            if readonly:
                # 只读字段**不进写回清单**：setReadOnly 只挡用户输入，写回是读框里
                # 的文本（程序改一下就会被原样写库）。唯一标识只能由系统赋值，所以
                # 干脆不参与"读值 → 比较 → 写回"这条链。
                continue
            self._readers.append((i, read))
            added += 1

        # 原值快照 = 表单当前读值（含 NULL 归一化），用于确认时比较
        self._orig = {i: read() for i, read in self._readers}
        self._apply_btn.setEnabled(True)
        if added == 0:
            self._set_status("该图层没有可编辑的属性字段。")
        else:
            self._set_status("已载入：修改字段后点「确认修改」保存（可 Ctrl+Z 撤销）。")

    def apply_edits(self) -> None:
        """确认：把表单改动一次性写回编辑缓冲（一个可撤销的编辑命令）。"""
        layer = self._layer
        fid = self._fid
        if layer is None or fid is None:
            return
        if not layer.isEditable():
            self._set_status("图层已停止编辑，改动未保存：请先「开始编辑」。")
            return

        # 逐行读值 + 校验
        changes: list[tuple[int, object]] = []
        for i, read in self._readers:
            try:
                new = read()
            except ValueError as exc:
                self._set_status(str(exc))
                return
            old = self._orig.get(i)
            if not self._same_value(old, new):
                changes.append((i, new))

        if not changes:
            self._set_status("没有需要保存的修改。")
            return

        layer.beginEditCommand("属性录入")
        try:
            for i, value in changes:
                if not layer.changeAttributeValue(fid, i, value):
                    # destroyEditCommand = 丢弃本次改动并关闭命令（PyQGIS 没有
                    # cancelEditCommand 这个方法，写了会在 except 里再抛
                    # AttributeError，把命令留在悬着状态、撤销栈永久失效）
                    layer.destroyEditCommand()
                    self._set_status(f"字段「{layer.fields()[i].name()}」写入失败，本次修改已全部取消。")
                    return
        except Exception:  # noqa: BLE001 —— 收宽：任何异常都必须关掉编辑命令
            layer.destroyEditCommand()
            self._set_status("写入失败：图层编辑 API 异常，本次修改已全部取消。")
            return
        layer.endEditCommand()

        # 更新快照 = 刚写入的值（控件仍显示用户输入，未动）
        self._orig = {i: read() for i, read in self._readers}
        self._set_status(f"已保存 {len(changes)} 个字段（可 Ctrl+Z 撤销）。")

    # ========================================================== 表单构造

    def _make_editor(
        self, fld: QgsField, value: object, readonly: bool = False
    ) -> tuple[Optional[QWidget], Optional[QWidget], Optional[Callable[[], object]]]:
        """按字段类型建编辑器行控件。

        :param readonly: 只读（唯一标识字段用）：仍然展示值，但不给用户改。
        :returns: (行容器 widget, 编辑器 widget, 读值回调)。不支持的字段返回
            (None, None, None)（由调用方跳过，不占行）。
        """
        ftype = fld.type()
        tip = "该字段由系统自动赋值，不可手工修改" if readonly else ""
        if ftype == QVariant.Bool:
            cb = QCheckBox()
            cb.setTristate(True)
            if value is None:
                cb.setCheckState(Qt.CheckState.PartiallyChecked)
            else:
                cb.setChecked(bool(value))
            if readonly:
                cb.setEnabled(False)   # QCheckBox 没有 setReadOnly，用禁用表达只读
                cb.setToolTip(tip)
            box = QWidget()

            def read_bool(cb=cb) -> object:
                state = cb.checkState()
                if state == Qt.CheckState.Checked:
                    return True
                if state == Qt.CheckState.Unchecked:
                    return False
                return None  # 半选 = NULL

            return box, cb, read_bool

        if ftype == QVariant.Date:
            # 日期用自定义的「文本框（内嵌 ×）+ ▼ 弹日历」：QDateEdit 在本应用里
            # 日历点不开（主题样式接管了它的绘制），见 app.ui.common.date_field
            field, read_date = make_date_field(value, self._to_qdate, "date")
            if readonly:
                field.setEnabled(False)
                field.setToolTip(tip)
            box = QWidget()
            return box, field, read_date

        if ftype == QVariant.Time:
            # 时间 / 日期时间都换成自定义控件（见 make_date_field）：QTimeEdit /
            # QDateTimeEdit 带着上下微调箭头，而且日历点不开（用户实报「修改时间」
            # 那一行还是老样子）
            field, read_time = make_date_field(value, self._to_qtime, "time")
            if readonly:
                field.setEnabled(False)
                field.setToolTip(tip)
            box = QWidget()
            return box, field, read_time

        if ftype == QVariant.DateTime:
            field, read_datetime = make_date_field(value, self._to_qdatetime, "datetime")
            if readonly:
                field.setEnabled(False)
                field.setToolTip(tip)
            box = QWidget()
            return box, field, read_datetime

        # 数值 / 文本：QLineEdit，空文本 = NULL
        line = QLineEdit()
        # 有内容时右端出现 × 一键清空（QLineEdit 自带，与 QGIS 的输入框一致）
        line.setClearButtonEnabled(True)
        if value is None:
            line.setText("")
        else:
            line.setText(str(value))
        if readonly:
            line.setReadOnly(True)
            line.setToolTip(tip)
        numeric = ftype in (
            QVariant.Int,
            QVariant.LongLong,
            QVariant.Double,
            QVariant.UInt,
            QVariant.ULongLong,
        )
        box = QWidget()

        def read_line(line=line, fname=fld.name(), num=numeric) -> object:
            text = line.text().strip()
            if text == "":
                return None
            if num:
                try:
                    if ftype in (QVariant.Int, QVariant.LongLong):
                        return int(text)
                    return float(text)
                except ValueError as exc:
                    raise ValueError(
                        f"字段「{fname}」的值「{text}」不是有效数字，请修正后再确认。"
                    ) from exc
            return text

        return box, line, read_line

    # ========================================================== 内部

    def _read_attrs(self, layer: QgsVectorLayer, fid: int) -> Optional[dict]:
        """读要素当前属性（合并编辑缓冲里已存在的未提交修改）。

        直接 ``layer.getFeature`` 读到的是 data provider 里的原始值，不含
        编辑缓冲中其它工具/命令的修改；若要素被先前命令改过属性而用户又
        来录入，需以缓冲合并值为准（防止用旧值覆盖新改动）。

        :returns: {fieldIdx: value}；要素已不存在 / 已删除返回 None。
        """
        buffer = layer.editBuffer()
        if buffer is not None and fid in buffer.deletedFeatureIds():
            return None
        feature = layer.getFeature(fid)
        if not feature.isValid():
            return None
        count = layer.fields().count()
        values = {i: feature.attribute(i) for i in range(count)}
        if buffer is not None:
            changed = buffer.changedAttributeValues().get(fid)
            if changed:
                for i, v in changed.items():
                    if isinstance(v, QVariant):
                        v = v.value()
                    values[i] = v
        return values

    def _clear_form(self) -> None:
        """清空表单（销毁旧行控件，重置状态）。"""
        while self._form.count() > 1:
            item = self._form.takeAt(0)
            w = item.widget()
            if w is not None:
                try:
                    w.deleteLater()
                except RuntimeError:
                    pass
        self._rows.clear()
        self._readers.clear()
        self._orig.clear()
        self._layer = None
        self._fid = None
        self._apply_btn.setEnabled(False)

    def _set_status(self, text: str) -> None:
        self._status.setText(text)

    # ============================================== 关闭 / 最小化

    def done(self, result: int) -> None:  # noqa: N802 —— Qt 命名
        """关闭（× / 取消 / Esc / 程序化）：收起小条，并清掉要素的选中高亮。

        QDialog 的所有关闭路径最终都汇到 ``done``，是唯一的收口点（与
        「节点列表」同一做法）。
        """
        self._hide_mini_bar()
        self._clear_selection()
        super().done(result)

    def showEvent(self, event) -> None:  # noqa: N802 —— Qt 命名
        # 从最小化小条恢复、或点要素重新载入时，小条要让位
        self._hide_mini_bar()
        super().showEvent(event)

    def _clear_selection(self) -> None:
        """清掉本要素的选中高亮（#8）。

        **只清"正好是这一个"的选中集**：载入时工具把选中集替换成了该要素
        （见 :meth:`AttributeEntryTool._select_only`），确认无误才动；用户若在
        别处改了选中集就不碰。
        """
        layer = self._layer
        fid = self._fid
        if layer is None or fid is None or sip.isdeleted(layer):
            return
        try:
            if list(layer.selectedFeatureIds()) != [fid]:
                return
            layer.removeSelection()
        except (RuntimeError, AttributeError):
            logger.debug("清除属性录入选中高亮失败（不影响关闭）", exc_info=True)

    def _minimize_to_bar(self) -> None:
        """最小化：藏起弹窗，在画布底部浮出一条小条（#9）。"""
        if self._canvas is None:  # 测试桩 / 无画布：退回系统最小化
            self.showMinimized()
            return
        self._restore_geo = self.geometry()
        self.hide()
        bar = self._ensure_mini_bar()
        bar.set_summary(self._summary_text())
        bar.show()
        bar.raise_()
        bar.reposition()

    def _restore_from_bar(self) -> None:
        self._hide_mini_bar()
        if self._restore_geo is not None:
            self.setGeometry(self._restore_geo)
        self.show()
        self.raise_()

    def _summary_text(self) -> str:
        """小条上那行字：图层 + 要素（拿不到就退回弹窗标题）。"""
        layer = self._layer
        if layer is None or self._fid is None:
            return self.windowTitle()
        try:
            name = layer.name()
            feature = layer.getFeature(self._fid)
            for idx in (auto_id_field_index(layer.fields()), uid_field_index(layer.fields())):
                if idx is not None and feature.isValid():
                    value = feature.attribute(idx)
                    if value not in (None, ""):
                        return f"{name} · {value}"
        except (RuntimeError, AttributeError):
            pass
        return f"{name} · FID {self._fid}"

    def _ensure_mini_bar(self):
        if self._mini_bar is None or sip.isdeleted(self._mini_bar):
            self._mini_bar = _MinimizedBar(self._canvas, on_restore=self._restore_from_bar)
        return self._mini_bar

    def _hide_mini_bar(self) -> None:
        bar = self._mini_bar
        if bar is not None and not sip.isdeleted(bar):
            bar.hide()

    # -------------------------------------------------- 类型换算

    @staticmethod
    def _same_value(a: object, b: object) -> bool:
        """宽松相等比较：数值做近似比较，避免浮点显示/解析抖动误报改动。"""
        if a is None or b is None:
            return a is b
        if isinstance(a, (int, float)) and isinstance(b, (int, float)):
            return abs(float(a) - float(b)) < 1e-9
        try:
            return bool(a == b)
        except Exception:  # noqa: BLE001
            return a is b

    @staticmethod
    def _to_qdate(value) -> QDate:
        """QDate / python date → QDate。"""
        if isinstance(value, QDate):
            return value
        try:
            py = value.toPyDate()
        except AttributeError:
            py = value
        return QDate(py.year, py.month, py.day)

    @staticmethod
    def _to_qtime(value) -> QTime:
        if isinstance(value, QTime):
            return value
        try:
            py = value.toPyTime()
        except AttributeError:
            py = value
        return QTime(py.hour, py.minute, py.second)

    @staticmethod
    def _to_qdatetime(value) -> QDateTime:
        if isinstance(value, QDateTime):
            return value
        try:
            return QDateTime(value)
        except Exception:  # noqa: BLE001
            pass
        # python datetime
        dt = value.toPyDateTime() if hasattr(value, "toPyDateTime") else value
        return QDateTime(dt.year, dt.month, dt.day, dt.hour, dt.minute, dt.second)


class _MinimizedBar(QFrame):
    """弹窗最小化后浮在**画布底部**的细条：一行摘要 + 「恢复」。

    作为画布的子控件（仓库既定覆盖层模式，见
    :class:`app.ui.tools.measure.MeasureReadout`）：不占布局、不挤地图，画布
    尺寸变化靠 ``mapCanvasRefreshed`` 回调重新贴底。

    与测量读数条的唯一区别：**这里不能透明穿透鼠标**——「恢复」按钮要能点。
    """

    def __init__(self, canvas, on_restore) -> None:
        super().__init__(canvas)
        self._canvas = canvas
        self._on_restore = on_restore
        self.setObjectName("AttrEntryMinBar")
        self.setCursor(Qt.CursorShape.ArrowCursor)
        self.hide()

        row = QHBoxLayout(self)
        row.setContentsMargins(10, 4, 10, 4)
        row.setSpacing(8)
        self._label = QLabel("")
        row.addWidget(self._label, 1)
        restore = QPushButton("恢复")
        restore.setObjectName("AttrEntryMinRestore")
        restore.setCursor(Qt.CursorShape.ArrowCursor)
        restore.clicked.connect(self._restore)
        row.addWidget(restore)

        self._apply_style()
        ThemeManager.instance().theme_changed.connect(self._on_theme_changed)
        canvas.mapCanvasRefreshed.connect(self.reposition)

    def set_summary(self, text: str) -> None:
        self._label.setText(text)
        self._label.setToolTip(text)

    def _restore(self) -> None:
        self._on_restore()

    def _on_theme_changed(self, _theme=None) -> None:
        self._apply_style()
        self.reposition()

    def _apply_style(self) -> None:
        t = ThemeManager.instance().current()
        self.setStyleSheet(
            f"""
            QFrame#AttrEntryMinBar {{
                background-color: {t.input_bg};
                border: 1px solid {t.input_border};
                border-radius: 4px;
            }}
            QFrame#AttrEntryMinBar QLabel {{ color: {t.menu_text}; font-weight: 600; }}
            QPushButton#AttrEntryMinRestore {{ padding: 2px 12px; }}
            """
        )

    def reposition(self) -> None:
        """贴画布底边、横跨整个宽度（画布 resize 后由 ``mapCanvasRefreshed`` 校正）。"""
        try:
            self.setGeometry(0, max(self._canvas.height() - self.sizeHint().height(), 0),
                             self._canvas.width(), self.sizeHint().height())
        except RuntimeError:  # 画布 C++ 对象已销毁
            pass


__all__ = ["AttributeEntryDialog"]

