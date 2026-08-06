# qgis_dev

PyQGIS 桌面应用 + FastAPI 服务的工程化版本。

> 旧代码在 `legacy/`，新代码在 `app/`。新代码从 `app.bootstrap` 启动，所有业务逻辑走 `app.core.domain` 服务，UI 走 `app.ui`，服务端走 `app.server`（独立进程）。

## 目录

```
qgis_dev/
├── app/
│   ├── bootstrap/        启动流程（单例装配：config → log → qgis → main_window）
│   ├── core/            业务层（不依赖 UI）
│   │   ├── domain/      check / drawing / editing / query / identify / tiles
│   │   └── data/        离线数据脚本（pgsql / gdb_to_postgis / import_maxent）
│   ├── env/             QGIS 路径装配
│   ├── infra/           横切关注：logging / fs / messagebar
│   ├── server/          FastAPI 服务（独立进程）
│   └── ui/              表现层：common / main_window / docks / dialogs / tools
├── config/              app.yaml
├── scripts/             命令行入口
├── tests/               pytest 测试（api / core / env / infra / ui）
├── tools/               pyinstaller.spec / build_windows.ps1 / build_linux.sh / docker/
├── legacy/              归档旧代码
├── conftest.py          pytest conftest（sys.path 清理 + log 隔离）
├── __init__.py          项目根包标记（防 pytest namespace 冲突）
└── pyproject.toml       依赖、ruff、mypy、pytest 配置
```

## 启动

```bash
# ===== 一次性配置（推荐）=====
# 把项目装到 QGIS Python 的 site-packages，之后任意脚本都能直接 import app.*
# 注意：要在 QGIS Python 里执行这条命令
"D:/zsh/app/qgis_3.40/bin/python-qgis-ltr.bat" -m pip install -e "D:/zsh/code/py/qgis_dev"

# ===== 桌面端 =====
"D:/zsh/app/qgis_3.40/bin/python-qgis-ltr.bat" -m app.main      # 推荐：模块模式
"D:/zsh/app/qgis_3.40/bin/python-qgis-ltr.bat" app/main.py       # 脚本模式（已内置 sys.path bootstrap）
python scripts/run_dev.py                                       # 等价

# FastAPI 服务（独立进程）
"D:/zsh/app/qgis_3.40/bin/python-qgis-ltr.bat" -m app.server   # 模块模式（推荐）
python -m app.server                      # 默认 0.0.0.0:8000
python scripts/run_server.py
python -m app.server --host 127.0.0.1 --port 9000

# 离线数据脚本
python -m app.core.data.import_maxent --maxent-root D:/data/maxent
python -m app.core.data.gdb_to_postgis --gdb D:/data/xxx.gdb
```

## 测试

```bash
# 不依赖 QGIS 的全部测试
pytest tests/ -m "not gui"

# 全部测试（需要 QGIS Python 环境）
pytest tests/

# 单测 + 静态检查
pytest tests/infra tests/env -v
ruff check app/ config/ tests/
mypy --strict app/infra app/env
```

## 打包

```bash
# Windows
pwsh tools/build_windows.ps1

# Linux
bash tools/build_linux.sh

# Docker
docker build -f tools/docker/Dockerfile -t qgis-app .
```

## 重构进度

| 阶段 | 状态 | 关键产出 |
|---|---|---|
| 1. 基础设施 | ✅ | `app/infra/{logging,fs,messagebar}` + `app/env/{config,qgis_runtime}` + `app/bootstrap` |
| 2. 质检重写 | ✅ | `app.core.domain.check` — dataclass + @register + 修 `QSGGeometry` → `QgsGeometry` |
| 3. Core Domain 其它 | ✅ | `drawing` / `editing` / `query` / `identify` / `tiles` 全部 service 化 |
| 4. UI 重建 | ✅ | `MainWindow` 600 行 + 6 个子组件 + 4 dock + 10 dialog |
| 5. FastAPI + 离线 + 打包 | ✅ | `app.server` 独立进程 + `core/data/*` + `tools/pyinstaller.spec` + CI |

详见 `C:/Users/zk/.claude/plans/staged-soaring-cake.md`。
