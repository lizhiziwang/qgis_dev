"""日志面板自动刷新回归测试。

修复前 LogDock 只在构造 / 手动点「刷新日志」时读文件，运行期新写的日志不
出现。修复后挂 2s QTimer，dock 可见且日志文件 mtime 变化时才重载。本测试
手动触发 ``_auto_reload``（不依赖真实定时器），覆盖「新日志出现」与
「mtime 未变不重载」两个行为。
"""

from __future__ import annotations

import pytest

pytest.importorskip("PyQt6")


@pytest.fixture
def log_dock(qgis_app, tmp_path, monkeypatch):
    """构造 LogDock，把日志文件重定向到临时目录并挂到可见主窗口。"""
    from PyQt6.QtCore import Qt
    from PyQt6.QtWidgets import QMainWindow

    import app.ui.docks.log_dock as log_dock

    log_path = tmp_path / "app.log"
    log_path.write_text("", encoding="utf-8")
    monkeypatch.setattr(log_dock, "_ALL_LOG_PATH", str(log_path))

    dock = log_dock.LogDock()
    win = QMainWindow()
    win.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, dock)
    win.show()
    qgis_app.processEvents()
    dock._timer.stop()  # 测试手动触发 _auto_reload，不依赖真实定时
    yield dock, log_path, win
    win.close()


@pytest.mark.gui
def test_auto_reload_shows_new_log_lines(log_dock):
    """文件有新日志时 _auto_reload 把新行渲染进面板。"""
    dock, log_path, _ = log_dock
    assert dock.isVisible(), "dock 必须可见（自动刷新前置条件）"
    assert dock.txt.toPlainText() == ""

    log_path.write_text("2026-08-20 10:00:00 - INFO - 测试自动刷新\n", encoding="utf-8")
    dock._auto_reload()

    assert "测试自动刷新" in dock.txt.toPlainText()


@pytest.mark.gui
def test_auto_reload_skips_when_file_unchanged(log_dock):
    """mtime 未变时不重载（避免无谓重绘），面板内容保持不变。"""
    dock, log_path, _ = log_dock
    log_path.write_text("第一条日志\n", encoding="utf-8")
    dock._auto_reload()
    first = dock.txt.toPlainText()
    assert "第一条日志" in first

    # 不写文件直接再触发：mtime 不变 → 不重载、不抛异常、内容一致
    dock._auto_reload()
    assert dock.txt.toPlainText() == first


@pytest.mark.gui
def test_auto_reload_skips_when_hidden(log_dock, qgis_app):
    """dock 不可见时不重载（定时 tick 的第一个短路条件）。"""
    dock, log_path, win = log_dock
    win.hide()
    qgis_app.processEvents()

    # 写文件后再触发：isVisible() 为 False → 直接返回，面板不更新
    log_path.write_text("不可见时不加载\n", encoding="utf-8")
    dock._auto_reload()

    assert "不可见时不加载" not in dock.txt.toPlainText()
