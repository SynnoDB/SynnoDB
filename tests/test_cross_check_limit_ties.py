"""Cross-checking a result that a top-level LIMIT truncated.

``ORDER BY k LIMIT n`` fixes how many rows tied on ``k`` survive the cut but never WHICH ones. When
the cut falls inside a tie group DuckDB keeps whichever members its hash table happened to yield -
not even reproducibly across runs of the identical query - so comparing row for row asks a correct
engine to reproduce an arbitrary choice. This is what stalled ClickBench Q4: the agent flipped a
tie-break comparator back and forth for two hours chasing a reference that was itself unstable.

The fix does not weaken the check. The query is re-run wide enough to hold the whole ranking the
window was drawn from, and the engine's rows must be MEMBERS of it: the ORDER BY key sequence pins
what each position is worth, membership pins each row to a genuine row of the ranking. Every row
stays checked, and the freedom appears only where the SQL genuinely leaves the choice open.

Run: .venv/bin/python -m pytest tests/test_cross_check_limit_ties.py -q
"""

from __future__ import annotations

import pyarrow as pa

from synnodb.router.adapt import candidate_superset, results_diff, results_equal
from synnodb.router.normalize import top_level_limit_offset, widened_query


# ---- SQL helpers (unit) -----------------------------------------------------
def test_reads_top_level_limit_and_offset():
    assert top_level_limit_offset("SELECT a FROM t ORDER BY a DESC LIMIT 10") == (10, 0)
    assert top_level_limit_offset("SELECT a FROM t ORDER BY a LIMIT 20 OFFSET 5") == (
        20,
        5,
    )
    assert top_level_limit_offset("SELECT a FROM t") == (None, 0)


def test_ignores_limits_nested_in_subqueries():
    """Only the top-level clause bounds the result set."""
    assert top_level_limit_offset("SELECT a FROM (SELECT a FROM t LIMIT 5) x") == (
        None,
        0,
    )
    assert top_level_limit_offset(
        "SELECT a FROM (SELECT a FROM t LIMIT 5) x LIMIT 3"
    ) == (3, 0)
    assert top_level_limit_offset(
        "WITH c AS (SELECT a FROM t LIMIT 7) SELECT a FROM c ORDER BY a LIMIT 2 OFFSET 1"
    ) == (2, 1)


def test_limit_is_none_when_unreadable():
    """A non-literal count or an unparseable statement reports "not truncated", which keeps the
    comparison strict - that can only over-reject, never wave a wrong result through."""
    assert top_level_limit_offset("SELECT a FROM t LIMIT ?") == (None, 0)
    assert top_level_limit_offset("this is not sql ((") == (None, 0)


def test_widened_query_rewrites_the_limit_and_drops_the_offset():
    """The widened query must start at rank 1, or the rows an OFFSET skipped - which the window's
    first tie group may extend back into - would be missing from the superset."""
    out = widened_query("SELECT a FROM t ORDER BY a DESC LIMIT 10 OFFSET 25", 400)
    assert "LIMIT 400" in out and "OFFSET" not in out
    assert "ORDER BY a DESC" in out
    # A nested LIMIT is part of the query's meaning, not its window: it must survive untouched.
    nested = widened_query("SELECT a FROM (SELECT a FROM t LIMIT 5) x LIMIT 3", 12)
    assert "LIMIT 5" in nested and "LIMIT 12" in nested
    assert widened_query("SELECT a FROM t ORDER BY a", 10) is None  # nothing to widen


def test_widened_query_does_not_corrupt_the_parse_cache():
    """The rewrite works on a copy; the shared cached tree must be left alone."""
    sql = "SELECT a FROM t ORDER BY a DESC LIMIT 10"
    assert widened_query(sql, 999).endswith("LIMIT 999")
    assert top_level_limit_offset(sql) == (10, 0)


# ---- candidate_superset: growing the reference until the group closes -------
def _t(u, phrase):
    return pa.table({"u": pa.array(u, pa.int64()), "phrase": pa.array(phrase)})


#: A ranking where 6 phrases tie at u=1 - far past a LIMIT 4 window.
_RANKING = _t([5, 3, 1, 1, 1, 1, 1, 1], ["a", "b", "c", "d", "e", "f", "g", "h"])


def _fetch(limit: int):
    return _RANKING.slice(0, limit)


def test_superset_grows_until_the_tie_group_closes():
    """The window ends inside the u=1 group, so widening must continue until the whole ranking is
    in hand - here that means running out of rows rather than finding a different trailing key."""
    asked = []

    def fetch(limit: int):
        asked.append(limit)
        return _fetch(limit)

    rows = candidate_superset(
        _RANKING.slice(0, 4),
        order_keys=[0],
        row_limit=4,
        row_offset=0,
        fetch_widened=fetch,
    )
    assert asked == [
        8,
        16,
    ]  # doubled once, then again to prove the ranking was exhausted
    assert len(rows) == 8  # every candidate row, not just the window


def test_superset_stops_as_soon_as_the_trailing_key_changes():
    """A window ending on a key whose group is fully inside the widened result needs no further
    widening."""
    ranking = _t([9, 9, 7, 7, 4, 2, 2, 1], list("abcdefgh"))
    asked = []

    def fetch(limit: int):
        asked.append(limit)
        return ranking.slice(0, limit)

    rows = candidate_superset(
        ranking.slice(0, 2),
        order_keys=[0],
        row_limit=2,
        row_offset=0,
        fetch_widened=fetch,
    )
    assert asked == [
        4
    ]  # one widening sufficed: it ends on u=7, so the u=9 group is closed
    assert len(rows) == 4


