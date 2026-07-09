import re
import textwrap
from dataclasses import dataclass
from pathlib import Path
from string import Template

from synnodb.tools.run_tool_mode import RunToolMode
from synnodb.workloads.workload_provider import ExecSettings

_PROMPTS_DIR = Path(__file__).parent / "prompts"


def _load_txt(path: Path) -> str:
    with open(path, "r") as f:
        return f.read()


def parallelism_note(num_threads: int) -> str:
    """Describe the parallelism the generated engine will run at.

    Injected into the storage-plan and base-impl prompts so the layout and the query
    implementation are designed for the SAME thread count the run tools validate at and
    the published engine is served at (see ``--threads`` / DuckDB ``config={'threads': N}``).
    """
    if num_threads <= 1:
        return (
            "Execution model: the generated engine runs each query on a single thread. "
            "Optimize for sequential scan locality; no cross-thread partitioning is needed."
        )
    return (
        f"Execution model: the generated engine runs each query on {num_threads} worker threads "
        f"that share this in-memory data. Design so the dominant scans and aggregations split into "
        f"independent morsels / contiguous row-ranges (about {num_threads}, or a small multiple, so "
        f"every thread gets several): keep each thread's accumulator state private and cheaply "
        f"mergeable, build any shared lookup / hash / dictionary / zone-map structure once up front "
        f"and treat it as read-only during the scan, and avoid a single hot shared structure that "
        f"every row must update. Prefer layouts where a thread owns a contiguous slice of a table "
        f"(and can emit its results in slice order) over ones that interleave rows across threads."
    )


# ── Misc────────────────────────────────
def gen_incorrect_output_prompt(
    trace_mode: bool,
    qids: list[str],
    debug_msg: str,
    query_impl_path: str,
    builder_path: str,
    persistent_storage: bool,
) -> str:
    template_str = _load_txt(_PROMPTS_DIR / "incorrect_output.txt")
    template = Template(template_str)

    error_hints_persistent = """Before editing, classify the failure:
- If the debug information mentions `ColumnHandle: computed range exceeds pinned page`, audit the storage representation and every `pin_range()`/`contiguous_rows()` use in the failing query before changing code. This usually means a page-layout invariant is broken or a multi-column scan chunk was not bounded by all pinned columns.
- If previous attempts fixed the same symptom, stop patching one suspected site at a time and inspect all accesses in the target query plus the relevant Database handles."""

    return template.substitute(
        tracing_mode=trace_mode,
        qids=",".join(qids),
        msg=debug_msg,
        query_impl_path=query_impl_path,
        builder_path=builder_path,
        error_hints=error_hints_persistent if persistent_storage else "",
    )


# ── Storage Plan Generation ────────────────────────────────


def gen_storage_plan_prompt(
    queries_filename: str,
    schema: str,
    storage_plan_filename: str,
    persistent_storage: bool,
    num_threads: int,
) -> str:
    if persistent_storage:
        # SSD/persistent base implementations are not parallel-ready (the in-memory pool is
        # in-memory only), so the SSD template intentionally carries no parallelism note.
        prompt_path = _PROMPTS_DIR / "ssd" / "gen_storage_plan_ssd.txt"
    else:
        prompt_path = _PROMPTS_DIR / "gen_storage_plan.txt"

    template_str = _load_txt(prompt_path)
    template = Template(template_str)
    return template.substitute(
        queries_path=queries_filename,
        schema=schema,
        storage_plan_filename=storage_plan_filename,
        parallelism_note=parallelism_note(num_threads),
    )


# ── Base Implementation ────────────────────────────────────


