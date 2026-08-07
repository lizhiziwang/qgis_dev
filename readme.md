# qgis_dev

基于 **PyQGIS** 的桌面 GIS 应用 + FastAPI 服务，工程化重构版本。

> 旧代码归档在 `legacy/`，新代码在 `app/`。新代码从 `app.bootstrap` 启动：业务逻辑走 `app.core.domain` 服务，UI 走 `app.ui`，服务端走 `app.server`（独立进程）。所有模块只依赖注入、不反向 import UI。

---

## 目录功能

```
qgis_dev/
├── app/                        # 应用主代码
│   ├── main.py                 # 桌面端入口（python -m app.main）
│   ├── bootstrap/              # 启动流程：读配置 → 装配路径 → 装配日志 →
│   │   │                       #   装配 QGIS runtime → 登录校验 → initQgis → 主窗口
│   │   ├── __init__.py         #   main()；_ensure_portable_env() 打包免装 QGIS 自动探测
│   │   └── main_window.py      #   主窗口构建工厂
│   ├── env/                    # 环境装配
│   │   ├── config.py           #   AppConfig：config/app.yaml 反序列化
│   │   └── qgis_runtime.py     #   QgisRuntime：for_dev / for_prod / for_portable
│   │                           #   （便携模式环境变量指向包内，目标机免装 QGIS）
│   ├── infra/                  # 横切关注（不依赖 UI）
│   │   ├── logging/            #   日志：Logger / 文件+控制台 / LogDock 渲染
│   │   ├── fs/                 #   paths.py（统一路径服务）/ io.py
│   │   └── messagebar/         #   全局消息条
│   ├── core/                   # 业务层（不依赖 UI，只发信号/返回结果）
│   │   ├── domain/             #   领域服务
│   │   │   ├── basemap.py      #     天地图 XYZ 底图加载（Referer 预处理）
│   │   │   ├── check/          #     质检：参数/查询/注册表/服务/空间索引
│   │   │   ├── drawing/        #     绘图（DrawManager + qgis 工具）
│   │   │   ├── editing/        #     图层编辑服务
│   │   │   ├── identify/       #     点击识别
│   │   │   ├── query/          #     属性/空间查询
│   │   │   └── tiles/          #     瓦片相关
│   │   └── data/               #   离线数据脚本：pgsql / gdb_to_postgis / import_maxent
│   ├── ui/                     # 表现层
│   │   ├── common/             #   base_dialog / icons / styles / widgets
│   │   ├── main_window/        #   MainWindow 拆分子组件：
│   │   │   │                   #     menu_bar / tool_bar / status_bar
│   │   │   │                   #     layer_dock（图层树）/ browser_dock（数据浏览器）
│   │   │   │                   #     draw_toolbar / qgis_digitizing_toolbar
│   │   │   └── main_window.py  #   主窗口装配 + 事件分发
│   │   ├── docks/              #   面板：attribute / feature_info / log / tools /
│   │   │                       #     analyse / project（工程管理：数据图层树+参考图层树+渲染标注）
│   │   ├── dialogs/            #   对话框：login / query_builder / check_editor /
│   │   │                       #     style_io / vector_tile / execute_sql / custom_attr ...
│   │   └── tools/              #   交互工具：pan_zoom / find_feature / draw_manager
│   └── server/                 # FastAPI 服务（独立进程）
│       └── routes/             #   路由
├── config/app.yaml             # 主配置：env / crs / map(底图) / qgis / paths / logging / server
├── scripts/                    # 命令行入口
│   ├── run_dev.py              #   桌面端开发入口
│   ├── entry.py                #   PyInstaller 打包入口（包外脚本，避免包内入口导入问题）
│   └── run_server.py / run_gdb2pg.py / run_check.py
├── tools/                      # 构建 / 打包
│   ├── pyinstaller.spec        #   统一 PyInstaller spec（Win/Linux 共用，QGIS 路径读 config）
│   ├── build_windows.ps1       #   Windows 打包：清理 → PyInstaller → 复制 QGIS 运行时
│   ├── build_linux.sh          #   Linux 打包
│   ├── runtime/                #   便携启动器
│   │   ├── launch_qgis.bat     #     与 app.exe 同目录，设 QGIS 环境后启动（打包会复制进产物）
│   │   └── launch_qgis.sh
│   └── docker/
├── tests/                      # pytest（api / core / env / infra / ui）
├── legacy/                     # 旧代码归档（只读参考）
├── pyproject.toml              # 依赖、ruff、mypy、pytest 配置
└── dist/app/                   # 打包产物（app.exe + 便携 QGIS 运行时）
```

