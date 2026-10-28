# 自动检测更新（客户端 + 服务端）

本文是 `qgis_dev` 自动更新的完整说明：客户端检查 / 下载 / 安装、清单签名、
服务端发布流程，以及部署与排障。方案决策见
`.claude/archive/2026-10-22_检测更新方案.md`。

## 1. 总体流程

```
客户端启动
  └─ 登录成功、主窗口显示、写 .startup-ok
       └─ UpdateController.start()
            ├─ 启动后 10s 检查一次（update.startup_delay_ms）
            └─ 每 4 小时检查一次（update.check_interval_hours）

检查：GET /api/app/manifest?appId&channel&versionCode&runtimeId&os&arch
  └─ 响应 data = { manifest(base64), signature(base64), keyId, ... }
       └─ 验签（内置公钥）→ 解析 → 版本 / 平台匹配 → 防回退（publishSeq）

有新版本
  ├─ 自动安装可用：后台下载（Range 续传 + SHA-256）→ 启动独立更新器 → 主程序退出
  └─ 自动安装不可用：打开下载页 / 打开已下载文件所在文件夹
```

手动入口：主窗口「帮助 → 检查更新」。手动检查**总是**给出结果（已是最新 /
没有匹配安装包 / 检查失败），不受“跳过此版本”影响。

## 2. 客户端配置

`config/app.yaml`：

```yaml
update:
  enabled: true
  base_url: ""             # 留空 = services.feedback_url
  channel: "internal"      # internal 允许未签名安装包；stable 客户端 fail closed
  check_on_startup: true
  startup_delay_ms: 10000  # 主窗口显示后再等 10s，避开启动高峰
  check_interval_hours: 4
  request_timeout_seconds: 20
```

- `base_url` 必须指向服务端 **context path**（例：
  `http://182.108.40.116:9003/cs-feedback`），末尾斜杠可省。
- `channel` 留空时使用构建戳 `app/__init__.py` 的 `CHANNEL`。
- 客户端对 `stable` 通道直接报错：当前没有 Windows 代码签名证书，stable 只
  允许签名安装包；在有证书之前不要切到 stable。

## 3. 清单与版本规则

服务端生成的清单（签名字节）结构：

```json
{
  "schema": 1,
  "appId": "qgis-desktop",
  "channel": "internal",
  "publishSeq": 7,
  "version": "1.0.1",
  "versionCode": 10001,
  "releaseId": 12,
  "generatedAt": "2026-10-22T...",
  "releaseNotes": "本次更新内容",
  "mandatory": false,
  "minVersionCode": 0,
  "packages": [
    {
      "id": 3,
      "kind": "full",
      "fileName": "qgis-dev-app-1.0.1-linux-x86_64.tar.gz",
      "url": "api/app/download/3",
      "sizeBytes": 123456789,
      "sha256": "…",
      "runtimeId": "linux-x86_64-cp312-qgis3.40",
      "os": "linux",
      "arch": "x86_64"
    }
  ]
}
```

- `versionCode = major*10000 + minor*100 + patch`，**必须**与 `__version__`
  同步递增（`tools/version.py` 会校验）。客户端只安装 `versionCode` 更大的版本。
- `publishSeq` 由服务端单调分配；客户端记住见过的最大序号，收到更小值会拒绝，
  防止回放旧清单 / 降级。
- `mandatory` 或 `当前 versionCode < minVersionCode` 时只影响**提示频率**：
  没有“跳过此版本”，每次启动 / 周期检查都会再弹，但永远不阻断使用。
- 包匹配：`runtimeId` 精确匹配优先，其次 `os` / `arch`；留空表示通配。
  同分时取 `id` 最大的包。`kind=full` 优先于 `code`。
- 安装包 URL 支持相对路径（相对 `base_url`）或绝对 `http(s)` 地址。

## 4. 签名与密钥

- 算法：RSA-3072 PKCS#1 v1.5 + SHA-256（`SHA256withRSA`）。
- 签名对象：清单 JSON 的**原始 UTF-8 字节**（`manifest` base64 解码后的内容），
  客户端和服务端都不重新序列化，避免键序 / 空格差异导致验签失败。
- `keyId = SHA-256(SubjectPublicKeyInfo DER) 前 16 位十六进制`，当前为
  `cbac7533ef6de3b5`。