def base_planner_prompt(
    queries_path: str,
    builder_path: str,
    num_queries: int,
    read_storage_plan: bool,
    storage_plan_path: str,
    query_impl_path: str,
    example_query: str,
    example_query_params: str,
    args_path: str,
    parquet_path: str,
    base_impl_todo_file: str,
    persistent_storage: bool,
    schema_example_table: str,
    num_threads: int,
    serve_from: "str" = "parquet",
    schema_ddl: str | None = None,
) -> str:
    if persistent_storage:
        prompt_path = _PROMPTS_DIR / "ssd" / "base_planner_ssd.txt"
    else:
        prompt_path = _PROMPTS_DIR / "base_planner.txt"
    template_str = _load_txt(prompt_path)

    template = Template(template_str)

    query_str = f"{num_queries} {'query' if num_queries == 1 else 'queries'}"

    # How the planner reads the table schemas. A parquet workload dumps them from a parquet file;
    # a DuckDB-native subset has no parquet, so the schema DDL is inlined directly (the subset is a
    # single ``subset.duckdb`` there is no per-table file to point a dumper at).
    from synnodb.utils.utils import ServeFrom

    if ServeFrom.coerce(serve_from) == ServeFrom.DUCKDB:
        schema_hint = (
            "The table schemas (from the source DuckDB) are:\n\n"
            f"{schema_ddl or '(schema unavailable)'}"
        )
    else:
        schema_hint = (
            "Table schemas can be inspected with: parquet-dump-schema "
            f"`{parquet_path}/{schema_example_table}.parquet`"
        )

    # Beyond the schema, the agent can read the actual data to ground its physical-design choices.
    schema_hint += (
        "\n\nWith the query_data tool you can run simple, cheap read-only SQL queries against a "
        "small representative subset of the benchmark data. Keep queries light (SUMMARIZE, DESCRIBE, WHERE filters, LIMIT, "
        "per-column stats). A query that scans or joins large tables and runs too long is "
        "cancelled and you are asked to simplify."
    )

    # In-memory loading: the framework owns turning Arrow columns into typed C++ vectors,
    # which is easy to get wrong. The agent composes these helpers rather than decoding
    # Arrow itself; the helpers cast via Arrow, so physical representations are handled
    # centrally and a bad cast or overflow raises. See
    # docs/CAUGHT_ERRORS_IN_GENERATION.md (G2: a hand-written decimal decode loaded every
    # value column as zero).
    ingest_hint = (
        " To populate the in-memory columns from the input ArrowTables, you MUST compose "
        "the framework helpers in `column_ingest.hpp` (already on the include path: add "
        '`#include "column_ingest.hpp"`). Do NOT decode Arrow buffers yourself '
        "(no raw_values()/GetValue()/Decimal128/endianness/scale handling — that is a "
        "frequent source of silent bugs like all aggregates coming out zero). `column_ingest.hpp` "
        "is the sanctioned extension point for generic ingest behavior: if a flat scalar column "
        "needs handling the current helpers do not cover, extend that helper once while preserving "
        "Arrow safe-cast, null-mask, scale, and range-check semantics, then call it from the loader. "
        "Each helper takes the table and column name, accepts Arrow physical representations "
        "through Arrow casts, and returns a "
        "std::vector you index positionally into your struct-of-arrays. Use Arrow casts to decode "
        "correctly, then store the narrowest correct C++ representation chosen by the storage plan:\n"
        '  - DECIMAL / money / quantity -> `synnodb::ingest::scaled_integer<T>(*tables->TBL, "COL", DECIMALS)` '
        "(std::vector<T> of value*10^DECIMALS, exact fixed-point; choose T as the narrowest safe "
        "integer such as int16_t/int32_t/int64_t, and use the column's decimal scale);\n"
        '  - integer/code/key columns -> `synnodb::ingest::as_integer<T>(*tables->TBL, "COL")` '
        "(choose T from int8_t/uint8_t/.../int64_t/uint64_t based on the declared/ranged domain; "
        "do not default to int64_t when the plan proves a narrower type is correct);\n"
        '  - string columns  -> `synnodb::ingest::as_string(*tables->TBL, "COL")`;\n'
        '  - date columns    -> `synnodb::ingest::as_date_days(*tables->TBL, "COL")` (int32 days since 1970-01-01);\n'
        '  - floating columns -> `synnodb::ingest::as_double(*tables->TBL, "COL")`.\n'
        "`as_int64` and `scaled_int64` remain compatibility aliases, not a storage-layout default. "
        "Declare each Database column with the matching narrow element type."
    )

    if read_storage_plan:
        if persistent_storage:
            storage_hint = f"The storage plan is described in the file `{storage_plan_path}`. It describes the SSD-backed columnar storage layout: which columns to serialize to binary files, their sort order, and any zone-map or acceleration structures that fit within the RAM budget. Implement the ColumnHandle<T> and StringColumnHandle fields in the Database struct according to this plan, and make sure build() streams Parquet row groups and writes/registers every referenced persisted column. "
        else:
            storage_hint = (
                f"The storage plan is described in the file `{storage_plan_path}`. It describes how to store the parquet data in-memory for optimal query execution. Use this storage plan to implement the in-memory data structure accordingly. "
                + ingest_hint
            )
    else:
        if persistent_storage:
            storage_hint = """Use ColumnHandle<T> (from column_handle.hpp) for page-safe fixed-width numeric columns and StringColumnHandle for variable-length string columns. The minimum should be one binary file per page-safe fixed-width column and offsets + bytes files for each string column, struct-of-arrays layout. Flat fixed-width storage is valid only when BP_PAGE_BYTES % sizeof(T) == 0; otherwise use StringColumnHandle or the page-aligned fixed-char helpers. The Database struct must declare a handle for every column needed by the queries, and build() must stream Parquet row groups, serialize, register, and assign each handle."""
        else:
            storage_hint = "The minimum should be a struct-of-arrays." + ingest_hint

    return template.substitute(
        queries_path=queries_path,
        query_str=query_str,
        builder_path=builder_path,
        storage_hint=storage_hint,
        query_impl_path=query_impl_path,
        example_query=example_query,
        example_query_params=example_query_params,
        args_path=args_path,
        base_impl_todo_file=base_impl_todo_file,
        schema_hint=schema_hint,
        num_threads=num_threads,
        storage_plan_path=storage_plan_path,
    )


