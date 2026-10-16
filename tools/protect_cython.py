#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Cython 原生编译保护（打包前置步骤）。

把 ``app`` 下的纯 Python 模块编译成原生扩展（Windows ``.pyd`` / Linux ``.so``），
并输出 manifest 供 ``tools/pyinstaller.spec`` 使用。

为什么这样设计
--------------
PyInstaller 分析阶段需要看到原始 ``.py`` 才能把 import 图收集完整（本项目大量
动态导入，靠 ``collect_submodules`` 兜底，一旦换成二进制分析就全丢了）。因此：

1. PyInstaller 仍以原始源码做分析（spec 不变）；
2. 本工具把目标模块编译成原生扩展，写进 manifest；
3. spec 读到 manifest 后，把这些模块的**字节码从 PYZ 里剔除**，只把 ``.pyd``
   作为二进制放进产物目录。

运行期 PyInstaller 的冻结导入器在 PYZ 找不到模块时会回退到磁盘 FileFinder
（见 ``PyInstaller/loader/pyimod02_importers.py`` 的 ``PyiFrozenFinder.find_spec``），
于是加载到 ``.pyd``。

效果：反编译产物时，被编译模块既没有 ``.py`` 也没有 ``.pyc``，只剩机器码；
其余模块仍走字节码（如需再加固，可在配置里扩大 ``include`` 范围）。

用法::

    python tools/protect_cython.py \
        --project-root . \
        --config config/app.yaml \
        --out build/protect \
        --manifest build/protect/manifest.json
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import shutil
import sys
import sysconfig
from pathlib import Path

# 编译期编译器选项：
#   binding=True            保留 __globals__ / __annotations__，FastAPI、pydantic、
#                           inspect.signature 才能正常内省（已验证 get_type_hints 可用）
#   annotation_typing=False 关键：Cython 默认把类型注解当成 C 类型约束，会拒编
#                           “注解与实际返回值不符”的代码（如 -> str 却 return dict），
#                           并可能静默改变运行语义。关掉后注解仅作静态提示，
#                           与原 CPython 行为完全一致。
COMPILER_DIRECTIVES = {
    "binding": True,
    "language_level": 3,
    "annotation_typing": False,
}


# --------------------------------------------------------------------------- #
# 配置 / 命令行
# --------------------------------------------------------------------------- #
DEFAULT_CONFIG = {
    "enabled": True,
    "include": ["app"],
    "exclude": [],
    "fail_open": True,
    "jobs": 0,
    "compiler_include": "",
    "compiler_libs": "",
    "extra_compiler_directives": {},
    # 模块级 __getattr__（PEP 562 惰性导出）在 Cython 下会与导入机产生
    # 无限递归（实测栈溢出），默认跳过编译，保持字节码。
    "skip_module_getattr": True,
    # 只编译不依赖 Qt/QGIS 的模块。Cython 编译后的函数/类与 PyQt 的
    # signal/slot 机制不兼容（.connect(编译方法) 会 TypeError 甚至硬崩溃
    # 0xC0000409），所以所有 import qgis / PyQt 的模块一律保持字节码。
    "qt_free_only": True,
}

# Qt / GIS 顶层包名：出现在 import 里就认为该模块与 Qt 绑定
_QT_TOPLEVEL = {"qgis", "PyQt5", "PyQt6", "PySide2", "PySide6"}


def _load_config(path: Path) -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if not path or not path.is_file():
        return cfg
    try:
        import yaml
    except Exception:
        return cfg
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        return cfg
    section = ((raw.get("build") or {}).get("protect") or {}).get("cython") or {}
    if not isinstance(section, dict):
        return cfg
    for key in cfg:
        if key in section and section[key] is not None:
            cfg[key] = section[key]
    return cfg


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Cython 原生编译保护")
    p.add_argument("--project-root", default=".")
    p.add_argument("--config", default="config/app.yaml")
    p.add_argument("--out", default="build/protect")
    p.add_argument("--manifest", default="build/protect/manifest.json")
    p.add_argument("--include", nargs="*", default=None, help="点分包/模块（覆盖配置）")
    p.add_argument("--exclude", nargs="*", default=None, help="跳过的点分包/模块（覆盖配置）")
    p.add_argument("--jobs", type=int, default=None)
    p.add_argument("--compiler-include", default=None)
    p.add_argument("--compiler-libs", default=None)
    p.add_argument("--no-fail-open", action="store_true", help="任一模块编译失败即中断")
    p.add_argument("--verbose", action="store_true")
    p.add_argument("--clean", action="store_true", help="先清空输出目录")
    return p.parse_args()


