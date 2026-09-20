# qgis_dev Python 编码规范

> 适用范围：`app/`、`config/`、`tests/`、`scripts/`、`tools/` 下的全部 Python 代码。
> `legacy/` 为归档只读代码，**不适用**本规范，也不参与 CI。
>
> 本规范分三档：**必须（MUST）**、**应当（SHOULD）**、**可以（MAY）**。
> 其中「必须」项由 ruff / mypy / pytest / CI 强制执行，评审时不接受例外。
>
> 本文件随项目结构演进同步维护：结构变了而规范没变，等于没有规范。

---

## 0. 原则

1. **可执行优先**：规范尽量沉淀为工具配置（`pyproject.toml`）或脚本门禁
   （`scripts/check_import_placement.py`），而不是靠人记。
2. **分层清晰**：业务逻辑与 UI / 服务端解耦，依赖方向单向向下。
3. **组合优于膨胀**：一个类/模块过大时按功能**横向拆成 mixin / 子组件**，
   而不是继续堆方法（见 §2.3）。
4. **中文可读**：注释、文档字符串用中文；标识符、日志键名用英文。
5. **小步一致**：新代码 100% 达标；存量代码按 §14 分批整改。

---

## 1. 工具链与量化基线（必须）

| 项目 | 约定 |
| --- | --- |
| Python | `>= 3.10`（`pyproject.toml` 已锁定，勿用 3.11+ 专属语法） |
| 行宽 | `120`（ruff `line-length`） |
| Lint / Format | `ruff`（lint + format，二者同版本） |
| 类型检查 | `mypy --strict`（QGIS / 三方无 stub 的模块走 `ignore_missing_imports`） |
| 测试 | `pytest`，覆盖率门槛 60%（CI 为 50%） |
| 提交门禁 | `pre-commit`（ruff --fix + ruff-format + mypy + import 位置检查） |
| CI 门禁 | lint → test-no-qgis → test-qgis → build-linux |

**常用命令**（本地，Windows + QGIS 4.2 环境）：

```bash
# lint / format（只对改动文件跑 format，理由见 §14.2）
ruff check app/ config/ tests/
ruff check --select I --fix <改动的文件...>

# 函数内 import 位置门禁（§4）
python scripts/check_import_placement.py app

# 类型检查
mypy

# 无 QGIS 的用例（快，日常主力）
pytest tests/ -m "not gui"

# 需要 QGIS 的用例：必须逐文件分批跑
QT_QPA_PLATFORM=offscreen "D:/zsh/app/qgis4.2/bin/python-qgis.bat" \
    -m pytest tests/ui/main_window/test_layer_dock.py -m gui -q -p no:cacheprovider
```

**必须**：`ruff` / `mypy` 在 `pyproject.toml`、`.pre-commit-config.yaml` 与 CI 中
锁定**同一版本**。禁止 `pip install ruff` 后不管版本——历史教训：本地/CI 用新版
ruff 会启用新规则（如 `RUF001-003`），瞬间产生上万条与业务无关的报错，导致 lint
形同虚设。

> ⚠️ **当前版本未对齐（已知问题，见 §14.2）**：本地 `ruff 0.16.5`，
> `.pre-commit-config.yaml` 锁 `v0.5.0`，CI 装未锁版本的 `ruff`。
> 后果之一：`ruff 0.16` 不再对「重新抛出」或「用 `exc_info` 记日志」的宽泛捕获
> 报 `BLE001`，于是仓库里 67 处 `# noqa: BLE001` 变成无效标注（`RUF100`）。
> 整改时**先统一版本再清理**，否则清理结果不可复现。

---

## 2. 项目结构、分层与依赖方向（必须）

### 2.1 目录结构（当前实际布局）

