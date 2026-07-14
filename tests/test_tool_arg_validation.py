"""A malformed tool call must come back to the model as a tool result, never crash the run.

The agents SDK wraps any exception out of ``on_invoke_tool`` in a ``UserError`` that propagates
all the way up and kills the conversation. So a model that mis-shapes one tool call - which models
do, routinely - would burn the whole run.

This is not hypothetical: a real run died ~500 turns in, deep into base-impl synthesis, because the
model called ``replace_in_file`` with ``{"file_path": "db_loader.cpp", "diff": ""}`` - apply_patch's
argument shape, sent to the exact-string editor. Pydantic raised for the missing ``old_string`` /
``new_string``, and the run ended there.

Two properties are pinned here, and the first one is why ``every_tool`` below is a registry rather
than a list of hand-written per-tool tests:

1. EVERY tool the model can call survives a schema-invalid call. Guarding tool by tool is what let
   the crash happen in the first place - the guard is only as good as whoever remembers to add it to
   the next tool. ``every_tool`` fails the moment a tool is registered without one.
2. A rejected edit is RECORDED as rejected. ``RunStatsCollector.on_tool_end`` emits an edit metric
   for every apply_patch/replace_in_file/write_file call from state only the ``WorkspaceEditor``
   writes, so a tool that returns before reaching the editor logs the bad call as a successful
   +0/-0 no-op - the live-ui shows a clean edit where the model saw a failure.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from synnodb.llm.sdk.agents_sdk.openai_make_compile_tool import make_openai_compile_tool
from synnodb.llm.sdk.agents_sdk.openai_make_run_tool import make_openai_run_tool
from synnodb.llm.sdk.agents_sdk.openai_sdk_tools import (
    make_custom_openai_apply_patch_tool,
    make_custom_openai_read_file_tool,
    make_custom_openai_replace_in_file_tool,
    make_custom_openai_shell_tool,
    make_custom_openai_write_file_tool,
)
from synnodb.tools.workspace_editor import WorkspaceEditor

ORIGINAL_CONTENT = "int main() { return 0; }\n"


class _FakeCollector:
    """Records what the tools push at it, standing in for RunStatsCollector's per-call state."""

    def __init__(self) -> None:
        self.rejected: list[tuple[str | None, str]] = []
        self.read_paths: list[str] = []
        self.activity: list[str] = []

    def log_metrics_callback(self, metrics, log_and_increment=False):
        pass

    def add_to_activity_summary(self, entry):
        self.activity.append(entry)

    def record_apply_patch_rejected(self, path, reason):
        self.rejected.append((path, reason))

    def log_read_file_stats(self, path):
        self.read_paths.append(path)

    def log_apply_patch_stats(self, *args, **kwargs):
        pass

    def record_apply_patch_cache_hit(self):
        pass


class _FakeSnapshotter:
    """The edit ops snapshot before they run and refuse to run uncached, so an editor that can
    perform a real edit (the retry after a rejection) needs both this and a cache dir."""

    def __init__(self) -> None:
        self.current_hash = "start"

    def restore(self, snapshot_hash: str) -> None:
        self.current_hash = snapshot_hash

    def snapshot(self, name: str):
        self.current_hash = f"snapshot-{name}"
        return None, self.current_hash


def _editor(root: Path, collector: _FakeCollector) -> WorkspaceEditor:
    workspace = root / "workspace"
    workspace.mkdir()
    (workspace / "db_loader.cpp").write_text(ORIGINAL_CONTENT)
    return WorkspaceEditor(
        root=workspace,
        run_stats_collector=collector,  # type: ignore[arg-type]
        readonly_files=set(),
        untracked_cpp_runner_content="",
        snapshotter=_FakeSnapshotter(),  # type: ignore[arg-type]
        cache_dir=root / "cache",
    )


@pytest.fixture
def collector() -> _FakeCollector:
    return _FakeCollector()


@pytest.fixture
def editor(tmp_path: Path, collector: _FakeCollector) -> WorkspaceEditor:
    return _editor(tmp_path, collector)


def _invoke(tool, args_json: str):
    return asyncio.run(tool.on_invoke_tool(None, args_json))


def _every_tool(editor: WorkspaceEditor) -> dict:
    """Every tool the model can be handed. A new tool belongs here; if it is registered
    without an argument guard, the tests below fail rather than a run dying mid-flight."""
    return {
        "apply_patch": make_custom_openai_apply_patch_tool(editor),
        "replace_in_file": make_custom_openai_replace_in_file_tool(editor),
        "write_file": make_custom_openai_write_file_tool(editor),
        "read_file": make_custom_openai_read_file_tool(editor),
        "shell": make_custom_openai_shell_tool(shell_executor=SimpleNamespace()),  # type: ignore[arg-type]
        "compile": make_openai_compile_tool(compile_tool=SimpleNamespace()),  # type: ignore[arg-type]
        "run": make_openai_run_tool(run_tool=SimpleNamespace()),  # type: ignore[arg-type]
    }


