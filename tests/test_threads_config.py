"""The single ``threads`` knob (the DuckDB ``config={'threads': N}``) wired through generation
and serving: resolution -> CORE_IDS, the prompt guidance, the engine manifest, and the runtime
override that runs a published engine at the configured thread count.
"""

from __future__ import annotations

import synnodb
from synnodb.conversations.prompts_gen import (
    base_impl_query_prompt,
    gen_storage_plan_prompt,
    parallelism_note,
)
from synnodb.duckdb_compat import discovery
from synnodb.router.manifest import EngineManifest, QueryTemplate
from synnodb.utils.core_utils import core_ids_to_env


# ── CORE_IDS env resolution ────────────────────────────────────────────────


def test_core_ids_to_env_serial_and_parallel():
    # No cores / empty -> a single thread, NOT the "use all cores" fallback.
    assert core_ids_to_env(None) == "1"
    assert core_ids_to_env([]) == "1"
    # A list -> that many threads, pinned to exactly those cores.
    assert core_ids_to_env([3, 5, 7]) == "3,5,7"
    assert len(core_ids_to_env([3, 5, 7]).split(",")) == 3


# ── threads config -> concrete cores ───────────────────────────────────────


def test_resolve_target_cores_semantics():
    from synnodb.utils.core_utils import resolve_target_cores

    # None (unset) -> 1 (single-threaded default).
    assert resolve_target_cores(None)[0] == 1
    # N -> N (up to the machine's usable cores).
    assert resolve_target_cores(1)[0] == 1
    # 0 -> auto-detect every usable core (>= the single-thread case).
    all_n, _ = resolve_target_cores(0)
    assert all_n >= resolve_target_cores(1)[0]
    # Negative is rejected.
    import pytest

    with pytest.raises(ValueError):
        resolve_target_cores(-1)


# ── multi-threaded validation thread counts ────────────────────────────────


def test_largest_prime_le():
    from synnodb.utils.core_utils import largest_prime_le

    assert largest_prime_le(1) is None  # nothing > 2
    assert largest_prime_le(2) is None  # must be strictly > 2
    assert largest_prime_le(3) == 3
    assert largest_prime_le(4) == 3
    assert largest_prime_le(8) == 7
    assert largest_prime_le(16) == 13


def test_validation_thread_counts_covers_1_8_prime_and_target():
    from synnodb.utils.core_utils import validation_thread_counts

    # N=12 on a 16-core host: 1, 8, largest prime <= 12 (11), and 12.
    assert validation_thread_counts(target_threads=12, max_available=16) == [
        1,
        8,
        11,
        12,
    ]


def test_validation_thread_counts_dedupes():
    from synnodb.utils.core_utils import validation_thread_counts

    # N=8: 8 and the target coincide, and there is no separate prime > 8.
    assert validation_thread_counts(target_threads=8, max_available=16) == [1, 7, 8]


def test_validation_thread_counts_caps_internal_widths_silently(caplog):
    import logging

    from synnodb.utils.core_utils import validation_thread_counts

    # 4-core host: the internal probe width 8 is silently capped to 4 (no worker can be
    # pinned past the usable cores). The oversubscription *warning* is reserved for the
    # user's own thread selection, so it must NOT fire here.
    with caplog.at_level(logging.WARNING):
        counts = validation_thread_counts(target_threads=4, max_available=4)
    assert counts == [1, 3, 4]
    assert max(counts) <= 4
    assert not any("oversubscribes" in r.message for r in caplog.records)


def test_clamp_threads_to_available_warns_and_caps(caplog):
    import logging

    from synnodb.utils.core_utils import clamp_threads_to_available

    # Under-subscribed: returned as-is, no warning.
    with caplog.at_level(logging.WARNING):
        assert clamp_threads_to_available(4, max_available=8) == 4
    assert not any("oversubscribes" in r.message for r in caplog.records)

    # A user-selected count above the usable cores is warned about and capped.
    with caplog.at_level(logging.WARNING):
        assert clamp_threads_to_available(32, max_available=8) == 8
    assert any("oversubscribes" in r.message for r in caplog.records)


# ── Prompt guidance (the planner/base writer are told the target) ──────────


def test_parallelism_note_degrades_to_serial_for_one_thread():
    assert "single thread" in parallelism_note(1)
    assert "worker threads" not in parallelism_note(1)


def test_parallelism_note_states_the_thread_count():
    note = parallelism_note(8)
    assert "8 worker threads" in note
    assert "morsel" in note  # partition guidance present


