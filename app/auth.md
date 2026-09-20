# 登录鉴权模块

> **每次启动都要登录**：用户名密码提交到**授权服务器**换取 token，
> token 只存在于本次运行的内存里，**不落盘**（没有"记住我 / 30 天免登录"）。
>
> 登录通过后再用 token 拉一次**当前用户信息**（姓名 / 单位 / 部门 / 上次登录，
> `currentUserByToken`）供主界面标题栏显示（见 §2.2.1）——拉不到不拦登录。
>
> 服务器地址**按环境区分**：环境名取 `config/app.yaml` 的 `env:`，每个环境的
> 地址 / 接口写在 `app/core/auth/config.py` 的 `PROFILES`（见 §2.1）。
>
> **"记住账号"只影响登录框的输入体验**：登录成功后可以把本次用户名 + 密码留在
> 本机（Windows 用 DPAPI、Linux / 麒麟用机器绑定的本地密钥，都是加密，见 §4.1），
> 下次点用户名输入框弹气泡一键回填（见 §4.1）；
> 它**不是免登录**，每次仍然要连服务器换 token。

---

## 1. 模块清单

| 文件 | 作用 |
|---|---|
| `app/ui/dialogs/login.py` | `LoginDialog`：启动登录框，**后台线程**调授权服务器（PyQt6-only） |
| `app/core/auth/config.py` | **授权服务器配置**：`PROFILES`（按 `app.yaml` 的 `env:` 选段）+ 请求响应字段名 + `DEFAULT_*` 兜底 + `QGIS_DEV_AUTH_*` 环境变量覆盖 |
| `app/core/auth/client.py` | `AuthClient`：**只负责登录**（标准库 `urllib`）；`AuthResult` |
| `app/core/auth/current_user.py` | `CurrentUser` + `fetch_current_user()`：登录后拉当前用户信息（**走 `service_client`**，见 §2.2.1） |
| `app/core/auth/errors.py` | `AuthError`（单独放，免得 `client` / `current_user` 互引） |
| `app/core/auth/_payload.py` | 响应 JSON 取字段的通用小工具（登录与当前用户共用） |
| `app/core/auth/saved_logins.py` | **记住的账号**：本机保存「用户名 + 密码」，供登录框气泡回填（见 §4.1） |
| `app/core/auth/_secret.py` | 密码保护**统一入口**：Windows 走 DPAPI，Linux / macOS 走机器绑定的本地密钥文件 |
| `app/core/auth/_user_paths.py` | 用户级配置目录（账号文件与密钥文件共用） |
| `app/core/auth/_dpapi.py` | Windows DPAPI（`CryptProtectData`）加解密，被 ^ 的 `_secret` 调用 |
| `app/ui/dialogs/saved_login_popup.py` | `SavedLoginPopup`：点用户名输入框弹出的"已保存账号"气泡 |
| `app/core/auth/__init__.py` | 模块导出 |
| `app/bootstrap/__init__.py` | 启动流程：`QgsApplication` 之后弹登录 → 拉当前用户信息 → 把 `AuthResult` 传给主窗口 |
| `app/ui/main_window/title_bar.py` | 标题栏用户区：按钮显示姓名，菜单里是账号 / 单位 / 部门 / 上次登录 / 会话有效期 |
| `app/core/util/service_client.py` | **业务接口请求工具**：`request(路径, 参数, 方式)` 自动按环境取 `ip:port` + 自动带登录 token（`unitkey: Bearer <token>`），**除登录以外的业务服务器访问都走它**（见 §2.2.2） |

> 已删除的历史实现：`app/core/auth/credential.py`（本地 HMAC 凭证 + 30 天免登录）。
> 需要时可在 git 历史里找回。

---

## 2. 授权服务器

### 2.1 改哪里（按环境选服务器）

服务器地址**按环境区分**：环境名来自主配置 `config/app.yaml` 的 `env:`，每个环境
用哪台机、哪个接口写在 `app/core/auth/config.py` 的 `PROFILES`。

```yaml
# config/app.yaml
env: dev            # ← 选哪一段：dev / prod / 自加的名字
```

```python
# app/core/auth/config.py
PROFILES = {
    "dev": {},                     # 空 dict ＝ 这几个字段全走 DEFAULT_*（＝开发环境的值）
    "prod": {                      # 只写要覆盖的字段
        "scheme": "http",
        "ip": "59.55.120.111",     # TODO 生产环境地址（当前先与 dev 相同）
        "port": 7008,
        "login_path": "/forestry-api/system/unit/unified",
    },
}
```

