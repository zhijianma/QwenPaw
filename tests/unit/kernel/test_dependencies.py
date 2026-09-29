# -*- coding: utf-8 -*-
"""Architecture boundary tests for the shared kernel package."""

import ast
from pathlib import Path

import qwenpaw.kernel


FORBIDDEN_EXTERNAL_PREFIXES = (
    "agentscope",
    "fastapi",
    "sqlalchemy",
    "sqlite3",
)

FORBIDDEN_FILESYSTEM_MODULES = frozenset({"os", "pathlib", "shutil"})


def _is_forbidden(module: str) -> bool:
    """Reject product, framework, persistence, and filesystem imports."""
    if module in FORBIDDEN_FILESYSTEM_MODULES:
        return True
    if module == "qwenpaw" or module.startswith("qwenpaw."):
        return not (
            module == "qwenpaw.kernel" or module.startswith("qwenpaw.kernel.")
        )
    return module.startswith(FORBIDDEN_EXTERNAL_PREFIXES)


def test_kernel_has_no_product_or_framework_dependencies() -> None:
    kernel_dir = Path(qwenpaw.kernel.__file__).parent
    violations: list[str] = []

    for path in sorted(kernel_dir.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if _is_forbidden(alias.name):
                        violations.append(f"{path.name}:{node.lineno}")
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if node.level == 0 and _is_forbidden(module):
                    violations.append(f"{path.name}:{node.lineno}")

    assert not violations