def base_impl_storage(
    builder_path: str,
    query_impl_path: str,
    base_impl_todo_file: str,
    args_path: str,
    persistent_storage: bool,
    storage_plan_filename: str,
) -> str:
    if persistent_storage:
        prompt_path = _PROMPTS_DIR / "ssd" / "base_impl_storage_ssd.txt"
    else:
        prompt_path = _PROMPTS_DIR / "base_impl_storage.txt"

    template_str = _load_txt(prompt_path)
    template = Template(template_str)
    return template.substitute(
        builder_path=builder_path,
        query_impl_path=query_impl_path,
        base_impl_todo_file=base_impl_todo_file,
        args_path=args_path,
        storage_plan_filename=storage_plan_filename,
    )


def _detect_hardware_context(
    serving_threads: int | None = None,
    memory_budget_mb: int | None = None,
) -> str:
    """Describe the envelope the optimizer must build within.

    This is deliberately *not* a raw dump of the host. The engine is designed,
    validated, and served at a configured degree of parallelism (``serving_threads``)
    and runs its build under a fixed memory ceiling (``memory_budget_mb``, enforced via
    the builder's cgroup ``memory.max``). Advertising the machine's full logical-core
    count or total RAM instead invites the model to oversubscribe threads and to trade
    memory for build speed until it overruns the budget and the build is killed - the
    out-of-memory-during-build failure mode the ceiling exists to prevent. So report the
    physical cores as a ceiling for one-time build parallelism, but anchor the numbers
    the model optimizes against to the configured serving threads and memory budget.
    """
    import psutil

    cpu_model = "unknown"
    total_ram_gb = 0

    try:
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            if line.startswith("model name"):
                cpu_model = line.partition(":")[2].strip()
                break
    except Exception:
        pass

    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemTotal:"):
                total_ram_gb = int(line.split()[1]) // (1024 * 1024)
                break
    except Exception:
        pass

    physical_cores = psutil.cpu_count(logical=False) or 0
    logical_cores = psutil.cpu_count(logical=True) or 0

    # Derive the build-time parallelism ceiling from the process's CPU affinity so
    # that cpuset/affinity-limited workers (where psutil.cpu_count() reports host-wide
    # counts) see the cores they can actually use, matching the run tool's own core
    # resolution via get_cores_for_current_machine().
    try:
        from synnodb.utils.core_utils import get_cores_for_current_machine

        usable_cores, _ = get_cores_for_current_machine(
            leave_core_0_out=False, allow_hyperthreading=True
        )
    except Exception:
        usable_cores = physical_cores

    parts = [f"CPU: {cpu_model}"]
    if usable_cores > 0:
        ht_str = (
            "hyperthreading enabled"
            if logical_cores > physical_cores
            else "no hyperthreading"
        )
        parts.append(f"{usable_cores} cores available for build parallelism ({ht_str})")
    elif logical_cores > 0:
        parts.append(f"{logical_cores} logical cores")

    if serving_threads and serving_threads > 0:
        parts.append(
            f"the engine is built, validated, and served at {serving_threads} query "
            "worker threads (size build-time parallelism to the available cores above, but "
            "this is the degree the engine actually runs at - do not exceed it for serving)"
        )

    # Memory is a hard ceiling, never free headroom. Report the budget the build runs
    # under so the model sizes structures to it; fall back to total RAM only when no
    # explicit budget is set, and even then frame it as a shared ceiling.
    if memory_budget_mb and memory_budget_mb > 0:
        parts.append(
            f"{memory_budget_mb // 1024} GB memory budget - a hard ceiling enforced on the "
            "build; exceeding it kills the build, so do not trade memory for speed beyond it"
        )
    elif total_ram_gb > 0:
        parts.append(
            f"{total_ram_gb} GB system RAM (shared ceiling - the build must fit well within "
            "it; do not size structures assuming all of it is free)"
        )

    return ", ".join(parts)