```
qgis_dev/
├── app/                        # 应用主代码（235 个模块）
│   ├── main.py                 # 桌面端入口（python -m app.main）
│   ├── bootstrap/              # 启动装配：读配置 → 路径 → 日志 → QGIS runtime
│   │                           #   → 登录 → initQgis → 主窗口（唯一跨层组合点）
│   ├── env/                    # 环境装配：AppConfig(config/app.yaml) / QgisRuntime
│   ├── infra/                  # 横切能力（不依赖 UI、不依赖 core）
│   │   ├── logging/            #   日志：logger / config / sink / formatter / renderer
│   │   │                       #   / styles / crash
│   │   ├── fs/                 #   paths.py（统一路径服务）/ io.py
│   │   └── messagebar/         #   全局消息条：floating_bar / qgis_bar
│   ├── core/                   # 业务层（不依赖 UI）
│   │   ├── auth/               #   登录鉴权：config.py（服务器地址/接口）+ client.py（HTTP）
│   │   │                       #   + saved_logins.py（记住的账号）/ _secret.py（密码保护：
│   │   │                       #     Windows→DPAPI、Linux/macOS→本机密钥）/ _user_paths.py
│   │   ├── data/               #   数据脚本：pgsql / gdb_to_postgis / local_db / import_maxent
│   │   ├── util/               #   底层工具：wkt / gpkg_geom / local_layer_loader / ogc_server_util
│   │   └── domain/             #   领域服务（一个能力一个子包）
│   │       ├── buffer/ check/ drawing/ editing/ flow/ generic_alg/ identify/
│   │       ├── imagery_mbtiles/ imagery_tiles/ offline_download/ oriented_bbox/
│   │       ├── overlay/ query/ reproject/ shortestpath/ tiles/
│   │       └── basemap.py geom_cleanup.py online_basemap_templates.py tile_prefetch.py
│   ├── server/                 # FastAPI 独立进程
│   │   ├── app.py cli.py config.py dependencies.py lifespan.py
│   │   └── routes/             #   health / check / map
│   └── ui/                     # 表现层（Qt）
│       ├── common/             #   基类与主题：base_dialog / frameless / frosted / theme /
│       │                       #   styles / icons / widgets / qt_compat / splash /
│       │                       #   algo_desc / canvas_zoom / tool_icons
│       ├── main_window/        #   主窗口（见 §2.3）
│       │   ├── main_window.py  #     只留装配顺序 + Qt 生命周期钩子（约 210 行）
│       │   ├── mixins/         #     12 个功能 mixin（见 §2.3）
│       │   ├── title_bar.py tool_bar.py side_toolbar.py status_bar.py
│       │   ├── draw_toolbar.py snapping_toolbar.py
│       │   └── layer_dock.py browser_dock.py
│       ├── docks/              #   面板：attribute / attribute_entry / feature_info /
│       │                       #   log / tools / analyse / data_manage / online_basemap / project
│       ├── dialogs/            #   对话框：login / query_builder / check_editor /
│       │                       #   style_io / vector_tile / execute_sql / custom_attr ...
│       ├── flow_editor/        #   流程编排编辑器：scene / node_item / edge_item /
│       │                       #   param_panel / log_panel / node_library / window
│       ├── tools/              #   交互工具：pan_zoom / select_feature / feature_edit /
│       │                       #   split_feature / merge_geometry / qmap_compare / swipe ...
│       └── resources/          #   资源装载
├── config/                     # app.yaml（主配置）/ online_basemap_config.yaml
├── resource/                   # 图标 / 字体 / svg / 密钥（非 Python 资源）
├── processing/                 # 随包分发的 QGIS processing 插件（grassaddons/models/scripts）
├── scripts/                    # 命令行入口与门禁脚本
│   ├── run_dev.py entry.py run_server.py run_check.py run_gdb2pg.py
│   ├── check_import_placement.py    # §4 的函数内 import 门禁（CI + pre-commit）
│   └── alias_lifecycle_check.py verify_draw_snap.py test_gpkg_path.py ...
├── tools/                      # 构建 / 打包：pyinstaller.spec / build_*.sh|ps1 / docker
├── tests/                      # pytest（api / core / env / infra / ui）
├── docs/                       # 本规范（docs/coding-standards.md）
├── .pi/                        # 会话记忆与提示词模板（**不入库**，本地开发辅助）
├── legacy/                     # 旧代码归档（只读参考，不参与 lint/CI）
└── pyproject.toml
```

### 2.2 依赖方向

依赖方向**单向向下**，禁止反向 import，禁止循环依赖：

```
app.bootstrap       启动装配（唯一允许同时碰 UI 与 infra 的入口）
      │
      ├── app.ui            表现层（Qt）
      ├── app.server        FastAPI 独立进程
      │
      ├── app.core.domain   业务层（不 import UI）
      ├── app.core.util     底层工具
      ├── app.core.data     数据脚本
      │
      └── app.env / app.infra   环境与横切能力（不依赖 UI / core）
```

| 层 | 允许依赖 | 禁止 |
| --- | --- | --- |
| `app.bootstrap` | 全部（装配点） | 放业务逻辑 |
| `app.ui` | `app.core.domain`、`app.core.util`、`app.infra`、`app.env`、`qgis.PyQt` | 被 `app.core` / `app.infra` import |
| `app.server` | `app.core.domain`、`app.infra` | import `app.ui` |
| `app.core` | `app.core.util`、`app.infra`、`qgis.core` | import `app.ui` / `app.server` / `app.bootstrap` |
| `app.infra` | 标准库、三方库 | import `app.core` / `app.ui` |
| `app.env` | 标准库、`yaml` | import UI |

**规则**：
- 业务层通过**返回值**或**信号（Signal）**把结果交给 UI，UI 负责渲染；
- 跨进程能力（如瓦片服务）走 HTTP，不要让 UI 进程内直接 `import app.server`；
- 横向能力（日志、路径、消息条）一律下沉到 `app.infra`，不得在业务层各自实现；
- 当前实测：`app.core` / `app.infra` / `app.env` / `app.server` 均无反向依赖；
  唯一 `import app.ui` 的是 `app/bootstrap/main_window.py`（合法装配点）。

### 2.3 `MainWindow` 的 mixin 组合（本项目特有，必须遵守）

`app/ui/main_window/main_window.py` 历史上膨胀到 2700+ 行 / 118 个方法，现按功能
拆成 **12 个 mixin**（`app/ui/main_window/mixins/`），由 `MainWindow` 多继承组合。

| mixin | 职责 |
| --- | --- |
| `shell.WindowShellMixin` | 画布、中心控件、停靠区、悬浮层、画布拖拽 |
| `chrome.WindowChromeMixin` | 顶部 chrome / 状态栏 / 主题 / Tab 切换器 |
| `edit_stack.EditStackMixin` | 撤销重做栈 |
| `map_tools.MapToolsMixin` | 地图工具与选择 |
| `app_actions.AppActionsMixin` | 各功能入口（分析工具、在线底图…） |
| `project_ops.ProjectOpsMixin` | 工程操作、图层入工程 |
| `online_basemap.OnlineBasemapMixin` | 在线底图 Dock |
| `file_browser.FileBrowserMixin` | 打开文件 / 数据浏览器 |
| `layer_actions.LayerActionsMixin` | 图层动作与右键菜单 |
| `layer_tree_ops.LayerTreeOpsMixin` | 图层树操作（增删改组、移动） |
| `layer_dialogs.LayerDialogsMixin` | 图层相关对话框 |
| `gpkg_alias.GpkgAliasMixin` | gpkg 别名清扫 |