def test_superset_keeps_widening_while_the_group_might_continue():
    """A widened result that ends ON the window's key proves nothing - the group may run past it,
    so widening continues even though the cut happens to fall at the group's real edge."""
    ranking = _t([9, 9, 9, 9, 7, 7, 4, 2], list("abcdefgh"))
    asked = []

    def fetch(limit: int):
        asked.append(limit)
        return ranking.slice(0, limit)

    candidate_superset(
        ranking.slice(0, 2),
        order_keys=[0],
        row_limit=2,
        row_offset=0,
        fetch_widened=fetch,
    )
    assert asked == [4, 8]  # LIMIT 4 still ends on u=9; only LIMIT 8 settles it


def test_superset_covers_the_rows_an_offset_skipped():
    """With OFFSET the widening must start from rank 1 and span offset+limit before doubling."""
    asked = []

    def fetch(limit: int):
        asked.append(limit)
        return _fetch(limit)

    candidate_superset(
        _RANKING.slice(3, 3),
        order_keys=[0],
        row_limit=3,
        row_offset=3,
        fetch_widened=fetch,
    )
    assert asked[0] == 12  # 2 * (offset 3 + limit 3), not 2 * limit


def test_no_superset_when_the_result_was_not_truncated():
    """Fewer rows than the LIMIT means nothing was cut, so the reference is already the whole
    answer and there is nothing to widen."""
    called = []
    assert (
        candidate_superset(
            _RANKING.slice(0, 3),
            order_keys=[0],
            row_limit=10,
            row_offset=0,
            fetch_widened=lambda n: called.append(n),
        )
        is None
    )
    assert called == []


def test_no_superset_when_the_widened_query_is_unavailable():
    """A fetch that cannot run leaves the caller to compare strictly, not to skip checking."""
    assert (
        candidate_superset(
            _RANKING.slice(0, 4),
            order_keys=[0],
            row_limit=4,
            row_offset=0,
            fetch_widened=lambda n: None,
        )
        is None
    )


# ---- results_equal against the superset -------------------------------------
_WINDOW = _RANKING.slice(0, 4)  # u = [5, 3, 1, 1]
_POOL = list(
    zip(_RANKING.column("u").to_pylist(), _RANKING.column("phrase").to_pylist())
)


def _equal(eng, candidates=_POOL):
    return results_equal(
        eng,
        _WINDOW,
        ordered=True,
        order_keys=[0],
        row_limit=4,
        candidates=candidates,
    )


def test_a_different_pick_from_the_tie_group_is_accepted():
    """The Q4 reproduction: six phrases tie at u=1 and two slots remain, so DuckDB kept an
    arbitrary two. An engine that kept two others is equally correct."""
    assert _equal(_t([5, 3, 1, 1], ["a", "b", "g", "h"])) is True
    # ...and without the superset the very same correct result is rejected, which is the bug.
    assert _equal(_t([5, 3, 1, 1], ["a", "b", "g", "h"]), candidates=None) is False


def test_a_row_outside_the_ranking_is_rejected():
    """The hole this design closes: a row invented at the cut is not a member of the ranking.
    Its key is right, its position is right, and it is still wrong."""
    assert _equal(_t([5, 3, 1, 1], ["a", "b", "c", "INVENTED"])) is False


def test_a_row_duplicated_to_fill_a_slot_is_rejected():
    """Membership counts multiplicity, so an engine cannot pad the window by repeating one
    legitimate row."""
    assert _equal(_t([5, 3, 1, 1], ["a", "b", "c", "c"])) is False


def test_rows_that_do_not_tie_are_still_pinned_exactly():
    """Where a key value has exactly as many rows in the ranking as the window shows, membership
    IS identity - u=5 has only one row, so no substitute passes."""
    assert _equal(_t([5, 3, 1, 1], ["c", "b", "d", "e"])) is False


def test_a_wrong_key_sequence_is_still_caught():
    """The ordering contract is never relaxed, superset or not."""
    assert _equal(_t([3, 5, 1, 1], ["b", "a", "c", "d"])) is False


def test_a_wrong_aggregate_value_is_still_caught():
    """A phrase paired with a count it does not have is not a row of the ranking."""
    assert _equal(_t([5, 3, 1, 1], ["a", "b", "c", "b"])) is False


def test_untruncated_results_are_unaffected():
    """Without a LIMIT the reference is the whole answer and the comparison stays exact."""
    duck = _t([5, 3, 1], ["a", "b", "c"])
    assert results_equal(duck, duck, ordered=True, order_keys=[0]) is True
    assert (
        results_equal(
            _t([5, 3, 1], ["a", "b", "z"]), duck, ordered=True, order_keys=[0]
        )
        is False
    )


def test_diff_reports_the_stray_row_not_the_tie_permutation():
    """The failure report must name the row that is not in the ranking, not the rows the engine
    was free to choose."""
    eng = _t([5, 3, 1, 1], ["a", "b", "h", "INVENTED"])
    diffs, total = results_diff(
        eng, _WINDOW, ordered=True, order_keys=[0], row_limit=4, candidates=_POOL
    )
    assert total == 1
    assert diffs[0][1] == "<row not in the ranking>"
    assert "INVENTED" in diffs[0][2]