- 私钥只存在发布机的 `.keys/update-private.pem`（`.gitignore` 已排除），
  **不得**提交、上传服务器、打进安装包或写日志。服务端只保存公钥。

三处公钥必须一致（轮换密钥时同时更新）：

| 位置 | 用途 |
| --- | --- |
| `app/update/keys.py` | 客户端内置公钥 |
| `tools/linux/qgis-dev-updater` 内 `PUBLIC_KEY_PEM` | Linux root 更新器独立验签 |
| `problem-feedback/src/main/resources/update/update_public_key.pem` | 服务端发布时验签 |

签名工具：

```bash
# 查看私钥 keyId / 位数（确认与服务端一致）
python tools/update_signing.py key-info --private-key .keys/update-private.pem

# 离线签名（payload 从管理页 / update_release.py prepare 下载）
python tools/update_signing.py sign \
    --payload signed-payload-1.0.1-7.json \
    --private-key .keys/update-private.pem \
    --out signed-1.0.1-7.json

# 发布前自检
python tools/update_signing.py verify --signed signed-1.0.1-7.json
```

## 5. 发布流程

`tools/update_release.py` 负责打包和在线步骤，签名仍走离线工具。

### 5.1 改版本、构建

版本唯一事实源是 `config/app.yaml`：

```yaml
app:
  version: "1.0.1"   # 改版本只改这一行
```

`app/__init__.py` 在导入期读它并派生 `VERSION_CODE = 10001`
（major*10000 + minor*100 + patch）。Windows 安装器升级时按设计保留用户
`config/app.yaml`，所以启动时若发现安装包根目录 `build_info.json` 的版本更新，
会以它为准并把 yaml 里的版本行回写。

然后构建：

```powershell
powershell -ExecutionPolicy Bypass -File tools\build_windows.ps1
```

```bash
bash tools/build_linux.sh
```

产物根 `dist/app/build_info.json` 必须包含本次版本、`versionCode`、`runtimeId`
和 `platform`。服务端按 `runtimeId` 匹配包，不匹配的客户端会收到“没有匹配的安装包”。

### 5.2 打更新包

Linux full 包是 tar.gz，压缩包**根目录直接是 `dist/app` 的内容**（更新器
把它整体换入 `/opt/qgis_app`）：

```bash
python tools/update_release.py package --dist dist/app --out dist/releases
# 产物：dist/releases/qgis-dev-app-1.0.1-linux-x86_64.tar.gz
#       dist/releases/qgis-dev-app-1.0.1-linux-x86_64.tar.gz.json（上传描述）
```

Windows full 包是 Inno Setup 安装器（`tools/build_windows.ps1` 第 5 步生成）：

```powershell
python tools/update_release.py package ^
    --file "dist/图斑核实平台_V1.0.1_Setup.exe" ^
    --dist dist/app --out dist/releases
```

`package` 会算 SHA-256 / 大小，并写同名 `.json` 描述（含 `runtimeId`、`os`、
`arch`、`versionCode`）。上传前会再次校验文件未被改动。

### 5.3 建发布单、上传、生成待签清单

```bash
export APP_RELEASE_ADMIN_TOKEN='…'   # 生产环境必须设置；命令行参数会进 shell 历史

python tools/update_release.py create \
    --base-url http://182.108.40.116:9003/cs-feedback \
    --channel internal --notes-file notes.md
# 输出 releaseId，例如 12

python tools/update_release.py upload \
    --base-url http://182.108.40.116:9003/cs-feedback \
    --release-id 12 \
    --descriptor dist/releases/qgis-dev-app-1.0.1-linux-x86_64.tar.gz.json

python tools/update_release.py prepare \
    --base-url http://182.108.40.116:9003/cs-feedback \
    --release-id 12 --out dist/releases/payload-1.0.1-7.json
```

`upload` 按 8MB 分片（服务端上限 16MB），支持大文件；`complete` 时服务端会
重新计算并核对 SHA-256。**只有草稿状态**能上传；已生成清单后要改包，先
`reset-signature`（管理页按钮）。

### 5.4 离线签名、发布

把 `payload-*.json` 拷到离线机器执行第 4 节的 `sign`，再把 `signed-*.json`
拿回发布机：

```bash
python tools/update_release.py publish \
    --base-url http://182.108.40.116:9003/cs-feedback \
    --release-id 12 --signed signed-1.0.1-7.json
```

