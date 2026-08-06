# 登录与凭证模块

> 启动登录 → 30 天本地凭证 → 30 天内自动跳过登录。
>
> **本模块不是真正的安全认证**，仅用于免登录体验 + 轻量篡改检测。
> 生产部署请对接公司 SSO / OAuth / 自建 auth 服务。

---

## 1. 模块清单

| 文件 | 作用 |
|---|---|
| `app/ui/dialogs/login.py` | `LoginDialog`：启动时弹出的登录对话框（PyQt5-only） |
| `app/core/auth/credential.py` | `Credential` 数据类 + HMAC 签名 + 加载/保存/清除 |
| `app/core/auth/__init__.py` | 模块导出 |
| `app/bootstrap/__init__.py` | 启动流程：先 `load_credential`，有就跳过登录；没有就弹 `LoginDialog`，登录成功后 `Credential.issue` + `save_credential` |

---

## 2. 启动流程

```
python app/main.py
    ↓
bootstrap.main()
    ├─ 配置 / 路径 / 日志
    ├─ qgis.apply_env()             ← 只设环境变量
    ├─ _ensure_qgis_application()   ← 创建 QApplication（不 initQgis）
    │
    ├─ load_credential() ────────────────┐
    │   ├─ 文件不存在       → None       │
    │   ├─ 签名校验失败     → None       │   任意一步失败
    │   ├─ 解析失败         → None       ├─→ 都视为"无有效凭证"
    │   └─ 当前时间 ≥ expires_at → None  │
    │                                    │
    │   全部通过 → 返回 Credential       │
    └────────────────────────────────────┘
                │
        ┌───────┴────────┐
        │                │
   返回 Credential    返回 None
        │                │
   跳过登录         _run_login_dialog()
   _run_qgis_app()       │
        ↑                ├─ Accept → 返回 username
        │                └─ Reject → return 0 (退出)
        │
   Credential.issue(username)        ← 30 天有效期
   save_credential(new_cred)
   _run_qgis_app(cfg, qgis, app)
```

---

## 3. 凭证格式

凭证以 JSON 形式存储在**用户级配置目录**（跨平台标准位置）：

### 3.1 文件位置

| OS | 路径 |
|---|---|
| Windows | `%APPDATA%\qgis_dev\credential.json` |
| macOS | `~/Library/Application Support/qgis_dev/credential.json` |
| Linux | `$XDG_CONFIG_HOME/qgis_dev/credential.json` 或 `~/.config/qgis_dev/credential.json` |

由 `app.core.auth.credential.user_credential_path()` 计算。

### 3.2 JSON 结构

```json
{
  "username": "admin",
  "issued_at": 1722345678,
  "expires_at": 1724937678,
  "signature": "a3f5c...（HMAC-SHA256 hex 64 字符）"
}
```

字段说明：

| 字段 | 类型 | 说明 |
|---|---|---|
| `username` | string | 登录时输入的用户名（仅展示用，不参与签名校验） |
| `issued_at` | int | 签发时间（unix 秒） |
| `expires_at` | int | 过期时间（unix 秒）= `issued_at + 30 * 86400` |
| `signature` | string | `hex(HMAC-SHA256(secret, "username\|issued_at\|expires_at"))` |

### 3.3 校验顺序

`load_credential()` 内部按以下顺序校验，任何一步失败返回 `None`：

1. 文件存在
2. JSON 解析成功且字段齐全
3. HMAC 签名匹配（防篡改）
4. 当前时间 `< expires_at`（在有效期内）

---

## 4. 安全模型

### 这是什么

- ✅ **免登录体验**：30 天内打开软件直接进工具
- ✅ **轻量篡改检测**：改 `expires_at` 续命会被签名校验挡住
- ✅ **跨平台用户级路径**：符合 OS 规范，`%APPDATA%` / `~/.config` / `Library`
- ✅ **文件权限收紧**：保存时 `chmod 0o600`（Linux/macOS 生效）

### 这不是什么

- ❌ **不是真正的身份认证**：用户名密码校验是写死在 `LoginDialog._on_accept` 里的
- ❌ **不可防反编译**：HMAC 密钥编译进二进制，懂逆向的人能伪造凭证
- ❌ **不防中间人**：没有服务端校验
- ❌ **不强制吊销**：除非要换密钥或删文件，否则凭证始终有效到过期

**简而言之：能挡住"改系统时间续命 / 直接编辑 JSON 续命"两类懒人操作，挡不住认真的攻击者。**

---

## 5. 如何替换为真实 auth 服务

### 5.1 服务端校验（推荐）

把 `LoginDialog._on_accept` 里的本地硬编码改成调远端 API：

