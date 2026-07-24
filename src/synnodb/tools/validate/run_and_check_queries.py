import dataclasses
import logging
from collections import Counter, defaultdict, deque
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import DefaultDict, Dict, List, Optional

import pandas as pd
import pyarrow as pa

import wandb
from synnodb.observability.logging.wandb_plots_gen import create_wandb_speedup_plot
from synnodb.router.adapt import candidate_superset, results_diff, results_equal
from synnodb.router.normalize import top_level_limit_offset, widened_query
from synnodb.router.process_engine import read_and_delete_result
from synnodb.utils.utils import prefix_dict
from synnodb.workloads.query_execution_cache import QueryExecutionCache
from synnodb.workloads.system_factory import System
from synnodb.workloads.workload_provider import ExecSettings, QueryBatch, QueryEntry

logger = logging.getLogger(__name__)


@dataclass
class Measurement:
    query_id: str
    req_id: str
    exec_time: float


def assemble_error(
    exec_settings: ExecSettings | None,
    query_ids_executed: List[str],
    exception: bool = True,
    query_id: Optional[str] = None,
    query_id_not_recognized: Optional[str] = None,
) -> Dict:
    # assemble default failed metrics

    return {
        **prefix_dict(
            asdict(exec_settings) if exec_settings is not None else {}, "validation/"
        ),
        "validation/correct": False,
        "validation/error": exception,
        "validation/query_ids_executed": query_ids_executed,
        "validation/num_queries": len(query_ids_executed),
        "validation/num_successful_queries": 0,
        "validation/failed_query_id": query_id,
        "validation/query_id_not_recognized": query_id_not_recognized,
    }


def assemble_exec(
    exec_settings: ExecSettings | None, num_queries_executed: int
) -> Dict:
    # assemble default successful metrics without correctness info
    return {
        **prefix_dict(
            asdict(exec_settings) if exec_settings is not None else {}, "validation/"
        ),
        "validation/num_queries": num_queries_executed,
    }


def _widened_reference_fetcher(
    query_execution_cache: QueryExecutionCache,
    query_batch: QueryBatch,
    inst: QueryEntry,
):
    """A ``fetch_widened(limit)`` for :func:`candidate_superset`: runs *inst*'s query over the
    first *limit* rows of its ranking on DuckDB and returns the Arrow result (``None`` if it
    cannot be built, rewritten, or executed).

    It goes through the ordinary query-execution cache, so each widened variant is executed once
    and then read from disk on later runs. The variants are keyed on their own SQL, so they never
    collide with the measured query's cache entry - and their runtimes are ignored, since these
    are correctness queries, not benchmark ones.
    """

    def fetch(limit: int) -> Optional[pa.Table]:
        sql = widened_query(inst.sql, limit)
        if sql is None:
            return None
        entry = dataclasses.replace(
            inst, sql=sql, query_args="", rep_index=0, num_reps=1
        )
        batch = dataclasses.replace(query_batch, query_list=[entry])
        try:
            results = query_execution_cache.lookup_or_execute_query_batch(
                system=System.DUCKDB, batch=batch
            )
        except Exception as exc:
            # A widened reference is an optimization, never a correctness requirement: without it
            # the comparison stays strict. Most likely cause is a cache in only_from_cache mode.
            logger.warning(
                f"could not fetch a widened reference for query {inst.query_id} "
                f"(LIMIT {limit}): {exc!r}"
            )
            return None
        return results[0].result if results else None

    return fetch


@dataclass
class ValidationOutput:
    result_message: str
    correct: bool
    metrics: Dict
    trace_output: Optional[str] = None


