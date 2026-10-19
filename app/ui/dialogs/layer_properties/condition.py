"""图层属性「条件」页：用表达式筛选图层显示的数据。

输入的表达式会写进图层的 ``subsetString``（QGIS 的「定义查询」），画布与属性表
只呈现满足条件的要素；留空即显示全部。写回推迟到 :meth:`ConditionTab.apply`
（点「确定 / 应用」），点「取消」不会改动图层。
"""

from __future__ import annotations

from qgis.core import QgsExpression, QgsVectorLayer
from qgis.PyQt.QtWidgets import (
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
)

from app.infra.logging import get_logger
from app.ui.common.code_editor import CodeEditor
from app.ui.dialogs.layer_properties.base import LayerPropertyTab

logger = get_logger(__name__)


def _field_names(layer) -> list[str]:
    if not isinstance(layer, QgsVectorLayer):
        return []
    try:
        fields = layer.fields()
        return [fields.at(i).name() for i in range(fields.size())]
    except Exception:  # noqa: BLE001
        logger.warning("读取字段名失败", exc_info=True)
        return []


def _condition_error(layer, condition: str) -> str | None:
    """检查表达式语法与字段引用；返回错误信息，``None`` 表示合法。"""
    expr = QgsExpression(condition)
    if expr.hasParserError():
        return f"表达式有语法错误：\n{expr.parserErrorString()}"
    valid = set(_field_names(layer))
    unknown = [
        name
        for name in expr.referencedColumns()
        if name not in valid and not name.startswith(("@", "$"))
    ]
    if unknown:
        return "无效字段：" + "、".join(unknown)
    return None


class ConditionTab(LayerPropertyTab):
    """图层属性「条件」页。"""

    def __init__(self, layer=None, parent=None) -> None:
        super().__init__(parent)
        self._layer = layer
        self._build()
        self.load(layer)

    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(10)

        box = QGroupBox("显示条件", self)
        layout = QVBoxLayout(box)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)

        hint = QLabel(
            "输入 QGIS 表达式，仅显示满足条件的要素；留空则显示全部。"
            '例如："类型" = \'道路\' AND "长度" > 100。'
            "修改后点「确定 / 应用」生效。",
            box,
        )
        hint.setObjectName("PropHint")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self.expr_edit = CodeEditor(box)
        self.expr_edit.setPlaceholderText("在此输入筛选表达式")
        layout.addWidget(self.expr_edit, 1)

        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        self.builder_btn = QPushButton("表达式构建器…", box)
        self.builder_btn.setToolTip("打开表达式构建器编辑显示条件")
        self.builder_btn.clicked.connect(self._open_builder)
        buttons.addWidget(self.builder_btn)
        self.clear_btn = QPushButton("清除条件", box)
        self.clear_btn.setToolTip("清空输入框；点「确定 / 应用」后恢复显示全部要素")
        self.clear_btn.clicked.connect(self.expr_edit.clear)
        buttons.addWidget(self.clear_btn)
        buttons.addStretch(1)
        layout.addLayout(buttons)

        root.addWidget(box, 1)

        self.status_label = QLabel("", self)
        self.status_label.setObjectName("PropHint")
        self.status_label.setWordWrap(True)
        root.addWidget(self.status_label)

    def _populate_completions(self) -> None:
        words = _field_names(self._layer)
        try:
            words.extend(f.name() for f in QgsExpression.Functions())
        except Exception:  # noqa: BLE001
            logger.warning("读取表达式函数列表失败", exc_info=True)
        self.expr_edit.set_completions(words)

    def _sync_enabled(self) -> None:
        is_vector = isinstance(self._layer, QgsVectorLayer)
        self.expr_edit.setEnabled(is_vector)
        self.builder_btn.setEnabled(is_vector)
        self.clear_btn.setEnabled(is_vector)

    def _refresh_status(self) -> None:
        if not isinstance(self._layer, QgsVectorLayer):
            self.status_label.setText("仅矢量图层可以设置显示条件。")
            return
        try:
            applied = self._layer.subsetString()
            count = self._layer.featureCount()
        except Exception:  # noqa: BLE001
            applied = ""
            count = -1
        count_text = "—" if count < 0 else str(count)
        if applied:
            self.status_label.setText(f"图层当前可见要素数：{count_text}（已应用条件）")
        else:
            self.status_label.setText(f"图层当前显示全部要素：{count_text}")

    def _open_builder(self) -> None:
        try:
            from app.env.user_expressions import load_user_expression_functions

            load_user_expression_functions()

            from app.ui.dialogs.expression_builder import ExpressionBuilderDialog

            dlg = ExpressionBuilderDialog(
                self._layer, self.expr_edit.toPlainText().strip(), self
            )
            try:
                accepted = bool(dlg.exec())
            finally:
                dlg.deleteLater()
            if accepted:
                self.expr_edit.setPlainText(dlg.expression_text())
        except Exception as exc:  # noqa: BLE001
            logger.error("打开表达式构建器失败: %s", exc, exc_info=True)
            QMessageBox.warning(self, "图层属性", f"打开表达式构建器失败：{exc}")

    def load(self, layer) -> None:
        self._layer = layer
        self._sync_enabled()
        if not isinstance(layer, QgsVectorLayer):
            self.expr_edit.clear()
            self._refresh_status()
            return
        try:
            condition = layer.subsetString()
        except Exception:  # noqa: BLE001
            condition = ""
        self.expr_edit.setPlainText(condition or "")
        self._populate_completions()
        self._refresh_status()

    def apply(self, layer) -> bool:
        if not isinstance(layer, QgsVectorLayer):
            return True

        condition = self.expr_edit.toPlainText().strip()
        if condition:
            error = _condition_error(layer, condition)
            if error is not None:
                QMessageBox.warning(self, "图层属性", error)
                return False

        try:
            current = layer.subsetString()
        except Exception:  # noqa: BLE001
            current = None
        if current == condition:
            self._refresh_status()
            return True

        if layer.isEditable():
            QMessageBox.warning(
                self,
                "图层属性",
                "图层处于编辑状态，无法应用显示条件。请先保存 / 停止编辑后再试。",
            )
            return False

        try:
            if not layer.setSubsetString(condition):
                QMessageBox.warning(
                    self,
                    "图层属性",
                    "图层提供者拒绝了该显示条件，条件未生效！",
                )
                return False
        except Exception as exc:  # noqa: BLE001
            logger.error("应用显示条件失败: %s", exc, exc_info=True)
            QMessageBox.warning(self, "图层属性", f"应用显示条件失败：{exc}")
            return False

        self._refresh_status()
        return True
