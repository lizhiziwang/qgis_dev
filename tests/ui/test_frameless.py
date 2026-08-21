"""无边框窗口纯函数测试。

只测平台无关的 :func:`hit_test` / :func:`_cursor_for`，不依赖 QGIS / GUI，
也不需要 QApplication 实例（Qt.Edge / Qt.CursorShape 都是纯枚举）。
CI 默认 ``-m 'not gui'`` 也能跑（只要装了 PyQt6）。
"""

from __future__ import annotations

import pytest

pytest.importorskip("PyQt6")

from PyQt6.QtCore import QPoint, QRect, Qt  # noqa: E402

from app.ui.common.frameless import (  # noqa: E402
    RESIZE_MARGIN,
    SC_MOVE,
    SC_SIZE,
    FramelessMixin,
    _cursor_for,
    hit_test,
    sc_size_wparam,
)
from app.ui.common.frameless import (  # noqa: E402
    HTBOTTOM,
    HTBOTTOMLEFT,
    HTBOTTOMRIGHT,
    HTLEFT,
    HTRIGHT,
    HTTOP,
    HTTOPLEFT,
    HTTOPRIGHT,
)


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


# ---------------------------------------------------------------------------
# sc_size_wparam：WM_NCHITTEST HT 码 → WM_SYSCOMMAND(SC_SIZE) 方向值。
# 必须保证命令仍是 SC_SIZE 而不是被 & 0xFFF0 解析成 SC_MOVE（SC_SIZE | HT 直
# 接 OR 时 HTBOTTOMRIGHT=17 占 5 bit 会溢出到命令位）。纯函数，跨平台可测。
# ---------------------------------------------------------------------------

#: (HT 码, 期望方向值)。SC_SIZE 方向编号与 HT 码错一位：10..17 → 1..8。
_HT_TO_SC_SIZE = [
    (HTLEFT, 0xF001),
    (HTRIGHT, 0xF002),
    (HTTOP, 0xF003),
    (HTTOPLEFT, 0xF004),
    (HTTOPRIGHT, 0xF005),
    (HTBOTTOM, 0xF006),
    (HTBOTTOMLEFT, 0xF007),
    (HTBOTTOMRIGHT, 0xF008),
]


def test_sc_size_wparam_command_is_never_sc_move() -> None:
    """方向值低 4 位，命令位必须保持 SC_SIZE（否则系统按 SC_MOVE 处理成拖动）。"""
    for ht, _ in _HT_TO_SC_SIZE:
        wp = sc_size_wparam(ht)
        assert wp & 0xFFF0 == SC_SIZE, f"ht={ht} → 0x{wp:X} 被解析成非 SC_SIZE 命令"
        assert wp & 0xFFF0 != SC_MOVE


def test_sc_size_wparam_direction_mapping() -> None:
    """HT 码 10..17 → 方向 1..8（与暴力实测的 SC_SIZE 方向表一致）。"""
    for ht, expected in _HT_TO_SC_SIZE:
        assert sc_size_wparam(ht) == expected, f"ht={ht} → 期望 0x{expected:X}"


# ---------------------------------------------------------------------------
# _continue_manual_resize：Wayland / 合成器不支持 startSystemResize 时的手动
# 兜底几何计算。纯几何，无 GUI 依赖，跨平台可测。
# ---------------------------------------------------------------------------


class _ManualResizeHost:
    """最小宿主：只暴露 _continue_manual_resize 用到的四个成员。"""

    def __init__(self) -> None:
        self._resize_edges = Qt.Edge(0)
        self._resize_start_pos = QPoint(100, 100)
        self._resize_start_geo = QRect(100, 100, 800, 600)
        self.last_geo = None

    def setGeometry(self, geo) -> None:
        self.last_geo = geo


def _host_with_edges(edges) -> _ManualResizeHost:
    host = _ManualResizeHost()
    host._resize_edges = edges
    return host


def test_manual_resize_right_edge() -> None:
    host = _host_with_edges(Qt.Edge.RightEdge)
    FramelessMixin._continue_manual_resize(host, QPoint(180, 100))
    assert host.last_geo == QRect(100, 100, 880, 600)


def test_manual_resize_left_edge() -> None:
    host = _host_with_edges(Qt.Edge.LeftEdge)
    FramelessMixin._continue_manual_resize(host, QPoint(60, 100))
    assert host.last_geo == QRect(60, 100, 840, 600)


def test_manual_resize_bottom_edge() -> None:
    host = _host_with_edges(Qt.Edge.BottomEdge)
    FramelessMixin._continue_manual_resize(host, QPoint(100, 150))
    assert host.last_geo == QRect(100, 100, 800, 650)


def test_manual_resize_top_edge() -> None:
    host = _host_with_edges(Qt.Edge.TopEdge)
    FramelessMixin._continue_manual_resize(host, QPoint(100, 60))
    assert host.last_geo == QRect(100, 60, 800, 640)


def test_manual_resize_bottom_right_corner() -> None:
    host = _host_with_edges(Qt.Edge.BottomEdge | Qt.Edge.RightEdge)
    FramelessMixin._continue_manual_resize(host, QPoint(180, 150))
    assert host.last_geo == QRect(100, 100, 880, 650)


def test_manual_resize_top_left_corner() -> None:
    host = _host_with_edges(Qt.Edge.TopEdge | Qt.Edge.LeftEdge)
    FramelessMixin._continue_manual_resize(host, QPoint(60, 60))
    assert host.last_geo == QRect(60, 60, 840, 640)


def test_manual_resize_noop_when_no_edges() -> None:
    """_resize_edges == 0 时（无 resize 进行中）直接返回，不改几何。"""
    host = _host_with_edges(Qt.Edge(0))
    FramelessMixin._continue_manual_resize(host, QPoint(500, 500))
    assert host.last_geo is None