---

## 启动（开发）

```bash
# 一次性：把项目装进 QGIS Python 的 site-packages（在 QGIS Python 里执行）
"D:/zsh/app/qgis_3.40/bin/python-qgis-ltr.bat" -m pip install -e "D:/zsh/code/py/qgis_dev"

# 桌面端（dev）
"D:/zsh/app/qgis_3.40/bin/python-qgis-ltr.bat" -m app.main
# 或
python scripts/run_dev.py

# FastAPI 服务（独立进程）
python -m app.server                 # 默认 0.0.0.0:8000
python scripts/run_server.py

# 离线数据脚本
python -m app.core.data.gdb_to_postgis --gdb D:/data/xxx.gdb
python -m app.core.data.import_maxent --maxent-root D:/data/maxent
```

---

## 测试 / 静态检查

```bash
pytest tests/ -m "not gui"        # 不依赖 QGIS 的测试
pytest tests/                     # 全部（需 QGIS Python 环境）
ruff check app/ config/ tests/
mypy --strict app/infra app/env
```

---

## 打包（技术方案）

### 目标：绿色便携，目标机免装 QGIS

打包产物 `dist/app/` 是**自包含便携包**：QGIS 运行时（`proj.db`、`qgis` python 包、`etc/`、`plugins/`、`i18n/`、核心 DLL、Qt 平台插件）全部打进目录，目标机器**无需安装 QGIS**。

### 构建命令

```bat
powershell -ExecutionPolicy Bypass -File tools\build_windows.ps1     rem Windows
bash tools/build_linux.sh                                            rem Linux
```

`build_windows.ps1` 自动完成三步：
1. **清理** 旧 `dist/`、`build/`
2. **PyInstaller**（`tools/pyinstaller.spec`）：入口 `scripts/entry.py`（包外脚本，避免直接以包内 `app/main.py` 为入口时 `app.bootstrap` 打不进包）
3. **复制 QGIS 运行时**：`etc/`、`o4w_env.bat`、`plugins/`、`i18n/`、`providers/` → `dist/app/`，并把 `launch_qgis.bat` 一并复制进产物

### 启动打包结果

- **直接双击 `dist\app\app.exe`**：应用冻结态自动探测便携包（见下），无需任何环境变量
- **双击 `dist\app\launch_qgis.bat`**：显式设置环境变量后启动（与 app.exe 同目录即可用）

### 免装 QGIS 的关键设计

1. **扁平便携布局**：QGIS 运行时直接铺在 `dist/app/` 根下（`proj.db`、`qgis/`、`etc/`、`plugins/`、`i18n/`、DLL），而非 QGIS 安装的 `apps/qgis-ltr` 结构。
2. **`QgisRuntime.for_portable`**（`app/env/qgis_runtime.py`）：把 `QGIS_PREFIX_PATH`、`PROJ_LIB`、`GDAL_DATA`、`QT_PLUGIN_PATH` 等环境变量全部指向 `dist/app`，PROJ 能在包内找到 `proj.db`。
3. **`_build_qgis_runtime` 优先 `QGIS_PREFIX`**（`app/bootstrap/__init__.py`）：launcher 设 `QGIS_PREFIX=dist/app` → 走 `for_portable`；若指向完整 QGIS 安装（有 `apps/qgis-ltr`）→ 走 `for_prod`。
4. **`_ensure_portable_env` 自动探测**：PyInstaller 冻结态下，`app.exe` 所在目录若含 `qgis/` + `proj.db` 即识别为便携包，自动设置全部环境变量——所以**直接双击 app.exe 也能跑**。
5. **config 打包 + `QGIS_DEV_*` 环境变量**：打包后 `__file__` 指向 `_internal/`，而 `config/`、`resource/` 在顶层，靠 launcher/自动探测把路径指回去。
6. **Qt 平台插件**：spec 里额外打包 `apps/Qt5/plugins/platforms/` → `dist/app/platforms/`（GUI 显示必需）。

