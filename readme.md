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
│   │   │   ├── flow/           #     流程编排：类型/模型/拓扑/序列化/执行器/算法库
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
│   │   ├── flow_editor/        #   流程编排编辑器：画布/节点/连线/参数面板/日志
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
│   ├── protect_cython.py       #   Cython 原生编译保护：Qt-free 模块 → .pyd/.so + manifest
│   ├── build_windows.ps1       #   Windows 打包：清理 → 编译保护 → PyInstaller → 复制 QGIS 运行时
│   ├── build_linux.sh          #   Linux 打包
│   ├── runtime/                #   便携启动器
│   │   ├── start_runtime.bat   #     与 app.exe 同目录，设 QGIS 环境后启动（打包会复制进产物）
│   │   └── launch_qgis.sh
│   └── docker/
├── tests/                      # pytest（api / core / env / infra / ui）
├── legacy/                     # 旧代码归档（只读参考）
├── pyproject.toml              # 依赖、ruff、mypy、pytest 配置
├── docs/coding-standards.md    # Python 编码规范（分层 / 命名 / 类型 / 日志 / 测试）
└── dist/app/                   # 打包产物（见「打包（技术方案）」的目录结构）
```

---

## 开发规范

编码规范见 **[docs/coding-standards.md](docs/coding-standards.md)**，提交前请确保：

```bash
ruff check app/ config/ tests/ --fix
ruff format app/ config/ tests/
mypy
pytest tests/ -m "not gui"
```

---

## 启动（开发）

```bash
# 一次性：把项目装进 QGIS Python 的 site-packages（在 QGIS Python 里执行）
"D:/zsh/app/qgis4.2/bin/python-qgis.bat" -m pip install -e "D:/zsh/code/py/qgis_dev"

# 桌面端（dev）
"D:/zsh/app/qgis4.2/bin/python-qgis.bat" -m app.main
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

### 产物目录结构

根目录只保留 6 项，其余全部收进 `_internal/`（PyInstaller 6 标准布局）：

```
dist/app/
├── app.exe                     入口
├── start_runtime.bat           启动器（显式设 QGIS 环境；双击 app.exe 也可）
├── config/                     应用配置（明文，可直接改）
├── resource/                   应用资源（明文，可直接改）
├── logs/                       运行日志（运行时生成）
├── runtime/                    QGIS 运行时数据（可整体替换 / 升级）
│   ├── etc/ i18n/ plugins/ providers/ platforms/
│   ├── proj.db
│   └── resources/srs.db
└── _internal/                  Python 运行时 + 全部依赖库 + 业务 .pyd
    ├── python312.dll  base_library.zip
    ├── qgis/ osgeo/ numpy/ PyQt6/ ...（第三方包）
    └── app/**/*.pyd            编译保护后的业务代码
```

### 构建命令

```bat
powershell -ExecutionPolicy Bypass -File tools\build_windows.ps1     rem Windows
bash tools/build_linux.sh                                            rem Linux / 银河麒麟 V10
```

> **银河麒麟 V10 打包**：代码已做 PyQt6 → `qgis.PyQt` 兼容改造（麒麟源/conda-forge 的 QGIS 均为 PyQt5），
> 在麒麟机/容器上 `bash tools/build_linux.sh` → `bash tools/kylin/build_deb.sh` 出 `.deb`。
> 完整指南见 [`tools/kylin/README.md`](tools/kylin/README.md)。

`build_windows.ps1` 自动完成四步：
1. **清理** 旧 `dist/`、`build/`
2. **编译保护**（可选）：把 Qt-free 的纯 Python 模块用 Cython 编成 `.pyd`（详见下节）
3. **PyInstaller**（`tools/pyinstaller.spec`）：入口 `scripts/entry.py`（包外脚本，避免直接以包内 `app/main.py` 为入口时 `app.bootstrap` 打不进包）
4. **复制运行时可分离部分**：QGIS 数据（`etc/`、`plugins/`、`i18n/`、`providers/`、`platforms/`、`proj.db`、`srs.db`）→ `dist/app/runtime/`；`config/`、`resource/` → 产物根；并把 `start_runtime.bat` 复制进产物

### 启动打包结果

- **直接双击 `dist\app\app.exe`**：应用冻结态自动探测便携包（见下），无需任何环境变量
- **双击 `dist\app\start_runtime.bat`**：显式设置环境变量后启动（与 app.exe 同目录即可用）

### 免装 QGIS 的关键设计

