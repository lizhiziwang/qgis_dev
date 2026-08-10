# 工具条图标改用 Font Awesome

日期：2026-08-10
分支：dev

## 背景

上一轮用 QPainter 手绘的 12 个矢量图标，用户反馈「太丑」，要求重新设计。
手绘路径难以达到专业观感，改用随应用分发的 Font Awesome Free 5 Solid 字体
（OFL-1.1 协议，允许嵌入再分发）渲染，标准现代扁平实心图标，随主题变色。

## 做了什么

| 文件 | 改动 |
|---|---|
| `resource/fonts/fa-solid-900.ttf`（新增） | Font Awesome Free 5.15.4 Solid 字体（202KB），随应用分发 |
| `resource/fonts/README.txt`（新增） | 字体来源 + OFL-1.1 授权说明 |
| `app/ui/common/tool_icons.py`（重写） | 弃用 QPainter 手绘路径，改用 QFont 渲染 FA 码位；`icon(key, color)` API 不变（tool_bar / main_window 零改动） |
| `app/ui/main_window/tool_bar.py` | `iconSize` 已为 36×36（上轮改动），图标 `_LOGICAL=36` 与之对齐 |

## 图标映射（key → FA5 Solid 码位）

| key | FA | 码位 | 说明 |
|---|---|---|---|
| pan | arrows-alt | U+F0B2 | 平移 |
| identify | search | U+F002 | 属性（放大镜） |
| zoom_in | search-plus | U+F00E | 放大 |
| zoom_out | search-minus | U+F010 | 缩小 |
| quality | shield-alt | U+F3ED | 质检（盾牌） |
| analyse | chart-bar | U+F080 | 分析（柱状图） |
| zoom_full | globe | U+F0AC | 全图（地球） |
| refresh | sync | U+F021 | 刷新 |
| test | flask | U+F0C3 | 测试（烧瓶） |
| space_query_point | map-marker | U+F3C5 | 点查询（定位针） |
| space_query_line | route | U+F4D7 | 线查询（路线） |
| space_query_polygon | draw-polygon | U+F5EE | 面查询（多边形） |

## 关键实现点

- **字体路径解析**：`Paths.from_env(Paths.default()).resource("fonts", ...)`——
  开发态落仓库 `resource/fonts/`，打包态由 bootstrap `_ensure_portable_env` 设
  `QGIS_DEV_PATHS_RESOURCE` 指到便携包 `resource/`。懒加载（首次 `icon()` 才注册
  字体），避免模块导入时序问题。
- **打包**：`tools/pyinstaller.spec` 已整目录打进 `resource/`，字体自动跟随；
  无需改 spec。
- **渲染**：`QPixmap(36*2, 36*2)` + `setDevicePixelRatio(2)`，字形
  `_GLYPH_PX=int(36*0.8)=28` 居中绘制，留边距；`(key, color)` 缓存，主题切换
  用 `ToolBar.apply_theme` 按新色重新生成。
- **字体缺失兜底**：画实心圆点，避免空白按钮。

## 关键结果

- 码位探针：12 个 key 的 `ord(glyph)` 与预期完全一致。
- ASCII 位图逐图标核对：全部是标准 FA 图形、居中、无裁切、四周留白均匀。
- `py_compile` 通过；字体文件 202KB，应用体积增量可忽略。

## 备注

- `_GLYPH_PX`（当前 28）即图标大小的调参点：想更小/更大改这一个值即可。
- FA5 Free 的 family 名是 `Font Awesome 5 Free Solid`（Solid 字重 900）。
