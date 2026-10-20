"""训练子进程（由训练页用外部 Python 启动）。

用法::

    python -u train_sidecar.py --manifest <run 目录>/manifest.json

stdout **只**走单行 JSON 协议（``hello`` / ``log`` / ``started`` / ``progress`` /
``epoch`` / ``done`` / ``stopped`` / ``error``），第三方库的打印被重定向到 stderr。
GUI 通过 manifest 所在目录（run 目录）下的 ``stop.request`` 文件请求停止
（当前轮结束后保存 ``last.pt``），训练循环轮询该文件；**不**在后台线程阻塞读
stdin（Windows + Python 3.10 下管道读线程会与后续 DLL 加载死锁）。

本脚本不导入 QGIS / PyQt / ``app.*``；``trainlib`` 与本文件同目录，可直接被
外部 Python（torch / torchvision / SMP）执行。
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
import traceback
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from trainlib import common  # noqa: E402

# 控制台可能是 GBK / cp936；协议固定 UTF-8，避免中文日志乱码或编码异常
for _stream in (sys.stdout, sys.stderr):
    with contextlib.suppress(AttributeError, ValueError):
        _stream.reconfigure(encoding="utf-8", errors="replace")


def _emit_error(error_type: str, message: str, exc: BaseException | None = None) -> None:
    if exc is not None:
        traceback.print_exc(file=sys.stderr)
    common.emit("error", error_type=error_type, message=message)
    common.log(f"训练失败：{message}")


def _classify_error(exc: BaseException) -> tuple[str, str]:
    text = str(exc)
    if isinstance(exc, ImportError):
        missing = getattr(exc, "name", "") or text
        return (
            "import_error",
            f"训练环境缺少依赖：{missing}。请点工具栏「安装训练依赖」，"
            f"或手动执行 python -m pip install segmentation-models-pytorch",
        )
    if isinstance(exc, RuntimeError) and "out of memory" in text.lower():
        return (
            "cuda_oom",
            "显存不足：请减小批大小或图像尺寸，或在训练参数里把设备改为 CPU 后重试",
        )
    if isinstance(exc, ValueError):
        return "invalid", text
    return "internal", f"{type(exc).__name__}: {text}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="QGIS 深度学习训练子进程")
    parser.add_argument("--manifest", required=True, help="manifest.json 路径")
    args = parser.parse_args(argv)

    common.emit("hello", version=1, pid=os.getpid())

    try:
        with open(args.manifest, encoding="utf-8") as handle:
            manifest = json.load(handle)
    except Exception as exc:  # noqa: BLE001
        _emit_error("invalid", f"无法读取训练清单：{exc}", exc)
        return 2

    out_dir = Path(manifest.get("output_dir") or Path(args.manifest).parent)
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        _emit_error("invalid", f"无法创建输出目录：{exc}", exc)
        return 2
    common.set_log_file(out_dir / "train.log")
    common.set_stop_file(Path(args.manifest).parent / common.STOP_REQUEST_NAME)
    common.log(f"训练子进程启动（Python {sys.version.split()[0]}，{args.manifest}）")

    # 从这里开始第三方库（tqdm / torch / SMP）的 print 全部进 stderr
    sys.stdout = sys.stderr
    try:
        from trainlib.engine import run_training

        return int(run_training(manifest) or 0)
    except ImportError as exc:
        error_type, message = _classify_error(exc)
        _emit_error(error_type, message, exc)
        return 3
    except Exception as exc:  # noqa: BLE001 —— 子进程边界，必须转成协议错误
        error_type, message = _classify_error(exc)
        _emit_error(error_type, message, exc)
        return 1
    finally:
        common.log("训练子进程退出")


if __name__ == "__main__":
    sys.exit(main())