### spec 要点（`tools/pyinstaller.spec`）

- 入口 `scripts/entry.py`，`pathex` 为项目根；用 `SPECPATH`（而非 `__file__`）推导项目根（spec 命名空间没有 `__file__`）。
- `datas`：`qgis` python 包、`resource/`、`config/`、`srs.db`、`proj.db`。
- `binaries`：`qgis_core.dll`、`qgis_gui.dll`、`gdal311.dll`、`geos_c.dll`、`pdalcpp.dll`、`proj_9.dll`、Qt5 核心 DLL、Qt 平台插件。
- `hiddenimports`：qgis.core/gui/utils + 全部 PyQt5 子模块。

---

## 技术方案

### 分层架构

```
app.bootstrap  →  装配（config → paths → logging → qgis_runtime → login → 主窗口）
      │
      ▼
app.core.domain  ←  业务服务（check / drawing / editing / query / identify / tiles / basemap）
      │
      ▼
app.ui  ←  表现层（MainWindow + docks + dialogs + tools），只发信号、由 MainWindow 做业务
```

- **单向依赖**：`app.core` 不依赖 UI；`app.ui` 通过信号/回调把业务交给 `MainWindow`（回调注入模式）。
- **统一路径**：所有资源路径走 `app.infra.fs.paths.Paths`，支持环境变量覆盖（打包后由 launcher 注入）。
- **QGIS 运行时装配**：`QgisRuntime.apply_env()` 幂等地写入 `QGIS_PREFIX_PATH` / `PROJ_LIB` / `GDAL_DATA` / `QT_PLUGIN_PATH` 等，并加入 `sys.path`；支持三种模式（dev 安装 / prod 安装 / portable 扁平包）。

### 主窗口（`app/ui/main_window/main_window.py`）

旧 `MainWindow` 700+ 行，拆成子组件 + 回调：
`MenuBar`（文件/查看菜单）· `ToolBar` · `StatusBar` · `LayerDock`（图层树）· `BrowserDock`（数据浏览器）· `LogDock`，外加绘图/编辑工具条。

### 工程管理（`app/ui/docks/project_dock.py`）

新建工程时弹窗输入工程名 → 创建两个固定分组 **数据图层 / 参考图层** → 名字带到右侧工程 Dock 显示。Dock 含：
1. 工程名（只读展示）
2. 数据图层树 / 参考图层树（各显示一个分组，普通 `QTreeView` + `QgsLayerTreeModel`，用 `setRootIndex` 指向分组）
3. 渲染与标注（对当前选中图层打开渲染/标注设置）

### 登录与凭证（`app/core/auth`）

启动优先本机未过期凭证；无凭证弹 `LoginDialog`，登录成功签发 30 天凭证。打包后 `initQgis` 在登录通过后才执行。

### FastAPI 服务（`app/server`）

独立进程，默认 `0.0.0.0:8000`，路由在 `app/server/routes/`；与桌面端可并行部署。

---

## 重构进度

| 阶段 | 状态 | 关键产出 |
|---|---|---|
| 1. 基础设施 | ✅ | `app/infra/{logging,fs,messagebar}` + `app/env/{config,qgis_runtime}` + `app/bootstrap` |
| 2. 质检重写 | ✅ | `app.core.domain.check` |
| 3. Core Domain 其它 | ✅ | drawing / editing / query / identify / tiles 全部 service 化 |
| 4. UI 重建 | ✅ | `MainWindow` 拆子组件 + docks + dialogs |
| 5. FastAPI + 离线 | ✅ | `app.server` 独立进程 + `core/data/*` |
| 6. 便携打包 | ✅ | `tools/` 统一 spec + ps1/sh + `launch_qgis` 便携启动器 + 免装 QGIS |