**必须**：

1. **新功能加到对应 mixin，不要往 `main_window.py` 加方法**。`main_window.py`
   只保留 `__init__`（装配顺序）与 Qt 生命周期钩子（`closeEvent` 等）；个别与
   多 mixin 协同相关的胶水方法（如 `_clear_global_messagebar`）也留在此处。
2. **MRO 顺序**：功能 mixin 全部排在 `FramelessMixin` **之前**，
   `FramelessMixin` 仍须排在 `QMainWindow` 之前——`FramelessMixin` 与功能 mixin
   都定义/依赖 `eventFilter`，靠 `super()` 链协同；顺序错了，无边框缩放会先于
   宿主逻辑吃掉事件。
3. **方法签名与 `self.*` 契约不变**：mixin 只能访问 `self` 上的共享状态
   （`self.canvas` / `self.project` / `self.layer_dock` / `self._selected_layer` …），
   不得新增 mixin 之间的隐式依赖。这是「方法级测试零改动」的前提。
4. **模块 docstring 里的 mixin 清单顺序必须与真实 MRO 一致**（`main_window.py`
   的类 docstring 亦然）。清单与 `class MainWindow(...)` 基类顺序错位，是评审
   必挑的问题。
5. **每个 mixin 的 docstring 结尾统一写**：
   「由 :class:`~app.ui.main_window.main_window.MainWindow` 多继承组合，方法只依赖
   ``self`` 上的共享状态。」
6. **mixin 内不得写 `__init__`**，初始化逻辑留在 `MainWindow.__init__`。
7. 拆分/搬运必须**逐字搬运方法体**（不顺手 reformat），便于用脚本核验
   「方法数一致 + 源码逐字一致」。

> ⚠️ **搬迁方法会改变测试 monkeypatch 的目标模块**。测试里以模块路径字符串
> monkeypatch 模块级名字（如 `"app.ui.main_window.main_window.QMessageBox.information"`）
> 时，方法搬走后必须**同步更新**测试目标指向新 mixin 模块——re-export 无法让
> `monkeypatch.setattr` 生效。这是拆分的必要配套改动，不是「改了测试凑绿」。

### 2.4 包 `__init__.py` 与惰性导出（必须）

- 一般包 `__init__.py` 可以导出聚合符号（如 `app/ui/common/__init__.py` 导出
  `BaseDialog` / `Icons`）。
- **但不得让「QGIS 未初始化阶段会走到的 import 链路」触发重依赖加载。**
  登录对话框（`app.ui.dialogs.login`）在 `QgsApplication.initQgis()` **之前**运行，
  而无边框标题栏（`app.ui.common.frameless`）同一条链路上只需要 `TitleBar`、
  不依赖 QGIS。若包 `__init__` eager import 了 `MainWindow`（其顶层
  `from qgis.core import ...`），登录阶段就会 `ModuleNotFoundError`。
- 因此 `app/ui/main_window/__init__.py` 采用**惰性导出**：

  ```python
  __all__ = ["MainWindow"]

  def __getattr__(name: str):
      if name == "MainWindow":
          from .main_window import MainWindow
          return MainWindow
      raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
  ```

  调用方写法不变（`from app.ui.main_window import MainWindow` 照常可用）。
- `app/ui/main_window/mixins/__init__.py` **刻意不做 re-export**，只放包 docstring，
  避免上层意外加载 QGIS。

---

## 3. 命名（必须）

| 对象 | 规则 | 示例 |
| --- | --- | --- |
| 包 / 模块 | `snake_case`，简短 | `offline_download`、`oriented_bbox` |
| 类 | `PascalCase` | `BufferService`、`CheckParam` |
| 函数 / 方法 / 变量 | `snake_case` | `build_where`、`feature_count` |
| 常量 | `UPPER_SNAKE_CASE` | `_REGISTRY`、`MAX_TILE_ZOOM` |
| 私有成员 | 前缀 `_` | `_jobs`、`_on_render_finished` |
| 布尔量 | `is_` / `has_` / `can_` / `should_` | `is_valid`、`has_crs` |
| 测试 | `test_<被测>_<场景>` | `test_buffer_point_returns_layer` |
| mixin | `<域>Mixin`，放 `mixins/<域>.py` | `LayerTreeOpsMixin` / `layer_tree_ops.py` |

- **禁止**用 `l`、`I`、`O` 作变量名（`E741`），易与数字混淆。
- Qt 事件/虚函数覆盖**必须**保持 C++ 驼峰名（`closeEvent`、`showEvent`、
  `paintEvent`、`eventFilter`…），否则 Qt 无法回调；这类名字已在 `pyproject.toml`
  的 `pep8-naming.extend-ignore-names` 中按 glob **集中豁免**（`*Event`、
  `eventFilter`、`sizeHint`、`push*`、`*Completed` …）。
  **集中豁免是唯一被认可的方式**：不要手写 `# noqa: N802`。历史遗留的 82 处
  `# noqa: N802` 中有 76 处已因 glob 豁免而失效（`RUF100`），按 §14 清理。
- 非覆盖的普通方法一律 `snake_case`（例如 `sendMessageToTopParent` 应改名
  `send_message_to_top_parent`，见 §14.2）。
- Qt `Signal` 类属性沿用 camelCase（与 C++ 信号一致，如 `taskCompleted`）；
  对应外部配置/接口契约的字段名（如 Pydantic 的 `basePass`）保持源名不改。
- 缩写词按小写处理：`crs`、`wkt`、`gdb`、`api`、`url`（类名中可 `Crs` / `Wkt`）。