也可以全程用管理页 `http://<host>:9003/cs-feedback/admin/releases` 操作
（创建 / 上传 / 生成清单 / 粘贴签名 / 发布 / 下线）。

同一 `appId + channel` 只保留一个已发布版本；发布新版本会自动下线旧版本。

## 6. 自动安装与回滚

### 6.1 客户端下载

- 下载到每用户更新目录的 `downloads/`，`.part` 分片可断点续传（HTTP Range）。
- 完成后校验大小与 SHA-256，通过才原子改名；失败删除 `.part` 并提示。
- 已下载成功的包记入 `state.json` 的 `pending`；下次启动**优先提示离线安装**，
  不再重复下载。安装失败回滚到旧版本后，旧版本仍能看到“已下载待安装”。

更新状态目录：

| 平台 | 路径 |
| --- | --- |
| Windows | `%LOCALAPPDATA%\qgis_dev\updates\<install-key>\` |
| Linux（安装版） | `${XDG_DATA_HOME:-~/.local/share}/qgis_dev/updates/` |

`QGIS_DEV_UPDATE_DIR` 可整体覆盖（测试 / 运维排查），此时不再追加 install-key。
`install-key` 是安装目录路径的短哈希，同一路径升级后保持不变，一套机器上的
两个 Windows 副本互不干扰。Linux 不追加 install-key：`launch_qgis.sh` 与
提权更新器都用同一个每用户目录，否则直接运行 `qgis_dev` 和通过启动脚本运行
会写入两套健康标记，更新器永远等不到。

### 6.2 Windows

前置条件：冻结态（PyInstaller 产物）+ 安装包后缀 `.exe` / `.msi` +
**Inno 安装版**（安装目录含 `unins*.exe`）。便携目录运行时程序会退化为
手工安装提示——静默安装只会装到 `%LOCALAPPDATA%`，更新不到当前目录。

`app/update/updater_scripts.py` 内嵌 PowerShell 更新器，主程序只写任务 JSON
并以 `CREATE_NO_WINDOW` 启动它（**不能**用 `DETACHED_PROCESS`：实测该标志下
powershell 会立即退出，脚本一行都不执行），然后等更新器写出**启动心跳**
（`<更新状态目录>/updater/.updater-started`，含 pid）再正常关闭主程序。
8 秒内没有心跳 → 主程序不退出，弹窗提示可能被安全软件拦截并给出日志路径；
这样不会出现“程序退出了、更新却没发生、用户看不到任何东西”的情况。

更新器进程启动后自己弹一个 WinForms 进度窗口（无控制台），分阶段显示进度、
用时和失败原因，长操作（robocopy / 安装器 / 等健康标记）期间持续 `DoEvents`：

1. 校验安装包 SHA-256；
2. 等旧进程 PID 退出（单实例互斥体同名），窗口提示“程序关闭后自动继续”；
   等待超时（600 秒）只关闭窗口、不改任何文件；
3. `robocopy` 整目录备份到更新状态目录的 `backup/`（失败即中止，宁可不升级）；
4. 静默运行 Inno（`/VERYSILENT /SUPPRESSMSGBOXES /NORESTART /CLOSEAPPLICATIONS`）
   或 `msiexec /qn`，退出码 0 / 3010 视为成功；
5. 校验安装后 `build_info.json` 的 `versionCode`；
6. 重启 `app.exe`，等 `.startup-ok`；
7. 新进程已退出且无健康标记（含启动后没写标记就崩溃、被 `Start-Process`
   拿到的 PID 已消失）→ 杀残留进程、恢复备份、拉起旧版本；
   超时但进程仍活着（例如停在登录框）→ **不回滚**，保留新版本。

窗口行为：成功 10 秒后自动关闭；失败保留，显示原因并提供“打开日志文件夹”和
“关闭”按钮，回滚完成后才退出。日志路径通过命令行 `-LogFile` 传入，任务 JSON
解析失败也会留下记录。

安装器 `tools/installer/installer.iss` 必须保持 `AppMutex` 与
`app/__init__.py` 的 `APP_MUTEX` 一致；`config/app.yaml` 用
`onlyifdoesntexist` 安装，升级不会覆盖用户配置。

日志：`<更新状态目录>/updater/updater.log`、`installer.log`。

### 6.3 银河麒麟 / Linux

前置条件：`.deb` 安装（程序位于 `/opt/qgis_app`），且
`/usr/local/lib/qgis_dev/qgis-dev-updater` 存在（由 `tools/kylin/build_deb.sh`
安装为 root:root 0755）、系统有 `pkexec`、安装包为 `.tar.gz` / `.tgz`。

提权边界：任务 JSON 在普通用户可写目录，**不能作为信任来源**。root 更新器：

1. 用内置公钥重新验签清单，校验 `appId=qgis-desktop`、`channel=internal`；
2. 校验安装包 SHA-256 与清单中 `packageId` 对应包一致；
3. 安装目录固定 `/opt/qgis_app`，忽略任务里的 `appDir` / `exePath`；
4. tar.gz 只接受普通文件 / 目录，拒绝绝对路径、`..`、符号链接、设备文件；
5. 解压后递归 `chown root:root`，清除 setuid/setgid；
6. 备份当前目录 → 换入新树 → 校验 `build_info.json` 版本 → 以 `PKEXEC_UID`
   对应用户身份重启 → 等 `.startup-ok`；
7. 新进程退出且无标记（启动即崩）→ 恢复备份并拉起旧版本；进程仍活着但
   未写标记（停在登录框 / 启动慢）→ 超时后保留新版本。

日志：`/var/log/qgis_dev-updater.log`（root 可写）。`pkexec` 会弹系统认证框，
用户取消则不会安装。

自动更新只替换 `/opt/qgis_app`，**不会更新** `/usr/local/lib/qgis_dev/qgis-dev-updater`
和桌面入口。提权更新器本身有安全修复时，必须重新发布 `.deb`（或让用户
`sudo dpkg -i` 覆盖安装），不能只依赖应用内更新。

安装进度（helper `HELPER_VERSION >= 2`）：

- 客户端在任务 JSON 里带 `statusFile`（固定名 `install-status.json`，位于用户的
  `updates/updater/` 目录）。helper 校验该目录属于 `PKEXEC_UID` 对应用户后
  打开目录 fd，之后**相对 fd** 原子写入 JSON（临时文件 + `os.replace`）：
  `{schema:1, jobId, version, phase, message, done, total, percent, ts, pid}`。
  用目录 fd 而不是路径字符串，防止调用者在校验后把父目录换成指向 `/etc` 的
  符号链接，让 root 写到任意目录。
- 阶段：`starting → verify → extract → wait_for_exit → backup → swap → launch
  → health → done`，任一步失败写 `error`。`extract` 按 tar 成员数报百分比。
- 客户端每 300ms 轮询：`starting/verify/extract` 期间保持主程序运行并显示真实
  进度；看到 `wait_for_exit`（或之后阶段）才关闭程序。helper 中途死掉 → 报失败
  并允许重试；helper 活着但 60 秒没有任何进度（进度目录不可写等）→ 退回旧流程
  先退出程序，helper 的 `wait_process_exit` 立即满足，安装照常完成。用户在
  polkit 授权框上输密码的时间不计入这 60 秒（`/proc/<pid>/comm` 仍是 `pkexec`
  表示 helper 尚未启动）；退回旧流程后仍继续轮询，窗口关闭被“未保存工程”拦住
  时，迟到的进度或 helper 失败仍会反映到对话框。
- 兼容：旧 helper（无 `HELPER_VERSION` 或 < 2）→ 客户端自动退回“启动后 600ms
  退出”的旧行为；旧客户端不传 `statusFile` → helper 只写日志，安装不受影响。
- **一次性要求**：自动更新不替换 helper，因此 1.0.8 及更早的机器必须先安装一次
  新 `.deb`（1.0.9+），后续自动更新才会显示进度窗口。
- 复测进度窗口（机器和服务端都已是同一版本、没有可更新版本时）：把
  `QGIS_DEV_BUILD_INFO` 指向一份 `{"version":"1.0.8","versionCode":10008}` 的
  JSON 再启动程序，客户端会把自己当成旧版本并看到服务端的更新；更新完成后
  磁盘上是真实版本，测试文件即可删除。

便携版 / 源码运行 / 非 `/opt/qgis_app` 的安装不会自动安装，UI 退化为
“打开下载页 / 打开所在文件夹”。

### 6.4 健康标记

- `.process-started`：`bootstrap.main()` 入口即写（含 PID / 版本）；
- `.startup-ok`：主窗口 `show()` 之后、模态启动回调之前写。

这两个标记是“更新成功”的唯一判据。**不要**把 `.startup-ok` 写到登录框之前：
新版本可能停在登录界面，更新器会误判启动失败并回滚；也不要写到启动回调之后：
“打开地图文档”是模态的，会阻塞到用户操作，更新器等不到标记同样会超时。

## 7. 服务端部署

项目：`E:\code\problem-feedback`（Spring Boot 2.7 / JDK 8 / PostgreSQL schema `cs`，
context path `/cs-feedback`）。

1. 执行建表脚本（**只执行一次**）：
   `sql/app_update.sql`。当前开发库尚未执行，需 DBA / 有权限的人手动跑。
2. 配置环境变量：

   | 变量 | 说明 |
   | --- | --- |
   | `APP_RELEASE_STORAGE` | 安装包存储目录（默认 `${file-path}/app-releases`），需可写且容量足够 |
   | `APP_RELEASE_ADMIN_TOKEN` | 管理接口令牌；为空则管理接口无鉴权（仅限本地开发） |
   | `APP_RELEASE_PUBLIC_KEY` | 清单验签公钥路径；默认 `classpath:update/update_public_key.pem` |

3. 管理页：`http://<host>:9003/cs-feedback/admin/releases`（导航栏「版本发布」）。
4. 客户端接口：
   - `GET /api/app/manifest?...`（无需登录）
   - `GET /api/app/download/{packageId}`（支持 Range，无需登录）

