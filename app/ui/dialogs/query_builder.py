"""SQL 查询构建器 Dialog。

迁自 :class:`component.QueryBuilder.QueryBuilder`。"""

from __future__ import annotations

import contextlib
import threading

from qgis.core import (
    QgsExpression,
    QgsFeatureRequest,
    QgsVectorLayer,
)
from qgis.gui import QgsFieldComboBox
from qgis.PyQt.QtCore import QObject, Qt, QVariant, pyqtSignal
from qgis.PyQt.QtGui import QTextCursor
from qgis.PyQt.QtWidgets import (
    QDialog,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTextEdit,
    QVBoxLayout,
)

from app.ui.common.base_dialog import BaseDialog

_OPERATORS = ["=", "<", ">", "LIKE", "%", "IN", "NOT IN", "<=", ">=", "!=", "ILIKE", "AND", "OR", "NOT"]


class _SqlCountWorker(QObject):
    """后台统计匹配要素数（"测试条件"全表扫描不阻塞主线程）。"""

    counted = pyqtSignal(int)
    failed = pyqtSignal(str)

    def __init__(self, layer: QgsVectorLayer, cond: str) -> None:
        super().__init__()
        self.layer = layer
        self.cond = cond
        self._cancel = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="sql-count", daemon=True)
        self._thread.start()

    def cancel(self) -> None:
        self._cancel.set()

    def _run(self) -> None:
        try:
            req = QgsFeatureRequest().setFilterExpression(self.cond)
            n = 0
            it = self.layer.getFeatures(req)
            for _ in it:
                if self._cancel.is_set():
                    break
                n += 1
            with contextlib.suppress(Exception):
                it.close()
            if not self._cancel.is_set():
                self.counted.emit(n)
        except Exception as e:  # noqa: BLE001
            self.failed.emit(str(e))


