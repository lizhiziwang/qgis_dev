"""回传前的打码：日志与配置都是会被发出去的文件。

**为什么单独一个模块**：打码有两个地方要用——一是操作快照（
:mod:`app.infra.logging.operation` 记"用户点了什么"，图层 ``source`` 里可能带 XYZ
底图的 ``&tk=…``），二是诊断包导出（:mod:`app.infra.logging.diagnostics` 收进来的
``config/app.yaml`` 里有**真实**的天地图 key）。两边各写一套键名表和正则，早晚会
漂移：快照那边补了一个键名、导出那边没补，漏出去的凭据就是从这条缝里走的。所以
表与正则只留一份，两边都从这里取。

三种形态都要处理：

- **值里以 ``k=v`` 出现**（:func:`redact`）：WMTS / XYZ 的 URL 就是这么带 key 的
  （``…&FORMAT=tiles&tk=xxxx``），是日志里最常见的形态，也是唯一能覆盖"值本身
  裸在文本里"的兜底。
- **键名本身就是凭据**（:func:`is_secret_key` / :func:`is_secret_name`）：``{"password":
  "…"}``、dataclass 的 ``token`` 字段——值是裸的，只能靠键名认。
- **整份结构化文本**（:func:`redact_yaml` / :func:`redact_json`）：先按键名把命中的
  **值**挖出来做字面替换，再兜一层 ``k=v``。这样既保住注释与原始缩进（现场同事看得
  懂那份配置），又不依赖逐行解析（多行 / 引号 / 块标量都不会漏）。

结构化文本要"定点抹值"**和**"挖真值再字面替换"两条一起上，谁也替不了谁：

- 定点抹值（``password: admin`` → ``password: ***``）管得住**短值**——短值不能拿去做
  全文替换（``password: 1`` 会把文件里所有 1 都打掉）；但它只管赋值那一处。
- 字面替换管得住**别处**——真值可能同时出现在注释里、另一个 URL 里、被引用的字段里
  （``config/app.yaml`` 的注释就写着"换 key 只改这里一处"）；但它要求值够长才敢动手。

写日志时也挂一道（:class:`RedactionFilter`）：用户不一定按我们想的发诊断包，他可能
直接把 ``logs/app.log`` 拖过来。

光靠 ``k=v`` 不够——真值也可能**裸着**出现在日志里（图层源被拼进缓存路径、某个
异常消息原样带出了 key）。所以启动装配日志时把配置里凭据键的**值**登记进来
（:func:`register_secrets` / :func:`collect_secret_values`），之后任何日志行里的
这串值都按字面替换；导出诊断包时再从包内那份配置里挖一遍（诊断包可能和启动时
不是同一份配置）。两处都不能少：前者覆盖运行期写的所有日志，后者覆盖导出时配置
已经被改过、或本进程没登记到的情况。
"""

from __future__ import annotations

import dataclasses
import json
import logging
import re
from collections.abc import Callable, Iterable, Mapping
from typing import Any

import yaml

#: 打码后的占位。**全文统一一个写法**：grep ``***`` 就知道哪里被动过。
REDACTED = "***"

#: 键名命中即整项打码（**严格**匹配：键名本身就是凭据名）。
SECRET_KEYS = frozenset(
    {
        "tk",
        "token",
        "access_token",
        "refresh_token",
        "session_token",
        "password",
        "passwd",
        "pwd",
        "secret",
        "api_key",
        "apikey",
    }
)

#: 宽松匹配额外认的名字：``map.keys.tianditu_tk`` / ``gaode_key`` 这类"平台_凭据"
#: 命名，以及光叫 ``key`` 的字段。**只在导出配置时用**（见 :func:`is_secret_name`）。
_EXTRA_NAMES = frozenset(
    {"key", "keys", "app_key", "appkey", "gaode_key", "gaodekey", "tianditu_tk", "private_key"}
)

