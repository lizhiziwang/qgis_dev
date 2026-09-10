"""检查函数体内的 ``import`` 是否只用于重依赖 / 循环依赖（见 docs/coding-standards.md §4）.

规则
----
函数体内的 ``import`` **仅允许**下列两类：

1. 重依赖：``qgis.*`` / ``qgis.PyQt.*`` / ``osgeo.*`` / ``processing*`` /
   ``rasterio`` / ``psycopg2`` 等，或其目标 ``app`` 模块自身（传递地）依赖这些
   重依赖——顶层导入会让无 QGIS / GDAL 运行时的环境（``tests/api``、
   ``tests/core``）连模块都 import 不了。
2. 确属无法避免的项目内循环依赖；此时必须紧邻写
   ``# noqa: PLC0415 - <为什么不能上提>``。

其余（标准库、轻量三方库、一般 ``app.*`` 模块）一律放模块顶层，否则视为违规。
违规行若带 ``# noqa: PLC0415 - <原因>`` 也放行——那就是规范里给出的显式逃生口。

用法
----
    python scripts/check_import_placement.py            # 默认检查 app/
    python scripts/check_import_placement.py app tests  # 指定目录
退出码 1 表示存在违规。``tests/`` 默认不检查：测试常在函数内延迟导入以隔离
QGIS 收集期副作用，属既有约定。
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

# 重依赖的顶层包名：这些包（或其依赖它们的 app 模块）必须延迟导入。
HEAVY_ROOTS = (
    "qgis",
    "osgeo",
    "processing",
    "PyQt5",
    "PyQt6",
    "psycopg2",
    "rasterio",
    "scipy",
    "networkx",
)
_NOQA_RE = re.compile(r"#\s*noqa:(?P<codes>[^\n#]*)")
_REASON_RE = re.compile(r"PLC0415\s*-\s*\S")


def _module_name(path: Path, base: Path) -> str:
    parts = list(path.relative_to(base).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _package_of(module: str, path: Path) -> str:
    return module if path.name == "__init__.py" else ".".join(module.split(".")[:-1])


def _from_targets(module: str, path: Path, node: ast.ImportFrom) -> list[str]:
    """把 ``ImportFrom``（含相对导入、``from . import X``）解析成绝对点分目标."""
    if node.level == 0:
        return [node.module] if node.module else []
    pkg_parts = _package_of(module, path).split(".")
    up = node.level - 1
    base = pkg_parts[: len(pkg_parts) - up] if up else pkg_parts
    if node.module:
        return [".".join([*base, node.module])]
    return [".".join([*base, alias.name]) for alias in node.names]


def _top_deps(path: Path, module: str, files: dict[str, Path]) -> set[str]:
    """模块顶层的依赖集合；重依赖记为 ``__HEAVY__``，app 模块归一化为已知模块名."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    deps: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.If) and getattr(node.test, "id", "") == "TYPE_CHECKING":
            continue
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root in HEAVY_ROOTS:
                    deps.add("__HEAVY__")
                elif alias.name.startswith("app"):
                    deps.add(_normalize(alias.name, files))
        elif isinstance(node, ast.ImportFrom):
            for target in _from_targets(module, path, node):
                if target.split(".")[0] in HEAVY_ROOTS:
                    deps.add("__HEAVY__")
                elif target.startswith("app"):
                    deps.add(_normalize(target, files))
    deps.discard("")
    return deps


def _normalize(target: str, files: dict[str, Path]) -> str:
    """把可能指向子模块/符号的点分名归一化到最近的已知模块."""
    if target in files:
        return target
    parts = target.split(".")
    for i in range(len(parts), 1, -1):
        candidate = ".".join(parts[:i])
        if candidate in files:
            return candidate
    return ""


def _heavy_modules(graph: dict[str, set[str]]) -> set[str]:
    memo: dict[str, bool] = {}

    def is_heavy(module: str, stack: frozenset[str] = frozenset()) -> bool:
        if module in memo:
            return memo[module]
        if module in stack:
            return False
        if "__HEAVY__" in graph.get(module, set()):
            memo[module] = True
            return True
        result = any(is_heavy(dep, stack | {module}) for dep in graph.get(module, set()) if dep)
        memo[module] = result
        return result

    return {module for module in graph if is_heavy(module)}


def _reachable(start: str, graph: dict[str, set[str]]) -> set[str]:
    seen: set[str] = set()
    stack = list(graph.get(start, set()))
    while stack:
        node = stack.pop()
        if node in seen:
            continue
        seen.add(node)
        stack.extend(graph.get(node, set()))
    return seen


def _marker(lines: list[str], node: ast.AST) -> tuple[bool, bool]:
    """返回 (是否有 PLC0415 标记, 是否带原因)."""
    text = "\n".join(lines[node.lineno - 1 : node.end_lineno or node.lineno])
    codes = _NOQA_RE.search(text)
    if not codes or "PLC0415" not in codes.group("codes"):
        return False, False
    return True, bool(_REASON_RE.search(text))


def _function_scoped_imports(tree: ast.AST) -> set[int]:
    scope: set[int] = set()
    for fn in (n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))):
        scope.update(id(child) for child in ast.walk(fn))
    return scope


def check(paths: list[Path]) -> int:
    base = Path.cwd().resolve()
    files: dict[str, Path] = {}
    for root in paths:
        for path in root.rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            files[_module_name(path.resolve(), base)] = path

    graph = {module: _top_deps(path, module, files) for module, path in files.items()}
    heavy = _heavy_modules(graph)

    violations: list[str] = []
    for module, path in sorted(files.items()):
        lines = path.read_text(encoding="utf-8").splitlines()
        tree = ast.parse("\n".join(lines))
        scope = _function_scoped_imports(tree)
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Import, ast.ImportFrom)) or id(node) not in scope:
                continue
            allowed = False
            circular = False
            if isinstance(node, ast.Import):
                allowed = any(alias.name.split(".")[0] in HEAVY_ROOTS for alias in node.names)
            else:
                for target in _from_targets(module, path, node):
                    root = target.split(".")[0]
                    if root in HEAVY_ROOTS:
                        allowed = True
                        continue
                    normalized = _normalize(target, files)
                    if not normalized:
                        continue
                    if module == normalized or module in _reachable(normalized, graph):
                        circular = True
                    elif normalized in heavy and module not in heavy:
                        allowed = True
            has_marker, has_reason = _marker(lines, node)
            if has_marker and has_reason:
                continue
            if has_marker and not has_reason:
                violations.append(
                    f"{path}:{node.lineno}: PLC0415 标记缺少原因，应写成 `# noqa: PLC0415 - <为什么不能上提>`"
                )
                continue
            if allowed and not circular:
                continue
            kind = "循环依赖" if circular else "轻量导入"
            violations.append(
                f"{path}:{node.lineno}: 函数体内{kind}，应移到模块顶层（确需保留请加 `# noqa: PLC0415 - <原因>`）"
            )

    if violations:
        print("函数体内 import 违规（docs/coding-standards.md §4）：")
        for item in violations:
            print(f"  {item}")
        print(f"\n共 {len(violations)} 处违规。")
        return 1
    print(f"检查 {len(files)} 个模块：函数体内 import 均属重依赖/已标注豁免。")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    roots = [Path(arg) for arg in args] or [Path("app")]
    missing = [str(root) for root in roots if not root.is_dir()]
    if missing:
        print(f"目录不存在：{', '.join(missing)}")
        return 2
    return check(roots)


if __name__ == "__main__":
    raise SystemExit(main())
