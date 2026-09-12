"""新建要素时的属性编辑 Dialog。

迁自 :class:`component.CustomAttrDialog.CustomAttrDialog`。"""

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


class CustomAttrDialog(BaseDialog):
    """自定义属性编辑 Dialog。"""

    def __init__(self, layer: QgsVectorLayer, feature=None, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.layer = layer
        self.feature = feature
        self.attr_values = {}
        self._attr_fields = {}
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

        for field in self.layer.fields():
            label = QLabel(field.name())
            label.setMinimumWidth(80)
            line_edit = QLineEdit()
            line_edit.setText("NULL")
            line_edit.focusInEvent = lambda e, le=line_edit: self._on_focus(le, e)
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

    def _on_focus(self, line_edit, event) -> None:
        if line_edit.text() == "NULL":
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
        if raw == "" or raw.upper() == "NULL":
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