def base_optimize_build(
    builder_path_cpp: str,
    builder_path_hpp: str,
    current_ingest_time_ms: float,
    current_exec_config: ExecSettings,
    persistent_storage: bool,
    allow_storage_restructuring: bool,
    storage_plan_filename: str,
    base_impl_todo_filename: str,
    serving_threads: int | None = None,
    memory_budget_mb: int | None = None,
) -> str:
    if persistent_storage:
        prompt_path = _PROMPTS_DIR / "ssd" / "base_optimize_build_ssd.txt"
    else:
        prompt_path = _PROMPTS_DIR / "base_optimize_build.txt"

    if allow_storage_restructuring:
        storage_constraint = ""
        interface_compat_hint = ""
    else:
        storage_constraint = (
            "- Always load all data from Parquet during ingestion (do not skip rows or columns). "
            "Do not inspect `query*.cpp` to decide what to load, skip, or reorder. "
            "Storage layout must remain general enough that arbitrary SQL could in theory be executed."
        )
        interface_compat_hint = (
            f"- **Interface compatibility**: Any change to a field's type or layout in `{builder_path_hpp}` "
            "(column type, index structure, field name) must preserve the interface that existing query files use. "
            "If the underlying type changes, wrap it in a class that exposes the same `.find()`/`.end()` or `[]` "
            "semantics - do not modify query files to adapt to a new interface. "
            "A type mismatch can compile silently and crash at runtime."
        )

    template_str = _load_txt(prompt_path)
    template = Template(template_str)
    return template.substitute(
        builder_path_cpp=builder_path_cpp,
        builder_path_hpp=builder_path_hpp,
        config_str=str(current_exec_config),
        current_ingest_time_ms=f"{int(current_ingest_time_ms)} ms",
        run_tool_mode=RunToolMode.INGEST.name,
        storage_constraint=storage_constraint,
        interface_compat_hint=interface_compat_hint,
        hardware_context=_detect_hardware_context(
            serving_threads=serving_threads,
            memory_budget_mb=memory_budget_mb,
        ),
        storage_plan_filename=storage_plan_filename,
        base_impl_todo_filename=base_impl_todo_filename,
    )


def base_exec_validate_prompt(
    query_impl_path: str,
    args_path: str,
    builder_path: str,
    show_ssd_error_hints: bool,
) -> str:
    template_str = _load_txt(_PROMPTS_DIR / "base_exec_validate.txt")
    template = Template(template_str)
    return template.substitute(
        run_tool_mode=RunToolMode.EXHAUSTIVE.name,
        query_impl_path=query_impl_path,
        args_parser=args_path,
        builder_path=builder_path,
        error_hints="If the error mentions `ColumnHandle: computed range exceeds pinned page`, first audit storage/page invariants and every `pin_range()` call in the target query. Do not fix only the most recent suspected call site."
        if show_ssd_error_hints
        else "",
    )


def base_validate_mt_prompt(
    query_id: str,
    num_threads: int,
    builder_path: str,
    error: str,
) -> str:
    """Per-query fix prompt for the multi-threaded correctness gate: query
    ``query_id`` was correct single-threaded but diverges at ``num_threads``
    threads (a data race in its parallel section)."""
    template_str = _load_txt(_PROMPTS_DIR / "base_validate_mt.txt")
    template = Template(template_str)
    return template.substitute(
        query_id=query_id,
        query_file=f"query{query_id}.cpp",
        num_threads=num_threads,
        builder_path=builder_path,
        error=error,
    )


def base_impl_query_prompt(
    is_first_query: bool,
    sample_query_args_dict: dict | None,
    query_id: str,
    queries_path: str,
    args_path: str,
    builder_path: str,
    query_impl_path: str,
    sql: str,
    persistent_storage: bool,
    num_threads: int,
    storage_plan_filename: str,
    base_impl_todo_filename: str,
) -> str:
    if persistent_storage:
        prompt_path = _PROMPTS_DIR / "ssd" / "base_impl_query_ssd.txt"
    else:
        prompt_path = _PROMPTS_DIR / "base_impl_query.txt"

    template_str = _load_txt(prompt_path)
    template = Template(template_str)

    if is_first_query:
        prefix = "Start implementing the query execution logic. Implement ONLY"
    else:
        prefix = "Continue implementing the query execution logic. Implement ONLY"

    if sample_query_args_dict is not None and query_id in sample_query_args_dict:
        sample_args_str = f" Example instantiation of the query placeholders are:\n{sample_query_args_dict[query_id]}\nNULL values might appear in IN-Lists and are represented with the string '<<NULL>>'."
    else:
        sample_args_str = ""

    return template.substitute(
        prefix=prefix,
        query_id=query_id,
        sample_args_str=sample_args_str,
        queries_path=queries_path,
        args_path=args_path,
        builder_path=builder_path,
        query_impl_path=query_impl_path,
        sql=sql,
        parallelism_note=parallelism_note(num_threads),
        storage_plan_filename=storage_plan_filename,
        base_impl_todo_filename=base_impl_todo_filename,
    )


def base_exec_validate_for_query_prompt(
    query_id: str,
    run_tool_mode: RunToolMode,
    builder_path: str,
    sql: str,
    show_ssd_error_hints: bool,
) -> str:
    template_str = _load_txt(_PROMPTS_DIR / "base_exec_validate_for_query.txt")
    template = Template(template_str)

    if show_ssd_error_hints:
        error_hints = "If the error mentions `ColumnHandle: computed range exceeds pinned page`, first audit storage/page invariants and every `pin_range()` call in the target query. Do not fix only the most recent suspected call site."
    else:
        error_hints = ""

    return template.substitute(
        query_id=query_id,
        run_tool_mode=run_tool_mode.name,
        builder_path=builder_path,
        sql=sql,
        error_hints=error_hints,
    )