#: 宽松匹配认的后缀
_SECRET_SUFFIXES = ("_tk", "_key", "_token", "_secret", "_password", "_passwd", "_pwd")

#: 键名太短就不值得做字面替换：``password: 1`` 这种值一旦拿去全文替换，会把文件里
#: 所有 ``1`` 都打掉（缩进里的数字也跑不掉）。6 是"一眼不像凭据"的下限。
_MIN_LITERAL_LEN = 6

#: 值里以 ``k=v`` 形态出现的凭据（URL 查询串 / 连接信息 / 命令行参数）。
#: 名字按长度降序排，长的先试——``api_key`` 别被 ``key`` 抢先匹配成半个。
_NAME_ALT = "|".join(
    sorted((re.escape(k) for k in set(SECRET_KEYS) | set(_EXTRA_NAMES)), key=len, reverse=True)
)
_SECRET_VALUE_RE = re.compile(rf"(?i)\b({_NAME_ALT})=([^&\s;,'\"<>]+)")

#: YAML ``键: 值`` 行（**赋值那行**的兜底：短值也跑不掉，且只影响这一行）
_PAIR_RE = re.compile(r"^(?P<lead>\s*(?:-\s+)?)(?P<key>[\w.\-]+)(?P<sep>\s*:\s+)(?P<value>\S.*?)\s*$")

#: YAML 里值在**下面几行**的写法（块标量）：整块都要抹，只抹了 ``|`` 等于没抹
_BLOCK_INDICATORS = ("|", ">")

#: JSON 的 ``"键": "值"``
_JSON_PAIR_RE = re.compile(r'"(?P<key>[\w.\-]+)"(?P<sep>\s*:\s*)"(?:[^"\\]|\\.)*"')


#: 运行期登记的真值（启动时从配置里挖出来，见 :func:`register_secrets`）。
#: 进程级、只增不减：它是"本进程确实知道的那几串凭据"。
_known_secrets: set[str] = set()


def register_secrets(values: Iterable[Any]) -> None:
    """登记一组凭据**真值**：之后日志里裸出现它们也会被打成 ``***``。

    只收够长的字符串（同 :data:`_MIN_LITERAL_LEN`）：短值的字面替换会把无关文本
    打掉，短值交给 ``k=v`` 与结构化抹值那两层。
    """
    for value in values:
        if isinstance(value, str) and len(value) >= _MIN_LITERAL_LEN:
            _known_secrets.add(value)


def known_secrets() -> frozenset[str]:
    """当前登记的真值（只读视图，便于排查"为什么这行没打码"）。"""
    return frozenset(_known_secrets)


def reset_secrets() -> None:
    """清空登记表（测试用；生产进程从头到尾只登记一次）。"""
    _known_secrets.clear()


def redact(text: str, secrets: Iterable[str] | None = None) -> str:
    """把凭据打成 ``***``：先字面替换登记的真值，再兜 ``k=v`` 形态。

    非字符串先 ``str()``（快照什么都能收到）。

    :param secrets: 额外的真值（导出诊断包时从配置里现挖的），与运行期登记的
        一起参与字面替换。
    """
    if not isinstance(text, str):
        text = str(text)
    if not text:
        return text
    for value in _literal_values(secrets):
        if value in text:
            text = text.replace(value, REDACTED)
    return _SECRET_VALUE_RE.sub(rf"\1={REDACTED}", text)


def is_secret_key(name: Any) -> bool:
    """键名是否**就是**一个凭据（严格匹配）。

    用在操作快照里：那边宁可少打一个码，也别把 ``{"key": "geometrytype"}`` 这种普通
    字段整个抹掉——快照是排查用的，抹掉等于丢信息。
    """
    return _last_segment(name) in SECRET_KEYS


