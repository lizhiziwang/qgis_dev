# qgis_dev Python 编码规范

> 适用范围：`app/`、`config/`、`tests/`、`scripts/`、`tools/` 下的全部 Python 代码。
> `legacy/` 为归档只读代码，**不适用**本规范，也不参与 CI。
>
> 本规范分三档：**必须（MUST）**、**应当（SHOULD）**、**可以（MAY）**。
> 其中「必须」项由 ruff / mypy / pytest / CI 强制执行，评审时不接受例外。

---

## 0. 原则

1. **可执行优先**：规范尽量沉淀为工具配置（`pyproject.toml`），而不是靠人记。
2. **分层清晰**：业务逻辑与 UI / 服务端解耦，依赖方向单一。
3. **中文可读**：注释、文档字符串用中文；标识符、日志键名用英文。
4. **小步一致**：新代码 100% 达标；存量代码按 §14 分批整改。

---

## 1. 工具链与量化基线（必须）

| 项目 | 约定 |
| --- | --- |
| Python | `>= 3.10`（`pyproject.toml` 已锁定，勿用 3.11+ 专属语法） |
| 行宽 | `120`（ruff `line-length`） |
| Lint / Format | `ruff`（lint + format，二者同版本） |
| 类型检查 | `mypy --strict`（QGIS / 三方无 stub 的模块走 `ignore_missing_imports`） |
| 测试 | `pytest`，覆盖率门槛 60%（CI 为 50%） |
| 提交门禁 | `pre-commit`（ruff --fix + ruff-format + mypy） |
| CI 门禁 | lint → test-no-qgis → test-qgis → build-linux |

**常用命令**（提交前本地自测）：

```bash
ruff check app/ config/ tests/ --fix
ruff format app/ config/ tests/
mypy
pytest tests/ -m "not gui"
```

**必须**：`ruff`、`mypy` 在 `pyproject.toml` 与 `.pre-commit-config.yaml` 中锁定**同一版本**，
CI 也要锁版本（`pip install 'ruff==0.5.0'`）。禁止 `pip install ruff` 后不管版本——
历史教训：本地/CI 用新版 ruff 会启用 preview 规则（如 `RUF001-003`），
瞬间产生上万条与业务无关的报错，导致 lint 形同虚设。

---

## 2. 架构分层与依赖方向（必须）

依赖方向**单向向下**，禁止反向 import，禁止循环依赖：

```
app.bootstrap       启动装配（唯一允许同时碰 UI 与 infra 的入口）
      │
      ├── app.ui            表现层（Qt）
      ├── app.server        FastAPI 独立进程
      │
      └── app.core.domain   业务层（不 import UI）
      │
      └── app.infra         横切能力（logging / fs / messagebar，不依赖 UI）
```

| 层 | 允许依赖 | 禁止 |
| --- | --- | --- |
| `app/ui` | `app.core.domain`、`app.infra`、`qgis.PyQt` | 直接被 `app.core` import |
| `app/server` | `app.core.domain`、`app.infra` | import `app.ui` |
| `app.core.domain` | `app.infra`、`app.core.util`、`qgis.core` | import `app.ui` / `app.server` |
| `app.infra` | 标准库、三方库 | import `app.core` / `app.ui` |

**规则**：
- 业务层通过**返回值**或**信号（Signal）**把结果交给 UI，UI 负责渲染；
- 跨进程能力（如瓦片服务）走 HTTP，不要让 UI 进程内直接 `import app.server`；
- 横向能力（日志、路径、消息条）一律下沉到 `app.infra`，不得在业务层各自实现。

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