---

## 4. 导入（必须）

1. 每个模块首行（docstring 之后）统一：

   ```python
   from __future__ import annotations
   ```

2. 导入分三组，组间空一行：标准库 → 三方库 → 本项目（`app`）。
   `known-first-party = ["app"]` 已配置，交给 `ruff --fix`（`I001`）自动排序。

3. **延迟导入只给重依赖**。函数体内的 `import` **仅允许**下列两类：

   | 允许放在函数内 | 原因 |
   | --- | --- |
   | 重依赖：`qgis.*`、`qgis.PyQt.*`、`osgeo.*`、`processing*`、`rasterio`、`psycopg2`、`PyQt5`/`PyQt6`、`scipy`、`networkx` 等 | 无 QGIS / GDAL 运行时的环境下，顶层 `import` 会让整个模块不可导入，直接拖垮 `tests/api`、`tests/core` 等无 QGIS 用例 |
   | 确属无法避免的项目内循环依赖 | 必须紧邻写 `# noqa: PLC0415 - <为什么不能上提>` |

   **其余一律放模块顶端**，包括：
   - 标准库：`os` / `sys` / `json` / `logging` / `pathlib` / `tempfile` / `math` / `sqlite3` / `ctypes` / `typing` …
   - 轻量三方库（如 `yaml` 等非 GIS 重依赖）；
   - 一般的 `app.*` 项目内模块。

   函数内出现上述轻量导入即视为违规；不要用「函数内 import 比较快」当理由——
   Python 的 import 有模块缓存，成本在首次导入，与放在函数内无关。

   反例（禁止）：

   ```python
   def export(path: str) -> None:
       import json                       # ✗ 标准库
       import tempfile                   # ✗ 标准库
       from pathlib import Path          # ✗ 标准库
       from app.infra.fs import paths    # ✗ 项目内模块（除非为打破循环并注明）
   ```

   正例：

   ```python
   from typing import TYPE_CHECKING

   if TYPE_CHECKING:
       from qgis.core import QgsVectorLayer

   def run(self, layer: QgsVectorLayer) -> None:
       from qgis import processing  # ✓ 只有 QGIS 环境才有，必须延迟
       ...
   ```

4. **打破循环依赖**优先用结构性手段，不要把 import 塞进函数：
   - 仅用于类型标注 → 放 `if TYPE_CHECKING:`；
   - 需要真实对象 → 依赖注入（构造器参数）或把公共部分下沉到更底层模块；
   - 父子模块相互引用 → import 具体子模块，而不是包 `__init__` 的聚合符号。
   若穷尽手段仍需在函数内 import 项目内模块，必须写 `# noqa: PLC0415 - <原因>`。

5. 禁止 `from x import *`；`__all__` 按字母序排列（`RUF022`）。

6. **自动检查**。`scripts/check_import_placement.py` 扫描 `app/`（当前 235 个模块，
   0 违规）：
   - 重依赖（`HEAVY_ROOTS`：`qgis` / `osgeo` / `processing` / `PyQt5` / `PyQt6` /
     `psycopg2` / `rasterio` / `scipy` / `networkx`，或目标 `app` 模块自身传递依赖
     这些重依赖）的延迟导入自动放行（这类不该逐行标注）；
   - 其余函数内 import 必须带 `# noqa: PLC0415 - <原因>`，否则 CI 失败。
   - `tests/**` 不在检查范围：测试常在函数内延迟导入以隔离 QGIS 收集期副作用。
   - 已接入 `pre-commit` 与 CI（`lint` job）。当前全仓仅 5 处显式豁免。

---

## 5. 类型标注（必须）

- 所有**公共**函数、方法、模块级变量必须有完整标注；mypy `strict = true` 已开启。
- 使用 **PEP 604 / PEP 585** 新语法（目标 3.10，且已有 `from __future__ import annotations`）：

  ```python
  # 推荐
  def load(path: str, crs: str | None = None) -> list[QgsVectorLayer]: ...
  # 避免（存量代码待迁移，见 §14）
  def load(path: str, crs: Optional[str] = None) -> List[QgsVectorLayer]: ...
  ```

- 前向引用：有了 `from __future__ import annotations` 后，**不再需要**给标注加引号
  （`UP037`），删除冗余引号。
- `Any` 只能显式出现，并在同行注释说明原因；不得用 `Any` 逃避建模。
- QGIS 等无 stub 库的返回值可标注为具体类，`ignore_missing_imports` 只解决找不到模块的问题。

---

## 6. 数据建模（应当）

- **传参与内部数据结构**优先用 `@dataclass(frozen=True)`，字段带类型与默认值；
  字段含义用 `#:` 注释（Sphinx 风格），示例见 `app/core/domain/buffer/service.py`。

  ```python
  @dataclass(frozen=True)
  class BufferJob:
      """一次矢量缓冲区生成的参数。"""

      #: 输入矢量图层（必须是图层对象，不能是 source 字符串）
      layer: QgsVectorLayer
      distance: float = 100.0
  ```

- 配置文件（`config/app.yaml`）用 `@dataclass(frozen=True)` + `from_file/from_dict`
  工厂（见 `app/env/config.py` 的 `AppConfig`）。
- **边界**（HTTP 请求/响应）用 Pydantic v2 `BaseModel`；领域内部不要用 Pydantic。
- 禁止可变默认值（`def f(x=[])`），用 `None` + 内部初始化，或 `field(default_factory=...)`。
- 对外状态尽量不可变，修改用 `dataclasses.replace` 生成新对象。

---

## 7. 文档字符串与注释（必须）