def _format_query_ids(query_ids: list[str]) -> str:
    """Render query ids as a run-tool argument list, e.g. ['1'] -> ["1"]."""
    return "[" + ", ".join(f'"{qid}"' for qid in query_ids) + "]"


def base_check_correctness_all_prompt(
    run_tool_mode: RunToolMode, query_ids: list[str]
) -> str:
    template_str = _load_txt(_PROMPTS_DIR / "base_correctness_all.txt")
    template = Template(template_str)
    return template.substitute(
        run_tool_mode=run_tool_mode.name,
        query_ids=_format_query_ids(query_ids),
    )


def base_run_all_and_fix_prompt(
    query_impl_path: str, run_tool_mode: RunToolMode, query_ids: list[str]
) -> str:
    template_str = _load_txt(_PROMPTS_DIR / "base_run_all_and_fix.txt")
    template = Template(template_str)
    return template.substitute(
        run_tool_mode=run_tool_mode.name,
        query_impl_path=query_impl_path,
        query_ids=_format_query_ids(query_ids),
    )


def base_fix_slow_queries_prompt(
    slow_queries: list[tuple[str, float, float, float]],
    query_impl_path: str,
    builder_path: str,
) -> str:
    """Build a prompt to fix queries that are more than 5x slower than DuckDB.

    Args:
        slow_queries: list of (query_id, impl_rt_ms, duckdb_rt_ms, speedup)
    """
    template_str = _load_txt(_PROMPTS_DIR / "base_fix_slow_queries.txt")
    lines = []
    for qid, impl_rt_ms, duckdb_rt_ms, speedup in slow_queries:
        lines.append(
            f"  - Query {qid}: {impl_rt_ms:.1f} ms (impl) vs {duckdb_rt_ms:.1f} ms (DuckDB) → {speedup:.3f}x speedup"
        )
    slow_query_list = "\n".join(lines)
    template = Template(template_str)
    return template.substitute(
        slow_query_list=slow_query_list,
        query_impl_path=query_impl_path,
        builder_path=builder_path,
    )


# ── Optimization round 1 ────────────────────────────────────


def optim_prompt_pretext(
    queries_path: str,
    num_queries: int,
    query_impl_path: str,
    builder_path: str,
) -> str:
    template_str = _load_txt(_PROMPTS_DIR / "optim_pretext_general.txt")
    template = Template(template_str)
    query_str = "query" if num_queries == 1 else "queries"
    return template.substitute(
        queries_path=queries_path,
        num_queries=num_queries,
        query_str=query_str,
        query_impl_path=query_impl_path,
        builder_path=builder_path,
    )


def optim_prompt_pretext_optim(
    bespoke_storage: bool,
    query_impl_path: str,
    builder_path: str,
    persistent_storage: bool,
) -> str:
    if persistent_storage:
        prompt_path = _PROMPTS_DIR / "ssd" / "optim_pretext_optim_ssd.txt"
    else:
        prompt_path = _PROMPTS_DIR / "optim_pretext_optim.txt"

    template_str = _load_txt(prompt_path)
    template = Template(template_str)
    storage_layout = "storage layout, " if bespoke_storage else ""
    return template.substitute(
        storage_layout=storage_layout,
        query_impl_path=query_impl_path,
        builder_path=builder_path,
    )


def optim_prompt_constraints(
    persistent_storage: bool,
    allow_storage_changes: bool = True,
) -> str:
    if persistent_storage:
        prompt_path = _PROMPTS_DIR / "ssd" / "optim_constraints_ssd.txt"
    else:
        prompt_path = _PROMPTS_DIR / "optim_constraints.txt"

    txt = _load_txt(prompt_path)
    if not allow_storage_changes:
        txt = (
            txt
            + "\n- You are NOT allowed to change the storage layout. Leave it as Struct-of-Arrays. Do not change the ordering of columns."
        )
    return txt


def optim_prompt_pinning(core_id: int) -> str:
    template_str = _load_txt(_PROMPTS_DIR / "optim_pinning.txt")
    template = Template(template_str)
    affinity_prompt = get_affinity_prompt(include_numa=False)
    return template.substitute(
        query_impl_path="query_impl.cpp",
        affinity_prompt=affinity_prompt,
        core_id=core_id,
    )


def optim_prompt_add_timings_pretext() -> str:
    return _load_txt(_PROMPTS_DIR / "optim_add_timings_collect_stats_pretext.txt")


def optim_prompt_add_timings_per_query(
    qids_str: str,
    refer_to_prev_queries: bool,
) -> str:
    template_str = _load_txt(
        _PROMPTS_DIR / "optim_add_timings_collect_stats_per_query.txt"
    )
    template = Template(template_str)
    return template.substitute(
        qids_str=qids_str,
        refer_to_prev=" Align instrumentation with previous queries."
        if refer_to_prev_queries
        else "",
        run_tool_mode=RunToolMode.FAST_CHECK.name,
    )