环境段里可用的键：`scheme` / `ip` / `port` / `login_path` / `timeout` /
`verify_ssl` / `password_encoding`（请求体、响应体的字段契约不分环境，见同文件
下方的 `DEFAULT_*` 常量）。

**加一个环境（如 uat）**：在 `PROFILES` 里加一段 → 把 `app.yaml` 的 `env:` 换成
`uat` → 重启生效，业务代码一行不动。环境名没在 `PROFILES` 里配（`env: staging`
却没加那段）→ 记一条 warning 后按 `DEFAULT_*`（＝dev）走，不会崩。

启动日志会打印实际打到了哪台机，出问题先看这一行：

```text
INFO app.bootstrap: 授权服务器（环境 dev）：http://59.55.120.111:7008/forestry-api/system/unit/unified（超时 10s，密码 md5）
```

**覆盖优先级（高 → 低）**：`QGIS_DEV_AUTH_*` 环境变量 → `PROFILES` 里当前环境
那一段 → `DEFAULT_*` 常量。不改代码临时换机 / 部署注入用下表的变量：

| 环境变量 | 覆盖项 |
|---|---|
| `QGIS_DEV_AUTH_BASE_URL` | 完整 base（`scheme://host:port`），给了就忽略 IP/PORT/SCHEME |
| `QGIS_DEV_AUTH_IP` / `QGIS_DEV_AUTH_PORT` / `QGIS_DEV_AUTH_SCHEME` | 服务器地址三段 |
| `QGIS_DEV_AUTH_LOGIN_PATH` | 登录接口路径 |
| `QGIS_DEV_AUTH_TIMEOUT` | 超时（秒） |
| `QGIS_DEV_AUTH_VERIFY_SSL` | https 是否校验证书（`1`/`0`） |

读取规则（都不抛异常，启动登录不能因为配置问题崩）：

- `app.yaml` 路径可以整份换：`QGIS_DEV_CONFIG`（与 bootstrap / 在线底图同一约定）；
- `app.yaml` 缺失 / 读不动 / YAML 写坏 / 没写 `env:` → 按 `DEFAULT_ENV = "dev"` 选段；
- 环境段里某个字段缺失 / 类型不对（端口写成 `abc`）→ 只有那个字段回退上一级。

> `env:` 是全局开关，不是只给登录用的：bootstrap 还用 `prod` / `dev` 决定 QGIS
> 运行时怎么探测（`QgisRuntime.for_prod()` / `for_dev()`）。切到 `prod` 前先确认
> 那台机上的 QGIS 安装位置也是生产布局，否则看 `config/app.yaml` 里的
> `qgis.install_path` / `QGIS_PREFIX`。

### 2.2 接口契约（curl 实测）

```http
POST http://59.55.120.111:7008/forestry-api/system/unit/unified
Content-Type: application/json

{"username": "ayxlyj", "password": "<md5 小写 32 位 hex>", "isCode": false}
```

成功（HTTP 200）：

```json
{"code": 5000, "message": "操作成功", "success": true,
 "data": {"access_token": "dc46a0ed-…", "token_type": "bearer",
          "refresh_token": "e8be8423-…", "expires_in": 21599, "scope": "message flow search log form user"}}
```

失败（**HTTP 也是 200**，靠 body 判断）：

```json
{"code": 5001, "message": "密码错误，请检查后重试", "success": false}
```

解析要点（都在 `config.py` 里可配）：

- 密码提交 `md5(密码)` 小写 hex（`password_encoding="md5"`，可改 `"plain"`）；
- 额外固定字段 `isCode: false` 由 `DEFAULT_EXTRA_BODY` 拼进 body；
- 成功码是 **5000**（非常见 0/200）→ 以 `success` 布尔为准，`code` 仅作兜底；
- token 在 `data.access_token`（`DEFAULT_TOKEN_FIELDS` 依次找，允许包一层 `data`/`result`）；
- 响应里没有 username → 回退用请求里提交的那个。

### 2.2.1 当前用户信息（进主界面时拉一次）