- **模块、公共类、公共函数/方法**必须有中文 docstring；私有函数（`_` 前缀）可省略。
- 风格采用 Google 风格，支持 Sphinx 角色交叉引用（`:class:`、`:func:`、`:meth:`）。

  ```python
  def run(self, job: BufferJob, feedback: QgsProcessingFeedback | None = None) -> QgsVectorLayer:
      """执行缓冲区生成，返回结果图层（已置入工程上下文，未加入工程）。

      Args:
          job: 缓冲区参数。
          feedback: 进度反馈；``None`` 时内部新建一个。

      Returns:
          结果图层；文件输出时返回从文件重新加载的图层。

      Raises:
          RuntimeError: 结果图层无效。
      """
  ```

- 注释解释**为什么**，而不是复述代码；复杂取舍、踩坑、外部约束必须写清。
  本项目的踩坑注释是重要资产，例：`layer_actions._remove_layer` 的
  「关键时序 / 关键时序二」、`frameless.py` 的 MRO 说明。
- 中文注释/字符串中的**全角标点（，。：；（）「」）是允许且推荐的**，
  `RUF001/002/003` 已在配置中豁免，无需改写为半角。
- 禁止提交被注释掉的死代码，交给 git 历史保存。

---

## 8. 日志（必须）

- 模块级取得 logger，名字固定用 `__name__`：

  ```python
  from app.infra.logging import get_logger

  logger = get_logger(__name__)
  ```

- 用 **`%s` 参数化**，不要用 f-string 或 `%` 预拼接（否则丢失惰性求值与来源信息）：

  ```python
  logger.info("执行算法 %s：输出=%s", "native:buffer", out)   # 正确
  logger.info(f"执行算法 native:buffer：输出={out}")          # 错误
  ```

- 异常日志用 `logger.exception(...)` 或加 `exc_info=True`；可疑值用 `%r`；
  大对象只记关键特征（数量、类型、CRS 等）。
- **禁止**记录密码、token、连接串明文；日志文案中文，键值简洁。
- 异常/告警只记一次，不要每层重复打印同一堆栈。

---

## 9. 异常处理（必须）

- **禁止**裸 `except:`；**禁止** `except Exception: pass` 静默吞异常。
- 需要宽泛捕获时必须：① 加 `# noqa: BLE001`；② 写清为什么吞；③ 记日志；④ 重新抛出
  或返回明确的兜底值。

  ```python
  try:
      results = processing.run("native:buffer", params, context=context, feedback=fb)
  except Exception as exc:  # noqa: BLE001 - 算法可能抛各类运行时错误，需记录输入特征后重抛
      logger.error("native:buffer 执行失败 %s: %s", job.output_name, exc, exc_info=True)
      raise
  ```

- **`BLE001` 的豁免边界**（决定要不要写 `# noqa`）：
  - handler 里**重新抛出**（`raise`）→ 不报，**不需要** noqa；
  - handler 里用 `logger.exception(...)` / `exc_info=True` 记日志 → 不报，**不需要** noqa；
  - 只有真正「吞掉且不记日志」的 `except Exception:` 才需要 `# noqa: BLE001 - <原因>`。
  当前全仓 233 处 `# noqa: BLE001` 中 67 处已属多余（§14.2）。
- 业务错误定义专用异常（如 `CheckError`），UI 边界统一兜底并提示用户；
  不要用 `assert` 做运行期校验（`-O` 下会被移除）。
- `raise ... from exc` 保留因果链，除非刻意屏蔽底层细节。

---

## 10. Qt / UI 规范（必须）

- Qt 绑定**统一**用 `from qgis.PyQt.QtWidgets import ...`；不直接 `import PyQt6` /
  `PyQt5`（由 QGIS 版本决定绑定），`pyproject.toml` 里的 PyQt6 仅供 pip 解析。
  枚举作用域与 `exec()` 已由 `qgis.PyQt` 做 Python 层 shim；**未 shim 的差异**
  （如 `QMouseEvent.globalPosition()` 仅 Qt6 有、Qt5 要用 `globalPos()`）集中到
  `app/ui/common/qt_compat.py`，调用方只调 `qt_compat` 的辅助函数，不写版本分支。
- 信号槽：槽函数命名 `_on_<sender>_<signal>`；自定义信号用类属性 `Signal(...)`。
- 跨线程通信**只能**通过 Qt `Signal`（或队列），不得在子线程直接操作控件。
- 主线程（GUI 线程）**禁止**执行长耗时计算/阻塞 IO；耗时任务放 worker / 线程，
  结果经信号回主线程更新。
- UI 只做表现与交互，业务逻辑调用 `app.core.domain` 服务；UI 不写 SQL / 算法细节。
- 连接信号前确认接收者生命周期，避免对已销毁对象发信号；必要时用
  `getattr(self, "_alive", False)` 守卫。
- 自定义 `eventFilter` 必须返回 `super().eventFilter(obj, event)` 兜底（多继承链
  靠它传递，见 §2.3）；`MainWindow` 的 `eventFilter` 由 `FramelessMixin` 与
  `WindowShellMixin` 协作，改动前先看 MRO。

### 10.1 事件栈纪律（必须，踩坑高发）

Qt 信号槽栈内**不得**直接做会触发重绘/重新渲染的画布操作（`canvas.refresh()`、
`zoomScale`、`unsetMapTool`、增删图层后立刻刷新…）：QGIS 会在半更新状态下同步
render，Windows 上表现为原生崩溃（`0xC0000409`）或残留脏帧。统一用
`QTimer.singleShot(0, ...)` 推到下一个事件循环 tick：

