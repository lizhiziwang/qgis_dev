"""新建要素时的属性编辑 Dialog。

迁自 :class:`component.CustomAttrDialog.CustomAttrDialog`。

**输入框初值取自要素本身**（不是一律 ``"NULL"``）：新建要素在调用前已经过
:func:`app.ui.tools.feature_attrs.new_feature_attributes` 预填——面积 / 周长类
字段按刚画的几何算好、唯一标识（FGUID / mzguid…）生成新值。早期实现把每个框
硬写成 ``"NULL"``，保存时再原样解析回真 NULL，等于把预填的值又抹掉了（用户看到
的是"明明有 MZGUID 字段却没赋值"）。只有要素上确实是空值时才用 ``"NULL"`` 占位
（聚焦即清空，保存时写真 NULL，见 :meth:`_parse_field_value`）。

**唯一标识字段只读且不参与写回**：它只能由系统赋值——手工改会破坏"一要素一标识"
的约定，也会绕过分割 / 分解 / 绘制的赋新值逻辑。只设为 ``setReadOnly`` 不够：本
对话框写回是读输入框文本，框里值一变照样会写库，所以 ``_save`` 里直接跳过它们。
"""

from __future__ import annotations

from typing import Optional

from qgis.PyQt.QtCore import QDate, QDateTime, QTime, Qt, QVariant
from qgis.PyQt.QtWidgets import (
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from qgis.core import QgsField, QgsVectorLayer

from app.core.domain.check.space_index import clear_spatial_index
from app.ui.common.base_dialog import BaseDialog
from app.ui.tools.feature_attrs import uid_field_index

#: 空值占位文本：既表示"没值"，也是保存时转真 NULL 的判据
_NULL_TEXT = "NULL"


class CustomAttrDialog(BaseDialog):
    """自定义属性编辑 Dialog。"""

    def __init__(self, layer: QgsVectorLayer, feature=None, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.layer = layer
        self.feature = feature
        self.attr_values = {}
        self._attr_fields = {}
        #: 只读字段下标：全局唯一标识（MZGUID / FGUID / GlobalID…）**只给自动赋值**，
        #: 手工改会破坏"一要素一标识"的约定，也会绕过分割 / 分解 / 绘制的赋新值逻辑
        uid_idx = uid_field_index(layer.fields())
        self._readonly_indices: set[int] = {uid_idx} if uid_idx is not None else set()
        self._init_ui()

    def _init_ui(self) -> None:
        self.setWindowTitle(f"{self.layer.name()} - 要素属性")
        self.setModal(True)
        # 无边框标题栏占高 34px，固定高度 +TITLE_BAR_HEIGHT 保住内容区原尺寸
        self.setFixedSize(500, 550 + self.title_bar.height())

        main = QVBoxLayout()
        main.setContentsMargins(10, 10, 10, 10)
        main.setSpacing(5)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        main.addWidget(scroll)

        form_widget = QWidget()
        scroll.setWidget(form_widget)

        form = QFormLayout(form_widget)
        form.setContentsMargins(0, 0, 0, 0)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        form.setFormAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.DontWrapRows)

        for idx, field in enumerate(self.layer.fields()):
            label = QLabel(field.name())
            label.setMinimumWidth(80)
            line_edit = QLineEdit()
            line_edit.setText(self._initial_text(idx, field))
            line_edit.focusInEvent = lambda e, le=line_edit: self._on_focus(le, e)
            if idx in self._readonly_indices:
                line_edit.setReadOnly(True)
                line_edit.setToolTip("该字段由系统自动赋值，不可手工修改")
            self.attr_values[field.name()] = line_edit
            self._attr_fields[field.name()] = field
            form.addRow(label, line_edit)

        button_container = QWidget()
        button_container.setObjectName("button_container")
        button_layout = QHBoxLayout(button_container)
        button_layout.setContentsMargins(5, 0, 0, 0)
        button_layout.setAlignment(Qt.AlignmentFlag.AlignRight)
        bb = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
            Qt.Orientation.Horizontal,
        )
        bb.button(QDialogButtonBox.StandardButton.Ok).setDefault(True)
        bb.accepted.connect(self._save)
        bb.rejected.connect(self.reject)
        button_layout.addWidget(bb)
        main.addWidget(button_container)
        self.setLayout(main)

    def _is_readonly(self, field: QgsField) -> bool:
        """字段是否只读（按下标判定，避免同名字段 / 别名造成的误判）。"""
        try:
            return self.layer.fields().indexOf(field.name()) in self._readonly_indices
        except Exception:  # noqa: BLE001
            return False

    def _initial_text(self, idx: int, field: QgsField) -> str:
        """输入框初值：要素上已有的值（新增要素已预填面积 / GUID）；无值用占位文本。"""
        value = self._feature_value(idx)
        if value is None:
            return _NULL_TEXT
        text = self._format_field_value(field, value).strip()
        return text or _NULL_TEXT

    def _feature_value(self, idx: int):
        """要素第 ``idx`` 个字段的值；没有要素 / 取不到返回 ``None``。"""
        if self.feature is None:
            return None
        try:
            attrs = self.feature.attributes()
        except Exception:  # noqa: BLE001 —— 要素已销毁等收尾场景
            return None
        return attrs[idx] if 0 <= idx < len(attrs) else None

    @staticmethod
    def _format_field_value(field: QgsField, value) -> str:
        """字段值 → 输入框文本（与 :meth:`_parse_field_value` 互逆）。

        日期 / 时间必须按解析时的格式串输出——直接 ``str(QDate)`` 会得到
        ``'PyQt6.QtCore.QDate(2026, 9, 29)'`` 这种 repr，保存时解析失败。
        """
        ftype = field.type()
        if ftype == QVariant.Date and isinstance(value, QDate):
            return value.toString("yyyy-MM-dd")
        if ftype == QVariant.Time and isinstance(value, QTime):
            return value.toString("HH:mm:ss")
        if ftype == QVariant.DateTime and isinstance(value, QDateTime):
            return value.toString("yyyy-MM-dd HH:mm:ss")
        if ftype == QVariant.Bool:
            return "是" if value else "否"
        return str(value)

    def _on_focus(self, line_edit, event) -> None:
        if line_edit.text() == _NULL_TEXT:
            line_edit.clear()
        QLineEdit.focusInEvent(line_edit, event)

    def _save(self) -> None:
        """点「确定」：按字段类型解析各输入框后写回要素，非法值弹窗提示并停留。

        旧实现把输入框文本(含未填时的默认占位 ``"NULL"``)原样
        ``feature.setAttribute`` —— 数值字段因此被写入字符串 ``"NULL"``，
        之后停止编辑点「是」提交时被数据源拒绝（获取 QString, 预期 double/int），
        表现为「没填属性 → 保存必然失败」。这里按字段类型转换：
        未填（默认 ``"NULL"`` / 空串）→ 真 ``NULL``；数值/日期/布尔等字段做类型
        解析；解析失败给出明确原因并保持对话框打开，等用户修正或取消。
        """
        parsed = []
        for name, editor in self.attr_values.items():
            field = self._attr_fields.get(name)
            if field is None:
                continue
            # 只读字段不参与写回：setReadOnly 只挡用户输入，这里是读框里的文本，
            # 框里值一变就会被原样写库（唯一标识只能由系统赋值）
            if self._is_readonly(field):
                continue
            try:
                value = self._parse_field_value(field, editor.text())
            except ValueError as exc:
                QMessageBox.warning(self, "属性错误", str(exc))
                return
            parsed.append((name, value))
        for name, value in parsed:
            self.feature.setAttribute(name, value)
        if not self.layer.addFeature(self.feature):
            QMessageBox.warning(self, "错误", "要素添加失败，请检查后重试！")
            return
        # 新增要素 → 空间索引缓存失效。不清的话点选 / 框选用的
        # ``intersect_features`` 会一直读旧索引，新画的要素怎么都选不中。
        clear_spatial_index(self.layer)
        self.close()

    @staticmethod
    def _parse_field_value(field: QgsField, text: str):
        """把输入框文本转成字段类型对应的值。

        未填（默认 ``"NULL"`` 占位或空串）一律返回 ``None``(真 NULL)，保证数值/
        日期等字段不会被塞入字符串 ``"NULL"`` 导致提交失败；其余按字段类型解析，
        非法输入抛 :class:`ValueError`（调用方弹窗提示）。
        """
        raw = (text or "").strip()
        if raw == "" or raw.upper() == _NULL_TEXT:
            return None
        ftype = field.type()
        if ftype in (QVariant.Int, QVariant.LongLong, QVariant.UInt, QVariant.ULongLong):
            try:
                return int(raw)
            except ValueError:
                raise ValueError(
                    f"字段「{field.name()}」的值「{raw}」不是有效整数，请修正后再确定。"
                ) from None
        if ftype == QVariant.Double:
            try:
                return float(raw)
            except ValueError:
                raise ValueError(
                    f"字段「{field.name()}」的值「{raw}」不是有效数字，请修正后再确定。"
                ) from None
        if ftype == QVariant.Date:
            date = QDate.fromString(raw, "yyyy-MM-dd")
            if not date.isValid():
                raise ValueError(
                    f"字段「{field.name()}」的值「{raw}」不是有效日期(yyyy-MM-dd)，请修正后再确定。"
                )
            return date
        if ftype == QVariant.Time:
            time = QTime.fromString(raw, "HH:mm:ss")
            if not time.isValid():
                raise ValueError(
                    f"字段「{field.name()}」的值「{raw}」不是有效时间(HH:mm:ss)，请修正后再确定。"
                )
            return time
        if ftype == QVariant.DateTime:
            dt = QDateTime.fromString(raw, "yyyy-MM-dd HH:mm:ss")
            if not dt.isValid():
                raise ValueError(
                    f"字段「{field.name()}」的值「{raw}」不是有效日期时间"
                    "(yyyy-MM-dd HH:mm:ss)，请修正后再确定。"
                )
            return dt
        if ftype == QVariant.Bool:
            lowered = raw.lower()
            if lowered in ("true", "1", "yes", "y", "是"):
                return True
            if lowered in ("false", "0", "no", "n", "否"):
                return False
            raise ValueError(
                f"字段「{field.name()}」的值「{raw}」不是有效布尔值"
                "(是/否、true/false、1/0)，请修正后再确定。"
            )
        # 文本及其它类型：原样返回字符串
        return raw

