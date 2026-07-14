"""A malformed tool call must come back to the model as a tool result, never crash the run.

The agents SDK wraps any exception out of ``on_invoke_tool`` in a ``UserError`` that propagates
all the way up and kills the conversation. So a model that mis-shapes one tool call - which models
do, routinely - would burn the whole run.

This is not hypothetical: a real run died ~500 turns in, deep into base-impl synthesis, because the
model called ``replace_in_file`` with ``{"file_path": "db_loader.cpp", "diff": ""}`` - apply_patch's
argument shape, sent to the exact-string editor. Pydantic raised for the missing ``old_string`` /
``new_string``, and the run ended there. ``apply_patch`` / ``write_file`` / ``read_file`` already
guarded against this; ``replace_in_file``, ``shell`` and ``query_data`` did not.

Every tool the model can call must therefore report a schema-invalid call as text (in its own
result shape) and stay usable. These tests pin that for the tools that lacked it.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from synnodb.llm.sdk.agents_sdk.openai_sdk_tools import (
    make_custom_openai_read_file_tool,
    make_custom_openai_replace_in_file_tool,
    make_custom_openai_shell_tool,
    make_custom_openai_write_file_tool,
)
from synnodb.tools.workspace_editor import WorkspaceEditor


class _FakeCollector:
    def log_metrics_callback(self, metrics, log_and_increment=False):
        pass

    def add_to_activity_summary(self, entry):
        pass


@pytest.fixture
def editor(tmp_path: Path) -> WorkspaceEditor:
    (tmp_path / "db_loader.cpp").write_text("int main() { return 0; }\n")
    return WorkspaceEditor(
        root=tmp_path,
        run_stats_collector=_FakeCollector(),  # type: ignore[arg-type]
        readonly_files=set(),
        untracked_cpp_runner_content="",
    )


def _invoke(tool, args_json: str):
    return asyncio.run(tool.on_invoke_tool(None, args_json))


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
    assert (editor._root / "db_loader.cpp").read_text() == "int main() { return 0; }\n"


@pytest.mark.parametrize(
    "args_json",
    [
        "{}",  # nothing at all
        '{"file_path": "db_loader.cpp"}',  # missing both strings
        '{"file_path": "db_loader.cpp", "old_string": 5, "new_string": "x"}',  # wrong type
    ],
)
def test_replace_in_file_never_raises_on_bad_args(editor, args_json):
    out = _invoke(make_custom_openai_replace_in_file_tool(editor), args_json)
    assert out.startswith("Error: replace_in_file arguments failed validation")


def test_shell_reports_bad_args_in_its_own_result_shape():
    """shell returns a dict of command outcomes, not text - so a malformed call is reported as a
    failed command (exit_code 1, message on stderr), which the model already knows how to read."""
    tool = make_custom_openai_shell_tool(shell_executor=SimpleNamespace())  # type: ignore[arg-type]

    out = _invoke(tool, '{"cmd": "ls"}')  # `command`, not `cmd`
    assert out[0]["exit_code"] == 1
    assert "shell arguments failed validation" in out[0]["stderr"]
    assert out[0]["stdout"] == ""


def test_write_and_read_file_report_bad_args(editor):
    """These two already guarded; pin it so the guard is not dropped."""
    write_out = _invoke(make_custom_openai_write_file_tool(editor), '{"path": "a.cpp"}')
    assert write_out.startswith("Error: write_file arguments failed validation")

    read_out = _invoke(make_custom_openai_read_file_tool(editor), '{"offset": 1}')
    assert read_out.startswith("Error: read_file arguments failed validation")


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