def test_storage_plan_prompt_carries_the_thread_count_in_memory_only():
    in_mem = gen_storage_plan_prompt(
        "queries.json", "SCHEMA", "plan.txt", persistent_storage=False, num_threads=8
    )
    assert "8 worker threads" in in_mem

    serial = gen_storage_plan_prompt(
        "queries.json", "SCHEMA", "plan.txt", persistent_storage=False, num_threads=1
    )
    assert "single thread" in serial

    # SSD/persistent base impls are not parallel-ready, so that template stays inert.
    ssd = gen_storage_plan_prompt(
        "queries.json", "SCHEMA", "plan.txt", persistent_storage=True, num_threads=8
    )
    assert "worker threads" not in ssd
    assert "${parallelism_note}" not in ssd


def test_base_impl_prompt_carries_the_thread_count_in_memory_only():
    in_mem = base_impl_query_prompt(
        is_first_query=True,
        sample_query_args_dict=None,
        query_id="1",
        queries_path="q.json",
        args_path="a.json",
        builder_path="b.hpp",
        query_impl_path="qi.cpp",
        sql="SELECT 1",
        persistent_storage=False,
        num_threads=8,
        storage_plan_filename="storage_plan.txt",
        base_impl_todo_filename="base_impl_todo.txt",
        read_storage_plan=True,
    )
    assert "8 worker threads" in in_mem

    ssd = base_impl_query_prompt(
        is_first_query=True,
        sample_query_args_dict=None,
        query_id="1",
        queries_path="q.json",
        args_path="a.json",
        builder_path="b.hpp",
        query_impl_path="qi.cpp",
        sql="SELECT 1",
        persistent_storage=True,
        num_threads=8,
        storage_plan_filename="storage_plan.txt",
        base_impl_todo_filename="base_impl_todo.txt",
        read_storage_plan=True,
    )
    assert "worker threads" not in ssd


# ── Engine manifest records the build-time parallelism ─────────────────────


def test_manifest_round_trips_threads():
    m = EngineManifest(
        engine_id="eng-x", queries=(QueryTemplate("1", "SELECT 1"),), threads=4
    )
    d = m.to_dict()
    assert d["threads"] == 4
    assert d["schema_version"] == 5
    assert EngineManifest.from_dict(d).threads == 4


def test_manifest_back_compat_without_threads():
    # An older (v4) manifest has no ``threads`` key -> None (engine keeps its own default).
    old = {
        "schema_version": 4,
        "engine_id": "eng-old",
        "queries": [{"query_id": "1", "sql_template": "SELECT 1", "placeholders": []}],
    }
    assert EngineManifest.from_dict(old).threads is None


# ── Runtime: serve at the recorded count, override via connect config ──────


def test_engine_extra_env_override_beats_manifest_and_empty_when_unknown():
    m = EngineManifest("e", (), threads=2)
    # connect-time override wins over the manifest's recorded count.
    over = discovery._engine_extra_env(m, threads_override=1)
    assert over["CORE_IDS"] == core_ids_to_env(_resolve(1))
    # no override -> the manifest's recorded count.
    rec = discovery._engine_extra_env(m, threads_override=None)
    assert rec["CORE_IDS"] == core_ids_to_env(_resolve(2))
    # neither known -> no CORE_IDS; the engine keeps its own default.
    assert (
        discovery._engine_extra_env(EngineManifest("e", (), threads=None), None) == {}
    )


def _resolve(n: int):
    from synnodb.utils.core_utils import get_cores_for_current_machine

    _, core_ids = get_cores_for_current_machine(
        leave_core_0_out=True, allow_hyperthreading=True, ncores_to_use=n
    )
    return core_ids


def test_connect_config_threads_is_captured_and_forwarded():
    # The override is captured for routed engines ...
    con = synnodb.connect(config={"threads": 3})
    assert con._engine_threads == 3
    # ... and still reaches inner DuckDB (the genuine DuckDB knob is untouched).
    assert con.duckdb.execute("SELECT current_setting('threads')").fetchone()[0] == 3
    # cursors inherit it.
    assert con.cursor()._engine_threads == 3
    # absent -> None (engine serves at its own recorded count).
    assert synnodb.connect()._engine_threads is None


def test_discovery_threads_the_override_into_engine_binding(monkeypatch, tmp_path):
    engines = tmp_path / "engines"
    d = engines / "eng-cap"
    d.mkdir(parents=True)
    EngineManifest(
        engine_id="eng-cap",
        queries=(QueryTemplate("1", "SELECT 1"),),
        parquet_dir="/unused",
        threads=2,
    ).write(d)

    captured = {}

    def fake_bind(conn, manifest, engine_dir, *, mount, threads_override=None):
        captured["threads_override"] = threads_override
        return None  # not servable; stops before registration

    monkeypatch.setattr(discovery, "_bind_engine", fake_bind)

    con = synnodb.connect(engines=str(engines), config={"threads": 5})
    con.refresh_engines()
    assert captured["threads_override"] == 5