- **禁止**用 `l`、`I`、`O` 作变量名（`E741`），易与数字混淆。
- Qt 事件/虚函数覆盖**必须**保持 C++ 驼峰名（`closeEvent`、`showEvent`、`paintEvent`…），
  否则 Qt 无法回调；这类名字已在 `pyproject.toml` 的 `pep8-naming.extend-ignore-names`
  中集中豁免（`*Event`、`eventFilter`、`sizeHint`、`push*`…），**不要**手写 `# noqa`。
  非覆盖的普通方法一律 `snake_case`（例如 `sendMessageToTopParent` 应改名
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
   | `qgis.*`、`qgis.PyQt.*`、`osgeo.*`、`processing*`、`rasterio`、`psycopg2` 等重依赖 | 无 QGIS / GDAL 运行时的环境下，顶层 `import` 会让整个模块不可导入，直接拖垮 `tests/api`、`tests/core` 等无 QGIS 用例 |
   | 确属无法避免的项目内循环依赖 | 必须紧邻写 `# noqa: PLC0415 - <为什么不能上提>` |

   **其余一律放模块顶端**，包括：
   - 标准库：`os` / `sys` / `json` / `logging` / `pathlib` / `tempfile` / `math` / `sqlite3` / `ctypes` / `typing` …
   - 轻量三方库（如 `yaml`、`numpy` 等非 GIS 重依赖）；
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

6. **自动检查**。`scripts/check_import_placement.py` 扫描 `app/`：
   - 重依赖（`qgis` / `osgeo` / `processing` 等，或目标 `app` 模块自身传递依赖这些
     重依赖）的延迟导入自动放行（这类不该逐行标注）；
   - 其余函数内 import 必须带 `# noqa: PLC0415 - <原因>`，否则 CI 失败。
   `tests/**` 不在检查范围：测试常在函数内延迟导入以隔离 QGIS 收集期副作用。

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

- **边界**（HTTP 请求/响应、配置文件）用 Pydantic v2 `BaseModel`；领域内部不要用 Pydantic。
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

- 异常日志加 `exc_info=True`；可疑值用 `%r`；大对象只记关键特征（数量、类型、CRS 等）。
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

- 业务错误定义专用异常（如 `CheckError`），UI 边界统一兜底并提示用户；
  不要用 `assert` 做运行期校验（`-O` 下会被移除）。
- `raise ... from exc` 保留因果链，除非刻意屏蔽底层细节。

---

## 10. Qt / UI 规范（必须）

- Qt 绑定**统一**用 `from qgis.PyQt.QtWidgets import ...`；
  不直接 `import PyQt6` / `PyQt5`（由 QGIS 版本决定绑定），`pyproject.toml` 里的 PyQt6 仅供 pip 解析。
- 信号槽：槽函数命名 `_on_<sender>_<signal>`；自定义信号用类属性 `Signal(...)`。
- 跨线程通信**只能**通过 Qt `Signal`（或队列），不得在子线程直接操作控件。
- 主线程（GUI 线程）**禁止**执行长耗时计算/阻塞 IO；耗时任务放 worker / 线程，
  结果经信号回主线程更新。
- UI 只做表现与交互，业务逻辑调用 `app.core.domain` 服务；UI 不写 SQL / 算法细节。
- 连接信号前确认接收者生命周期，避免对已销毁对象发信号；必要时用 `getattr(self, "_alive", False)` 守卫。

---

## 11. 服务端 FastAPI 规范（必须）

- 通过 `create_app()` 工厂构造，路由用 `APIRouter`，按功能拆分到 `app/server/routes/`。
- 依赖（配置、服务）走 `Depends(get_xxx)`（见 `app/server/dependencies.py`），不在路由里 new 单例。
- 请求/响应体用 Pydantic 模型；路由函数只做参数校验与调服务，不写业务实现。
- CORS 开发期可放开，**生产必须**改为白名单（`create_app` 内已有注释标注）。
- 服务端组件不得 import `app.ui`，保证可独立部署。

---

## 12. 测试规范（必须）

- 目录镜像被测代码：`tests/api`、`tests/core`、`tests/env`、`tests/infra`、`tests/ui`。
- 需要 GUI / QGIS 的测试加 `@pytest.mark.gui`，无 QGIS 的用例不得依赖它；
  可选依赖用 `pytest.importorskip("fastapi")` 保护。