1. **运行时可分离**：QGIS 数据集中放 `dist/app/runtime/`（`proj.db`、`etc/`、`plugins/`、`i18n/`、`providers/`、`platforms/`、`resources/srs.db`）；Python 库与扩展（含 `qgis` python 包）在 `_internal/`。
2. **`QgisRuntime.for_portable`**（`app/env/qgis_runtime.py`）：把 `QGIS_PREFIX_PATH`、`PROJ_LIB`、`GDAL_DATA`、`QT_PLUGIN_PATH` 等环境变量全部指向 `dist/app/runtime`，PROJ 能在包内找到 `proj.db`。
3. **`_build_qgis_runtime` 优先 `QGIS_PREFIX`**（`app/bootstrap/__init__.py`）：launcher 设 `QGIS_PREFIX=dist/app/runtime` → 走 `for_portable`；若指向完整 QGIS 安装（有 `apps/qgis`）→ 走 `for_prod`。
4. **`_ensure_portable_env` 自动探测**：PyInstaller 冻结态下，`app.exe` 所在目录若含 `qgis/` + `proj.db` 即识别为便携包，自动设置全部环境变量——所以**直接双击 app.exe 也能跑**。
5. **config 打包 + `QGIS_DEV_*` 环境变量**：打包后 `__file__` 指向 `_internal/`，而 `config/`、`resource/` 在顶层，靠 launcher/自动探测把路径指回去。
6. **Qt 平台插件**：`apps/qt6/plugins/platforms/` → `dist/app/runtime/platforms/`（GUI 显示必需）。
7. **DLL 运行时隔离**（`tools/rthook_dll_dirs.py`）：`qgis/__init__.py` 会把 `PATH` 里每个目录都 `add_dll_directory`；若目标机还装着另一份 QGIS/GDAL（OSGeo4W/conda），会加载到版本不匹配的 `gdal313.dll`/`proj_9.dll`，报 `ImportError: DLL load failed while importing _core: 找不到指定的程序`。运行时钩子在 `qgis` 被 import 前把 `_internal/` 前置到 DLL 搜索路径，并**剔除提供了随包同名 DLL 的外部 PATH 目录**（系统目录除外），保证便携包只用随包运行时。

### spec 要点（`tools/pyinstaller.spec`）

- 入口 `scripts/entry.py`，`pathex` 为项目根；用 `SPECPATH`（而非 `__file__`）推导项目根（spec 命名空间没有 `__file__`）。
- `contents_directory="_internal"`（仅 Windows）：collected 文件（Python 库、扩展、DLL）全部落到 `_internal/`，根目录干净；Linux 构建脚本已自建 `lib/`、`share/qgis/` 布局，保持扁平。
- `datas`（Windows）：只留 `qgis` python 包；`resource/`、`config/`、`srs.db`、`proj.db`、`platforms/` 改由 `build_windows.ps1` 的 STAGE 4 复制到产物根 / `runtime/`。
- `binaries`（Windows）：`qgis_core.dll`、`qgis_gui.dll`、`gdal313.dll`、`geos_c.dll`、`pdalcpp.dll`、`proj_9.dll`、Qt6 核心 DLL（均落 `_internal/`）。
- `runtime_hooks`：`tools/rthook_dll_dirs.py`，隔离随包运行时与宿主机同名 DLL（见上）。
- `icon`（Windows）：构建时用 Pillow 从 `resource/png/app_icon.png` 生成多尺寸 `build/app_icon.ico`（16/24/32/48/64）嵌进 `app.exe`——即资源管理器 / 任务栏上看到的文件图标。**换图标只需替换该 png**（源图小于 256 时不生成 256 档位）。
  ⚠️ 必须用 `bitmap_format="bmp"` 生成：Pillow 11.3+ 默认把 ICO 各档位存成 **PNG 压缩条目**，资源管理器的图标渲染对“纯 PNG、无 BMP 兜底”的 ICO 会回退成默认图标（尽管 `ExtractIconEx` 能读到），表现为“exe 图标没换”。
- `hiddenimports`：qgis.core/gui/utils + qgis.PyQt + 全部 PyQt 子模块（Win=PyQt6，Linux=PyQt5，spec 按平台自动选）。

### 代码保护（Cython 原生编译）

**背景**：PyInstaller 只是打包器——PYZ 就是一堆 `.pyc` 字节码，用标准库 `struct/zlib/marshal` 几十行即可从中还原全部模块名与反编译，`console=False`/UPX 不构成任何保护；PyInstaller 6 更已**移除**内置加密（`--key`/`cipher`），PyArmor 试用版会拒绝大脚本。因此本项目采用 **Cython 原生编译**：把纯逻辑模块编译成机器码 `.pyd`，产物里既无 `.py` 也无 `.pyc`。

**流程**（`tools/protect_cython.py` + `tools/pyinstaller.spec`）：