实现在 `app/core/auth/current_user.py`（`fetch_current_user(token) -> CurrentUser`），
**请求本身走业务接口工具** `app.core.util.service_client`（不自己拼 URL / 发 HTTP）：

```python
from app.core.auth.current_user import fetch_current_user

user = fetch_current_user(result.token)                 # 按环境配置找服务器
user = fetch_current_user(result.token, config=server)  # 已经有配置对象就直接注入
```

```http
GET http://59.55.120.111:7008/forestry-api/system/unit/currentUserByToken?access_token=<token>
unitkey: Bearer <token>
```

成功：

```json
{"code": 5000, "message": "操作成功", "success": true,
 "data": {"userInfo": {"userId": "4d640d2f…", "userAccount": "ayxlyj", "userName": "测试安义县",
                    "headIcon": "/api/file/Image/userAvatar/001.png",
                    "departmentName": "办公室", "organizeName": "南昌市安义县林业局",
                    "areaName": "安义县", "mobilePhone": "",
                    "prevLoginTime": 1773105860000, "prevLoginIPAddress": "10.8.61.23"},
          "unitKey": "Bearer dc46a0ed-…", "needResetPassword": false}}
```

token 不对 / 过期（**HTTP 仍是 200**）：

```json
{"code": 401, "message": "登录过期，请重新登录", "success": false}
```

实测要点（2026-09，逐条探过）：

- **鉴权只看 query 里的 `access_token`**：请求头带不带、带什么都一样（垃圾 token
  + 合法头仍然 401；不带 token 返回 `code:500`“服务出现错误”）；
- 虽然如此，代码仍按前端约定带上 `unitkey: Bearer <token>`头（后端哪天改成
  看头也不用改代码，头名可用 `QGIS_DEV_AUTH_TOKEN_HEADER` / `token_header` 改）；
- 响应里的 **`data.unitKey`（形如 `"Bearer <token>"`）就是后续业务接口要带的头值**，
  已存到 `CurrentUser.unit_key`（见 §6）；
- `prevLoginTime` 是**毫秒**时间戳（代码自动换算成秒），`mobilePhone` / `positionIds`
  等字段可能是空串 / 空数组；
- 字段名 / 路径全在 `config.py`（`DEFAULT_CURRENT_USER_PATH`、`current_user_path`、
  `QGIS_DEV_AUTH_CURRENT_USER_PATH`），换接口不用改业务代码。

### 2.2.2 其他业务接口怎么调（`app/core/util/service_client.py`）

> **规矩：除登录（`AuthClient.login`）以外，访问业务服务器的都走这个入口。**
> 当前用户信息（§2.2.1）已经是这么做的，以后新增业务接口照栉。

登录换到的 token 在 bootstrap 里登记一次（`set_session_token(result.token)`），
之后业务接口都走这一个入口，**服务器地址不用自己拼**（与登录同一份环境配置，
不会出现"登录打到新机、业务还在老机"）：

```python
from app.core.util.service_client import ServiceError, request

# GET  /forestry-api/xxx/list?pageNum=1&keyword=%E5%AE%89%E4%B9%89
request("/forestry-api/xxx/list", {"pageNum": 1, "keyword": "安义"}, "GET")

# POST /forestry-api/xxx/save   body: {"name":"A"}（非 GET 方法 params 就是 JSON body）
request("/forestry-api/xxx/save", {"name": "A"}, "POST")

# 只认 query 的接口（如 currentUserByToken）要显式打开 token_in_query
request("/forestry-api/system/unit/currentUserByToken", token_in_query=True)

try:
    resp = request("/forestry-api/xxx/list", {"pageNum": 1}, "POST")
except ServiceError as exc:          # 网络不通 / 超时 / HTTP 非 2xx / 业务失败
    logger.warning("取列表失败：%s", exc)
else:
    rows = resp.data["records"]      # resp.data ＝ payload["data"]
```

命令行直接打一次（排障用；`--token` 不给则用会话 token，CLI 是独立进程通常
要显式给）：

```bash
python -m app.core.util.service_client GET  /forestry-api/xxx/list '{"pageNum":1}'
python -m app.core.util.service_client POST /forestry-api/xxx/save '{"name":"A"}'
python -m app.core.util.service_client GET  /forestry-api/system/unit/currentUserByToken \
    --token <token> --token-in-query
```

接口要点：