def check_output_correctness(
    exec_settings: ExecSettings,
    query_batch: QueryBatch,
    measurements: List[Measurement],
    out_path: Path,
    cmd: Optional[str],
    stop_on_first_error: bool,
    all_query_ids: List[str],
    stdout: Optional[str],
    stderr: Optional[str],
    trace_mode: bool,
    query_execution_cache: QueryExecutionCache,
    trace_data: str = "",
    use_umbra: bool = False,
) -> ValidationOutput:
    logger.info(f"Comparing results with DuckDB for {exec_settings}...")

    # retrieve query ids executed from instantiations
    query_ids_executed_set = set()
    for inst in query_batch.query_list:
        query_ids_executed_set.add(inst.query_id)
    query_ids_executed: List[str] = sorted(list(query_ids_executed_set))

    # collect the runtimes for each query
    duckdb_rt_lists: DefaultDict[str, List] = defaultdict(list)
    bespoke_rt_lists: DefaultDict[str, List] = defaultdict(list)
    umbra_rt_lists: DefaultDict[str, List] = defaultdict(list)

    log_collector = ""

    trace_output = trace_data if trace_mode else None

    # run queries against duckdb
    duckdb_reference_results = query_execution_cache.lookup_or_execute_query_batch(
        system=System.DUCKDB, batch=query_batch
    )
    if use_umbra:
        umbra_reference_results = query_execution_cache.lookup_or_execute_query_batch(
            system=System.UMBRA, batch=query_batch
        )
    else:
        umbra_reference_results = [None] * len(query_batch.query_list)
    assert len(duckdb_reference_results) == len(query_batch.query_list), (
        f"Expected number of reference results from DuckDB ({len(duckdb_reference_results)}) to match number of queries in batch ({len(query_batch.query_list)})."
    )

    # compare with duckdb output
    for i, (inst, rt, duckdb_res, umbra_res) in enumerate(
        zip(
            query_batch.query_list,
            measurements,
            duckdb_reference_results,
            umbra_reference_results,
        )
    ):
        if rt.query_id != inst.query_id:
            return ValidationOutput(
                result_message=f'Error: query id stdout "{rt.query_id}" does not match expected query-id {inst.query_id} for line {i}.',
                correct=False,
                metrics=assemble_error(
                    exec_settings=exec_settings,
                    query_ids_executed=query_ids_executed,
                    exception=True,
                    query_id=inst.query_id,
                ),
                trace_output=trace_output,
            )

        # extract reference system runtimes
        umbra_time_ms = umbra_res.exec_time_ms if umbra_res is not None else None
        duckdb_time_ms = duckdb_res.exec_time_ms

        bespoke_rt_lists[inst.query_id].append(rt.exec_time)
        duckdb_rt_lists[inst.query_id].append(duckdb_time_ms)
        if umbra_time_ms is not None:
            umbra_rt_lists[inst.query_id].append(umbra_time_ms)

        # The reference is exact Arrow (DECIMAL stays decimal128). The comparison below is
        # Arrow-to-Arrow so decimals compare exactly and only genuine float columns use a
        # tolerance. A pandas round-trip would coerce DuckDB's decimals to float64 and make an
        # exact-but-differently-typed bespoke value compare unequal - the "values look identical
        # but pandas says they differ" failure.
        assert duckdb_res.result is not None, (
            "DuckDB result is None, cannot compare results"
        )
        reference_table = duckdb_res.result

        # log times
        if umbra_time_ms is not None:
            faster = rt.exec_time < umbra_time_ms
            logger.info(
                f"Q{inst.query_id} ({exec_settings}): {rt.exec_time}ms (Bespoke), {umbra_time_ms:.2f}ms (Umbra) {'-- faster' if faster else ''}"
            )
        else:
            logger.info(
                f"Q{inst.query_id} ({exec_settings}): {rt.exec_time}ms (Bespoke), {duckdb_time_ms:.2f}ms (DuckDB)"
            )

        bespoke_rt_lists[inst.query_id].append(rt.exec_time)

        # The engine writes its result as exact Arrow (cpp_helpers/result_writer.hpp:
        # "result_<req_id>.arrow"). Read it as Arrow and keep it that way for the comparison.
        filename = f"result_{rt.req_id}.arrow"
        res_path = out_path / filename
        if not res_path.exists():
            return ValidationOutput(
                result_message=f"Error: {res_path} not found after executing command: {cmd}",
                correct=False,
                metrics=assemble_error(
                    exec_settings=exec_settings,
                    query_ids_executed=query_ids_executed,
                    exception=True,
                    query_id=inst.query_id,
                ),
                trace_output=trace_output,
            )

        try:
            # Reads the result into memory and deletes it immediately, so nothing stale is left
            # on disk for a later run to misvalidate (see read_and_delete_result).
            bespoke_table = read_and_delete_result(res_path)
        except Exception as e:
            return ValidationOutput(
                result_message=f"Error: failed to read result Arrow {filename}: {e}",
                correct=False,
                metrics=assemble_error(
                    exec_settings=exec_settings,
                    query_ids_executed=query_ids_executed,
                    exception=True,
                    query_id=inst.query_id,
                ),
                trace_output=trace_output,
            )

        # ---- Arrow-to-Arrow correctness check -------------------------------------------------
        # Compare the engine's exact Arrow result to DuckDB's exact Arrow reference with the shared
        # comparator (router.adapt.results_equal): DECIMAL / int / string / date / bool columns must
        # match EXACTLY (a NULL is distinct from any value), and only genuine float (DOUBLE) columns
        # use a tolerance. A top-level ORDER BY is checked tie-aware - the key columns must agree in
        # sequence while rows tied on the keys may permute. This replaces a pandas
        # assert_frame_equal that coerced decimals to float and flagged exact-but-differently-typed
        # values as different.
        ref_names = list(reference_table.column_names)
        bes_names = list(bespoke_table.column_names)
        # A multiset comparison, not a set one: a query may legitimately project the same column
        # name twice (e.g. SELECT n.name, cn.name ...), so a plain set would hide a count mismatch
        # and dedupe the duplicate names.
        if Counter(ref_names) != Counter(bes_names):
            output = (
                f"Result columns do not match for query {inst.query_id} "
                f"with placeholders: {inst.placeholders}\n(SQL: {inst.sql})\n"
                f"Reference columns: {ref_names}\nBespoke columns: {bes_names}\n"
            )
            if stop_on_first_error:
                return ValidationOutput(
                    result_message=output,
                    correct=False,
                    metrics=assemble_error(
                        exec_settings=exec_settings,
                        query_ids_executed=query_ids_executed,
                        exception=False,
                        query_id=inst.query_id,
                    ),
                    trace_output=trace_output,
                )
            logger.error(output)
            log_collector += output + "\n"
        else:
            # Align bespoke columns to the reference order so the positional comparison lines up.
            bes_indices_by_name: DefaultDict[str, deque] = defaultdict(deque)
            for idx, col_name in enumerate(bes_names):
                bes_indices_by_name[col_name].append(idx)
            align_indices = [
                bes_indices_by_name[col_name].popleft() for col_name in ref_names
            ]
            bespoke_aligned = bespoke_table.select(align_indices)

            # A top-level ORDER BY makes row order meaningful: resolve its key columns to output
            # indices for a tie-aware comparison; otherwise compare with set/multiset semantics.
            ordered = inst.order_by_info is not None and len(inst.order_by_info) > 0
            order_keys = None
            if ordered:
                sort_cols = [
                    "count_star()" if col.lower() == "count(*)" else col
                    for col, _ in inst.order_by_info
                ]
                missing = [c for c in sort_cols if c not in ref_names]
                assert not missing, (
                    f"ORDER BY column(s) {missing} not in result {ref_names}\n{inst.sql}\n{inst.placeholders}"
                )
                order_keys = [ref_names.index(c) for c in sort_cols]

            # A top-level LIMIT cutting through a tie group makes DuckDB's pick at the cut
            # arbitrary - and not stable across runs of the identical query - so demanding the
            # engine reproduce it rejects correct implementations. Re-run the query wide enough to
            # hold the whole ranking the window was drawn from and check membership instead.
            row_limit, row_offset = top_level_limit_offset(inst.sql)
            candidates = None
            if ordered and order_keys and row_limit is not None:
                candidates = candidate_superset(
                    reference_table,
                    order_keys=order_keys,
                    row_limit=row_limit,
                    row_offset=row_offset,
                    fetch_widened=_widened_reference_fetcher(
                        query_execution_cache, query_batch, inst
                    ),
                    float_tol=1e-5,
                )
                if candidates is None and reference_table.num_rows == row_limit:
                    logger.warning(
                        f"Query {inst.query_id} was truncated by LIMIT {row_limit} but its tie "
                        f"group at the cut could not be closed; comparing strictly, which may "
                        f"reject a correct engine that broke the tie differently.\n"
                        f"(SQL: {inst.sql})"
                    )

            if not results_equal(
                bespoke_aligned,
                reference_table,
                ordered=ordered,
                order_keys=order_keys,
                float_tol=1e-5,
                row_limit=row_limit,
                candidates=candidates,
            ):
                diffs, total = results_diff(
                    bespoke_aligned,
                    reference_table,
                    ordered=ordered,
                    order_keys=order_keys,
                    row_limit=row_limit,
                    candidates=candidates,
                )
                diff_lines = "\n".join(
                    f"  row {r}, column '{c}': bespoke={a!r} duckdb={b!r}"
                    for r, c, a, b in diffs
                )
                output = (
                    f"Results do not match for query {inst.query_id} "
                    f"with placeholders: {inst.placeholders}\n(SQL: {inst.sql})\n"
                    f"{'Order or data' if ordered else 'Data (set-semantic)'} differs: {total} "
                    f"differing cell(s)/row(s); first {len(diffs)}:\n{diff_lines}\n"
                    f"Reference (first rows):\n{reference_table.slice(0, 12).to_pandas()}\n"
                    f"Bespoke (first rows):\n{bespoke_aligned.slice(0, 12).to_pandas()}\n"
                )
                if stop_on_first_error:
                    return ValidationOutput(
                        result_message=output,
                        correct=False,
                        metrics=assemble_error(
                            exec_settings=exec_settings,
                            query_ids_executed=query_ids_executed,
                            exception=False,
                            query_id=inst.query_id,
                        ),
                        trace_output=trace_output,
                    )
                logger.error(output)
                log_collector += output + "\n"

    if log_collector != "":
        return ValidationOutput(
            result_message=log_collector,
            correct=False,
            metrics=assemble_error(
                exec_settings=exec_settings,
                query_ids_executed=query_ids_executed,
                exception=False,
            ),
            trace_output=trace_output,
        )

    # Compute aggregate statistics
    avg_duckdb_rts = dict()
    avg_bespoke_rts = dict()
    avg_umbra_rts = dict()

    assert len(query_ids_executed) > 0, "No queries to validate"
    assert len(bespoke_rt_lists) > 0, "No runtimes recorded"

    for query in query_ids_executed:
        q = str(query)
        if q not in duckdb_rt_lists or q not in bespoke_rt_lists:
            return ValidationOutput(
                result_message=(
                    f"Error: missing runtime measurements for query {q}. "
                    f"Reference keys: {sorted(duckdb_rt_lists.keys())}, your keys: {sorted(bespoke_rt_lists.keys())}"
                ),
                correct=False,
                metrics=assemble_error(
                    exec_settings=exec_settings,
                    query_ids_executed=query_ids_executed,
                    exception=False,
                    query_id=q,
                ),
                trace_output=trace_output,
            )

        if len(duckdb_rt_lists[q]) == 0 or len(bespoke_rt_lists[q]) == 0:
            return ValidationOutput(
                result_message=(
                    f"Error: no runtime measurements recorded for query {q}. "
                    f"Umbra runtimes: {umbra_rt_lists[q]}, your runtimes: {bespoke_rt_lists[q]}"
                ),
                correct=False,
                metrics=assemble_error(
                    exec_settings=exec_settings,
                    query_ids_executed=query_ids_executed,
                    exception=False,
                    query_id=q,
                ),
                trace_output=trace_output,
            )

        avg_duckdb_rt = sum(duckdb_rt_lists[q]) / len(duckdb_rt_lists[q])
        avg_bespoke_rt = sum(bespoke_rt_lists[q]) / len(bespoke_rt_lists[q])

        if q in umbra_rt_lists and len(umbra_rt_lists[q]) > 0:
            avg_umbra_rt = sum(umbra_rt_lists[q]) / len(umbra_rt_lists[q])
            avg_umbra_rts[q] = avg_umbra_rt

        avg_duckdb_rts[q] = avg_duckdb_rt
        avg_bespoke_rts[q] = avg_bespoke_rt

    # compute total runtimes, total speedup, average speedup
    total_duckdb_rt = sum(avg_duckdb_rts.values())
    total_bespoke_rt = sum(avg_bespoke_rts.values())
    if len(avg_umbra_rts) == len(avg_duckdb_rts):
        total_umbra_rt = sum(avg_umbra_rts.values())
    else:
        total_umbra_rt = None
    total_speedup = (
        total_duckdb_rt / total_bespoke_rt if total_bespoke_rt > 0 else float("inf")
    )
    average_speedup = sum(
        (
            avg_duckdb_rts[q] / avg_bespoke_rts[q]
            for q in avg_duckdb_rts
            if avg_bespoke_rts[q] > 0
        )
    ) / len(avg_duckdb_rts)

    umbra_rt_str = (
        f"{total_umbra_rt:.2f}ms (Umbra)"
        if total_umbra_rt is not None
        else "N/A (Umbra)"
    )
    logger.info(
        f"Aggregated Runtimes: {total_bespoke_rt:.2f}ms (Bespoke) vs {umbra_rt_str} vs {total_duckdb_rt:.2f}ms (DuckDB)"
    )
    logger.info(f"Avg. Speedup: {average_speedup:.2f}x")
    logger.info(f"Total Speedup: {total_speedup:.2f}x")

    # Report metrics to wandb if callback is set

    metrics = {
        **prefix_dict(asdict(exec_settings), "validation/"),
        "validation/correct": True,
        "validation/error": False,
        "validation/total_duckdb_runtime_ms": total_duckdb_rt,
        "validation/total_bespoke_runtime_ms": total_bespoke_rt,
        "validation/total_umbra_runtime_ms": total_umbra_rt,
        "validation/total_speedup": total_speedup,
        "validation/avg_speedup": average_speedup,
        "validation/num_queries": len(query_ids_executed),
        "validation/num_successful_queries": len(query_ids_executed),
        "validation/query_ids_executed": query_ids_executed,
    }

    if len(query_ids_executed) == len(all_query_ids):
        metrics["validation/all_queries"] = True
        metrics["validation/all_queries_avg_speedup"] = average_speedup
        metrics["validation/all_queries_total_speedup"] = total_speedup

        # prepare to log full speeedups table
        measurements_df = pd.DataFrame(
            {
                "query_id": query_ids_executed,
                "duckdb_runtime_ms": [
                    avg_duckdb_rts[str(q)] for q in query_ids_executed
                ],
                "bespoke_runtime_ms": [
                    avg_bespoke_rts[str(q)] for q in query_ids_executed
                ],
                "umbra_runtime_ms": [
                    avg_umbra_rts.get(str(q)) for q in query_ids_executed
                ],
            }
        )
        t = wandb.Table(dataframe=measurements_df)
        metrics["validation/all_queries_data"] = t
        metrics["validation/all_queries_plot"] = create_wandb_speedup_plot(
            t, exec_settings
        )

    # Add per-query metrics
    for q in query_ids_executed:
        # make q 3-digit string with leading zeros
        q_str = str(q)
        q_3d_str = q_str.zfill(3)

        metrics[f"validation/query_{q_3d_str}/duckdb_runtime_ms"] = avg_duckdb_rts[
            q_str
        ]
        metrics[f"validation/query_{q_3d_str}/bespoke_runtime_ms"] = avg_bespoke_rts[
            q_str
        ]
        metrics[f"validation/query_{q_3d_str}/umbra_runtime_ms"] = (
            avg_umbra_rts[q_str] if q_str in avg_umbra_rts else None
        )
        metrics[f"validation/query_{q_3d_str}/speedup"] = (
            avg_duckdb_rts[q_str] / avg_bespoke_rts[q_str]
            if avg_bespoke_rts[q_str] > 0
            else float("inf")
        )

    result_message = ""
    if stdout is not None or stderr is not None:
        result_message += f"STDOUT:\n{stdout}\n"
        result_message += f"STDERR:\n{stderr}\n"
        result_message += "BENCHMARK AND VALIDATION RESULTS:\n"

    result_message += (
        "All results match!\n"
        + f"DuckDB runtimes (ms): {', '.join([f'Query {q}: {r:.2f}' for q, r in avg_duckdb_rts.items()])} - sum: {sum(avg_duckdb_rts.values()):.2f}\n"
        + f"Your Implementation runtimes (ms): {', '.join([f'Query {q}: {r:.2f}' for q, r in avg_bespoke_rts.items()])} - sum: {sum(avg_bespoke_rts.values()):.2f}\n"
    )

    if trace_mode:
        result_message += f"Tracing output: \n{trace_output}"

    return ValidationOutput(
        result_message=result_message,
        correct=True,
        metrics=metrics,
        trace_output=trace_output,
    )