def is_secret_name(name: Any) -> bool:
    """宽松匹配：严格匹配之外，还认 ``tianditu_tk`` / ``gaode_key`` / 光叫 ``key`` 的。

    导出配置时用这个：宁可多抹一点——凭据漏出去是事故，配置里少一行只是少点信息。
    """
    last = _last_segment(name)
    return last in SECRET_KEYS or last in _EXTRA_NAMES or last.endswith(_SECRET_SUFFIXES)


def redact_yaml(text: str, secrets: Iterable[str] | None = None) -> str:
    """YAML 文本整体打码（保留注释与缩进，现场同事能对照原件读）。

    三道，顺序有讲究：

    1. :func:`redact` 的 ``k=v`` 兜底——URL 串里的 ``&tk=…``；
    2. 按真值**字面替换**——同一个 key 写在注释里、被别的字段引用时也一并清掉；
    3. ``键: 值`` 行**定点抹值**——短值（``password: admin``）不该做全文替换，但
       赋值那一行必须抹掉。

    3 放在最后是因为它只认"这一行的键名"，前面的替换动过的地方它照样对上。
    """
    return _mask_pair_lines(_redact_structured(text, yaml.safe_load, secrets))


def redact_json(text: str, secrets: Iterable[str] | None = None) -> str:
    """JSON 文本整体打码（``"键": "值"`` 定点抹 + 字面替换 + ``k=v`` 兜底）。"""
    return _mask_json_pairs(_redact_structured(text, json.loads, secrets))


def secret_values(text: str, loader: Callable[[str], Any] | None = None) -> set[str]:
    """从结构化文本里挖出"键名是凭据"的那些**值**（够长的字面量）。

    解析失败返回空集——调用方仍然有 :func:`redact` 那层兜底。
    """
    load = loader if loader is not None else yaml.safe_load
    try:
        data = load(text)
    except Exception:  # noqa: BLE001 —— 不是合法 YAML/JSON 而已，不该影响导出
        return set()
    return collect_secret_values(data)


def collect_secret_values(node: Any) -> set[str]:
    """从已解析的对象里挖凭据值：``dict`` / dataclass（可嵌套）/ 序列。

    启动时拿它扫 :class:`~app.env.config.AppConfig`：配置是冻住的 dataclass，
    ``map.keys.tianditu_tk`` 就在这棵树里。够长的值登记后，日志里裸出现的同一串
    值也会被打掉（见 :func:`register_secrets`）。
    """
    found: set[str] = set()
    _walk(node, found)
    return found