- 实际地址 = 当前环境的 `{scheme}://{ip}:{port}` + 传进去的路径（传完整 `http(s)://…`
  时直接用那个 URL，不再拼环境）；路径前导斜杠可省，`//a/b` 会归一化成 `/a/b`。
- 默认头 `Accept: application/json` + `unitkey: Bearer <token>`（token 来自登录，
  头名可配）；JSON body 自动带 `Content-Type`；`headers=` 里同名的头会**盖掉**默认头。
- query 里值为 `None` 的键丢掉、布尔写成小写 `true` / `false`（Java 后端认这个）、
  列表值同名重复拼；日志里的 `access_token` 等敏感 query 会打码。
- 默认 **HTTP 非 2xx 与业务失败（`success:false`，如 `code:401`）都抛 `ServiceError`**；
  不想抛就 `raise_on_error=False`，再看 `resp.ok` / `resp.code` / `resp.message` /
  `resp.data`（已把 `payload["data"]` 拆出来） / `resp.text`（非 JSON 响应）。
- 已经有配置对象（比如 bootstrap 里解析好的 `AuthServerConfig`）就 `config=cfg`
  直接注入：根地址 / 头名 / 超时都听它，不再去读环境变量（鉴权包内部就是这么用的）。
- 同步阻塞，GUI 里调要自己放后台线程（项目约定 `threading.Thread`）。

### 2.3 客户端与线程模型

`AuthClient.login(username, password) -> AuthResult` 与
`fetch_current_user(token) -> CurrentUser`（内部走 `service_client`）都是**同步阻塞**的，
自己不管线程；`LoginDialog` 用普通 `threading.Thread` 调登录（与
`attribute_dock.FeatureStreamWorker` 同一套路，**不用** QThread / QgsTask），结果经
Qt 信号回到主线程再更新 UI：

- 请求中：输入框 + 「登录」按钮禁用、文案变「登录中…」、忙碌光标；**取消按钮仍可用**；
- 取消 / 关窗：自增 `_attempt` 序号，**在途结果被丢弃**（不会事后 `accept()`）；
- 失败：把 `AuthError` 的中文文案直接显示在错误行（无本地兜底账号）；
- 成功：`AuthResult(username, token, expires_in, raw)` → `accept()`。

进主界面之前还会**同步**再发一次 `currentUserByToken`（见 §2.2.1），把
`CurrentUser` 挂到 `AuthResult.user` 上（`dataclasses.replace`）；这一次失败
**不拦登录**，只记一条 warning，标题栏回退显示登录账号。

无 QGIS / 命令行也能单测：`AuthClient` 只依赖标准库。
回归用例见 `tests/core/auth/test_auth_client.py`（本地假 HTTP 服务器，不联网）。

---

## 3. 启动流程

```
python app/main.py
    ↓
bootstrap.main()
    ├─ 配置 / 路径 / 日志
    ├─ qgis.apply_env()             ← 只设环境变量
    ├─ _ensure_qgis_application()   ← 创建 QApplication（不 initQgis）
    │
    ├─ _run_login_dialog(cfg) ────→ LoginDialog（模态，后台线程 POST 授权服务器）
    │      │                          ⚠ 此时**不**显示初始化状态页：
    │      │                            两者都是居中浮窗，会叠在一起
    │      ├─ Reject / 关窗 → return None → main() 直接 return 0（退出）
    │      └─ Accept → AuthResult(username, token, expires_in)
    │             ├─ set_session_token(token) → 业务接口工具（service_client）能自动带头
    │             └─ _with_current_user() → fetch_current_user()
    │                    └─ 走 service_client  GET currentUserByToken（同步）
    │                    └─ 成功：AuthResult.user=CurrentUser（失败只 warning，不拦登录）
    │
    ├─ _start_splash()              ← 登录通过才弹初始化状态页
    │
    └─ _run_qgis_app(cfg, qgis, app, splash, auth_result=…)
           ├─ app.initQgis() / 翻译 / processing / 用户表达式
           ├─ build_window(app_name, auth_result) → MainWindow
           │     └─ _build_chrome() → title_bar.set_user(username, expires_at, user)
           ├─ window.show() + 启动回调（底图 / 打开地图文档）
           └─ app.exec()
```

**没有"跳过登录"的分支**：进程每次启动都要走一遍授权服务器。
想免登录只能在业务上另设计（例如服务端签发长效 token 并加密落盘）——
但那等于把刚去掉的本地凭证又加回来，安全性与 `credential.py` 同级。

