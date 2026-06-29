"""A bring-your-own workload must validate small-SF-first: correctness is checked at the
cheapest scale factors that exist on disk before escalating to the target. The SF50 Q10 run
burned hours because the BYO registration collapsed the ladder to a single target SF, so a
bug that fails at SF1 was only ever exercised after a six-minute SF50 load.

These cover the derivation (`_derive_sf_ladder`) directly: target-only input is augmented with
the smallest available SFs, an explicit ladder is honoured, and a missing small SF warns loudly.
"""
from __future__ import annotations

import logging

from synnodb.workloads.byo_workload import _derive_sf_ladder, _discover_available_sfs


def _make_sf_tree(root, sfs):
    for sf in sfs:
        (root / f"sf{sf}").mkdir(parents=True)
    return root


def test_discovers_integral_sfs_ascending(tmp_path):
    _make_sf_tree(tmp_path, [2, 1, 50, 10])
    assert _discover_available_sfs(tmp_path) == [1, 2, 10, 50]


def test_target_only_is_augmented_with_smallest_available(tmp_path):
    """Passing just the target SF derives a small-first ladder from on-disk data."""
    _make_sf_tree(tmp_path, [1, 2, 10, 20, 50])
    fast_check, exhaustive, benchmark, ingest = _derive_sf_ladder((50,), tmp_path)
    assert fast_check == (1, 2)          # two cheapest rungs for fast iteration
    assert exhaustive == (1, 2, 50)      # small rungs then the target
    assert benchmark == 50
    assert ingest == (50,)


def test_explicit_ladder_is_honoured(tmp_path):
    """A caller-supplied multi-SF ladder is used verbatim (sorted), not overridden."""
    _make_sf_tree(tmp_path, [1, 2, 20])
    fast_check, exhaustive, benchmark, ingest = _derive_sf_ladder((1, 2, 20), tmp_path)
    assert fast_check == (1, 2)
    assert exhaustive == (1, 2, 20)
    assert benchmark == 20
    assert ingest == (20,)


def test_no_small_sf_warns_and_falls_back(tmp_path, caplog):
    """With only the target SF on disk, validation cannot be cheap - warn, don't hide it."""
    _make_sf_tree(tmp_path, [50])
    with caplog.at_level(logging.WARNING):
        fast_check, exhaustive, benchmark, ingest = _derive_sf_ladder((50,), tmp_path)
    assert fast_check == (50,)
    assert exhaustive == (50,)
    assert benchmark == 50
    assert any("No scale factor smaller than the target" in r.message for r in caplog.records)