1. `build_windows.ps1` 的 **STAGE 2** 调用 `tools/protect_cython.py`，按 `config/app.yaml` 的 `build.protect.cython` 配置把目标模块编成 `.pyd`，写出 `build/protect/manifest.json`。
2. PyInstaller **分析阶段仍用原始 `.py`**（保证 `collect_submodules` / 动态导入收集完整）。
3. spec 读 manifest，把这些模块的**字节码从 PYZ 剔除**，只把 `.pyd` 作为二进制收进 `dist/app/_internal/app/...`（Linux 仍为 `dist/app/app/...`）。
4. 运行期冻结导入器在 PYZ 找不到模块时回退到磁盘 FileFinder（`PyInstaller/loader/pyimod02_importers.py` 的 `PyiFrozenFinder.find_spec`），于是加载 `.pyd`。

**默认范围**：只编译 **不 import `qgis`/`PyQt` 的模块**（约 104 个），已覆盖 `app.core.auth`（密钥/授权/凭据/DPAPI）、`app.core.data`、`app.core.util`、`app.core.domain` 算法、`app.infra`、`app.env`、`app.server`。

**必须遵守的三条硬约束**（都踩过坑）：

| 约束 | 原因 |
|---|---|
| `qt_free_only: true`，Qt 模块保持字节码 | Cython 编译出的函数/类与 **PyQt signal/slot 不兼容**：`.connect(编译方法)` 会 `TypeError` 甚至硬崩溃 `0xC0000409` |
| `annotation_typing: false` | 否则 Cython 把类型注解当 C 类型约束，会拒编“注解与实际不符”的代码，并静默改变运行语义 |
| `skip_module_getattr: true` | 模块级 `def __getattr__`（PEP 562 惰性导出）编译后访问属性会与导入机无限递归（栈溢出） |

**配置**（`config/app.yaml`）：

```yaml
build:
  protect:
    cython:
      enabled: true
      include: [app]          # 编译范围（包会递归）
      exclude: []             # 显式跳过的模块/包
      fail_open: true         # 单模块编译失败则回退为字节码，不中断构建
      skip_module_getattr: true
      qt_free_only: true      # 只编译 Qt-free 模块（强烈建议保持 true）
      compiler_include: ""    # 留空自动探测 Python 开发文件
      compiler_libs: ""
```

**开关 / 注意事项**：

- `powershell -File tools\build_windows.ps1` 默认开启；`-NoProtect` 关闭（快速调试构建）。
- 编译需要 Python 开发文件（`Python.h` + `python3XX.lib`）。QGIS 自带 Python 通常是精简版（无 `Include/`、`libs/`），工具会自动探测：`sysconfig` → 环境变量 `QGIS_PYTHON_INCLUDE`/`QGIS_PYTHON_LIBS` → 同类 OSGeo4W/conda 安装（要求 `python3XX.lib` **次版本匹配**）。
- **扩大范围前务必实跑 UI**：把某个 Qt 模块加进 `include` 或关掉 `qt_free_only` 很容易让信号槽失效。若某模块出现 `TypeError: ... takes exactly 1 positional argument` 或启动即崩，把它加进 `exclude`。

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
- **QGIS 运行时装配**：`QgisRuntime.apply_env()` 幂等地写入 `QGIS_PREFIX_PATH` / `PROJ_LIB` / `GDAL_DATA` / `QT_PLUGIN_PATH` 等，并加入 `sys.path`；支持三种模式（dev 安装 / prod 安装 / portable `runtime/` 布局）。

### 主窗口（`app/ui/main_window/main_window.py`）

旧 `MainWindow` 700+ 行，拆成子组件 + 回调：
`MenuBar`（文件/查看菜单）· `ToolBar` · `StatusBar` · `LayerDock`（图层树）· `BrowserDock`（数据浏览器）· `LogDock`，外加绘图/编辑工具条。

### 工程管理（`app/ui/docks/project_dock.py`）

新建工程时弹窗输入工程名 → 创建两个固定分组 **数据图层 / 参考图层** → 名字带到右侧工程 Dock 显示。Dock 含：
1. 工程名（只读展示）
2. 数据图层树 / 参考图层树（各显示一个分组，普通 `QTreeView` + `QgsLayerTreeModel`，用 `setRootIndex` 指向分组）
3. 渲染与标注（对当前选中图层打开渲染/标注设置）

### 登录与鉴权（`app/core/auth`）