---

## 4. 会话信息去哪了

| 信息 | 生命周期 | 位置 |
|---|---|---|
| `username`（登录框里敲的那个） | 本次运行 | `AuthResult.username` |
| `access_token` | 本次运行 | `AuthResult.token`；**已用于** `currentUserByToken`（见 §2.2.1） |
| `refresh_token` | 本次运行 | `AuthResult.raw["data"]["refresh_token"]`（暂未使用） |
| `expires_in`（21599 秒 ≈ 6 小时） | 本次运行 | 标题栏「会话有效期：5 小时」（自适应 天/小时/分钟） |
| 当前用户（姓名 / 单位 / 部门 / 区域 / 上次登录） | 本次运行 | `AuthResult.user`（`CurrentUser`）→ 标题栏用户区 |
| 密码 | 提交后从内存丢弃；**可选落盘**（见 §4.1） | 只在登录框输入框里，不记日志 |

标题栏用户区：按钮上显示**姓名**（没姓名就用账号；`currentUserByToken` 拉不到时
回退只显示登录账号，与以前一致），下拉菜单是只读信息行（没数据的项不显示）：

```
账号：ayxlyj
姓名：测试安义县
单位：南昌市安义县林业局 / 办公室
区域：安义县
上次登录：2026-03-10 09:24 · 10.8.61.23
会话有效期：5 小时
⚠ 服务端要求重置密码        ← 仅 needResetPassword 为 true 时出现
```

`expires_at` 由 `AuthResult.expires_at()` 按 `登录时刻 + expires_in` 算出来
（`time.time() + expires_in`），只在主窗口构建时取一次快照。

---

### 4.1 记住账号（用户名 + 密码）

登录**成功之后**自动记住这次的输入；下次启动点用户名输入框就弹气泡，选中即
**同时回填用户名与密码**（回车 / 上下键 / 鼠标都能选，行尾 `✕` 删一条，
底部「清除全部」清空）。

```
┌──────────────────────────┐
│ 已保存的账号              │
│  zhangsan   ••••••••  ✕  │   ← 点行 → 回填两个输入框
│  ayxlyj     ••••••••  ✕  │
│               清除全部    │
└──────────────────────────┘
```

**存在哪**（跨平台，与用户级设置同目录）：

| 平台 | 路径 | 密码怎么存 |
|---|---|---|
| Windows | `%APPDATA%\qgis_dev\saved_logins.json` | **DPAPI 加密**（密钥由系统按当前 Windows 用户托管） |
| Linux / 麒麟 | `$XDG_CONFIG_HOME/qgis_dev/saved_logins.json`（默认 `~/.config/…`） | **本地密钥加密**：同目录 `.secret_key`（`0o600` 的 32 字节随机密钥）再揉上 `/etc/machine-id` 与 uid 派生，`"prot": "local-key"` |
| macOS | `~/Library/Application Support/qgis_dev/…` | 同上（本地密钥） |

> 备份 / 迁移时若想让记住的账号还能用，**密钥文件要一起带上**（只拷 json 则条目会被跳过）。
> 跨平台拷同一个文件不会崩：`prot` 是**逐条**记录的，解不开的条目直接跳过。

文件结构：

```json
{
  "version": 1,
  "prot": "dpapi",
  "logins": [
    {"username": "ayxlyj", "password": "<base64 密文>", "prot": "dpapi", "updated_at": 1758000000}
  ]
}
```

**行为细节**

- 最多记 8 条（`MAX_SAVED_LOGINS`），同名覆盖并挪到最前，不产生重复；
- 只在**登录成功**后保存（密码错 / 网络失败都不会存）；
- **保存即升级**：写入时所有条目都会用当前平台的方式重新加密（旧的 `"prot": "plain"`
  明文条目一次保存后就变成密文）；
- 读取时任何一条坏了（密文损坏 / 换了机器或用户 / 字段类型不对）只跳过该条，
  不会影响启动登录；
- **加密失败宁可不写**：拿不到密钥时本次不写文件（回退明文比丢一次保存更糟）；
- **气泡不会改动登录框本身的外观**：它是独立的 `Qt.Popup` 窗口，空闲态登录框
  与加这个功能之前逐像素一致（空输入框、不加图标、不加勾选框）。

