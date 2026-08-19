"""Static drift guard for the tutorials.

The notebooks' only other CI gate is nbstripout, a formatting check that cannot
see API drift, and the gen_full_* scripts are not imported by anything. This
test parses every tutorial code cell and script and binds each call to a public
SynnoDB entry point against the live signature, so a parameter rename (like
``queries`` -> ``query_subset``) fails CI instead of shipping a broken tutorial.
No cell or script is executed: everything is AST plus ``inspect``.
"""

import ast
import dataclasses
import inspect
import json
from pathlib import Path

import pytest

import synnodb
from synnodb import SynnoDB
from synnodb.api import SynnoConfig

REPO_ROOT = Path(__file__).resolve().parent.parent
NOTEBOOKS = sorted((REPO_ROOT / "tutorials").glob("*.ipynb"))
SCRIPTS = sorted((REPO_ROOT / "tutorials").glob("gen_full_*.py"))

# SynnoDB.__init__ forwards settings into SynnoConfig, so the config dataclass
# is the authority for constructor keywords alongside the explicit parameters.
_CONSTRUCTOR_KWARGS = set(inspect.signature(SynnoDB.__init__).parameters) | {
    f.name for f in dataclasses.fields(SynnoConfig)
}

# Methods whose tutorial call sites are checked: keyword names must exist on the
# current signature. Positional args are not checked (tutorials pass artifacts).
_METHODS = {
    name: set(inspect.signature(getattr(SynnoDB, name)).parameters)
    for name in (
        "in_memory",
        "on_ssd",
        "from_env",
        "with_",
        "sync_from_duckdb",
        "createStoragePlan",
        "createBaseImpl",
        "runOptimLoop",
        "addMultiThreading",
        "checkSfCorrectness",
        "run_synthesis",
    )
}
_CONNECT_KWARGS = set(inspect.signature(synnodb.connect).parameters)


def _python_source(cell_source: str) -> str:
    """Strip IPython line/cell magics and shell escapes so ast.parse sees Python."""
    lines = [
        line
        for line in cell_source.splitlines()
        if not line.lstrip().startswith(("%", "!"))
    ]
    return "\n".join(lines)


def _drift_errors(source: str, where: str) -> list[str]:
    """All API-drift problems in one piece of tutorial source."""
    errors: list[str] = []
    try:
        tree = ast.parse(_python_source(source))
    except SyntaxError as exc:
        return [f"{where}: does not parse: {exc}"]

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "synnodb":
            for alias in node.names:
                if not hasattr(synnodb, alias.name):
                    errors.append(
                        f"{where}: `from synnodb import {alias.name}` does not resolve"
                    )
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        kwargs = {kw.arg for kw in node.keywords if kw.arg is not None}
        if isinstance(func, ast.Name) and func.id == "SynnoDB":
            unknown = kwargs - _CONSTRUCTOR_KWARGS
            if unknown:
                errors.append(
                    f"{where}: SynnoDB(...) has unknown kwargs {sorted(unknown)}"
                )
        elif isinstance(func, ast.Attribute) and func.attr in _METHODS:
            unknown = kwargs - _METHODS[func.attr]
            # Constructor classmethods also accept every config field override.
            if func.attr in ("in_memory", "on_ssd", "from_env", "with_"):
                unknown -= _CONSTRUCTOR_KWARGS
            if unknown:
                errors.append(
                    f"{where}: .{func.attr}(...) has unknown kwargs {sorted(unknown)}"
                )
        elif isinstance(func, ast.Attribute) and func.attr == "connect":
            base = func.value
            if isinstance(base, ast.Name) and base.id in ("synnodb", "duckdb"):
                # The drop-in promise: synnodb.connect must accept what the
                # tutorials pass it (duckdb.connect aliases synnodb in demos).
                unknown = kwargs - _CONNECT_KWARGS
                if unknown and base.id == "synnodb":
                    errors.append(
                        f"{where}: synnodb.connect(...) has unknown kwargs {sorted(unknown)}"
                    )
    return errors


@pytest.mark.parametrize("nb_path", NOTEBOOKS, ids=lambda p: p.name)
def test_notebook_matches_current_api(nb_path):
    nb = json.loads(nb_path.read_text())
    errors = []
    for i, cell in enumerate(nb["cells"]):
        if cell["cell_type"] != "code":
            continue
        source = "".join(cell["source"])
        errors.extend(_drift_errors(source, f"{nb_path.name} cell {i}"))
    assert not errors, "\n".join(errors)


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_script_matches_current_api(script):
    errors = _drift_errors(script.read_text(), script.name)
    assert not errors, "\n".join(errors)


def test_guard_detects_renamed_kwarg():
    """The detector itself must flag the historical breakage (queries= was
    renamed to query_subset= and shipped broken in a notebook)."""
    bad = 'db = SynnoDB(model="m", queries="1-10")'
    assert any("queries" in e for e in _drift_errors(bad, "self-check"))
    good = 'db = SynnoDB(model="m", query_subset="1-10")'
    assert _drift_errors(good, "self-check") == []