# --------------------------------------------------------------------------- #
# Python 开发文件（Include / libs）探测
# --------------------------------------------------------------------------- #
def _has_headers(inc: Path) -> bool:
    return bool(inc) and (inc / "Python.h").is_file()


def _header_minor_matches(inc: Path) -> bool:
    """头文件次版本必须与当前解释器一致，否则生成的是错版 ABI 的 C 代码。"""
    patchlevel = inc / "patchlevel.h"
    if not patchlevel.is_file():
        return True  # 无法判断时不否决（尽量兼容非标准布局）
    try:
        text = patchlevel.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return True
    m = re.search(r"#\s*define\s+PY_MINOR_VERSION\s+(\d+)", text)
    if not m:
        return True
    return int(m.group(1)) == sys.version_info.minor


def _has_import_lib(libs: Path) -> bool:
    if not libs or not libs.is_dir():
        return False
    if sys.platform == "win32":
        # 必须精确匹配当前次版本（python312.lib），否则链接到别的 ABI
        return (libs / f"python{sys.version_info.major}{sys.version_info.minor}.lib").is_file()
    return True  # Linux 无需单独的 import lib，distutils 走 sysconfig


def _sibling_python_dirs() -> list[Path]:
    """在同类 OSGeo4W / conda 安装里找带开发文件的 Python 目录。"""
    found: list[Path] = []
    roots = []
    try:
        prefix = Path(sys.base_exec_prefix)
        # 依次向上找最多 4 层（qgis4.2/apps/Python312 -> apps -> qgis4.2 -> <sdk root>）
        node = prefix
        for _ in range(4):
            node = node.parent
            if len(node.parts) < 3:  # 别扫到 C:\ 这种
                break
            roots.append(node)
    except Exception:
        pass
    ver_tag = f"Python{sys.version_info.major}{sys.version_info.minor}"
    seen: set[str] = set()
    for root in roots:
        if not root.is_dir() or str(root) in seen:
            continue
        seen.add(str(root))
        try:
            children = list(root.iterdir())
        except OSError:
            continue
        for child in children:
            if not child.is_dir():
                continue
            for cand in (
                child,
                child / "apps" / ver_tag,
                child / "apps" / "Python312",
            ):
                cand_inc = cand / "Include"
                if _has_headers(cand_inc) and _header_minor_matches(cand_inc):
                    found.append(cand)
    return found


def resolve_dev_files(explicit_inc: str, explicit_libs: str) -> tuple[Path, Path]:
    """返回 (Include 目录, libs 目录)。找不到直接报错退出，避免"编到一半全挂"。"""
    sys_inc = Path(sysconfig.get_path("include") or "")
    sys_libs = Path(sys.base_exec_prefix) / "libs"

    candidates: list[tuple[Path, Path]] = []
    if explicit_inc:
        candidates.append((Path(explicit_inc), Path(explicit_libs) if explicit_libs else sys_libs))
    env_inc = os.environ.get("QGIS_PYTHON_INCLUDE")
    env_libs = os.environ.get("QGIS_PYTHON_LIBS")
    if env_inc:
        candidates.append((Path(env_inc), Path(env_libs) if env_libs else sys_libs))
    candidates.append((sys_inc, sys_libs))
    for sib in _sibling_python_dirs():
        candidates.append((sib / "Include", sib / "libs"))

    for inc, libs in candidates:
        try:
            ok_inc = _has_headers(inc) and _header_minor_matches(inc)
            ok_lib = _has_import_lib(libs)
        except Exception:
            continue
        if ok_inc and ok_lib:
            return inc.resolve(), libs.resolve()

    raise SystemExit(
        "找不到 Python 开发文件（Python.h / python3xx.lib）。\n"
        f"  当前解释器: {sys.executable}\n"
        f"  sysconfig include: {sys_inc}\n"
        "请任选其一：\n"
        "  1) 在 config/app.yaml 的 build.protect.cython.compiler_include / compiler_libs 指定；\n"
        "  2) 设置环境变量 QGIS_PYTHON_INCLUDE / QGIS_PYTHON_LIBS；\n"
        "  3) 安装 Python 开发包（OSGeo4W: python3-devel，或把完整安装的 Include/ 与 libs/ 拷进来）。"
    )