**每次启动都要登录**：弹 `LoginDialog`，用户名密码提交到授权服务器换取 token，
地址 / 接口按 `config/app.yaml` 的 `env:` 选段（`app/core/auth/config.py` 的 `PROFILES`，
加环境只改那一处），token 只存内存、**不落盘**。
登录成功后会**记住这次的用户名 + 密码**（`app/core/auth/saved_logins.py`；密码不落明文：
Windows 用 DPAPI，Linux / 麒麟用机器 + 用户绑定的本机密钥），下次启动点用户名输入框弹气泡选中即同时回填两个输入框（气泡是独立的
`Qt.Popup`，不改变登录框本身的外观）；不想记密码可设 `QGIS_DEV_REMEMBER_PASSWORDS=0`。
打包后 `initQgis` 在登录通过后才执行；取消登录则直接退出。
登录通过后还会拉一次**当前用户信息**（`GET …/currentUserByToken?access_token=…`，
同时带上 `unitkey: Bearer <token>` 头；实现在 `app/core/auth/current_user.py`，
**请求走下面那个统一工具**），主窗口标题栏因此显示的是**姓名 / 单位 /
部门 / 上次登录**（拉不到就回退只显示登录账号）；拉取失败**不拦登录**。详见 `app/auth.md`。

**除登录外，访问业务服务器统一走 `app/core/util/service_client.py`**：`request(接口路径, 参数, 请求方式)`
自动按 `env:` 取 `ip:port`、自动把登录 token 拼到 `unitkey` 头（登录时 bootstrap 已
`set_session_token`），失败抛 `ServiceError`；命令行可单发一次：
`python -m app.core.util.service_client GET /forestry-api/xxx/list`。

### 流程编排工具（`app/core/domain/flow` + `app/ui/flow_editor`）

可视化流程节点编排：画布上拖拽节点建流程，节点间连线表达数据流依赖，按拓扑序整跑或单节点分步执行。纯逻辑（类型/模型/拓扑/序列化/执行器）在 `app.core.domain.flow`，不依赖 QGIS/Qt，可独立测试；画布交互在 `app.ui.flow_editor`。

**打开入口**
- 主菜单「流程 → 流程节点编辑器」（Ctrl+Shift+F）
- 工具 dock「流程编排 → 节点式流程编排」

**建节点**
- 左侧**节点库**（数据源 / 矢量分析 / 栅格分析 / 通用）拖拽到画布；「处理算法」分组首次展开时枚举 QGIS 处理工具箱算法，拖入即生成节点
- 或选中节点后在右侧面板「选择处理算法」下拉 / 「插入常用算法」按钮（缓冲区 / 裁剪 / 投影转换 / 重分类 / 字段计算 / 栅格镶嵌），自动替换节点的输入输出与执行逻辑

**编辑节点（右侧参数面板，三块编辑区）**
- **输入配置**：名称 / 类型 / 默认值 / 必填 / 绑定上游；改动类型会自动断开不兼容的连线并提示
- **输出配置**：名称 / 类型 / 保存路径（可浏览）/ 写入临时图层 / 加载到图层面板
- **执行逻辑**：PyQGIS 代码编辑区，引用 `inputs` 字典、给 `outputs` 字典赋值（命名空间含 `processing` / `context` / `feedback` 及常用 QGIS 类）；「单节点运行」单独调试
- 标题栏双击重命名；右键菜单：重命名 / 复制 / 删除 / 禁用 / 跳过

**连线（端口类型校验）**
- 从输出端口拖到输入端口（反向拖也支持）；类型不兼容（矢量 / 栅格 / 字段表 / 数值 / 布尔 / 字符串 / 文件 / 文件夹 / 多选字段）拒绝建连
- 成环连线拒绝（拓扑校验）；选中节点后工具条「高亮依赖链」显示整条上下游链路，其余节点变灰
- 节点移动时连线跟随；删除节点自动清理相连边

**运行**
- 运行全部（F5）：按拓扑序执行，禁用 / 跳过节点不跑（灰色状态），任一失败即中止并把失败节点标红
- 单步：选中节点「单节点运行」，上游缺缓存时用默认值并记警告
- 重置参数：清空所有运行缓存与状态
- 底部日志面板：节点级耗时 / 输入输出摘要 / 成败（按级别着色）

**导入导出**
- 新建 / 打开 JSON / 保存 / 另存为；未保存关闭时弹窗确认。schema v1，坏边（引用未知节点 / 类型不兼容）导入时收集 warnings 并提示

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
| 6. 便携打包 | ✅ | `tools/` 统一 spec + ps1/sh + `start_runtime.bat`（Win）/ `launch_qgis.sh`（Linux）便携启动器 + 免装 QGIS |
| 7. 流程编排 | ✅ | `app.core.domain.flow`（类型/模型/拓扑/序列化/执行器/算法库）+ `app.ui.flow_editor`（画布/参数面板/日志）+ 菜单与工具 dock 入口 |


还需要安装

D:\zsh\app\qgis4.2\bin\python-qgis.bat -m pip install pysqlite3