def target_rt_prompt(model: str, current_rt_ms: float, rt_reduction: float = 2) -> str:
    if model in ["gpt-5.4"]:
        # model specific prompting with runtime target
        target_rt_ms = current_rt_ms / rt_reduction
        misc = f"\n\nAim for a {rt_reduction}x runtime reduction. The current runtime of the query is {int(current_rt_ms)}ms / target: {int(target_rt_ms)}ms."
    else:
        misc = ""

    return misc


def optim_prompt_w_sample_plan(
    query_id: str,
    constraints_str: str,
    query_plan: str,
    engine: str,
    general_pretext: str,
    model: str,
    current_rt_ms: float,
    current_exec_settings: ExecSettings,
    tracing_data: str,
    persistent_storage: bool,
) -> str:
    template_str = _load_txt(_PROMPTS_DIR / "optim_w_sample_plan.txt")
    template = Template(template_str)

    additional_instructions = ""
    tracing_block = ""
    if persistent_storage:
        tracing_block = f"Initial tracing/profiling output (single-threaded):\n```\n{tracing_data}\n```\n"
        additional_instructions += "- Storage access pattern alignment (which columns to scan, in what order, with what zone-map pruning).\n"
        additional_instructions += "Choose algorithms that will parallelize cleanly later (prefer sort-merge or partitioned hash over shared-mutable-hash designs; prefer per-row-range processing that can be split across threads).\n"

    return template.substitute(
        query_id=query_id,
        constraints=constraints_str,
        query_plan=query_plan,
        engine=engine,
        general_pretext=general_pretext,
        misc=target_rt_prompt(model, current_rt_ms),
        tracing_block=tracing_block,
        additional_instructions=additional_instructions,
        run_tool_mode_correctness=RunToolMode.EXHAUSTIVE.name,
        run_tool_mode_benchmark=RunToolMode.BENCHMARK.name,
        exec_settings_str=str(current_exec_settings),
    )


def optim_prompt_w_trace(
    query_id: str,
    constraints_str: str,
    current_rt_ms: float,
    current_exec_settings: ExecSettings,
    storage_is_bespoke: bool,
    tracing_data: str,
    general_pretext: str,
    model: str,
    persistent_storage: bool,
) -> str:
    template_str = _load_txt(_PROMPTS_DIR / "optim_w_trace.txt")
    template = Template(template_str)

    hints = ""
    more_constraints = ""
    if persistent_storage:
        hints += """- High `buffer_pool_read_page`, high `buffer_pool_page_misses`, or high `buffer_pool_bytes_read` indicates **I/O-bound** work. Reduce bytes read, improve zone-map skipping, or switch from one driver thread pinning all pages to page-range/chunk ownership by workers.
- High `buffer_pool_pin_page` with low `buffer_pool_read_page` indicates **buffer-pool metadata contention** or excessive pin/unpin frequency. Use larger chunks, fewer pins, or avoid repeatedly pinning already-known pages.
- A compute scope dominating while buffer-pool miss/read metrics are low indicates a **CPU-bound** hot loop — apply inner-loop optimizations (branchless, SIMD, register-resident accumulators).
- If per-thread scopes or counters are present and one thread dominates, that indicates **thread skew**. Repartition work or use a chunk queue.
- A scope dominated by shared accumulator/hash-table updates indicates **contention** — shard the resource, use thread-local copies and merge, or reduce hold time.\n"""
        more_constraints += "- A change that improves single-thread wall time but worsens parallel scalability is a regression. If per-thread trace scopes are available, per-thread time variance should not grow.\n"

    return template.substitute(
        query_id=query_id,
        constraints=constraints_str,
        bespoke_storage_related=" e.g. changes to the storage layout and especially ordering of columns"
        if storage_is_bespoke
        else "",
        tracing_data=tracing_data,
        general_pretext=general_pretext,
        misc=target_rt_prompt(model, current_rt_ms),
        analyze_hints=hints,
        more_constraints=more_constraints,
        exec_settings_str=str(current_exec_settings),
    )


def optim_prompt_w_human_reference(
    query_id: str,
    tracing_data: str,
    general_pretext: str,
    constraints_str: str,
    current_rt_ms: float,
    current_exec_settings: ExecSettings,
    storage_is_bespoke: bool,
    model: str,
    num_turns: int,
) -> str:
    template_str = _load_txt(_PROMPTS_DIR / "optim_w_human_reference.txt")
    template = Template(template_str)
    return template.substitute(
        query_id=query_id,
        constraints=constraints_str,
        bespoke_storage_related=" e.g. changes to the storage layout and especially ordering of columns"
        if storage_is_bespoke
        else "",
        tracing_data=tracing_data,
        general_pretext=general_pretext,
        misc=target_rt_prompt(model, current_rt_ms),
        num_turns=num_turns,
        exec_settings_str=str(current_exec_settings),
    )


