"""RTABench's parameterized templates stay honest.

A parameterization only blocks the constant-folding exploit if the answer actually moves
with the parameters and the query actually returns something. These tests hold the recipes
to that bar without needing the 9.7GB dataset: they check the templates build, carry the
placeholders their specs declare, sample distinct assignments, and - for the queries that
filter an entity and a time window together - draw the pair correlated rather than
independently.

The dataset-backed checks (non-empty and distinct results over real data) live in the
tutorial's own validation, since they need the loaded database.

Run: .venv/bin/python -m pytest tests/test_rtabench_parameterization.py -q
"""

from __future__ import annotations

import random
import re

import pytest

rtabench = pytest.importorskip("tutorials.workloads.rtabench.parameterize")
from synnodb.workloads.query_params import parse_param_space, substitute  # noqa: E402

PLACEHOLDER = re.compile(r"\[([A-Z][A-Z0-9_]*)\]")
# Queries that filter an order id and a time window together: independent draws miss each
# other on ~95% of samples, so an engine returning nothing would pass a correctness sweep.
CORRELATED = {"9", "10", "11"}


def _entries():
    try:
        return rtabench.parameterized_queries()
    except SystemExit as exc:  # RTABENCH_DIR unset
        pytest.skip(str(exc))


def test_every_recipe_builds_against_the_upstream_queries():
    entries = _entries()
    assert len(entries) == len(rtabench.PARAMETERIZATIONS)
    assert set(entries) == set(rtabench.PARAMETERIZATIONS)


def test_declared_specs_and_used_placeholders_agree():
    for qid, entry in _entries().items():
        used = set(PLACEHOLDER.findall(entry["sql"]))
        declared = set(entry.get("params", {}))
        for group in entry.get("param_groups", []):
            declared.update(group["placeholders"])
        assert used == declared, f"Q{qid}: used {used} vs declared {declared}"
        assert used, f"Q{qid}: no placeholders - the query is still constant"


def test_templates_parse_and_sample_distinct_assignments():
    for qid, entry in _entries().items():
        space = parse_param_space(
            entry.get("params", {}), entry.get("param_groups", []), entry["sql"]
        )
        drawn = {
            substitute(entry["sql"], space.sample(random.Random(seed)))
            for seed in range(8)
        }
        assert len(drawn) > 1, f"Q{qid}: every draw produced the same SQL"
        for sql in drawn:
            assert not PLACEHOLDER.search(sql), f"Q{qid}: unsubstituted placeholder"


def test_point_lookups_draw_their_id_and_window_together():
    for qid in CORRELATED:
        entry = _entries()[qid]
        groups = entry.get("param_groups", [])
        tuples = [g for g in groups if g["type"] == "tuples"]
        assert tuples, f"Q{qid}: an order id and a window must be drawn as a pair"
        placeholders = set(tuples[0]["placeholders"])
        assert "ORDER_ID" in placeholders
        assert any(p.startswith(("DAY", "DATE")) for p in placeholders), placeholders
        assert len(tuples[0]["values"]) >= 100, "too few pairs to be a real domain"


def test_drift_in_the_upstream_query_is_loud(tmp_path, monkeypatch):
    """A recipe that no longer matches upstream must fail, not silently do nothing."""
    _entries()  # skips when RTABENCH_DIR is unset, like every other test here
    original = rtabench.PARAMETERIZATIONS["1"]
    monkeypatch.setitem(
        rtabench.PARAMETERIZATIONS,
        "1",
        {**original, "edits": [["this fragment is not in the file", "x"]]},
    )
    with pytest.raises(rtabench.TemplateDrift):
        rtabench.parameterized_queries(only={"1"})
