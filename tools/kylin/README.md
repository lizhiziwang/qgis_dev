# 银河麒麟 V10 打包指南

把 qgis_dev（PyQGIS 桌面应用）打包成麒麟 V10 上可安装、可从桌面菜单启动的软件。
**目标形态**：自包含便携包 + `.deb` 安装包，目标机免装 QGIS。

---

## 0. 先回答三个问题（决定整个方案）

| # | 问题 | 检查命令 | 影响 |
|---|------|---------|------|
| 1 | 麒麟版本：V10 SP1/SP2 = **Ubuntu 20.04 底座**（glibc 2.31），V10 SP3 = **Ubuntu 22.04 底座**（glibc 2.35） | `cat /etc/os-release`、`ldd --version` | **打包机的底座必须 ≤ 目标机**，否则 glibc 不兼容、起不来 |
| 2 | CPU 架构：x86_64（兆芯/Intel/AMD）还是 aarch64（飞腾/鲲鹏） | `uname -m` | PyInstaller/conda 产物**按架构不通用**，两架构要各打一份 |
| 3 | 目标机能否联网（apt / 拷贝安装） | — | 本方案是**离线免装**：所有运行时打进包，目标机断网也能装 |

> 本仓库的便携包路线对 #3 是零依赖：QGIS/Qt/GDAL 全部打进 `/opt/qgis_app`，`.deb` 只依赖麒麟桌面自带的系统基础库。

---

## 1. 最大的坑：本项目原本用 PyQt6，但 Linux 的 QGIS 全是 PyQt5（已改造完成）

- 项目原本 84 个文件直接 `import PyQt6`（`PyQt6.QtCore/QtWidgets/QtGui`）。
- 麒麟软件源、conda-forge（已核实全部 1064 个 linux-64 构建）、qgis.org 官方 Linux 包，**全部是 PyQt5 构建的 QGIS**；没有现成的 PyQt6 版 QGIS Linux 包。
- 同一进程里：QGIS 绑定（PyQt5）+ 你的控件（PyQt6）= 两个 Qt 实例，直接崩溃。

### 已完成的解法：改为 QGIS 官方兼容层 `qgis.PyQt`

QGIS 3.28+ 提供 `qgis.PyQt`：它会自动映射到 QGIS 编译时用的绑定（PyQt5 或 PyQt6），
并且**提供 PyQt6 风格的 API**（作用域枚举 `Qt.MouseButton.LeftButton`、`exec()` 等）。
本项目本来就按 PyQt6 风格写的，改完后 Windows 的 QGIS 4.2（PyQt6）和麒麟的 QGIS（PyQt5）**同一套代码通吃**。

本次改造内容（2026-08，dev 分支）：

- `app/` 与 `tests/` 共 115 个文件：`from PyQt6.X import Y` → `from qgis.PyQt.X import Y`（含 `pyqtSignal`、`QVariant`、`QMetaType`）；测试里的 `pytest.importorskip("PyQt6")` → `importorskip("qgis.PyQt")`，`from PyQt6 import sip` → `from qgis.PyQt import sip`。
- 验证：QGIS 4.2（PyQt6）环境下 181 个模块全量 import 通过；pytest 全量通过（除 2 个 pytest-qt 4.5 兼容性失败 + 1 个 gdal_create 偶发，均为改造前既有问题）。
- **PyQt5 兼容性实测（QGIS 3.40 + PyQt5 5.15 环境，2026-08）**：全量回归除 3 个既有问题外全部通过，并顺带修掉 3 个 PyQt5/QGIS3.40 特有差异：
  1. `flow_editor/node_item.py`：PyQt5 下 `itemChange` 收到 `ItemSceneHasChanged` 的 value 是 `PyQt_PyObject` 型 QVariant（内部 Python 对象已被消费），`super().itemChange()` 透传会 `TypeError: unable to convert a QVariant back to a Python object`（删节点/删除选中即崩）→ 统一 `isinstance(value, QVariant)` 后 try/except 解包，失败置 None 安全透传。
  2. `tools/merge_geometry.py`：`QgsGeometry.concaveHullOfPolygons` 是 QGIS 3.42+ API，3.40 没有 → `hasattr` 守卫 + `convexHull()` 兜底（测试按版本分支断言：凹包 9 顶点 / 凸包 5 顶点）。
  3. `docks/tools_dock.py`：QGIS 3.40 的 processing 缺 `gdal:createcog`、`gdal:dataset_identify`，点开 `GenericAlgDialog` 因 `algorithmById` 为 None 崩溃 → `_build_tree` 按 `QgsApplication.processingRegistry()` 过滤缺失算法；同时 `tests/ui/conftest.py` 的 `qgis_app` fixture 统一 `Processing.initialize()`（消除各测试对执行顺序的隐性依赖）。
  4. `bootstrap/__init__.py`：`QLibraryInfo.path()` 是 Qt6 API，PyQt5 用 `location()` → try/except 双写（改造阶段已做）。
- pyproject.toml 保留 `PyQt6>=6.7` 声明（便于 pip 解析），运行时不直接 import。