def load_expert_knowledge(persistent_storage: bool) -> str:
    if persistent_storage:
        prompt_path = _PROMPTS_DIR / "ssd" / "expert_knowledge_ssd.txt"
    else:
        prompt_path = _PROMPTS_DIR / "expert_knowledge.txt"
    return _load_txt(prompt_path)


def optim_prompt_w_expert_knowledge(
    query_id: str,
    constraints_str: str,
    expert_knowledge: str,
    current_rt_ms: float,
    storage_is_bespoke: bool,
    general_pretext: str,
    model: str,
    persistent_storage: bool,
) -> str:
    if persistent_storage:
        prompt_path = _PROMPTS_DIR / "ssd" / "optim_w_expert_knowledge_ssd.txt"
    else:
        prompt_path = _PROMPTS_DIR / "optim_w_expert_knowledge.txt"

    template_str = _load_txt(prompt_path)
    template = Template(template_str)
    return template.substitute(
        query_id=query_id,
        constraints=constraints_str,
        expert_knowledge=expert_knowledge,
        bespoke_storage_related=" e.g. changes to the storage layout and especially ordering of columns"
        if storage_is_bespoke
        else "",
        general_pretext=general_pretext,
        misc=target_rt_prompt(model, current_rt_ms),
    )


# ── Optimization round 2: multi-threading ────────────────────────────────────


def optim2_prompt_constraints(
    allow_storage_changes: bool, persistent_storage: bool
) -> str:
    if persistent_storage:
        prompt_path = _PROMPTS_DIR / "ssd" / "optim2_constraints_ssd.txt"
    else:
        prompt_path = _PROMPTS_DIR / "optim2_constraints.txt"

    txt = _load_txt(prompt_path)
    if not allow_storage_changes:
        txt += "\n- You are NOT allowed to change the storage layout. Leave it as Struct-of-Arrays. Do not change the ordering of columns."
    return txt


def optim2_prompt_add_threadpool(
    db_loader_filename: str,
    thread_pool_filename: str,
    general_pretext: str,
    constraints_str: str,
    storage_is_bespoke: bool,
) -> str:
    template_str = _load_txt(_PROMPTS_DIR / "optim2_spawn_threadpool.txt")
    template = Template(template_str)
    return template.substitute(
        db_loader_filename=db_loader_filename,
        thread_pool_filename=thread_pool_filename,
        general_pretext=general_pretext,
        constraints=constraints_str,
        bespoke_storage_related=" e.g. changes to the storage layout and especially ordering of columns"
        if storage_is_bespoke
        else "",
        query_impl_cpp_filename="query_impl.cpp",
    )


def optim2_prompt_introduce_threading(
    query_id: str,
    constraints_str: str,
    current_rt_ms: float,
    general_pretext: str,
    storage_is_bespoke: bool,
    thread_pool_filename: str,
    db_loader_header_filename: str,
    persistent_storage: bool,
    tracing_data: str,
) -> str:
    if persistent_storage:
        prompt_path = _PROMPTS_DIR / "ssd" / "optim2_introduce_threading_ssd.txt"
    else:
        prompt_path = _PROMPTS_DIR / "optim2_introduce_threading.txt"

    template_str = _load_txt(prompt_path)
    template = Template(template_str)
    tracing_block = f"```\n{tracing_data}\n```\n"
    return template.substitute(
        constraints=constraints_str,
        query_id=query_id,
        current_rt=f"{int(current_rt_ms)}ms",
        bespoke_storage_related=" e.g. changes to the storage layout and especially ordering of columns"
        if storage_is_bespoke
        else "",
        general_pretext=general_pretext,
        thread_pool_filename=thread_pool_filename,
        db_loader_filename=db_loader_header_filename,
        tracing_block=tracing_block,
        run_tool_mode_correctness=RunToolMode.EXHAUSTIVE.name,
        run_tool_mode_benchmark=RunToolMode.BENCHMARK.name,
    )


def optim2_prompt_check_large_sf(
    general_pretext: str,
    constraints_str: str,
    storage_is_bespoke: bool,
) -> str:
    template_str = _load_txt(_PROMPTS_DIR / "optim2_check_large_sf.txt")
    template = Template(template_str)
    return template.substitute(
        general_pretext=general_pretext,
        constraints=constraints_str,
        bespoke_storage_related=" e.g. changes to the storage layout and especially ordering of columns"
        if storage_is_bespoke
        else "",
        run_tool_mode=RunToolMode.BENCHMARK.name,
    )


def optim2_prompt_optimize_w_trace(
    query_id: str,
    constraints_str: str,
    current_rt_ms: float,
    current_exec_settings: ExecSettings,
    tracing_data: str,
    general_pretext: str,
    storage_is_bespoke: bool,
    single_threaded_rt_ms: float,
) -> str:
    template_str = _load_txt(_PROMPTS_DIR / "optim2_optimize_w_trace.txt")
    template = Template(template_str)
    return template.substitute(
        constraints=constraints_str,
        query_id=query_id,
        current_rt=f"{int(current_rt_ms)}ms",
        tracing_data=tracing_data,
        bespoke_storage_related=" e.g. changes to the storage layout and especially ordering of columns"
        if storage_is_bespoke
        else "",
        general_pretext=general_pretext,
        st_rt=f"{int(single_threaded_rt_ms)}ms",
        exec_settings_str=str(current_exec_settings),
    )


