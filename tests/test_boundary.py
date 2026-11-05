"""The daemon boundary: front ends and the data CLI reach data only through DaemonClient."""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import findata

PACKAGE = Path(findata.__file__).parent
CORE = (
    "db",
    "security",
    "plaid_gateway",
    "sync",
    "linking",
    "waiting",
    "queries",
    "schema",
    "runtime",
    "contexts",
    "config",
)
# These three run or set up the core, and import it lazily inside their functions.
CORE_USERS = {
    PACKAGE / "cli" / "commands" / "daemon.py",
    PACKAGE / "cli" / "commands" / "setup.py",
    PACKAGE / "cli" / "commands" / "migrate.py",
}


def _is_core(module: str) -> bool:
    return any(
        module == f"findata.{name}" or module.startswith(f"findata.{name}.") for name in CORE
    )


def _imports(path: Path) -> list[tuple[int, str]]:
    """Every module a file imports, at any depth, as an absolute dotted name."""
    parts = ["findata", *path.relative_to(PACKAGE).with_suffix("").parts]
    # The package relative imports start from; an __init__.py's parts already end in "__init__".
    package = parts[:-1]
    found: list[tuple[int, str]] = []
    for node in ast.walk(ast.parse(path.read_text(), filename=str(path))):
        if isinstance(node, ast.Import):
            found += [(node.lineno, a.name) for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = package[: len(package) - (node.level - 1)]
                module = ".".join([*base, *([node.module] if node.module else [])])
            else:
                module = node.module or ""
            found.append((node.lineno, module))
            # `from findata import db` names a module through the imported name.
            found += [(node.lineno, f"{module}.{a.name}") for a in node.names]
    return found


def test_front_ends_and_data_cli_never_import_core_modules():
    files = [
        p
        for area in ("web", "mcp", "client", "cli")
        for p in (PACKAGE / area).rglob("*.py")
        if p not in CORE_USERS
    ]
    assert len(files) > 15  # the walk found the front ends
    offenders = [
        f"{path.relative_to(PACKAGE)}:{line} imports {module}"
        for path in sorted(files)
        for line, module in _imports(path)
        if _is_core(module)
    ]
    assert not offenders, "\n".join(offenders)


def test_cli_import_does_not_load_core_modules():
    code = (
        "import sys, findata.cli\n"
        f"core = {CORE!r}\n"
        "loaded = [m for m in sys.modules if any(m == 'findata.' + n or "
        "m.startswith('findata.' + n + '.') for n in core)]\n"
        "print(','.join(sorted(loaded)))\n"
    )
    result = subprocess.run(  # noqa: S603 - our own interpreter, fixed code
        [sys.executable, "-c", code], capture_output=True, text=True, check=True, timeout=60
    )
    loaded = result.stdout.strip()
    assert loaded == "", f"core modules loaded by importing findata.cli: {loaded}"