- `tests/conftest.py` 已提供 `sys.path` 注入与 logger 隔离的 autouse fixture，勿重复造轮子。
- 临时文件用 `tmp_path`；不要写死 `D:/...` 绝对路径到断言里。
- 断言具体行为与边界（空输入、无效 CRS、超长字段等），不要只断言「不抛异常」。
- 覆盖率不低于 60%（CI 当前 50%）；新增核心模块应自带用例。

---

## 13. 性能与资源（应当）

- 矢量批量处理用 `QgsFeatureRequest` 过滤、`setSubsetOfAttributes` 裁剪，避免全表遍历。
- 大栅格/大图层操作放后台线程或 Processing 算法，避免阻塞 GUI。
- 及时释放临时图层与文件句柄；写入文件后从文件重载再使用（见 `BufferService`）。
- 循环内避免重复的 `QgsProject.instance()` / 网络 / IO 调用，能提出循环就提出。

---

## 14. 例外与存量整改

### 14.1 Ruff 规则取舍（已写入 `pyproject.toml`）

| 规则 | 处理 | 原因 |
| --- | --- | --- |
| `RUF001/002/003` | **忽略** | 中文注释/字符串使用全角标点是刻意的 |
| `BLE001` | **启用** | 代码已用 `# noqa: BLE001` 标注宽泛捕获，需让 noqa 生效 |
| `N802/N803/N815` | **集中豁免** | Qt 事件/虚函数/Signal 必须保持 C++ 驼峰名，在 `pep8-naming.extend-ignore-names` 中按 glob 豁免 |
| `PLC0415` | **由脚本强制** | ruff 的 `PLC0415` 无法区分重依赖；改由 `scripts/check_import_placement.py` 检查函数内轻量导入，并在 `external` 中声明以接受 `# noqa: PLC0415 - <原因>` 标注（见 §4 第 6 条） |
| `E501` | **忽略** | 行宽统一交给 `ruff format` 处理 |
| `legacy/**` | **全部忽略** | 归档只读代码 |

### 14.2 存量整改 TODO（按批次做，做完删掉本节）

以下为当前 `ruff check` 仍报的历史问题，均可自动修复，建议单独一个 PR：

```bash
# 1) 现代化类型标注：Optional[X] -> X | None，List -> list，去掉冗余引号
ruff check app/ tests/ --select UP --fix

# 2) 排序导入、删除未用导入、补文件末尾换行、整理 __all__、清理失效 noqa
ruff check app/ tests/ --select I,F401,W292,RUF022,RUF100 --fix

# 3) 函数内导入（§4）：存量轻量导入已上提；余下 6 处刻意豁免
#    （4 循环依赖 + 2 可选依赖）已加 `# noqa: PLC0415 - <原因>`；
#    另有约 160 处重依赖延迟导入合规。
python scripts/check_import_placement.py

# 4) 上面这条作为 CI 硬门禁（替代无法区分重依赖的 ruff PLC0415）

# 5) 统一格式（会触碰较多文件，建议单独提交，便于 review 忽略空白 diff）
ruff format app/ config/ tests/
```

已知待人工确认项：`SIM105`（改 `contextlib.suppress`）、`E741`（变量名 `l`）、
`F821`（未定义名）、`B008`（默认参数里的函数调用）、`N802`（`sendMessageToTopParent`
应改名为 `send_message_to_top_parent`）。

---

## 15. 提交与评审

- 提交前 `pre-commit run --all-files` 必须全绿；CI 四道门任一失败不得合并。
- 一个提交只做一件事；重构与功能分成不同提交，便于回滚。
- PR 描述写清**动机、方案、影响面、验证方式**；涉及架构分层调整需在描述中说明。
- 新增依赖：同步更新 `pyproject.toml` 的 `dependencies` / `dev`，并说明用途与许可证。

---

## 附：新文件模板

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