## 8. 安全边界

- **没有 HTTPS**：管理令牌、清单响应、下载内容都以明文经过网络。清单和安装包
  有签名 / SHA-256 保护，篡改会被客户端拒绝；但管理令牌可被同网段抓包。
  上线前必须加 VPN / IP 白名单，尽快配 nginx TLS。
- **没有 Windows 代码签名证书**：安装包首次运行会被 SmartScreen 拦截，只能走
  `internal` 通道；`stable` 客户端 fail closed。`internal` 也不是“不安全”：
  客户端仍会验签清单并校验包 SHA-256。
- 私钥离线保管；服务端只有公钥。Linux root 更新器也内置公钥，不读用户任务里的
  公钥 / 签名密钥。
- 管理接口令牌为空时服务端启动会打 warning；线上必须设置。
- `.deb` 未做 CA / UKey 签名，麒麟 dpkg 会提示未签名（现有安装脚本会关闭
  udeblist 校验），这不是自动更新链路引入的问题。

## 9. 排障

| 现象 | 排查 |
| --- | --- |
| 手动检查提示“服务端还没有发布可用版本” | 服务端没有 published 记录；确认 `appId`、`channel` |
| “发现新版本，但当前平台没有匹配的安装包” | 清单里的 `runtimeId` / `os` / `arch` 与客户端不一致；看 `logs/app.log` 里的 `runtimeId` |
| 验签失败 / 未知签名密钥 | 服务端公钥、客户端 `keys.py`、Linux 更新器内置公钥三者不一致；或清单被重新序列化 |
| “发布序号回退” | 客户端见过更大的 `publishSeq`；重新生成清单会分配新序号，不要手工改 state.json |
| 下载总是从头开始 | 服务端不支持 Range 或 `.part` 被清理；确认网关没有缓冲 / 改写响应 |
| Windows 更新后没变化 | 看 `<状态目录>/updater/updater.log`；安装器是否静默参数成功、`AppMutex` 是否一致 |
| Linux 点击更新没反应 | `pkexec` 是否可用、`/usr/local/lib/qgis_dev/qgis-dev-updater` 是否存在且 root:root 0755；`/var/log/qgis_dev-updater.log` |
| 新版本被回滚 | 看更新器日志里的健康标记等待结果；确认 `.startup-ok` 在主窗口显示后写出 |
| 想重置本地更新状态 | 关闭程序，删除 `<状态目录>/state.json` 和 `downloads/`（或设置 `QGIS_DEV_UPDATE_DIR` 指到空目录） |

## 10. 测试

```bash
python -m pytest tests/update tests/bootstrap -q
```

覆盖：RSA 签名 / 验签、清单解析与包选择、状态损坏恢复与越界清理、验签与
防重放、断点续传 / 校验失败 / 取消下载、跳过与强制提示、离线待安装、发布包
tar.gz 结构与描述校验、启动健康标记顺序。