```python
# 延迟刷新画布到下一个事件循环 tick，让 layersRemoved 信号链走完
QTimer.singleShot(0, self._deferred_refresh_after_remove)
```

渲染类覆盖层（如 `app/ui/tools/swipe.py` 的卷帘镜像渲染）另需**过期 job 淘汰**：
用单调递增的 `_render_seq` 给每个 job 编号，回调时对不上序号、或会话已拆
（`_alive` 为假）就直接 `return`，避免旧渲染覆盖新状态：

```python
def _on_render_finished(self, seq: int, kind: str) -> None:
    if not getattr(self, "_alive", False) or seq != self._render_seq:
        return  # 过期 / 被取消 job 的 finished，忽略（会话可能已拆）
```

### 10.2 对象所有权与悬垂访问（必须）

- `QgsProject` 对加入其中的图层**持有所有权**：`project.removeMapLayer(id)` 会连
  C++ 对象一起 `delete`。因此**凡是要从 `layer` 读的字段（`id()` / `name()` /
  `source()` / `isEditable()` …）必须在 `removeMapLayer` 之前读完**；之后再碰
  `layer` 会抛 `RuntimeError: wrapped C/C++ object ... has been deleted`
  （`sip.isdeleted(layer)` 为 `True`），把后续逻辑整段跳过。典型症状：图层从
  面板消失了，画布上图形还在原地——因为排在后面的刷新被异常跳过。
  同类注意：`removeMapLayers([...])`、临时图层清理、流程执行器里替换图层。
- 需要判断「图层/对象是否已被销毁」优先用 `qgis.PyQt.sip.isdeleted(obj)`，不要靠
  `hasattr` 猜（对已删除的 sip 对象 `hasattr` 仍可能返回 `True`）。但
  `sip.isdeleted` 只对 sip 包装对象有意义；遍历任意顶层 `QWidget` 做清理时仍可
  用 `try/except RuntimeError` 兜底（见 `tests/ui/conftest.py` 的收尾逻辑）。
- 显式销毁顶层 QWidget 用 `sip.delete(w)`；`close()` 只隐藏、不销毁 C++ 对象。

### 10.3 父子控件与坐标换算（必须）

- `QWidget.mapTo(parent, pos)` **要求 `parent` 是祖先**；两个**兄弟**控件之间换算
  坐标要经由全局坐标做差：

  ```python
  # 侧边工具条与画布是兄弟：用 mapToGlobal 之差换算
  canvas_pos = self.mapToGlobal(QPoint(0, 0)) - canvas.mapToGlobal(QPoint(0, 0))
  ```

- 覆盖层（悬浮消息条、侧边工具条、SwipeOverlay、SplitDividerOverlay…）之间存在
  z-order 竞争：挂到画布子级会互相遮挡。统一挂到中心控件（`central`）并显式
  置顶/重排，不要依赖创建顺序。

---

## 11. 服务端 FastAPI 规范（必须）

- 通过 `create_app()` 工厂构造（`app/server/app.py`），路由用 `APIRouter`，
  按功能拆分到 `app/server/routes/`（`health` / `check` / `map`）。
- 依赖（配置、服务）走 `Depends(get_xxx)`（见 `app/server/dependencies.py`），
  不在路由里 new 单例；生命周期资源放 `lifespan.py`。
- 请求/响应体用 Pydantic 模型；路由函数只做参数校验与调服务，不写业务实现。
- CORS 开发期可放开，**生产必须**改为白名单（`create_app` 内已有注释标注）。
- 服务端组件不得 import `app.ui`，保证可独立部署。

---

## 12. 测试规范（必须）

- 目录镜像被测代码：`tests/api`、`tests/core`（含 `tests/core/domain/<能力>/`）、
  `tests/env`、`tests/infra`、`tests/ui`（含 `tests/ui/docks`、`tests/ui/dialogs`、
  `tests/ui/main_window`、`tests/ui/flow_editor`）。
- 需要 GUI / QGIS 的测试加 `@pytest.mark.gui`（当前 77 个测试文件、66 个带 gui 标记），
  无 QGIS 的用例不得依赖它；可选依赖用 `pytest.importorskip("fastapi")` 保护。
- `tests/conftest.py` 提供 `sys.path` 注入与 logger 隔离的 autouse fixture；
  `tests/ui/conftest.py` 提供 `qgis_app`（session 级）等 QGIS fixture，勿重复造轮子。
- 临时文件用 `tmp_path`；不要写死 `D:/...` 绝对路径到断言里。
- 断言具体行为与边界（空输入、无效 CRS、超长字段等），不要只断言「不抛异常」。
- 覆盖率不低于 60%（CI 当前 50%）；新增核心模块应自带用例。

### 12.1 本地运行（Windows + QGIS，必须逐文件）

> ⚠️ **`tests/` 当前未纳入版本库**（`.gitignore` 含 `tests/`）。
> 直接后果：CI 里的 `ruff check tests/` 与 `pytest tests/` **实际不跑用例**。
> 覆盖率门禁因此形同虚设。这是已知缺口（§14.2），修法见下。

- 无 QGIS 用例：`pytest tests/ -m "not gui"`。
- QGIS/GUI 用例必须**用 QGIS 自带 python 逐文件分批跑**：一次性跑多个文件会在
  多重 teardown 时触发原生堆损坏（`0xC0000409` / `0xC0000374`），表现为 pytest
  停在 `[100%]` 打不出汇总行。

  ```bash
  QT_QPA_PLATFORM=offscreen "D:/zsh/app/qgis4.2/bin/python-qgis.bat" \
      -m pytest tests/ui/main_window/test_layer_dock.py -m gui -q -p no:cacheprovider
  ```

