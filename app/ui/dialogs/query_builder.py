"""SQL 查询构建器 Dialog。

界面与取值逻辑复用 :class:`~app.ui.dialogs.query_builder_widget.QueryBuilderWidget`
（「图层属性 → 条件」页也用同一套），这里只负责测试条件、写回 ``subsetString``
或把条件交给调用方（导出等）。

迁自 :class:`component.QueryBuilder.QueryBuilder`。"""

from __future__ import annotations

import contextlib
import threading

from qgis.core import QgsFeatureRequest, QgsVectorLayer
from qgis.PyQt.QtCore import QObject, pyqtSignal
from qgis.PyQt.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
)

from app.ui.common.base_dialog import BaseDialog
from app.ui.dialogs.query_builder_widget import QueryBuilderWidget


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

    def __init__(
        self,
        layer: QgsVectorLayer,
        parent: QDialog | None = None,
        *,
        apply_to_layer: bool = True,
    ) -> None:
        """
        Args:
            layer: 要写条件的图层（字段列表、值预览都取自它）。
            parent: 父窗口。
            apply_to_layer: ``True`` = 老行为，点确定把条件写成图层的
                ``setSubsetString``（属性表筛选 / 条件查询走这个）；
                ``False`` = **只取条件**，不碰图层 —— 导出这类"条件只用于本次操作"
                的场合用（免得为了拿去导出，把地图上的图层也筛了一遍）。
        """
        super().__init__(parent)
        self.setWindowTitle("属性查询器")
        self.setMinimumSize(720, 500)
        self.layer = layer
        self._apply_to_layer = apply_to_layer
        self._test_worker: _SqlCountWorker | None = None
        self._init_ui()
        self._init_signals()
        if self._apply_to_layer:
            self._restore_applied_filter()
        else:
            self.widget.set_applied_text("条件只用于本次操作，不会改动图层本身的筛选")
            self.ok_btn.setText("确定")

    def _init_ui(self) -> None:
        main = QVBoxLayout()
        self.widget = QueryBuilderWidget(self.layer, self)
        main.addWidget(self.widget, 1)

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

        self.setLayout(main)

    def _init_signals(self) -> None:
        self.test_btn.clicked.connect(self._test_sql)
        self.clear_btn.clicked.connect(self._clear_sql)
        self.ok_btn.clicked.connect(self._on_ok)
        self.cancel_btn.clicked.connect(self.reject)

    def _restore_applied_filter(self) -> None:
        """回填上次已应用的筛选条件。

        图层的 ``subsetString()`` 就是上一次点「确定应用」写进去的条件（本对话框、
        属性表筛选都走它），所以重开对话框能直接看到并在其上继续编辑，而不是每次
        都从空白开始。
        """
        try:
            applied = self.layer.subsetString()
        except RuntimeError:  # 图层 C++ 对象已销毁
            applied = ""
        self.widget.set_applied_text(
            f"当前已应用筛选：{applied}" if applied else "当前未应用筛选"
        )
        if applied:
            self.widget.set_condition(applied)

    def _test_sql(self) -> None:
        cond = self.widget.condition()
        if not cond:
            QMessageBox.warning(self, "警告", "请输入有效的 SQL 筛选条件！")
            return
        error = self.widget.condition_error(cond)
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
        self.widget.clear_condition()

    def _on_ok(self) -> None:
        cond = self.widget.condition()
        if not self._apply_to_layer:
            # 只取条件：校验后发信号就走，**不改图层的子集串**（导出用）
            if cond:
                error = self.widget.condition_error(cond)
                if error is not None:
                    QMessageBox.critical(self, "条件有误", error)
                    return
            self.sqlConfirmed.emit(cond)
            self.accept()
            return
        if not cond:
            # 空条件 = 取消筛选（与「测试条件」不同：这里不报错，直接清子集串）
            self.layer.setSubsetString("")
            self.sqlConfirmed.emit("")
            self.accept()
            return
        error = self.widget.condition_error(cond)
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