def test_registry_covers_every_tool_factory():
    """The registry is only worth anything if it cannot fall behind the code. query_data is
    excluded deliberately - it needs a live DuckDB dataset to build, so it is exercised in
    test_query_data_reports_bad_args instead."""
    import inspect
    import pkgutil

    import synnodb.llm.sdk.agents_sdk as agents_sdk

    factories = set()
    for module in pkgutil.iter_modules(agents_sdk.__path__):
        if module.name == "guarded_tool":
            continue  # the factory that builds the guard, not a tool
        mod = __import__(f"{agents_sdk.__name__}.{module.name}", fromlist=["*"])
        factories |= {
            name
            for name, obj in inspect.getmembers(mod, inspect.isfunction)
            if name.endswith("_tool")
            and name.startswith("make_")
            and obj.__module__ == mod.__name__
        }

    covered = {
        "make_custom_openai_apply_patch_tool",
        "make_custom_openai_replace_in_file_tool",
        "make_custom_openai_write_file_tool",
        "make_custom_openai_read_file_tool",
        "make_custom_openai_shell_tool",
        "make_openai_compile_tool",
        "make_openai_run_tool",
        "make_openai_data_inspect_tool",
    }
    assert factories == covered, (
        "A tool factory was added or renamed. Add it to _every_tool so its argument guard is "
        "tested - an unguarded tool crashes the run the first time a model mis-shapes a call."
    )


@pytest.mark.parametrize(
    "tool_name",
    [
        "apply_patch",
        "replace_in_file",
        "write_file",
        "read_file",
        "shell",
        "compile",
        "run",
    ],
)
@pytest.mark.parametrize(
    "args_json",
    [
        "{}",  # nothing at all
        '{"nonsense": 1}',  # every required field missing
        '{"path": 5, "file_path": 5, "command": 5, "optimize": 5, "mode": 5, "sql": 5}',  # wrong types
    ],
)
def test_every_tool_survives_schema_invalid_args(editor, tool_name, args_json):
    tool = _every_tool(editor)[tool_name]

    # The assertion is simply that this returns: the tools reach their impls with no
    # executor behind them (SimpleNamespace), so anything that got past validation would
    # blow up here - which is exactly the crash being guarded against.
    result = _invoke(tool, args_json)

    rendered = result[0]["stderr"] if isinstance(result, dict) else result
    assert f"Error: {tool_name} arguments failed validation" in rendered


def test_replace_in_file_reports_apply_patch_shaped_args(editor):
    """The exact call that killed a real run: apply_patch's {file_path, diff} sent to
    replace_in_file. It must come back as a tool result naming the fields it actually needs, and
    the tool must stay usable for the retry."""
    tool = make_custom_openai_replace_in_file_tool(editor)

    out = _invoke(tool, '{"file_path": "db_loader.cpp", "diff": ""}')
    assert out.startswith("Error: replace_in_file arguments failed validation")
    # It names the fields the model actually has to send, and where a diff belongs instead - the
    # bare pydantic dump alone does not tell a model that confused two tools which one it wanted.
    assert "old_string" in out and "new_string" in out
    assert "apply_patch" in out
    # The file is untouched: a rejected call must not half-apply anything.
    assert (editor._root / "db_loader.cpp").read_text() == ORIGINAL_CONTENT

    # Still usable afterwards - the run goes on.
    ok = _invoke(
        tool,
        '{"file_path": "db_loader.cpp", "old_string": "return 0;", "new_string": "return 1;"}',
    )
    assert "Error" not in ok
    assert "return 1;" in (editor._root / "db_loader.cpp").read_text()


def test_shell_reports_bad_args_in_its_own_result_shape(editor):
    """shell returns a dict of command outcomes, not text - so a malformed call is reported as a
    failed command (exit_code 1, message on stderr), which the model already knows how to read."""
    tool = make_custom_openai_shell_tool(shell_executor=SimpleNamespace())  # type: ignore[arg-type]

    out = _invoke(tool, '{"cmd": "ls"}')  # `command`, not `cmd`
    assert out[0]["exit_code"] == 1
    assert "shell arguments failed validation" in out[0]["stderr"]
    assert out[0]["stdout"] == ""