class QueryBuilderDialog(BaseDialog):
    """SQL 条件构建器。"""

    sqlConfirmed = pyqtSignal(str)

    def __init__(self, layer: QgsVectorLayer, parent: QDialog | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("属性查询器")
        self.setMinimumSize(720, 500)
        self.layer = layer
        self.valid_fields = [f.name() for f in layer.fields()]
        #: 当前选中字段；由字段下拉初值与 ``fieldChanged`` 共同维护
        self.current_field: str = ""
        self._test_worker: _SqlCountWorker | None = None
        self._init_ui()
        self._init_signals()
        self._restore_applied_filter()

    def _init_ui(self) -> None:
        main = QVBoxLayout()
        splitter = QSplitter(Qt.Orientation.Horizontal)
        main.addWidget(splitter)

        # 左：字段 + 值预览
        field_group = QGroupBox("图层字段")
        f_layout = QVBoxLayout(field_group)
        f_layout.addWidget(QLabel("选中字段："))
        self.field_combo = QgsFieldComboBox()
        self.field_combo.setLayer(self.layer)
        self.field_combo.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        f_layout.addWidget(self.field_combo)
        f_layout.addWidget(QLabel("字段值预览："))
        self.value_list = QListWidget()
        self.value_list.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        f_layout.addWidget(self.value_list)
        btn_layout = QHBoxLayout()
        self.sample_btn = QPushButton("加载采样值")
        self.sample_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.all_btn = QPushButton("加载全部值")
        self.all_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
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
            b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            b.setMinimumSize(60, 30)
            b.clicked.connect(lambda _, t=op: self._insert_text(t))
            grid.addWidget(b, r, c)
        sql_layout.addLayout(grid)
        self.sql_edit = QTextEdit()
        self.sql_edit.setPlaceholderText("请输入 SQL 筛选条件（如：县代码 = '360202'）")
        self.sql_edit.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.sql_edit.setFocus()
        sql_layout.addWidget(self.sql_edit)
        # 已应用条件提示：编辑框可能被「清除条件」清空，这里始终反映图层真实状态
        self.applied_label = QLabel()
        self.applied_label.setObjectName("QueryBuilderAppliedLabel")
        self.applied_label.setWordWrap(True)
        sql_layout.addWidget(self.applied_label)
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
        self.setLayout(main)

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

    def _restore_applied_filter(self) -> None:
        """回填上次已应用的筛选条件，并初始化当前字段。

        图层的 ``subsetString()`` 就是上一次点「确定应用」写进去的条件（本对话框、
        属性表筛选都走它），所以重开对话框能直接看到并在其上继续编辑，而不是每次
        都从空白开始。
        """
        # 字段下拉的初值要同步到 current_field，否则首次「加载采样值」会误报无效字段
        self.current_field = self.field_combo.currentField()
        try:
            applied = self.layer.subsetString()
        except RuntimeError:  # 图层 C++ 对象已销毁
            applied = ""
        self.applied_label.setText(f"当前已应用筛选：{applied}" if applied else "当前未应用筛选")
        if not applied:
            return
        self.sql_edit.setPlainText(applied)
        # setPlainText 会把光标挪到开头，续编时（Ctrl+End 之前）希望落在末尾
        cursor = self.sql_edit.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        self.sql_edit.setTextCursor(cursor)

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
        # QGIS 4.x：QgsField.String 已移除，字段类型用 QVariant.Type 判断
        val = f"'{item.text().replace(chr(39), chr(39) * 2)}'" if field.type() == QVariant.String else item.text()
        self._insert_text(val)

    def _load_values(self, sample: bool = True, sample_count: int = 50) -> None:
        if not self.current_field:
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
            item.setData(Qt.ItemDataRole.UserRole, v)
            self.value_list.addItem(item)

    def _condition_error(self, cond: str) -> str | None:
        """条件合法性检查：返回错误信息，``None`` 表示合法。

        语法与字段引用都查一遍。字段名写错时 ``setSubsetString`` 仍返回 True，
        但匹配 0 个要素（memory / OGR 提供者实测都如此），不拦会“看起来成功、
        结果全空”。「测试条件」与「确定应用」共用同一口径。
        """
        expr = QgsExpression(cond)
        if expr.hasParserError():
            return f"SQL 语法错误！\n{expr.parserErrorString()}"
        unknown = [c for c in expr.referencedColumns() if c not in self.valid_fields]
        if unknown:
            return "无效字段：" + "、".join(unknown)
        return None

    def _test_sql(self) -> None:
        cond = self.sql_edit.toPlainText().strip()
        if not cond:
            QMessageBox.warning(self, "警告", "请输入有效的 SQL 筛选条件！")
            return
        error = self._condition_error(cond)
        if error is not None:
            QMessageBox.critical(self, "测试失败", error)
            return
        # 匹配计数走后台线程：大图层全表扫描不再卡死 UI（按钮禁用直到完成）
        self.test_btn.setEnabled(False)
        self.test_btn.setText("查询中…")
        worker = _SqlCountWorker(self.layer, cond)
        worker.counted.connect(self._on_sql_counted)
        worker.failed.connect(self._on_sql_count_failed)
        self._test_worker = worker
        worker.start()

    def _reset_test_btn(self) -> None:
        self._test_worker = None
        self.test_btn.setEnabled(True)
        self.test_btn.setText("测试条件")

    def _on_sql_counted(self, n: int) -> None:
        self._reset_test_btn()
        QMessageBox.information(self, "测试成功", f"SQL 合法！\n匹配要素：{n}")

    def _on_sql_count_failed(self, msg: str) -> None:
        self._reset_test_btn()
        QMessageBox.critical(self, "测试失败", f"查询执行异常！\n{msg}")

    def closeEvent(self, event) -> None:
        """关闭对话框时取消仍在后台扫描的计数线程（避免滞留读锁）。"""
        if self._test_worker is not None:
            self._test_worker.cancel()
            self._test_worker = None
        super().closeEvent(event)

    def _clear_sql(self) -> None:
        """清空编辑框与值列表。

        只改本地编辑内容，**不动图层**：提示条仍显示图层当前已应用的条件，
        真正取消筛选需要留空后点「确定应用」。
        """
        self.sql_edit.clear()
        self.value_list.clear()

    def _on_ok(self) -> None:
        cond = self.sql_edit.toPlainText().strip()
        if not cond:
            # 空条件 = 取消筛选（与「测试条件」不同：这里不报错，直接清子集串）
            self.layer.setSubsetString("")
            self.sqlConfirmed.emit("")
            self.accept()
            return
        error = self._condition_error(cond)
        if error is not None:
            QMessageBox.critical(self, "应用失败", error)
            return
        try:
            # 提供者可能拒绝（返回 False 时旧条件仍在）：不报成功、也不发信号，
            # 否则属性表模型会按新条件筛而图层还是旧子集，两边对不上。
            if not self.layer.setSubsetString(cond):
                QMessageBox.critical(self, "应用失败", "图层提供者拒绝了该筛选条件，条件未生效！")
                return
            self.sqlConfirmed.emit(cond)
            QMessageBox.information(self, "操作成功", f"已应用 SQL！\n剩余要素：{self.layer.featureCount()}")
            self.accept()
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(self, "应用失败", str(e))
