# 2026-08-13 QTextCursor 枚举迁移修复

## 背景

用户贴出崩溃：`_show_log_dock` 创建 `LogDock` 时 `_load_log_file` 里
`self.txt.moveCursor(QTextCursor.End)` 抛 `AttributeError: type object 'QTextCursor' has no attribute 'End'`。
Qt6 迁移遗留：`QTextCursor.End` 等移动枚举被改成作用域枚举。

## 做了什么

把所有 `QTextCursor.End` 改为 `QTextCursor.MoveOperation.End`（`moveCursor` 第二个参数用默认
`MoveMode.MoveAnchor`）：

- `app/ui/docks/log_dock.py:146`
- `app/ui/dialogs/imagery_tile.py:283`
- `app/ui/dialogs/imagery_mbtiles.py:281`

`legacy/component/component/LogDock.py:175` 同样有该问题，但 `legacy/` 是全仓无 import 的死代码，
按既定约定不修。

## 关键结果

- 语法检查通过；QGIS/PyQt6 环境验证 `QTextCursor.MoveOperation.{End,Start,Down}` 存在；
- 功能测试：真实 `QTextBrowser` 上 `setHtml` → `moveCursor(MoveOperation.End)` → `append` 后再滚动，均正常。
