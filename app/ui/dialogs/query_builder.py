"""SQL 查询构建器 Dialog。

迁自 :class:`component.QueryBuilder.QueryBuilder`。"""

from __future__ import annotations

from typing import Optional

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import (
    QDialog,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTextEdit,
    QVBoxLayout,
)

from qgis.core import (
    QgsExpression,
    QgsFeatureRequest,
    QgsField,
    QgsVectorLayer,
)
from qgis.gui import QgsFieldComboBox


_OPERATORS = ["=", "<", ">", "LIKE", "%", "IN", "NOT IN", "<=", ">=", "!=", "ILIKE", "AND", "OR", "NOT"]


class QueryBuilderDialog(QDialog):
    """SQL 条件构建器。"""

    sqlConfirmed = pyqtSignal(str)

    def __init__(self, layer: QgsVectorLayer, parent: Optional[QDialog] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("属性查询器")
        self.setMinimumSize(720, 500)
        self.layer = layer
        self.valid_fields = [f.name() for f in layer.fields()]
        self._init_ui()
        self._init_signals()

    def _init_ui(self) -> None:
        main = QVBoxLayout(self)
        splitter = QSplitter(Qt.Horizontal)
        main.addWidget(splitter)

        # 左：字段 + 值预览
        field_group = QGroupBox("图层字段")
        f_layout = QVBoxLayout(field_group)
        f_layout.addWidget(QLabel("选中字段："))
        self.field_combo = QgsFieldComboBox()
        self.field_combo.setLayer(self.layer)
        self.field_combo.setFocusPolicy(Qt.NoFocus)
        f_layout.addWidget(self.field_combo)
        f_layout.addWidget(QLabel("字段值预览："))
        self.value_list = QListWidget()
        self.value_list.setFocusPolicy(Qt.NoFocus)
        f_layout.addWidget(self.value_list)
        btn_layout = QHBoxLayout()
        self.sample_btn = QPushButton("加载采样值")
        self.sample_btn.setFocusPolicy(Qt.NoFocus)
        self.all_btn = QPushButton("加载全部值")
        self.all_btn.setFocusPolicy(Qt.NoFocus)
        btn_layout.addWidget(self.sample_btn)
        btn_layout.addWidget(self.all_btn)
        f_layout.addLayout(btn_layout)
        splitter.addWidget(field_group)

        # 右：SQL 编辑
        sql_group = QGroupBox("SQL 筛选条件")
        sql_layout = QVBoxLayout(sql_group)
        grid = QGridLayout()
        for i, op in enumerate(_OPERATORS):
            r, c = divmod(i, 5)
            b = QPushButton(op)
            b.setFocusPolicy(Qt.NoFocus)
            b.setMinimumSize(60, 30)
            b.clicked.connect(lambda _, t=op: self._insert_text(t))
            grid.addWidget(b, r, c)
        sql_layout.addLayout(grid)
        self.sql_edit = QTextEdit()
        self.sql_edit.setPlaceholderText("请输入 SQL 筛选条件（如：县代码 = '360202'）")
        self.sql_edit.setFocusPolicy(Qt.StrongFocus)
        self.sql_edit.setFocus()
        sql_layout.addWidget(self.sql_edit)
        splitter.addWidget(sql_group)

        # 底部按钮
        btm = QHBoxLayout()
        self.test_btn = QPushButton("测试条件")
        self.clear_btn = QPushButton("清除条件")
        self.ok_btn = QPushButton("确定应用")
        self.cancel_btn = QPushButton("取消")
        btm.addWidget(self.test_btn)
        btm.addWidget(self.clear_btn)
        btm.addStretch()
        btm.addWidget(self.ok_btn)
        btm.addWidget(self.cancel_btn)
        main.addLayout(btm)

        splitter.setSizes([300, 420])

    def _init_signals(self) -> None:
        self.field_combo.fieldChanged.connect(self._on_field_changed)
        self.sample_btn.clicked.connect(lambda: self._load_values(sample=True))
        self.all_btn.clicked.connect(lambda: self._load_values(sample=False))
        self.test_btn.clicked.connect(self._test_sql)
        self.clear_btn.clicked.connect(self._clear_sql)
        self.ok_btn.clicked.connect(self._on_ok)
        self.cancel_btn.clicked.connect(self.reject)
        self.value_list.itemDoubleClicked.connect(self._on_value_double_clicked)
        self.field_combo.fieldChanged.connect(self._on_field_chosen)

    def _on_field_changed(self, name: str) -> None:
        self.current_field = name
        self.value_list.clear()

    def _on_field_chosen(self, name: str) -> None:
        self._insert_text(f' "{name}" ')

    def _insert_text(self, text: str) -> None:
        cur = self.sql_edit.textCursor()
        cur.insertText(text)
        self.sql_edit.setTextCursor(cur)

    def _on_value_double_clicked(self, item: QListWidgetItem) -> None:
        field_name = self.field_combo.currentText()
        if not field_name:
            return
        field = self.layer.fields().field(field_name)
        if field is None:
            return
        if field.type() in (QgsField.String, getattr(QgsField, "Text", None)):
            val = f"'{item.text().replace(chr(39), chr(39)*2)}'"
        else:
            val = item.text()
        self._insert_text(val)

    def _load_values(self, sample: bool = True, sample_count: int = 50) -> None:
        if not hasattr(self, "current_field") or not self.current_field:
            QMessageBox.warning(self, "警告", "无效字段或图层，无法加载值！")
            return
        self.value_list.clear()
        request = QgsFeatureRequest()
        request.setSubsetOfAttributes([self.current_field], self.layer.fields())
        seen: set = set()
        for i, feat in enumerate(self.layer.getFeatures(request)):
            if sample and i >= sample_count:
                break
            v = feat.attribute(self.current_field)
            if v is None or v in seen:
                continue
            seen.add(v)
            item = QListWidgetItem(str(v))
            item.setData(Qt.UserRole, v)
            self.value_list.addItem(item)

    def _test_sql(self) -> None:
        cond = self.sql_edit.toPlainText().strip()
        if not cond:
            QMessageBox.warning(self, "警告", "请输入有效的 SQL 筛选条件！")
            return
        expr = QgsExpression(cond)
        if expr.hasParserError():
            QMessageBox.critical(self, "测试失败", f"SQL 语法错误！\n{expr.parserErrorString()}")
            return
        for col in expr.referencedColumns():
            if col not in self.valid_fields:
                QMessageBox.critical(self, "测试失败", f"无效字段：{col}")
                return
        try:
            req = QgsFeatureRequest().setFilterExpression(cond)
            count = sum(1 for _ in self.layer.getFeatures(req))
            QMessageBox.information(self, "测试成功", f"SQL 合法！\n匹配要素：{count}")
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(self, "测试失败", f"查询执行异常！\n{e}")

    def _clear_sql(self) -> None:
        self.sql_edit.clear()
        self.value_list.clear()

    def _on_ok(self) -> None:
        cond = self.sql_edit.toPlainText().strip()
        if not cond:
            self.layer.setSubsetString("")
            self.sqlConfirmed.emit("")
            self.accept()
            return
        expr = QgsExpression(cond)
        if expr.hasParserError():
            QMessageBox.critical(self, "应用失败", f"SQL 语法错误！\n{expr.parserErrorString()}")
            return
        try:
            self.layer.setSubsetString(cond)
            self.sqlConfirmed.emit(cond)
            QMessageBox.information(self, "操作成功", f"已应用 SQL！\n剩余要素：{self.layer.featureCount()}")
            self.accept()
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(self, "应用失败", str(e))