> 其他分支/仓库如需复现替换：`grep -rl "PyQt6" app/ --include="*.py" | xargs sed -i -e 's/from PyQt6\./from qgis.PyQt./g' -e 's/^import PyQt6\$/import qgis.PyQt/g' -e 's/^import PyQt6\./import qgis.PyQt./g'`

---

## 2. 路线总览

```
[打包机：麒麟 V10（或同底座 Ubuntu 容器）]
   conda env: python3.10 + qgis(PyQt5) + pyinstaller
        │
        │ bash tools/build_linux.sh
        ▼
   dist/app/  ← 绿色便携包（免装 QGIS，含 .so/Qt 插件/数据）
        │
        ├─ bash tools/kylin/build_deb.sh ──► dist/qgis-dev-app_*.deb ──► 目标机 dpkg -i
        └─ bash tools/kylin/install.sh     ──► 直接部署到 /opt/qgis_app
```

---

## 3. 打包机准备（一次性）

> **最省事做法：直接在目标型号的麒麟 V10 机器上打包**（打包机 = 目标机，底座/架构天然一致，
> 无需纠结 glibc 兼容）。步骤如下：

```bash
# 1) 把代码拷到麒麟打包机（源码即可，不含 dist/build）
#    git clone <仓库地址>  或  scp -r qgis_dev user@kylin:~/   （scp 记得先删掉 dist/ build/ .git）

# 2) 装 miniconda（版本与目标架构一致：x86_64 或 aarch64）
#    下载：https://repo.anaconda.com/miniconda/  （选 Miniconda3-latest-Linux-x86_64.sh 或 -aarch64.sh）
bash Miniconda3-latest-Linux-x86_64.sh -b -p ~/miniconda3
~/miniconda3/bin/conda init bash && exec bash

# 3) conda 25.x 起默认渠道强制要求接受 ToS，否则 conda create 报 CondaToSNonInteractiveError：
conda tos accept --override-channels --channel https://repo.anaconda.com/pkgs/main
conda tos accept --override-channels --channel https://repo.anaconda.com/pkgs/r

# 4) 建打包环境：python3.10 + QGIS（PyQt5）+ PyInstaller
#    GIS/服务依赖全部走 conda-forge（qgis/gdal 同源，避免 pip 版 gdal 覆盖 osgeo 造成 ABI 冲突）
conda create -n qgis_build -c conda-forge python=3.10 qgis pyqt pyinstaller \
    gdal rasterio networkx scipy fastapi uvicorn pydantic pyyaml psycopg2 -y
conda activate qgis_build

# 5) 装项目本身（--no-deps：绝不碰 conda 已装的 qgis/gdal/osgeo）
cd ~/qgis_dev
pip install -e . --no-deps --no-build-isolation

# 6) 验证
python -c "import qgis, PyQt5, qgis.PyQt; print('qgis', qgis.__version__)"
python -c "import osgeo.gdal; print('gdal', osgeo.gdal.VersionInfo())"
```

> - **版本对齐**：目标机是 SP1/SP2（Ubuntu 20.04 底座）就在 20.04 底座上打包；SP3（22.04 底座）用
>   22.04 底座。真机打包天然满足「底座 ≤ 目标机」。若在旧底座打包给新底座用没问题（glibc 2.31 产物
>   在 2.35 上能跑）；反过来不行。
> - **架构**：飞腾/鲲鹏（aarch64）目标 → 在 aarch64 麒麟机上打包（conda-forge 有 aarch64 的 qgis，
>   build_linux.sh 会自动走 /usr/lib/aarch64-linux-gnu）。
> - **网络受限**：打包机要能访问 conda-forge；不能就找台有网的同架构机器打，产物（.deb）拷过去装。

---

## 4. 打包（三步出 .deb）

```bash
# ① 打便携包（PyInstaller + 收集 QGIS 运行时，产物 dist/app/）
conda activate qgis_build
cd ~/qgis_dev
bash tools/build_linux.sh
#    产物自检：有显示器的麒麟桌面直接跑 dist/app/launch_qgis.sh，应能出登录窗

# ② 打成 .deb（桌面入口 + 图标 + postinst 全自动）
bash tools/kylin/build_deb.sh                    # 自动取版本号/架构
bash tools/kylin/build_deb.sh 1.0.0 arm64        # 指定版本、飞腾/鲲鹏架构

# ③ 安装验证（同一台机器上直接验，或拷到目标机）
# 注意：麒麟 V10 的 dpkg 会拒绝无签名的 deb（见第 7 节），用 install_deb.sh 一键安装
sudo bash tools/kylin/install_deb.sh dist/qgis-dev-app_*.deb
#     开始菜单 / 桌面搜索「图斑核实平台」启动；
#     终端排障：/opt/qgis_app/launch_qgis.sh
```

免 .deb 的内网快速部署：

```bash
bash tools/kylin/install.sh       # 在目标机上跑（把便携包拷过去后）
```

