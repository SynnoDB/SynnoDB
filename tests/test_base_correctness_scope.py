"""Regression for failure G7: the base-impl post-impl correctness stages must validate
ONLY the in-scope query_ids, not the whole benchmark.

Before the fix, `base_correctness_all` / `base_run_all_and_fix` told the model to
"check correctness of all queries / call the run tool once for all queries together"
with no scoping. For a base impl generated for a subset (e.g. just Q1), the other
query files are stubs — so the model saw them fail and went off implementing all 22.
The prompts now take explicit query_ids (matching the stages' post_stage_validate
gates, which were already scoped to self.all_query_ids).
"""
from __future__ import annotations

from synnodb.conversations.prompts_gen import (
    base_check_correctness_all_prompt,
    base_run_all_and_fix_prompt,
)


class _Mode:
    name = "EXHAUSTIVE"


def test_correctness_all_is_scoped_to_given_query_ids():
    p = base_check_correctness_all_prompt(_Mode(), ["1"])
    assert '["1"]' in p
    # the unscoped wording that caused the runaway must be gone
    assert "all queries together" not in p
    assert "of all queries" not in p
    # other queries are explicitly out of scope
    assert "Do NOT run, implement, or modify any other query" in p


def test_run_all_and_fix_is_scoped_to_given_query_ids():
    p = base_run_all_and_fix_prompt("query_impl.cpp", _Mode(), ["1"])
    assert '["1"]' in p
    assert "correct for all queries" not in p
    assert "Do NOT run, implement, or modify any other query" in p


def test_full_suite_still_lists_every_query():
    ids = [str(i) for i in range(1, 23)]
    p = base_check_correctness_all_prompt(_Mode(), ids)
    assert '"1"' in p and '"22"' in p


def test_multi_query_subset_scoping():
    p = base_run_all_and_fix_prompt("query_impl.cpp", _Mode(), ["1", "6"])
    assert '["1", "6"]' in p
