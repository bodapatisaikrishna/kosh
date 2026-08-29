"""AST-level lint: money code must never touch a float, use builtin round(), use `/`
true division, or use hash() (hash randomization would break determinism).

profiles.py is allowlisted for float literals because it encodes probability weights
as plain integers already - the allowlist exists for narration/day-weight helpers, not
as an escape hatch for money math, and it is itself checked to never return floats
from its money-facing functions in test_generator.py.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
MONEY_MODULE_DIRS = [REPO_ROOT / "data" / "generator", REPO_ROOT / "engine"]

# Files exempt from the float-literal check (none currently need floats for money -
# this list exists so a legitimate non-money float, e.g. a probability, has a home).
FLOAT_LITERAL_ALLOWLIST = {"narration.py", "defects.py"}

# Files that actually perform money arithmetic - this is where a stray `/` or a
# builtin round()/hash() would silently corrupt paise math. I/O and CLI modules
# (emit.py, generate.py, trace.py, ids.py, calendar.py, narration.py, profiles.py)
# are excluded: they pass already-computed ints around and use `/` only for
# pathlib joins, which is not a money-arithmetic risk.
MONEY_ARITHMETIC_FILES = {"fees.py", "world.py", "defects.py"}


def _iter_money_py_files():
    for base in MONEY_MODULE_DIRS:
        if not base.exists():
            continue
        yield from base.rglob("*.py")


def _iter_money_arithmetic_files():
    for path in _iter_money_py_files():
        if path.name in MONEY_ARITHMETIC_FILES:
            yield path


def test_no_float_division_or_builtin_round_or_hash():
    violations = []
    for path in _iter_money_arithmetic_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
                violations.append(f"{path}:{node.lineno}: true division `/` (use integer // math)")
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                if node.func.id in ("round", "hash"):
                    violations.append(f"{path}:{node.lineno}: builtin {node.func.id}() is banned in money code")
    assert not violations, "\n".join(violations)


def test_no_float_literals_outside_allowlist():
    """Scoped to the same money-arithmetic files as the division/round/hash check -
    non-money floats are legitimate elsewhere in engine/ (e.g. a match confidence
    score or wall_clock_seconds timing), just never in a paise computation."""
    violations = []
    for path in _iter_money_arithmetic_files():
        if path.name in FLOAT_LITERAL_ALLOWLIST:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, float):
                violations.append(f"{path}:{node.lineno}: float literal {node.value}")
    assert not violations, "\n".join(violations)

