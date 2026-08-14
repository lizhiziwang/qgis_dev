"""验证 Win11 无边框标题栏按钮失效假设：WM_NCHITTEST 物理/逻辑坐标错位。

真实窗口 + 逐按钮对比：
- 当前逻辑：widgetAt(物理坐标) → is_interactive_child
- 修复逻辑：物理→逻辑换算 → mapFromGlobal → 几何命中 _is_interactive_pos

即使 dev 机 100% 缩放，也用强制 dpr 模拟 HiDPI。
"""
import sys

sys.path.insert(0, r"d:\zsh\code\py\qgis_dev")

from qgis.core import QgsApplication

app = QgsApplication([], True)
app.setPrefixPath(r"D:/zsh/app/qgis4.2", True)
app.initQgis()

from PyQt6.QtCore import QPoint
from PyQt6.QtWidgets import QApplication, QMainWindow
from qgis.core import QgsApplication as QgsApp2

from app.ui.common.frameless import FramelessMixin


class TestWin(FramelessMixin, QMainWindow):
    def __init__(self):
        super().__init__()
        self.title_bar = self.enable_frameless(with_menu_bar=True)
        self.setWindowTitle("测试")
        self.setGeometry(100, 100, 1200, 800)
        self.show()


win = TestWin()
for _ in range(20):
    app.processEvents()
dpr = win.devicePixelRatioF()
print(f"devicePixelRatioF = {dpr}", flush=True)

tb = win.title_bar
buttons = {"min": tb._btn_min, "max": tb._btn_max, "close": tb._btn_close,
           "theme": tb._btn_theme, "user": tb._user_btn}


def pump():
    for _ in range(5):
        app.processEvents()


def simulate(label, scale):
    """scale=窗口物理/逻辑比例（HiDPI 模拟用 1.5）；scale=dpr 时是真实机器行为。"""
    for name, btn in buttons.items():
        center = btn.mapToGlobal(btn.rect().center())  # 逻辑全局坐标
        phys = QPoint(round(center.x() * scale), round(center.y() * scale))  # 系统会给的物理坐标

        # ---- 当前逻辑（物理坐标直传 widgetAt）----
        w = QApplication.widgetAt(phys)
        w_name = type(w).__name__ if w is not None else "None"
        # 当前判定是否返回 HTCAPTION（会吃掉点击）
        interactive = tb.is_interactive_child(w) if w is not None else False
        in_tb = (w is tb) or (w is not None and tb.isAncestorOf(w))
        cur_htcaption = in_tb and not interactive

        # ---- 修复逻辑：物理→逻辑换算 + 几何命中 ----
        logical = QPoint(round(phys.x() / scale), round(phys.y() / scale))
        local = tb.mapFromGlobal(logical)
        new_hit = tb._is_interactive_pos(local)
        new_htcaption = not new_hit

        tag = "OK" if (cur_htcaption == new_htcaption) else "MISMATCH"
        print(f"[{label}] {name:6s} widgetAt={w_name:12s} "
              f"cur={'CAP' if cur_htcaption else 'CLIENT':7s} new={'CAP' if new_htcaption else 'CLIENT':7s} {tag}",
              flush=True)


# 真实机器行为（当前 dpr）
simulate(f"真实 dpr={dpr}", dpr)

# 强制 HiDPI 1.5× 模拟：系统给物理坐标，Qt 逻辑坐标不变
simulate("HiDPI 1.5x", 1.5)

# 标题栏空白区（非按钮）应仍是 HTCAPTION（可拖动）
blank_local = QPoint(60, tb.height() // 2)
blank_center = tb.mapToGlobal(blank_local)
print(f"\n标题栏空白区: 逻辑({blank_center.x()},{blank_center.y()}) "
      f"几何命中={tb._is_interactive_pos(tb.mapFromGlobal(blank_center))} "
      f"(应为 False → HTCAPTION 可拖动)", flush=True)

# 边缘 resize 判定在 HiDPI 下是否也错位
g = win.frameGeometry()
m = 6
print(f"\n窗口 frameGeometry: {g.x()},{g.y()} {g.width()}x{g.height()} dpr={dpr}", flush=True)
print("边缘 6px 逻辑坐标 vs 物理坐标一致性（当前代码用物理坐标比逻辑 top/bottom）", flush=True)
# 窗口顶部边缘正中：逻辑 y=top+3，物理 y=(top+3)*dpr
top_mid_logical = QPoint(g.center().x(), g.top() + 3)
top_mid_phys = QPoint(round(top_mid_logical.x() * dpr), round(top_mid_logical.y() * dpr))
# 当前代码: if y <= top + m → 用物理 y 与逻辑 top 比
cur_hit_top = top_mid_phys.y() <= g.top() + m
# 修复: 先换算回逻辑再比
logical_y = round(top_mid_phys.y() / dpr)
new_hit_top = logical_y <= g.top() + m
print(f"顶部边缘: 物理y={top_mid_phys.y()} 逻辑y={top_mid_logical.y()} "
      f"cur_top_hit={cur_hit_top} new_top_hit={new_hit_top}", flush=True)
print("DONE", flush=True)
