"""``delete_result_files`` must remove every prior result the validator might read, so a
crashed run never leaves a stale result to be validated as the current one's.

The engine writes its result as ``result_<req_id>.arrow`` (exact Arrow egress); the legacy CSV
path is still cleaned for older engines. Result names are keyed by request id and therefore
stable across iterations, so missing either extension would silently misvalidate a stale file -
which is the regression this guards.
"""

from __future__ import annotations

from pathlib import Path

from synnodb.tools.run import delete_result_files


def test_deletes_both_arrow_and_csv_results_recursively(tmp_path: Path) -> None:
    (tmp_path / "results").mkdir()
    arrow = tmp_path / "results" / "result_q1-abc.arrow"
    csv = tmp_path / "results" / "result_q6-def.csv"
    nested = tmp_path / "nested" / "results"
    nested.mkdir(parents=True)
    nested_arrow = nested / "result_q1-ghi.arrow"
    for f in (arrow, csv, nested_arrow):
        f.write_bytes(b"stale")

    # Files that are not results must survive.
    keep_input = tmp_path / "lineitem.parquet"
    keep_other = tmp_path / "results" / "summary.json"
    for f in (keep_input, keep_other):
        f.write_bytes(b"keep")

    delete_result_files(tmp_path)

    assert not arrow.exists()
    assert not csv.exists()
    assert not nested_arrow.exists()
    assert keep_input.exists()
    assert keep_other.exists()


def test_no_results_is_a_noop(tmp_path: Path) -> None:
    (tmp_path / "data.parquet").write_bytes(b"x")
    delete_result_files(tmp_path)  # must not raise when there is nothing to delete
    assert (tmp_path / "data.parquet").exists()