> **备用：没有麒麟真机时**，可用同底座 Ubuntu 容器模拟（见 `tools/kylin/Dockerfile` +
> `build_in_docker.sh`，底座 Ubuntu 20.04 = 麒麟 SP1/SP2）。容器方案产物与真机一致，
> 但 aarch64 目标需要 ARM 机器/模拟，且无法验证麒麟桌面集成（菜单/图标）。
> 能用真机就不用容器。

---

## 5. 打包机到目标机的兼容性清单

| 项 | 要求 | 备注 |
|---|---|---|
| glibc | 打包机底座 ≤ 目标机 | SP1(2.31) 产物可在 SP1/SP2/SP3 跑；SP3(2.35) 产物不能跑 SP1 |
| 架构 | 完全一致 | x86_64 / aarch64 各自打包 |
| 基础库 | X11 / GL / fontconfig / libstdc++ | 麒麟桌面自带，deb 已声明 Depends |
| 磁盘 | 目标机 ≥ 5GB | 便携包约 1~2GB，解包后 /opt/qgis_app 更大 |
| 字体 | 中文显示 | 建议 `apt install fonts-noto-cjk`（deb Recommends 已带） |

---

## 6. 常见问题

**Q: 双击图标没反应 / 黑屏**
```bash
# 终端跑一下看报错
/opt/qgis_app/launch_qgis.sh
# 常见：xcb 平台插件缺失 → 检查 dist/app/platforms/libqxcb.so 是否存在；
#       DISPLAY 未设置（远程）→ export DISPLAY=:0 或 X11 转发
```

**Q: 报 `Could not load source layer ... proj.db` / 坐标转换失败**
确认 `dist/app/proj.db` 存在（build_linux.sh STAGE 3 第 6 步复制）。

**Q: 报 `Processing plugin has not been loaded`**
确认 `dist/app/plugins/processing` 存在（build_linux.sh 第 3 步）。

**Q: 麒麟 SP1（Ubuntu 20.04）上 Python 版本不够**
SP1 系统 python3 是 3.8，本项目要求 ≥3.10 → 用 conda 的 python3.10，不要用系统 python。

**Q: 目标机要连数据库/服务**
注意麒麟的防火墙和 selinux（麒麟默认未强制）；数据库驱动 psycopg2 已在 PyInstaller hiddenimports 里。

**Q: 以后代码更新怎么重打**
三步：`conda activate qgis_build` → `bash tools/build_linux.sh` → `bash tools/kylin/build_deb.sh 新版本号`。

---

## 7. 麒麟 V10 的 dpkg 签名校验（装 .deb 前必读）

麒麟 V10 SP1/SP2（Ubuntu 20.04 底座）的 dpkg 打了国产化安全补丁（`libkylin_bwlist` / `libkylin_signtool`）：

- kylin 官方仓库的 deb 带有 `_kylin.crt`（证书）+ `_kylin.sig`（签名）两个 ar 成员；
- **没有签名的 deb 会被 `/etc/dpkg/udeblist.xml`（bw_status="2"，uid 黑白名单）拦截**，报：
  `dpkg: 软件包 xxx 验证失败，拒绝安装`（英文：verification failed, refuse to install!）；
- 给 deb 签名需要 UKey 硬件 + CA 私钥（kylinsigntool 无 UKey 报“Ukey设备登录失败”），普通开发者不可行；
- 已实测确认：`dpkg -i`、`dpkg --force-all`、`--force-bad-verify`、`--allow-kylinsign`、`apt install` 全部无效，**只能关闭黑白名单校验**：
  ```bash
  sudo cp /etc/dpkg/udeblist.xml /etc/dpkg/udeblist.xml.bak
  sudo sed -i 's/bw_status="2"/bw_status="0"/' /etc/dpkg/udeblist.xml
  sudo dpkg -i qgis-dev-app_*.deb
  ```
  改 0 后无签名 deb 即可正常安装（本项目已实测通过，含迷你空包对照实验）。
  **`tools/kylin/install_deb.sh` 已封装上述步骤**（自动备份 + 改配置 + 安装），优先用它。
- 注意：此改动关闭的是 dpkg 的 uid 黑白名单加固（防恶意 deb 提权），属系统安全配置；
  我们的包全部 root 所有且来源可信，风险可接受，但部署到涉密/等保机器前请与安全管理员确认。
- SP3（Ubuntu 22.04 底座）是否有此校验未实测，如遇同样报错按上述步骤处理。

---

## 8. 相关文件

```
tools/build_linux.sh            便携包构建（QGIS 探测 / .so 收集 / Qt 插件 / GDAL 数据）
tools/pyinstaller.spec          PyInstaller spec（Win/Linux 平台分支）
tools/runtime/launch_qgis.sh    便携包启动器（LD_LIBRARY_PATH / QGIS_PREFIX / 日志目录）
tools/kylin/build_deb.sh        .deb 打包（/opt/qgis_app + 桌面入口 + 图标）
tools/kylin/install_deb.sh      麒麟一键安装（关黑白名单校验 + dpkg -i）
tools/kylin/install.sh          免 deb 直接部署
tools/kylin/qgis_dev.desktop    桌面入口模板（Exec=/opt/qgis_app/launch_qgis.sh）
tools/kylin/README.md           本指南
```