- `tests/ui/conftest.py` 的 session 收尾会 `sip.delete()` 残留顶层控件并
  `exitQgis()`——不要在用例里缓存窗口对象跨 session 复用。
- 判定「失败是否预先存在」用 `git stash`/`git checkout` 对照 HEAD 基线，或先写
  复现测试确认在未修复代码上 FAILS，再落码修复。

---

## 13. 性能与资源（应当）

- 矢量批量处理用 `QgsFeatureRequest` 过滤、`setSubsetOfAttributes` 裁剪，避免全表遍历。
- 大栅格/大图层操作放后台线程或 Processing 算法，避免阻塞 GUI（配合 §10.1）。
- 大数据量属性表按可视区域渲染 / 流式加载，不要一次拉全表。
- 及时释放临时图层与文件句柄；写入文件后从文件重载再使用（见 `BufferService`）。
- 图层生命周期见 §10.2：移除前读完需要的字段，移除后不要再引用对象。
- 循环内避免重复的 `QgsProject.instance()` / 网络 / IO 调用，能提出循环就提出。

---

## 14. 例外与存量整改

### 14.1 Ruff 规则取舍（已写入 `pyproject.toml`）

| 规则 | 处理 | 原因 |
| --- | --- | --- |
| `RUF001/002/003` | **忽略** | 中文注释/字符串使用全角标点是刻意的 |
| `BLE001` | **启用** | 需让 `# noqa: BLE001` 生效；豁免边界见 §9 |
| `N802/N803/N815` | **集中豁免** | Qt 事件/虚函数/Signal 必须保持 C++ 驼峰名，在 `pep8-naming.extend-ignore-names` 中按 glob 豁免 |
| `PLC0415` | **由脚本强制** | ruff 的 `PLC0415` 无法区分重依赖；改由 `scripts/check_import_placement.py` 检查函数内轻量导入，并在 `external` 中声明以接受 `# noqa: PLC0415 - <原因>` 标注（见 §4 第 6 条） |
| `E501` | **忽略** | 行宽统一交给 `ruff format` 处理 |
| `legacy/**` | **全部忽略** | 归档只读代码 |
| `tests/**` | 忽略 `PT011`、`S101` | 测试允许宽泛断言与 `assert` |

### 14.2 存量债务清单（实测，随整改更新）

当前基线：`ruff 0.16.5`，`ruff check app/ config/ tests/` → **1322 errors（998 可自动修复）**；
`ruff format --check` → **175 个文件待重排**（147 已合规）。
**CI 目前因此为红**（`lint` job 的 format 检查必失败）。

`ruff check app/ config/ tests/ --statistics` 输出（`[*]` = 可用 `--fix` 自动修复）：

```
286  UP045  [*] non-pep604-annotation-optional      （Optional[X] -> X | None）
199  RUF100 [*] unused-noqa                         （失效的 # noqa，细分见下）
184  UP037  [*] quoted-annotation                   （前向引用多余引号）
116  I001   [*] unsorted-imports
 87  F401   [*] unused-import                       （其中 app/ 内 67 条）
 79  SIM105 [ ] suppressible-exception               （try/except/pass -> contextlib.suppress）
 74  UP006  [*] non-pep585-annotation                （List[X] -> list[X]）
 49  UP035  [-] deprecated-import                    （typing.List 等旧式导入）
 49  PT018  [ ] pytest-composite-assertion
 20  RUF059 [ ] unused-unpacked-variable
 17  E741   [ ] ambiguous-variable-name              （变量名 l）
 15  BLE001 [ ] blind-except                         （真正未标注的宽泛捕获）
 12  W292   [*] missing-newline-at-end-of-file
 10  F841   [ ] unused-variable
  9  PT022  [ ] pytest-useless-yield-fixture
  8  B008   [ ] function-call-in-default-argument    （Depends / QModelIndex 等）
  8  N802   [ ] invalid-function-name                （app/ 内仅 1，见下）
  8  RUF022 [*] unsorted-dunder-all
  6  SIM108 [ ] if-else-block-instead-of-if-exp
  6  C416   [ ] unnecessary-comprehension
其余 31 条规则单条 <=5，合计 80 条（N803/N806/UP015/F821/B905/SIM102/RUF005…
另含 F811 3、RET501 3、F541 2、B009 2、UP012 2、E402 5 等）
Found 1322 errors. [*] 998 fixable.
```

需人工判断/有风险的重点项：

| 规则 | 处理 |
| --- | --- |
| `F821` ×5（**未定义名，潜在 `NameError`**） | **优先修**（基本都是一行漏写 import）：`core/data/gdb_to_postgis.py:97` 缺 `gdb_to_postgis_from_config`；`core/domain/identify/service.py:186` 缺 `QgsRasterLayer`；`core/domain/offline_download/layout.py:65,69` 缺 `Optional`；`ui/dialogs/offline_manager.py:192` 缺 `QFileDialog` |
| `RUF100` ×199 | 见下细分表；**先统一 ruff 版本再清**，否则结果不可复现 |
| `SIM105` ×79 | 改 `contextlib.suppress`（§14.3 第 5 步） |
| `E741` ×17 | 变量名 `l` 改名 |
| `BLE001` ×15 | 真宽泛捕获，补 `# noqa: BLE001 - <原因>` + 日志（§9） |
| `N802` ×8（`app/` 内仅 `messagebar/qgis_bar.py:70`） | 非 Qt 覆写，改名 `send_message_to_top_parent` |
| `B008` ×8 / `N818` ×2 / `N801` ×1 | 默认参数调用（`Depends` / `QModelIndex`）与命名，按规则取舍或集中豁免 |
| `E402` ×5（均在 `tests/`） | `tests/conftest.py` 的导入位置调整 |