# ── Sueprvision agent ────────────────────────────────────

SUPERVISION_SUCCESS_KW = "Success."

_DEV_HINTS_PROMPT_INSTRUCTION = (
    "\n- Also assess whether the supervised agent seemed confused, contradicted "
    "itself, or whether you noticed a likely bug or ambiguity in the pipeline or "
    "prompts themselves (as opposed to the agent simply doing a poor job) — this "
    "is meant to help the developers of this system, not the supervised agent. "
    "Wrap it in <dev_hints></dev_hints> tags, placed right after </run_summary>. "
    'Write "None" inside the tags if nothing noteworthy stood out.'
)

_RUN_SUMMARY_RE = re.compile(
    r"<run_summary>(.*?)</run_summary>", re.IGNORECASE | re.DOTALL
)
_DEV_HINTS_RE = re.compile(r"<dev_hints>(.*?)</dev_hints>", re.IGNORECASE | re.DOTALL)


@dataclass
class SupervisionResult:
    approved: bool
    feedback_text: (
        str  # `output` with the <run_summary>/<dev_hints> blocks stripped out
    )
    run_summary: str | None
    dev_hints: str | None  # None if absent or literally "None"


def parse_supervision_output(output: str) -> SupervisionResult:
    """Parse a supervisor agent's raw response into its structured parts.

    The supervisor is prompted to emit a `<run_summary>` (always) and a
    `<dev_hints>` (only when enabled) block ahead of the final verdict line.
    Those blocks are meta-information for the dashboard/developers — they are
    stripped out of `feedback_text`, which is what gets echoed back to the
    *supervised* agent as feedback when the stage isn't approved.
    """
    run_summary_match = _RUN_SUMMARY_RE.search(output)
    run_summary = run_summary_match.group(1).strip() if run_summary_match else None

    dev_hints_match = _DEV_HINTS_RE.search(output)
    dev_hints_raw = dev_hints_match.group(1).strip() if dev_hints_match else None
    dev_hints = (
        None
        if not dev_hints_raw or dev_hints_raw.strip().lower() == "none"
        else dev_hints_raw
    )

    feedback_text = _RUN_SUMMARY_RE.sub("", output)
    feedback_text = _DEV_HINTS_RE.sub("", feedback_text).strip()

    last_line = feedback_text.rsplit("\n", 1)[-1].strip()
    approved = last_line.lower() == SUPERVISION_SUCCESS_KW.strip().lower()

    return SupervisionResult(
        approved=approved,
        feedback_text=feedback_text,
        run_summary=run_summary,
        dev_hints=dev_hints,
    )


def supervision_agent_prompt(
    user_prompt: str,
    activity_summary: list[str],
    llm_output: str,
    stage_overview: str,
    be_relaxed_if_runtime_goal_not_reached: bool = False,
    generate_dev_hints: bool = False,
) -> str:
    template_str = _load_txt(_PROMPTS_DIR / "supervision_prompt.txt")
    template = Template(template_str)
    activity_summary_str = chr(10).join(
        f"- {activity}" for activity in activity_summary
    )

    misc = ""
    if be_relaxed_if_runtime_goal_not_reached:
        misc += "Be relaxed if a runtime goal is not reached. If you have the feeling that the agent spend some effort to reduce the runtime, this is fine. Approve the stage."

    return template.substitute(
        prompt=user_prompt,
        activity_summary=activity_summary_str,
        llm_output=llm_output,
        stage_overview=stage_overview,
        success_keyword=SUPERVISION_SUCCESS_KW,
        dev_hints_instruction=_DEV_HINTS_PROMPT_INSTRUCTION
        if generate_dev_hints
        else "",
        misc=misc,
    )


def get_affinity_prompt(
    include_numa: bool = False,
    filename: str = "cpu_affinity.hpp",
) -> str:
    numa_section = ""
    if include_numa:
        assert not include_numa
        numa_section = textwrap.dedent("""\
            NUMA placement:
              Pin the current process to a specific NUMA node to improve memory locality
              during initialization or data ingestion:
                void pin_process_to_numa_node(int node_id);

              Query the number of logical CPUs associated with a NUMA node:
                int get_numa_node_cpu_count(int node_id);

        """)

    return textwrap.dedent(f"""\
        CPU affinity helpers is predefined in {filename}.
        You have to use the following functions, no need to implement them yourself,
        they are already provided by the runtime:

        {numa_section}CPU affinity:
          Pin the process to a single logical CPU for deterministic execution:
            void pin_process_to_cpu(int cpu_id);

          Restore affinity to all available CPUs:
            void unpin_process_from_cpus();
    """)