```python
# app/ui/dialogs/login.py
def _on_accept(self):
    username = self.username_edit.text().strip()
    password = self.password_edit.text()
    if not username or not password:
        self._show_error("用户名和密码不能为空")
        return
    # === 替换为真实 auth ===
    if not self._verify_with_server(username, password):
        self._show_error("用户名或密码错误")
        return
    self._username = username
    self.accept()

def _verify_with_server(self, username, password) -> bool:
    import requests
    resp = requests.post(
        "https://auth.example.com/login",
        json={"username": username, "password": password},
        timeout=5,
    )
    return resp.status_code == 200
```

### 5.2 服务端签发凭证（更安全）

让服务端返回带过期时间的 token，本地只存 token：

```python
# 服务端返回
{
  "token": "eyJhbGciOi...",   # JWT 或任意签名 token
  "expires_at": 1724937678
}

# 本地保存
@dataclass(frozen=True)
class Credential:
    username: str
    expires_at: int
    token: str      # 替换 signature
    
    def verify_signature(self) -> bool:
        # 不再本地验签；调用远端 verify 接口，或用公钥验 JWT 签名
        return self._verify_token_with_public_key(self.token)
```

### 5.3 加机器绑定（防凭证复制到其它电脑）

把 `machine_id` 纳入签名 payload：

```python
import platform, hashlib

def _machine_fingerprint() -> str:
    raw = f"{platform.node()}|{platform.machine()}|{platform.processor()}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]

# 签发
sig = hmac.new(_SECRET, f"{username}|{machine_fp}|{iat}|{exp}".encode(), hashlib.sha256).hexdigest()

# 校验时多比对一项
```

---

## 6. 调试 / 测试

### 6.1 强制重新登录

```python
# 在 Python REPL 或临时脚本里
from app.core.auth import clear_credential
clear_credential()
```

或手动删除：

- Windows: `del %APPDATA%\qgis_dev\credential.json`
- Linux: `rm ~/.config/qgis_dev/credential.json`
- macOS: `rm ~/Library/Application\ Support/qgis_dev/credential.json`

### 6.2 验证凭证是否有效

```python
from app.core.auth import load_credential
cred = load_credential()
if cred:
    print(f"OK: {cred}")
    print(f"剩余 {cred.remaining_seconds()} 秒")
else:
    print("无效或不存在")
```

### 6.3 单元测试示例

```python
def test_issue_and_load_roundtrip(tmp_path, monkeypatch):
    from app.core.auth import credential
    # 把凭证路径重定向到 tmp_path
    monkeypatch.setattr(credential, "user_credential_path",
                        lambda: tmp_path / "credential.json")
    
    issued = credential.Credential.issue("testuser", validity_seconds=60)
    credential.save_credential(issued)
    
    loaded = credential.load_credential()
    assert loaded.username == "testuser"
    assert loaded.verify_signature()
    assert loaded.is_valid()


def test_expired_credential_rejected():
    cred = credential.Credential.issue("testuser", validity_seconds=-1)
    assert not cred.is_valid()
    # load_credential 应返回 None（即使签名通过）


def test_tampered_credential_rejected():
    cred = credential.Credential.issue("testuser")
    tampered = credential.Credential(
        username=cred.username,
        issued_at=cred.issued_at,
        expires_at=cred.expires_at + 365 * 86400,  # 续命一年
        signature=cred.signature,
    )
    assert not tampered.verify_signature()
```

---

## 7. 密钥轮换

如果怀疑密钥泄露（被人反编译拿到 `_SECRET`），修改：

```python
# app/core/auth/credential.py
_SECRET: bytes = b"qgis_dev::auth::v2::new-secret-2026-08-04"
```

注意：

- 改密钥后**所有已签发的旧凭证都会失效**（签名校验失败）
- 用户需要重新登录一次
- 建议在版本号里同步升级（`_v2` 后缀），便于追踪

---

## 8. FAQ

### Q: 凭证过期后会发生什么？

A: 下次启动 `load_credential()` 返回 `None`，正常弹登录对话框。登录成功后签发新凭证。

### Q: 多用户共用一台电脑怎么办？

A: 当前凭证只有一份（覆盖式写入），后登录的覆盖先登录的。如需多用户支持，文件名带用户名后缀（`credential.{username}.json`），并在 `load_credential` 里加选用户步骤。

### Q: 凭证能不能复制到另一台电脑用？

A: 当前**能**（无机器绑定）。要禁止请按 §5.3 加 machine fingerprint。

### Q: 为什么是 30 天不是 7 天 / 90 天？

A: 业务决定的默认值。改 `VALIDITY_SECONDS` 常量即可（`30 * 24 * 3600`）。也可以让用户在登录对话框上选"记住 7 / 30 / 90 天"，把 `Credential.issue(username, validity_seconds=user_choice)` 传进去。