**关掉 / 清理**

```bash
QGIS_DEV_REMEMBER_PASSWORDS=0 python app/main.py     # 不再保存新账号（仍可读旧数据、手动清理）
QGIS_DEV_SAVED_LOGINS=/tmp/x.json python app/main.py  # 换存储位置（多环境 / 测试隔离）
QGIS_DEV_SECRET_KEY=/tmp/k python app/main.py         # 换密钥文件（Linux / macOS）

# 看当前存了谁 / 存哪 / 密码是加密还是编码
python -c "from app.core.auth import saved_logins as s; print(s.saved_logins_path(), s.password_protection(), [x.username for x in s.load_saved_logins()])"
# 全部删掉（也可以直接在气泡里点「清除全部」）
python -c "from app.core.auth.saved_logins import clear_saved_logins as c; print(c())"
```

---

## 5. 安全模型

### 这是什么

- ✅ **服务端校验账号**：密码不出本机以外的信任边界（走内网 http，见下"不是什么"）
- ✅ **登录态不落盘**：本机没有 token 凭证文件，拷走磁盘也拿不到登录态
- ✅ **进程级会话**：关掉软件即登出，多人共用一台电脑不会串号
- ✅ **服务端文案直显**：密码错 / 账号锁定等提示由服务端给出

### 这不是什么

- ❌ **不是强身份认证的终点**：token 拿到后没有用于任何业务接口，只用于显示账号名
- ❌ **不防中间人**：默认 `http`（内网），抓包可见 md5 后的密码；生产建议 `https`
  （`DEFAULT_SERVER_SCHEME="https"`，内网自签证书可把 `DEFAULT_VERIFY_SSL=False`）
- ❌ **不做 token 刷新**：`refresh_token` 拿到了但没用；6 小时后 token 过期也不会踢人
  （本次运行仍继续，因为没有任何接口依赖它）
- ⚠️ **"记住密码"后本机确实能解出明文**：这是"记住密码"的固有代价，不是漏洞。
  - Windows：靠 DPAPI 把范围收敛到"当前 Windows 用户账号"——换个用户 /
    拷贝到别的机器都解不开。
  - Linux / 麒麟 / macOS：靠 `~/.config/qgis_dev/.secret_key`（`0o600`）+ 机器标识
    + uid 派生出的密钥，同样是"拷贝到别的机器 / 别的用户解不开"；但要清楚它
    **不防**"攻击者已能以同一用户身份执行代码"——那种情况下密钥文件也读得到。
  - 都不接受 → 设 `QGIS_DEV_REMEMBER_PASSWORDS=0`（或不做"记住密码"）。

---

## 6. 后续可做

- 业务接口直接走 `app.core.util.service_client.request()`（§2.2.2）——头值
  `AuthResult.user.unit_key`（＝ 服务端 `data.unitKey`）与 token 已经自动带上，
  一般不用自己拼 `unitkey`；只在需要换头名 / 不带 token 时才动 `headers=` / `token=`；
- token 快过期时用 `refresh_token` 静默刷新，或提示重新登录（标题栏已有
  「会话有效期」倒计时可做依据）；
- 「记住账号」进阶：Windows 改用**凭据管理器**（`CredWrite`/`CredRead`，能让用户在
  系统"凭据管理器"里看到并删除）、Linux 用 Secret Service（gnome-keyring / KWallet，
  `secret-tool` 或 libsecret）/ macOS 用 Keychain；当前实现已把入口收敛在
  :mod:`app.core.auth._secret` 的 :func:`~app.core.auth._secret.protect` /
  :func:`~app.core.auth._secret.unprotect`，加一个 backend 即可，
  `saved_logins` / UI 都不用改（`prot` 是逐条记录的，可新旧共存）。
  ⚠️ 麒麟 V10 上钥匙串守护进程/包不一定存在，所以目前默认不用它。
- 需要免登录时，走**服务端签发长效 token + 系统密钥链加密存储**，不要回到
  自签 HMAC 凭证（`credential.py` 那种，密钥在客户端等于没有）。

---

## 7. 调试 / 测试

### 7.1 单测（不联网、不依赖 QGIS）

```bash
pytest tests/core/auth/ -q      # 本地假 HTTP 服务器 + 记住账号的存储层 + 跨平台密钥层
```

