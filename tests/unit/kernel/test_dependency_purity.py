# -*- coding: utf-8 -*-
"""Architecture gate keeping the Kernel independent from product layers."""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).parents[3]
_KERNEL_ROOT = _PROJECT_ROOT / "src" / "qwenpaw" / "kernel"
_ALLOWED_EXTERNAL_ROOTS = frozenset({"pydantic"})


def _dependency_violations(path: Path, source: str) -> tuple[str, ...]:
    """Return stable diagnostics for imports crossing the Kernel boundary."""
    tree = ast.parse(source, filename=str(path))
    violations: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.level == 1:
                continue
            if node.level > 1:
                violations.append(
                    f"{path.name}:{node.lineno}: relative import exits kernel",
                )
                continue
            root = (node.module or "").partition(".")[0]
            if root not in sys.stdlib_module_names and (
                root not in _ALLOWED_EXTERNAL_ROOTS
            ):
                violations.append(
                    f"{path.name}:{node.lineno}: forbidden import "
                    f"{node.module}",
                )
        elif isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.partition(".")[0]
                if root not in sys.stdlib_module_names and (
                    root not in _ALLOWED_EXTERNAL_ROOTS
                ):
                    violations.append(
                        f"{path.name}:{node.lineno}: forbidden import "
                        f"{alias.name}",
                    )
    return tuple(sorted(violations))


def test_kernel_imports_only_domain_stdlib_and_pydantic() -> None:
    """Product, framework, persistence, and plugin layers stay downstream."""
    violations = []
    for path in sorted(_KERNEL_ROOT.rglob("*.py")):
        violations.extend(
            _dependency_violations(
                path.relative_to(_PROJECT_ROOT),
                path.read_text(encoding="utf-8"),
            ),
        )

    assert not violations, "\n".join(violations)


@pytest.mark.parametrize(
    ("source", "expected"),
    (
        (
            "from qwenpaw.app import workspace\n",
            "forbidden import qwenpaw.app",
        ),
        ("import fastapi\n", "forbidden import fastapi"),
        ("from ..tasks import service\n", "relative import exits kernel"),
    ),
)
def test_dependency_gate_rejects_reverse_dependencies(
    source: str,
    expected: str,
) -> None:
    """The gate itself detects absolute and relative boundary violations."""
    [violation] = _dependency_violations(Path("fixture.py"), source)
    assert expected in violation
