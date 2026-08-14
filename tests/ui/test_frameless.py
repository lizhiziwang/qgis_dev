"""无边框窗口纯函数测试。

只测平台无关的 :func:`hit_test` / :func:`_cursor_for`，不依赖 QGIS / GUI，
也不需要 QApplication 实例（Qt.Edge / Qt.CursorShape 都是纯枚举）。
CI 默认 ``-m 'not gui'`` 也能跑（只要装了 PyQt6）。
"""

from __future__ import annotations

import pytest

pytest.importorskip("PyQt6")

from PyQt6.QtCore import QPoint, QRect, Qt  # noqa: E402

from app.ui.common.frameless import RESIZE_MARGIN, _cursor_for, hit_test  # noqa: E402


def _rect(w: int = 200, h: int = 100) -> QRect:
    return QRect(0, 0, w, h)


def test_center_returns_no_edges() -> None:
    assert hit_test(_rect(), QPoint(100, 50)) == Qt.Edge(0)


def test_left_edge() -> None:
    assert hit_test(_rect(), QPoint(2, 50)) == Qt.Edge.LeftEdge


def test_right_edge() -> None:
    assert hit_test(_rect(), QPoint(198, 50)) == Qt.Edge.RightEdge


def test_top_edge() -> None:
    assert hit_test(_rect(), QPoint(100, 3)) == Qt.Edge.TopEdge


def test_bottom_edge() -> None:
    assert hit_test(_rect(), QPoint(100, 97)) == Qt.Edge.BottomEdge


def test_top_left_corner() -> None:
    assert hit_test(_rect(), QPoint(2, 2)) == Qt.Edge.TopEdge | Qt.Edge.LeftEdge


def test_bottom_right_corner() -> None:
    assert hit_test(_rect(), QPoint(198, 98)) == Qt.Edge.BottomEdge | Qt.Edge.RightEdge


def test_outside_window_still_hits_edge() -> None:
    """紧贴窗口外沿（负坐标）也算命中，与 Windows WM_NCHITTEST 语义一致。"""
    assert hit_test(_rect(), QPoint(-2, 50)) == Qt.Edge.LeftEdge
    assert hit_test(_rect(), QPoint(100, -3)) == Qt.Edge.TopEdge


def test_custom_margin() -> None:
    rect = _rect()
    # margin=20 时，(10, 50) 命中左边缘；默认 margin=6 时不命中
    assert hit_test(rect, QPoint(10, 50), margin=20) == Qt.Edge.LeftEdge
    assert hit_test(rect, QPoint(10, 50)) == Qt.Edge(0)


def test_default_margin_constant() -> None:
    assert RESIZE_MARGIN == 6


def test_cursor_shapes() -> None:
    assert _cursor_for(Qt.Edge.LeftEdge) == Qt.CursorShape.SizeHorCursor
    assert _cursor_for(Qt.Edge.RightEdge) == Qt.CursorShape.SizeHorCursor
    assert _cursor_for(Qt.Edge.TopEdge) == Qt.CursorShape.SizeVerCursor
    assert _cursor_for(Qt.Edge.BottomEdge) == Qt.CursorShape.SizeVerCursor
    assert _cursor_for(Qt.Edge.TopEdge | Qt.Edge.LeftEdge) == Qt.CursorShape.SizeFDiagCursor
    assert _cursor_for(Qt.Edge.BottomEdge | Qt.Edge.RightEdge) == Qt.CursorShape.SizeFDiagCursor
    assert _cursor_for(Qt.Edge.TopEdge | Qt.Edge.RightEdge) == Qt.CursorShape.SizeBDiagCursor
    assert _cursor_for(Qt.Edge.BottomEdge | Qt.Edge.LeftEdge) == Qt.CursorShape.SizeBDiagCursor
    assert _cursor_for(Qt.Edge(0)) == Qt.CursorShape.ArrowCursor