覆盖：请求体（md5 密码 + `isCode`）、成功/失败解析、HTTP 401/403/500、
非 JSON / 空响应 / 非对象响应、连接被拒 / 超时、**按环境选段**（`env:` 选段 /
加新环境 / 环境名写错回退 / `QGIS_DEV_CONFIG` 换配置文件）、环境变量覆盖与非法回退；
**当前用户接口**（URL 拼接与转义 / `unitkey` 头 / token 过期文案 / 平铺字段容错 /
毫秒时间戳换算 / 缺字段走默认值）；以及记住账号的保存 / 覆盖 / 上限 / 删除 /
坏文件容错 / 密文损坏跳过 /
`QGIS_DEV_REMEMBER_PASSWORDS=0` 开关；密码保护层（`test_secret.py`）的
加解密往返 / 随机 nonce / 换机器解不开 / 密文篡改检测 / 密钥文件懒创建与权限 / 坏密钥不覆盖。

> `test_secret.py` 里有一条 `test_key_file_mode_is_0600` 在 Windows 上会 skip
> （chmod 只影响只读位）——**部署到麒麟后请在那台机器上跑一次该文件**。

### 7.2 直接命令行试接口

```bash
curl -s -X POST http://59.55.120.111:7008/forestry-api/system/unit/unified \
  -H 'Content-Type: application/json' \
  -d '{"username":"ayxlyj","password":"'$(python -c "import hashlib;print(hashlib.md5('Ly2025@taiji1'.encode()).hexdigest())")'","isCode":false}'
```

### 7.3 临时换服务器 / 换环境

```bash
# 只换地址（最高优先级，不改任何配置文件）
QGIS_DEV_AUTH_IP=10.0.0.9 QGIS_DEV_AUTH_PORT=9000 python app/main.py

# 换整套环境：把 env: 改成 prod（或在临时配置文件里写 env: prod）
QGIS_DEV_CONFIG=/tmp/app_prod.yaml python app/main.py

# 换当前用户接口路径 / 头名（后端改接口时临时验证用）
QGIS_DEV_AUTH_CURRENT_USER_PATH=/forestry-api/system/unit/whoami \
QGIS_DEV_AUTH_TOKEN_HEADER=Authorization python app/main.py
```

或者命令行直接看当前会打哪台机（不启 UI）：

```bash
python -c "from app.core.auth.config import load_auth_server_config as l; print(l().describe())"
```

---

## 8. FAQ

### Q: 为什么每次启动都要登录？用户嫌麻烦怎么办？

A: 这是当前需求（去掉本地凭证）。若要免登录，正确做法是服务端签发长效 token
并加密落盘，而不是本地自签凭证。

### Q: token 过期了会怎样？

A: 本次运行不受影响：登录换 token 时会同步拉一次当前用户信息（失败也照样进
主界面），之后不再拿 token 调任何接口。重启软件重新登录即可。

### Q: 标题栏的姓名 / 单位是怎么来的？

A: 登录成功后 `GET /forestry-api/system/unit/currentUserByToken?access_token=…`
拉一次（见 §2.2.1），姓名 / 单位 / 部门 / 上次登录都从这里来；服务端那一刻
连不上或 token 不对，就只显示登录时敲的账号，不影响使用。

### Q: 登录框能记住用户名吗？

A: 能。登录成功后自动记住「用户名 + 密码」（见 §4.1），下次点用户名输入框弹
气泡选一条就同时回填两个输入框；气泡里可以单独删除某条或点「清除全部」。
不想让它记密码：`QGIS_DEV_REMEMBER_PASSWORDS=0`（保存时直接跳过）。
注意这是**本机记住密码**，与"免登录"无关，每次仍要连服务器换 token。

### Q: 记住的账号文件能拷到别的机器上用吗？

A: 拷贝可以，但密码解不开：Windows 上是 DPAPI 加密的，Linux / 麒麟上有
`.secret_key` 密钥文件 + 机器标识 / uid 参与派生，换机器 / 换用户都不行
（那一行被忽略，只留下列表里能解开的那几条）。要"跟人走"的只能是免登录 token
方案，见 §6。同一台机器上备份 / 迁移则把 `.secret_key` 一起拷上即可。

### Q: 多用户共用一台电脑怎么办？

A: 天然互不干扰——登录态只在本进程内存里，关掉软件即登出。
