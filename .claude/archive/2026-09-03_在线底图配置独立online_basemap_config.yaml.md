# 在线底图配置从 app.yaml 独立：online_basemap_config.yaml

日期：2026-09-03

## 需求
用户：在线底图的配置从 app.yaml 移出来，单独放 `online_basemap_config.yaml`。

已定裁决（AskUserQuestion）：
- 位置 = `config/online_basemap_config.yaml`（与 app.yaml 同目录）。路径从 app.yaml 所在
  目录推导固定名文件，QGIS_DEV_CONFIG 测试覆盖 / 打包目录自动跟随，不加新配置键。
- 兼容 = **只读独立文件（单一数据源）**。app.yaml 内联 `map.online_basemaps` 不再解析；
  仓库自己的 config/app.yaml 一并迁移。Dock 树 / 去重 / 写回全走新文件。

## 改动
- `app/env/config.py`：
  - `MapConfig` 去掉 `online_basemaps` 字段与解析，`map:` 只剩 base_tile + keys（docstring 注明）。
  - 新 `ONLINE_BASEMAPS_FILENAME` 常量 + 注释头 `_ONLINE_BASEMAPS_HEADER`。
  - `load_online_basemaps(path)`：缺失/空/纯注释 → `[]`；顶层非列表 → `ValueError`；否则
    `[OnlineBasemapDef.from_dict(x) for x in data]`。
  - `append_online_basemap_to_yaml(path, d)`：文件缺失/空 → 建文件（注释头 + 第一条目）；
    已有 → **先预校验可解析成列表**（坏文件/非列表顶层不落盘直接抛），再文本追加到最后
    一个列 0 `- ` 条目之后，注释/排版原样保留，文件尾注释留在插入点后面。返回追加后完整清单。
  - `_item_block(d, dash_col=0)` 复用（顶层列表：`- ` 在列 0，正文缩进 2）。
- `config/app.yaml`：删 `online_basemaps:` 整块，换成一行指针注释；`map.keys` / `map.base_tile`
  原样保留。
- `config/online_basemap_config.yaml`（新增）：注释头 + 原 7 条整体搬来（天地图×3 → 高德×3 →
  自定义 arcgis），顺序/URL/percent 编码原样。
- `app/ui/main_window/main_window.py`：新 `_online_basemaps_config_path()`（from app.yaml 路径
  with_name）；`_online_basemap_defs()` 改 `load_online_basemaps(path)`（try/except → 日志 +
  `[]`）；`_add_online_basemap_template()` 去重读新文件、写回新文件、文案改「已写入
  online_basemap_config.yaml」。
- 文档同步：online_basemap_templates.py / online_basemap_dock.py / custom_basemap.py 的
  docstring「config/app.yaml 的 map.online_basemaps」→ 独立清单文件。

## 验证（全绿）
- `tests/env/test_config_and_runtime.py`：35 passed（改改写 + 新增 load/append/missing/raises）。
- `tests/core/domain/basemap/test_basemap.py` + `tests/ui/dialogs/test_custom_basemap.py`：30 passed。
- `tests/ui/docks/test_online_basemap_dock.py -m gui`：13 passed。
- `tests/ui/main_window/test_online_basemap.py -m gui`：11 passed。
- 冒烟：仓库 app.yaml → `map` 无 online_basemaps 属性，keys/base_tile 正常；
  `load_online_basemaps(config/online_basemap_config.yaml)` == 7 条，group/name 与原 app.yaml 等价。

## 回归快照（全绿，与迁移相关）+ 归档
- 本任务结束，git 改动留在 dev 分支不提交，供用户审阅。

## 备注
- 在线底图写回现在只落 `online_basemap_config.yaml`（Dock「添加在线底图/自定义底图」），
  app.yaml 的 keys/base_tile 不受影响（管理 Key 仍写 app.yaml map.keys）。
- 真机手动项（用户侧）：Dock 空白处右键加模板/自定义 → 条目进新文件、树刷新、重启仍在。