@pytest.mark.parametrize(
    "tool_name, args_json, expected_path",
    [
        # The path is read off the raw arguments best-effort, under whichever name the tool
        # gives it, so the live-ui can still name the file a rejected call was aimed at.
        (
            "replace_in_file",
            '{"file_path": "db_loader.cpp", "diff": ""}',
            "db_loader.cpp",
        ),
        ("write_file", '{"path": "db_loader.cpp"}', "db_loader.cpp"),
        ("apply_patch", '{"path": "db_loader.cpp", "diff": "+x"}', "db_loader.cpp"),
        # No usable path in the arguments at all: recorded as a rejection regardless.
        ("write_file", "{}", None),
    ],
)
def test_rejected_edit_is_recorded_as_rejected(
    editor, collector, tool_name, args_json, expected_path
):
    """The bug behind the review: an edit tool that returns before reaching the editor still gets
    an edit metric emitted for it by on_tool_end, built from collector state. Unrecorded, the
    rejected call the model saw is logged as a successful, uncached +0/-0 edit."""
    _invoke(_every_tool(editor)[tool_name], args_json)

    assert len(collector.rejected) == 1
    path, reason = collector.rejected[0]
    assert path == expected_path
    assert reason  # the live-ui renders this as the failure reason
    # A rejection has no file side effects, so it must not write an activity-summary line
    # (that would perturb the supervisor prompt and its cache key).
    assert collector.activity == []


def test_rejected_read_records_the_attempted_path(editor, collector):
    """read_file's metric has no rejected flag - its output field already carries the error
    string - but a rejected read that recorded nothing is logged with a null path, losing the
    one thing that makes the step diagnosable."""
    out = _invoke(
        make_custom_openai_read_file_tool(editor),
        '{"path": "db_loader.cpp", "offset": "top"}',
    )

    assert out.startswith("Error: read_file arguments failed validation")
    assert collector.read_paths == ["db_loader.cpp"]


def test_rejected_edit_metric_reaches_the_live_ui_as_rejected(tmp_path: Path):
    """End-to-end over the real RunStatsCollector: drive the malformed call the way the SDK does
    (on_tool_start -> invoke -> on_tool_end) and check the metric the live-ui actually receives."""
    from synnodb.observability.logging.run_stats_collector import RunStatsCollector

    emitted: list[dict] = []
    collector = RunStatsCollector(model=None, git_snapshotter=None, drains=[])  # type: ignore[arg-type]
    # Capture what would be emitted to the drains. Stubbed rather than drained for real: the
    # real callback stamps turn/runtime/snapshot fields that need a whole run behind them, and
    # the rejection state is what this test is about.
    collector.log_metrics_callback = lambda metrics, log_and_increment=False: (
        emitted.append(  # type: ignore[method-assign]
            metrics
        )
    )

    editor = _editor(tmp_path, collector)  # type: ignore[arg-type]
    tool = make_custom_openai_replace_in_file_tool(editor)
    ctx = SimpleNamespace()
    call = SimpleNamespace(name="replace_in_file")

    asyncio.run(collector.on_tool_start(ctx, None, call))
    result = _invoke(tool, '{"file_path": "db_loader.cpp", "diff": ""}')
    asyncio.run(collector.on_tool_end(ctx, None, call, result))

    (metric,) = emitted
    assert (
        metric["apply_patch/rejected"] is True
    )  # log.js renders "⚠ invalid args (rejected)"
    assert metric["apply_patch/failed"]  # ... and lists why
    assert metric["apply_patch/files"] == ["db_loader.cpp"]
    assert metric["apply_patch/cached"] is False
    assert metric["apply_patch/added_loc_count"] == 0


def test_query_data_reports_bad_args(tmp_path):
    """query_data surfaces every other failure (bad SQL, a write, a timeout) as text; a malformed
    call is no different, and must not be the one that takes the run down."""
    duckdb = pytest.importorskip("duckdb")
    from synnodb.llm.sdk.agents_sdk.openai_make_data_inspect_tool import (
        make_openai_data_inspect_tool,
    )
    from synnodb.tools.data_inspect import DataInspectTool
    from synnodb.utils.utils import ServeFrom

    base = tmp_path / "parquet_root"
    (base / "fraction1").mkdir(parents=True)
    con = duckdb.connect(str(base / "fraction1" / "subset.duckdb"))
    con.execute("CREATE TABLE orders AS SELECT i AS o_orderkey FROM range(7) t(i)")
    con.close()

    spec = SimpleNamespace(
        name="wl",
        dataset_name="ds",
        dataset_version=None,
        serve_from=ServeFrom.DUCKDB,
        tables=("orders",),
        fast_check_sfs=(),
        subset_files=lambda d: [d / "subset.duckdb"],
    )
    provider = SimpleNamespace(
        spec=spec, benchmark_sf=1, base_parquet_dir=base, prepare=lambda: None
    )
    tool = make_openai_data_inspect_tool(DataInspectTool(workload_provider=provider))

    # No sql at all, and a full_dataset that is not a boolean.
    assert _invoke(tool, '{"max_rows": 10}').startswith(
        "Error: query_data arguments failed validation"
    )
    assert _invoke(
        tool, '{"sql": "SELECT 1", "full_dataset": "yes please"}'
    ).startswith("Error: query_data arguments failed validation")

    # Still usable afterwards.
    assert "7" in _invoke(tool, '{"sql": "SELECT count(*) AS n FROM orders"}')
