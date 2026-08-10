# 2026-08-10 主窗口 show() 崩溃修复（PyQt6 nativeEvent 返回值）

## 问题

QGIS 4.2 / PyQt6 迁移后，真实应用启动即崩：日志到 `[probe] build_window OK` 后进程静默退出（exit 127 / PyCharm exit 1），无 traceback。崩溃点在 `window.show()` 内部。

## 定位过程（文件探针二分）

1. 事件过滤器 `app.installEventFilter`：show() 期间**无任何 Qt 事件**被分发 → 崩在 native 窗口显示代码、Qt 事件分发之前。
2. `nativeEvent` 探针：show() 收到的第一条 native 消息是 `WM_WINDOWPOSCHANGING` (0x46)，随后在 `super().nativeEvent` 内崩。
3. `AddVectoredExceptionHandler` 探针（进程内 C 级异常钩子）：崩溃地址 `QtCore.pyd+0x33C40`，异常参数 (0, 0) → **NULL 指针读**。StackWalk64 在 VEH 内不稳定（dbghelp 二次崩溃），放弃。
4. 最小化二分：plain `QMainWindow`（含 frameless/theme/canvas/docks/bridge/zoomtool/menubar/services）全部通过；真实 `MainWindow`（即使 `_build_*` 全跳过）必崩。
5. **决定性测试**：空 `QMainWindow` 子类，仅重写 `nativeEvent`（纯透传）→ 崩；仅重写 `changeEvent` → 通过。纯 PyQt6 `QApplication`（不初始化 QGIS）同样崩 → 与 QGIS 无关。

## 根因

PyQt6 中 `super().nativeEvent()` 对 Qt 不处理的消息返回 `(False, None)`（`qintptr *result` 出参未被触碰 → sip 给 None）。把该元组从 `nativeEvent` 重写返回时，sip 无法把 `None` 转回 `qintptr` 结果参数 → 抛 TypeError，异常穿进 C++ 事件分发 → 原生访问冲突。PyQt5 容忍 None；PyQt6 不。

## 修复

`app/ui/main_window/main_window.py::MainWindow.nativeEvent` 末尾：

```python
ok, res = super().nativeEvent(eventType, message)
return (ok, res if isinstance(res, int) else 0)
```

所有 `nativeEvent` 重写都必须保证第二个元素是 int，绝不能是 None。全仓仅此一处重写。

## 其它清理

- 恢复被"临时注释掉"的 `QgisDigitizingToolbar`（当时为验证是否崩点；实为 nativeEvent 问题，恢复后 E2E 正常）。
- 移除 `app/bootstrap/__init__.py` 与 `app/ui/main_window/main_window.py` 里的 `[probe]` 日志行。

## 验证

- `D:/zsh/app/qgis4.2/bin/python-qgis.bat .tmp/run_pytest_qgis.py -q`：157 passed，1 skip（fastapi），EXIT=0。
- E2E 复刻 `app.bootstrap.main()` 全流程（runtime→QgsApplication→initQgis→build_window→show→exec）：EXIT=0。

## 记忆

已记入 `qgis-ui-integration-gotchas.md` QGIS 4.2 小节：`nativeEvent` 重写返回 `(False, None)` = show() 硬崩溃；调试用文件探针 + VEH；StackWalk64 在 VEH 内不可靠。