**`RUF100`（199 处失效标注）细分**——清理前先统一 ruff 版本（§1）：

| 成因 | 数量 | 处理 |
| --- | --- | --- |
| `unused: N802` | 76 | glob 已豁免，**直接删 `# noqa: N802`** |
| `unused: BLE001` | 67 | handler 已 re-raise / 已 `exc_info` 记日志（§9），**直接删 `# noqa: BLE001`** |
| `unused: E402` | 41 | 导入前有 `sys.path` / `os.environ` 调整，ruff 对这类**不报** `E402`，删标注 |
| `non-enabled: D401/S310/S606/ARG002` | 13 | 引用了未启用的规则集（pydocstyle / bandit / flake8-unused-arguments），删标注或正式启用规则 |
| 其它（`F841`/`F401`） | 2 | 原本就没触发，删标注 |

其它量化事实（供拆分/重构时对照）：

| 指标 | 值 |
| --- | --- |
| `app/` Python 模块数 / 行数 | 235 / 47,145 |
| `# noqa` 总行数 | 339（`BLE001` 233 / `N802` 82） |
| `# type: ignore` | 30 |
| `Optional[...]` 等旧式标注 | 326 处 |
| `app/ui/main_window/main_window.py` | 212 行（拆分前 2712 行 / 118 方法） |
| `app/ui/main_window/mixins/` | 13 文件 / 2,869 行 |
| `tests/` 测试文件 | 77（66 个带 `gui` 标记） |
| 函数内 import 门禁 | 235 模块，0 违规，5 处显式豁免 |

### 14.3 分批整改计划（做完删掉本节）

按「风险低 → 高」「可自动 → 需人工」排序，每步单独提交：

```bash
# 0) 先统一 ruff 版本（改 pyproject / .pre-commit-config / ci.yml），
#    否则 RUF100 等结果不可复现。

# 1) 清理失效标注（纯删除，无行为变化）
ruff check app/ config/ tests/ --select RUF100 --fix

# 2) 现代化类型标注：Optional[X] -> X | None，List -> list，去冗余引号
ruff check app/ tests/ --select UP --fix

# 3) 排序导入、删除未用导入、补文件末尾换行、整理 __all__
ruff check app/ tests/ --select I,F401,W292,RUF022 --fix

# 4) 修 F821 未定义名（人工，5 处，每处补测试）

# 5) 人工确认项：SIM105（contextlib.suppress）、E741（变量名 l）、
#    B008、N802（sendMessageToTopParent）

# 6) 统一格式（会触碰 175 个文件，单独提交，便于 review 忽略空白 diff）
ruff format app/ config/ tests/
```

**必须**：第 6 步落地后，CI 的 `ruff format --check` 才算真正生效；在此之前
**不要**全仓 `ruff format`，也不要以 format 结果为准做语义改动。

**结构性缺口（需产品/流程决策，非纯代码整改）**：

1. **`tests/` 未入库** → CI 的 lint/test 阶段对 `tests/` 实际为空跑，覆盖率门禁
   失效。建议至少把 `tests/` 纳入版本库（或改为 CI 单独拉取测试集）。
2. **CI 与本地 QGIS 版本不一致**：CI 用 `qgis/qgis:3.40.5`，本地开发为 QGIS 4.2；
   版本相关行为（枚举、`exec_`、插件路径）在 CI 上覆盖不到。
3. **CI 触发分支为 `main` / `dev`**，而当前开发分支为 `dev-feature-01`，
   推分支不触发流水线。
4. `ruff` / `mypy` 三方版本未锁（CI `pip install ruff` 无版本），见 §1。

---

## 15. 提交与评审

- 提交前 `pre-commit run --all-files` 必须全绿；CI 四道门任一失败不得合并。
- 一个提交只做一件事；重构与功能分成不同提交，便于回滚。
  - 纯搬运/拆分（如 §2.3 的 mixin 拆分）单独一个提交，且**保持方法体逐字不变**；
  - 拆分后配套的测试 monkeypatch 目标更新，可并入同一提交（属于必要条件）。
- 提交信息沿用项目既有风格：`task # <描述>` 或 `task 调整`。
- PR 描述写清**动机、方案、影响面、验证方式**；涉及架构分层调整需在描述中说明。
- 新增依赖：同步更新 `pyproject.toml` 的 `dependencies` / `dev`，并说明用途与许可证。

---

## 附 A：新文件模板

```python
"""<模块一句话说明>。

<可选：更详细的背景、约束、与旧实现的差异。>
"""

from __future__ import annotations

from dataclasses import dataclass

from app.infra.logging import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True)
class MyJob:
    """<一次任务/请求的参数。>"""

    #: <字段说明>
    name: str
    count: int = 1


def run(job: MyJob) -> str:
    """<执行并返回结果。>

    Args:
        job: <参数说明>。

    Returns:
        <返回值说明>。

    Raises:
        RuntimeError: <何时抛出>。
    """
    logger.info("run %s count=%s", job.name, job.count)
    return job.name
```

**新增 mixin**（见 §2.3）：文件头写职责说明 + 结尾统一句，类名 `<域>Mixin`，
只依赖 `self` 上既有状态；同时更新 `main_window.py` 的基类列表**与类 docstring
清单**（顺序一致）。

**新增领域服务**：`app/core/domain/<能力>/service.py`，配
`app/core/domain/<能力>/__init__.py` 导出服务类，测试放
`tests/core/domain/<能力>/test_service.py`。