class RedactionFilter(logging.Filter):
    """写日志时就打码：同一份记录过一遍，命中才动它。

    只在真的出现凭据时替换 ``record.msg``（并把 ``args`` 清空，避免 formatter 再
    ``%`` 一次已格式化的整串）。绝大多数记录原样放行，所以这是"零拷贝"路径。

    **必须挂在 handler 上**：原因同
    :class:`~app.infra.logging.operation.OperationFilter`——logger 上的 filter 看不到
    冒泡上来的记录。挂在 handler 上还有个好处：记录被第一个 handler 改过之后，后续
    handler 看到的已经是打过码的，不会出现"控制台是原文、文件里是 ``***``"。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 —— 拿不到就不动它，交给 formatter 自己去降级
            return True
        cleaned = redact(message)
        if cleaned != message:
            record.msg = cleaned
            record.args = ()
        return True


_REDACTION_FILTER = RedactionFilter()


def attach_redaction(handler: logging.Handler) -> logging.Handler:
    """给 handler 装打码 filter（幂等）。返回原 handler，便于链式装配。"""
    if _REDACTION_FILTER not in handler.filters:
        handler.addFilter(_REDACTION_FILTER)
    return handler


# --------------------------------------------------------------------- 内部


def _literal_values(extra: Iterable[str] | None) -> list[str]:
    """参与字面替换的真值：运行期登记的 + 调用方现给的（去重、长度够）。"""
    values = set(_known_secrets)
    if extra:
        values.update(v for v in extra if isinstance(v, str) and len(v) >= _MIN_LITERAL_LEN)
    return list(values)


def _last_segment(name: Any) -> str:
    """``map.keys.tianditu_tk`` → ``tianditu_tk``（配置里常带前缀）。"""
    return str(name).strip().lower().rsplit(".", 1)[-1]


def _redact_structured(
    text: str, loader: Callable[[str], Any] | None = None, secrets: Iterable[str] | None = None
) -> str:
    """先按真值字面替换（文本自身的 + 调用方给的），再兜一层 ``k=v``。"""
    if not isinstance(text, str):
        text = str(text)
    if not text:
        return text
    out = text
    for value in secret_values(text, loader):
        out = out.replace(value, REDACTED)
    return redact(out, secrets)


def _mask_pair_lines(text: str) -> str:
    """逐行把 ``键: 值`` 里的值抹掉（只认键名像凭据的行）。

    为什么不能只靠"字面替换"：``config/app.yaml`` 里开发账号的密码就是短短几个字符，
    这种值一旦拿去做全文替换，会把文件里所有同形字符串（缩进里的数字、别处的字词）
    都打掉。定点抹行没有这个问题，代价是**只**管赋值那一处——所以它和字面替换是一
    对组合，不是二选一。

    块标量（``password: |`` 后面缩进的那几行）单独处理：那里一个字面替换都碰不到
    （文件里的缩进让真值对不上），抹了 ``|`` 又等于把内容裸着留下——所以整块换成一行
    ``***``，既抹干净又保持 YAML 合法。
    """
    parts: list[str] = []
    block_indent = -1
    for line in text.splitlines(keepends=True):
        body = line.rstrip("\r\n")
        ending = line[len(body) :]
        indent = len(body) - len(body.lstrip(" \t"))
        if block_indent >= 0:
            if body.strip() and indent > block_indent:
                parts.append(f"{' ' * (block_indent + 2)}{REDACTED}{ending}")
                continue
            block_indent = -1
        match = _PAIR_RE.match(body)
        if match is not None and is_secret_name(match.group("key")):
            if match.group("value").startswith(_BLOCK_INDICATORS):
                parts.append(body + ending)
                block_indent = indent
                continue
            body = f"{match.group('lead')}{match.group('key')}{match.group('sep')}{REDACTED}"
        parts.append(body + ending)
    return "".join(parts)


def _mask_json_pairs(text: str) -> str:
    """JSON 的 ``"键": "值"`` 定点抹值。"""
    if not isinstance(text, str):
        return text

    def _replace(match: re.Match) -> str:
        if not is_secret_name(match.group("key")):
            return match.group(0)
        return f'"{match.group("key")}"{match.group("sep")}"{REDACTED}"'

    return _JSON_PAIR_RE.sub(_replace, text)


def _walk(node: Any, found: set[str]) -> None:
    """递归收集凭据键下的字符串值（``dict`` / dataclass / 序列都能走）。"""
    if dataclasses.is_dataclass(node) and not isinstance(node, type):
        node = {field.name: getattr(node, field.name, None) for field in dataclasses.fields(node)}
    if isinstance(node, Mapping):
        for key, value in node.items():
            if is_secret_name(key) and isinstance(value, str) and len(value) >= _MIN_LITERAL_LEN:
                found.add(value)
                continue
            # 键名像凭据但值是个子映射（``map.keys`` 就是），继续往下找真值
            _walk(value, found)
        return
    if isinstance(node, Iterable) and not isinstance(node, (str, bytes)):
        for item in node:
            _walk(item, found)


__all__ = [
    "REDACTED",
    "SECRET_KEYS",
    "RedactionFilter",
    "attach_redaction",
    "collect_secret_values",
    "is_secret_key",
    "is_secret_name",
    "known_secrets",
    "redact",
    "redact_json",
    "redact_yaml",
    "register_secrets",
    "reset_secrets",
    "secret_values",
]