# --------------------------------------------------------------------------- #
# 模块发现
# --------------------------------------------------------------------------- #
def _dotted(rel: Path) -> str:
    parts = list(rel.with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _names_for(py_file: Path, root: Path) -> tuple[str, str]:
    """返回 (PyInstaller 模块名, Cython 扩展名)。

    ``app/bootstrap/__init__.py`` -> ("app.bootstrap", "app.bootstrap.__init__")
    ``app/core/auth/_secret.py``  -> ("app.core.auth._secret", "app.core.auth._secret")

    Cython 用扩展名的最后一段作为产物文件名，所以包初始化必须显式落成
    ``__init__``，否则会生成 ``bootstrap.pyd`` 而不是 ``__init__.pyd``。
    """
    rel = py_file.relative_to(root).with_suffix("")
    module = _dotted(rel)
    if rel.name == "__init__":
        return module, module + ".__init__"
    return module, module


def _excluded(module: str, exclude_prefixes: list[str]) -> bool:
    for pref in exclude_prefixes:
        if module == pref or module.startswith(pref + "."):
            return True
    return False


def _defines_module_getattr(py_file: Path) -> bool:
    """模块顶层是否定义了 ``def __getattr__``（PEP 562 惰性导出）。"""
    try:
        tree = ast.parse(py_file.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return False
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "__getattr__":
            return True
    return False


def _imports_qt(py_file: Path) -> bool:
    """模块（含函数内部）是否 import 了 qgis / PyQt*。解析失败时保守返回 True。"""
    try:
        tree = ast.parse(py_file.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return True
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] in _QT_TOPLEVEL:
                    return True
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module.split(".")[0] in _QT_TOPLEVEL:
                return True
    return False


def discover(
    root: Path,
    include: list[str],
    exclude: list[str],
    skip_module_getattr: bool = True,
    qt_free_only: bool = True,
) -> tuple[list[Path], list[Path], list[Path]]:
    """返回 (待编译, 因模块级 __getattr__ 跳过, 因依赖 Qt 跳过)。"""
    files: set[Path] = set()
    for inc in include:
        target = root / Path(*inc.split("."))
        if target.is_dir():
            files.update(p for p in target.rglob("*.py") if "__pycache__" not in p.parts)
        elif target.with_suffix(".py").is_file():
            files.add(target.with_suffix(".py"))
        else:
            print(f"  [warn] include 目标不存在，跳过: {inc}")
    result, skipped_attr, skipped_qt = [], [], []
    for f in sorted(files):
        if _excluded(_dotted(f.relative_to(root)), exclude):
            continue
        if skip_module_getattr and _defines_module_getattr(f):
            skipped_attr.append(f)
            continue
        if qt_free_only and _imports_qt(f):
            skipped_qt.append(f)
            continue
        result.append(f)
    return result, skipped_attr, skipped_qt


# --------------------------------------------------------------------------- #
# 编译
# --------------------------------------------------------------------------- #
def _ext_suffix() -> str:
    from importlib.machinery import EXTENSION_SUFFIXES

    return EXTENSION_SUFFIXES[0]


def build_batch(
    batch: list[Path],
    root: Path,
    out: Path,
    tmp: Path,
    inc: Path,
    libs: Path,
    jobs: int,
    directives: dict,
    verbose: bool,
) -> list[Path]:
    """编译一批模块，返回产物路径。失败抛异常。"""
    from setuptools import Distribution, Extension
    from Cython.Build import cythonize

    ext_meta: dict[str, tuple[str, Path]] = {}
    exts = []
    for py in batch:
        module, ext_name = _names_for(py, root)
        ext_meta[ext_name] = (module, py)
        exts.append(
            Extension(
                ext_name,
                [str(py)],
                include_dirs=[str(inc)] if inc else [],
                library_dirs=[str(libs)] if libs else [],
            )
        )

    ext_modules = cythonize(
        exts,
        language_level=3,
        compiler_directives=directives,
        build_dir=str(tmp / "cy"),
        quiet=not verbose,
        nthreads=jobs if jobs and jobs > 1 else 0,
    )

    dist = Distribution({"ext_modules": ext_modules})
    cmd = dist.get_command_obj("build_ext")
    cmd.build_lib = str(out)
    cmd.build_temp = str(tmp / "obj")
    cmd.inplace = False
    if jobs and jobs > 1:
        cmd.parallel = jobs
    cmd.ensure_finalized()
    cmd.run()

    suffix = _ext_suffix()
    produced: list[Path] = []
    for _, py in ext_meta.values():
        module, _ = _names_for(py, root)
        parts = module.split(".")
        # app.bootstrap -> out/app/bootstrap/__init__<suffix>
        # app.core.x    -> out/app/core/x<suffix>
        if py.stem == "__init__":
            parent = out / Path(*parts)
            base = "__init__"
        else:
            parent = out / Path(*parts[:-1])
            base = parts[-1]
        cands = sorted(parent.glob(base + ".*")) if parent.is_dir() else []
        pyd = [c for c in cands if c.name.endswith(suffix)]
        if not pyd:
            raise RuntimeError(f"未生成编译产物: {module} (期望 {parent / (base + suffix)})")
        produced.append(pyd[0])
    return produced


def compile_all(
    files: list[Path],
    root: Path,
    out: Path,
    tmp: Path,
    inc: Path,
    libs: Path,
    jobs: int,
    directives: dict,
    verbose: bool,
    fail_open: bool,
) -> tuple[dict[str, Path], dict[str, str]]:
    """带二分的编译：单个模块失败不影响其他模块（fail_open 时）。"""
    ok: dict[str, Path] = {}
    failed: dict[str, str] = {}

    def rec(batch: list[Path], depth: int = 0) -> None:
        if not batch:
            return
        try:
            produced = build_batch(batch, root, out, tmp, inc, libs, jobs, directives, verbose)
            for py, p in zip(batch, produced):
                ok[_names_for(py, root)[0]] = p
            return
        except Exception as exc:  # noqa: BLE001 - 逐模块定位失败原因
            if len(batch) == 1:
                module = _names_for(batch[0], root)[0]
                failed[module] = f"{type(exc).__name__}: {exc}"
                print(f"  [fail] {module}: {type(exc).__name__}: {exc}")
                if not fail_open:
                    raise
                return
            if depth == 0:
                print(f"  [warn] 批次编译失败（{len(batch)} 个），二分定位…")
            mid = len(batch) // 2
            rec(batch[:mid], depth + 1)
            rec(batch[mid:], depth + 1)

    rec(files)
    return ok, failed


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def main() -> int:
    args = parse_args()
    root = Path(args.project_root).resolve()
    cfg = _load_config(root / args.config)

    include = args.include if args.include is not None else list(cfg["include"])
    exclude = args.exclude if args.exclude is not None else list(cfg["exclude"])
    jobs = args.jobs if args.jobs is not None else int(cfg["jobs"] or 0)
    fail_open = bool(cfg["fail_open"]) and not args.no_fail_open
    inc_arg = args.compiler_include if args.compiler_include is not None else cfg["compiler_include"]
    libs_arg = args.compiler_libs if args.compiler_libs is not None else cfg["compiler_libs"]

    directives = dict(COMPILER_DIRECTIVES)
    directives.update(cfg.get("extra_compiler_directives") or {})

    out = (root / args.out).resolve() if not Path(args.out).is_absolute() else Path(args.out)
    manifest_path = (
        (root / args.manifest).resolve() if not Path(args.manifest).is_absolute() else Path(args.manifest)
    )

    print("=" * 60)
    print("Cython 原生编译保护")
    print("=" * 60)
    print(f"  项目根   : {root}")
    print(f"  编译范围 : {', '.join(include) or '(空)'}")
    print(f"  跳过     : {', '.join(exclude) or '(无)'}")
    print(f"  输出     : {out}")

    if not (cfg["enabled"] or args.include is not None):
        print("  [skip] build.protect.cython.enabled = false，写出空 manifest")
        _write_manifest(manifest_path, {}, {}, inc_arg, libs_arg, out)
        return 0

    if not include:
        print("  [skip] 编译范围为空，直接写出空 manifest")
        files, skipped_attr, skipped_qt = [], [], []
    else:
        files, skipped_attr, skipped_qt = discover(
            root, include, exclude, bool(cfg["skip_module_getattr"]), bool(cfg["qt_free_only"])
        )

    if not files:
        _write_manifest(manifest_path, {}, {}, inc_arg, libs_arg, out)
        print("  无可编译模块")
        return 0

    try:
        import Cython  # noqa: F401
    except ImportError:
        raise SystemExit("未安装 Cython，无法进行原生编译保护：pip install Cython（或用 -NoProtect 跳过）")

    if args.clean and out.exists():
        shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True, exist_ok=True)
    tmp = out / "_build"
    tmp.mkdir(parents=True, exist_ok=True)

    inc, libs = resolve_dev_files(inc_arg, libs_arg)
    print(f"  头文件   : {inc}")
    print(f"  链接库   : {libs}")
    print(f"  并发     : {jobs or 'auto'}")
    print(f"  待编译   : {len(files)} 个模块")
    if skipped_attr:
        print(f"  跳过(模块级__getattr__, Cython 下会递归): {len(skipped_attr)}")
        for s in skipped_attr:
            print(f"    - {_dotted(s.relative_to(root))}")
    if skipped_qt:
        print(f"  跳过(依赖 Qt/QGIS, PyQt 信号槽与 Cython 不兼容): {len(skipped_qt)}")
    print("-" * 60)

    ok, failed = compile_all(
        files, root, out, tmp, inc, libs, jobs, directives, args.verbose, fail_open
    )

    _write_manifest(manifest_path, ok, failed, str(inc), str(libs), out)
    print("-" * 60)
    print(f"  成功: {len(ok)}    失败: {len(failed)}")
    print(f"  manifest: {manifest_path}")
    if failed:
        print("  失败模块（已回退为字节码，不中断构建）:")
        for name in sorted(failed):
            print(f"    - {name}")
    return 0


def _write_manifest(
    manifest_path: Path,
    ok: dict[str, Path],
    failed: dict[str, str],
    inc: str,
    libs: str,
    out: Path,
) -> None:
    entries = []
    for module, path in sorted(ok.items()):
        try:
            dest = path.relative_to(out).as_posix()
        except ValueError:
            dest = path.name
        entries.append(
            {
                "module": module,          # PyInstaller 里的点分模块名（用于从 PYZ 剔除）
                "dest": dest,              # 产物内的相对路径（COLLECT 落盘位置）
                "path": str(path),         # 编译产物绝对路径
            }
        )
    payload = {
        "generated_by": "tools/protect_cython.py",
        "python": f"{sys.version_info.major}.{sys.version_info.minor}",
        "abi_suffix": _ext_suffix() if ok else "",
        "compiler_include": inc,
        "compiler_libs": libs,
        "native": entries,
        "failed": [{"module": k, "error": v} for k, v in sorted(failed.items())],
    }
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
