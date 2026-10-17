"""把"用户正在做什么"钉进日志：一次操作 = 一个编号 + 入参快照。

**为什么**：用户传上来的日志里最值钱的一行不是 traceback——代码位置能对上源码——
而是"报错时他到底在干什么"：哪个文件、什么坐标系、选的什么参数。没有这一行，
traceback 往往只说到"processing 返回了失败"，看不出输入本身就是脏的（GDB 导出的
字段长度不足、源文件在网络盘上、图层是只读的……）。

做法：

- :func:`log_operation` 是**装饰器**，用在领域服务的高频矢量操作入口上
  （缓冲 / 叠加 / 投影 / 建库 / 导入）。实参按名字自动抓成一行快照：进入记
  ``OP op3 开始 缓冲区生成 | job=BufferJob(layer=图层<xx> source=… )``，退出记
  ``完成 + 耗时``，异常记 ``失败 + 耗时 + 完整堆栈``（**异常原样抛出**，调用方行为
  一点不变）。函数体里不用写任何日志。
- **编号跟着整段代码走**：:class:`OperationFilter` 把当前编号写进每条日志记录的
  ``op_id``，日志格式里有 ``op=%(op_id)s``。于是"用户点了缓冲"之后，Python 侧、
  QGIS 消息日志、Qt 消息、崩溃 hook 打出来的**所有**行都带同一个 ``op=N``，出了事
  按编号整段捞出来即可，不会被其它并行动作的行插花。
- **快照必须先打码**：图层 ``source()`` 里可能带业务 key（XYZ 底图 URL 的 ``tk=``），
  目标库 dict 里可能带密码。键名表与正则都在 :mod:`app.infra.logging.redaction` 里
  ——诊断包导出用的是同一份，免得两边各补一半、从缝里漏凭据——**日志是会被回传的文件**。
- 快照本身绝不能拖慢或拖垮操作：单个值有长度上限，取不到的属性退化成 ``?``，
  任何内部异常都吞掉退化成 ``<类型名 记录失败>``。

同时导出的还有 :func:`current_op_id`（当前编号）和 :func:`attach_filter`
（给 handler 装编号注入，见 :mod:`app.infra.logging.sink`）。
"""

from __future__ import annotations

import contextlib
import functools
import inspect
import itertools
import logging
import time
from collections.abc import Callable, Iterator
from contextvars import ContextVar
from dataclasses import fields, is_dataclass
from pathlib import Path
from typing import Any

from .redaction import REDACTED, is_secret_key, redact

logger = logging.getLogger(__name__)

#: 进程内自增的操作编号（``op1`` / ``op2`` …）。重启归零：这个编号只在同一份
#: app.log 里要有意义，够用；不用 uuid 是为了人能在一堆行里一眼扫到它。
_op_counter = itertools.count(1)

#: 当前操作编号。默认 ``"-"``＝不在任何操作里（启动、界面刷新、QGIS 自己刷消息）。
_op_id: ContextVar[str] = ContextVar("qgis_dev_op_id", default="-")

#: 快照里忽略的形参名：``feedback`` 是 QgsProcessingFeedback，repr 只是一串地址
_SKIP_PARAMS = frozenset({"self", "cls", "feedback"})

#: 长度上限：单个值 / 容器与 dataclass 里的每项 / 整条快照 / dataclass 与 dict。
#: 嵌套上限放到 120：图层的完整描述（名字 / provider / 几何 / 坐标系 / 字段数 /
#: 源路径）就是六七十个字符，卡在 60 会把坐标系和字段数切掉——而那两项恰恰是
#: 排查"为什么写不进去"的关键。
#: dataclass 单独给 500：``BufferJob`` / ``CreateDataSpec`` 这种参数包是**唯一**
#: 说明"用户选了什么"的载体，实测按 200 卡会把尾部的 ``output_path`` 切掉——
#: 而"结果写到哪"正是失败时要看的。
_MAX_VALUE = 200
_MAX_NESTED = 120
_MAX_TOTAL = 900
_MAX_STRUCT = 500


def current_op_id() -> str:
    """当前操作编号；不在任何操作里时返回 ``"-"``。"""
    return _op_id.get()


class OperationFilter(logging.Filter):
    """把当前操作编号写进每条日志记录（格式串里的 ``op=%(op_id)s``）。

    **必须挂在 handler 上**：logger 上的 filter 只管直接发给它的记录，冒泡上来的
    记录不经过——挂在 root handler 上才能覆盖所有子 logger。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        record.op_id = _op_id.get()
        return True


_FILTER = OperationFilter()


def attach_filter(handler: logging.Handler) -> logging.Handler:
    """给 handler 装编号注入 filter（幂等）。返回原 handler，便于链式装配。"""
    if _FILTER not in handler.filters:
        handler.addFilter(_FILTER)
    return handler


def log_operation(name: str) -> Callable:
    """把被装饰函数的一次调用记成一条 OP 记录（装饰器）。

    :param name: 操作名（写进日志的"用户在做什么"，如 ``"缓冲区生成"``）。

    实参按**形参名**自动抓成快照（:data:`_SKIP_PARAMS` 里的名字跳过），入参快照在
    任何异常下都不会让函数调用失败。失败时 ``logger.error(..., exc_info=True)``
    写完整堆栈后**原样抛出**；用户取消 / Ctrl-C 这类 ``BaseException`` 记一条
    ``中断`` 的 warning，不打堆栈（那不是故障，别把 error.log 灌满）。

    .. note::
        接入点都是普通调用（对话框 / 动作处理函数里直接调服务），**不要**把装饰后
        的方法拿去 ``.connect()``：打包时 ``tools/protect_cython.py`` 会把本模块编译
        成 .pyd，而 Cython 编译后的函数与 PyQt 信号槽不兼容。
    """

    def _decorate(fn: Callable) -> Callable:
        try:
            sig: inspect.Signature | None = inspect.signature(fn)
        except (TypeError, ValueError):  # 拿不到签名（内置函数之类）→ 只记名字
            sig = None

        @functools.wraps(fn)
        def _wrapper(*args: Any, **kwargs: Any) -> Any:
            op_id = f"op{next(_op_counter)}"
            snapshot = _snapshot(_bind(sig, args, kwargs))
            started = time.monotonic()
            with _use(op_id):
                logger.info("OP %s 开始 %s | %s", op_id, name, snapshot)
                try:
                    result = fn(*args, **kwargs)
                except Exception as exc:  # 记完就原样抛，所以不算"盲捕"
                    logger.error(
                        "OP %s 失败 %s 耗时 %d ms | %s | %s: %s",
                        op_id,
                        name,
                        _elapsed_ms(started),
                        snapshot,
                        type(exc).__name__,
                        exc,
                        exc_info=True,
                    )
                    raise
                except BaseException:  # 取消 / Ctrl-C / SystemExit：不是故障
                    logger.warning(
                        "OP %s 中断 %s 耗时 %d ms | %s", op_id, name, _elapsed_ms(started), snapshot
                    )
                    raise
                logger.info("OP %s 完成 %s 耗时 %d ms", op_id, name, _elapsed_ms(started))
                return result

        return _wrapper

    return _decorate


# --------------------------------------------------------------------- 内部


@contextlib.contextmanager
def _use(op_id: str) -> Iterator[None]:
    """把当前操作编号设上，退出恢复（嵌套时恢复外层编号）。"""
    token = _op_id.set(op_id)
    try:
        yield
    finally:
        _op_id.reset(token)


def _bind(
    sig: inspect.Signature | None, args: tuple, kwargs: dict
) -> dict[str, Any]:
    """按形参名绑定实参；只收调用方真的传了的参数（默认值不补）。

    绑定失败（参数不匹配）返回空快照——真调用该抛就让它抛，快照不掺和。
    """
    if sig is None:
        return {}
    try:
        bound = sig.bind(*args, **kwargs)
    except TypeError:
        return {}
    return {k: v for k, v in bound.arguments.items() if k not in _SKIP_PARAMS}


def _elapsed_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def _snapshot(inputs: dict[str, Any]) -> str:
    """``{形参名: 值}`` → 一行短文本（超长截断）。"""
    if not inputs:
        return "(无入参)"
    parts: list[str] = []
    used = 0
    for key, value in inputs.items():
        text = f"{key}={_describe(value)}"
        if used + len(text) > _MAX_TOTAL:
            parts.append(f"…（其余 {len(inputs) - len(parts)} 项省略）")
            break
        parts.append(text)
        used += len(text) + 1
    return " ".join(parts)


def _describe(value: Any) -> str:
    """一个实参 → 短文本（打码 + 截断）。**任何失败都不抛**。"""
    try:
        return redact(_inner(value, _budget(value)))
    except Exception:  # noqa: BLE001 —— 记入参绝不能影响操作本身
        return f"<{type(value).__name__} 记录失败>"


def _budget(value: Any) -> int:
    """整段渲染的字符额度：参数包（dataclass / dict）给宽一点，单个值 200 够。"""
    if isinstance(value, dict) or (is_dataclass(value) and not isinstance(value, type)):
        return _MAX_STRUCT
    return _MAX_VALUE


def _inner(value: Any, limit: int) -> str:
    """按类型挑一种便宜又有信息量的写法。"""
    if value is None or isinstance(value, (bool, int, float)):
        return repr(value)
    if isinstance(value, (str, Path)):
        return _cut(str(value), limit)
    # QGIS 图层：要素数要扫库（网络数据源可能要几秒），故意不取
    if callable(getattr(value, "source", None)) and callable(getattr(value, "wkbType", None)):
        return _cut(_describe_layer(value), limit)
    # 坐标系：authid 就够（``EPSG:4490``）
    if callable(getattr(value, "authid", None)):
        return _cut(_describe_crs(value), limit)
    if is_dataclass(value) and not isinstance(value, type):
        return _cut(_describe_dataclass(value), limit)
    if isinstance(value, dict):
        return _cut(_describe_pairs(value.items()), limit)
    if isinstance(value, (list, tuple, set, frozenset)):
        return _cut(_describe_items(list(value)), limit)
    return _cut(repr(value), limit)


def _describe_layer(layer: Any) -> str:
    """``图层<名称> provider/几何/坐标系/字段N source=…``，取不到的项写 ``?``。

    故意紧凑：图层通常是被嵌在 job dataclass 里记的，太长会被外层截断，而
    "哪个文件 / 什么几何 / 什么坐标系 / 几个字段"是最该保住的部分。
    """
    crs = _call(layer, "crs")
    fields = _call(layer, "fields")
    where = _call(layer, "providerType") or "?"
    shape = _enum_name(_call(layer, "wkbType"))
    count = len(fields) if fields is not None else "?"
    return (
        f"图层<{_call(layer, 'name') or '?'}> {where}/{shape}/{_crs_authid(crs)}/字段{count} "
        f"source={_call(layer, 'source') or '?'}"
    )


def _describe_crs(crs: Any) -> str:
    """``CRS(EPSG:4490)``；只有描述时退回描述。"""
    return f"CRS({_crs_authid(crs)})"


def _describe_dataclass(obj: Any) -> str:
    """dataclass（如 ``BufferJob`` / ``CreateDataSpec``）逐字段展开。"""
    inner = ", ".join(f"{f.name}={_field(getattr(obj, f.name, None))}" for f in fields(obj))
    return f"{type(obj).__name__}({inner})"


def _describe_pairs(items: Any) -> str:
    """dict → ``{键=值, …}``；键名命中凭据表的（严格匹配）值整项打码。"""
    parts = []
    for key, value in items:
        text = REDACTED if is_secret_key(key) else _field(value)
        parts.append(f"{key}={text}")
    return "{" + ", ".join(parts) + "}"


def _describe_items(values: list) -> str:
    """list / tuple / set → 头 8 项，其余折成 ``…(+N)``。"""
    shown = ", ".join(_field(v) for v in values[:8])
    extra = f", …(+{len(values) - 8})" if len(values) > 8 else ""
    return f"[{shown}{extra}]"


def _field(value: Any) -> str:
    """容器 / dataclass 里的单个值：坏掉一个字段不该拖垮整条快照。"""
    try:
        return _inner(value, _MAX_NESTED)
    except Exception:  # noqa: BLE001
        return f"<{type(value).__name__}>"


def _crs_authid(crs: Any) -> str:
    if crs is None:
        return "?"
    return _call(crs, "authid") or _call(crs, "description") or "?"


def _enum_name(value: Any) -> str:
    """PyQt6 枚举取 ``.name``（``WkbType.Polygon`` → ``Polygon``）。"""
    if value is None:
        return "?"
    return getattr(value, "name", None) or str(value)


def _call(obj: Any, name: str) -> Any:
    """调 ``obj.name()``；属性不存在 / 抛异常都返回 ``None``（快照不能出意外）。"""
    try:
        return getattr(obj, name)()
    except Exception:  # noqa: BLE001
        return None


def _cut(text: str, limit: int) -> str:
    return text if len(text) <= limit else f"{text[:limit]}…(+{len(text) - limit})"


__all__ = [
    "OperationFilter",
    "attach_filter",
    "current_op_id",
    "log_operation",
]
