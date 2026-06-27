import logging
from collections import defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import DefaultDict, Dict, List, Optional

import pandas as pd
import wandb

from synnodb.observability.logging.wandb_plots_gen import create_wandb_speedup_plot
from synnodb.utils.utils import prefix_dict
from synnodb.workloads.query_execution_cache import QueryExecutionCache
from synnodb.workloads.system_factory import System
from synnodb.workloads.workload_provider import ExecSettings, QueryBatch

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
    use_umbra: bool = True,
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

        # write df to csv and read back in - ensure consistent formatting
        assert duckdb_res.result is not None, (
            "DuckDB result is None, cannot compare results"
        )
        duckdb_res.result.to_csv(out_path / "ref_result.csv", index=False)
        reference_df = pd.read_csv(out_path / "ref_result.csv")

        # remove ref_result.csv
        (out_path / "ref_result.csv").unlink()

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

        # check that result was produced. The runner names result files by the
        # request id (see query_impl writer template: "result_" + req_id + ".csv").
        filename = f"result_{rt.req_id}.csv"
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

        # load result from csv into dataframe
        try:
            bespoke_df = pd.read_csv(
                res_path,
                header=0,
                delimiter=",",
                escapechar="\\",
                quotechar='"',
                doublequote=True,
            )
        except Exception as e:
            return ValidationOutput(
                result_message=f"Error: failed to read result CSV {filename} into DataFrame: {e}",
                correct=False,
                metrics=assemble_error(
                    exec_settings=exec_settings,
                    query_ids_executed=query_ids_executed,
                    exception=True,
                    query_id=inst.query_id,
                ),
                trace_output=trace_output,
            )

        # check equality of columns
        if set(reference_df.columns) != set(bespoke_df.columns):
            output = (
                f"Result columns do not match for query {inst.query_id} "
                f"with placeholders: {inst.placeholders}\n(SQL: {inst.sql})\n"
                f"Reference columns: {reference_df.columns.tolist()}\n"
                f"Bespoke columns: {bespoke_df.columns.tolist()}\n"
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
            else:
                logger.error(output)
                log_collector += output + "\n"

        # check row-content using set semantic (i.e., ignore ordering)
        reference_df_sorted = reference_df.sort_values(
            by=list(reference_df.columns)
        ).reset_index(drop=True)
        bespoke_df_sorted = bespoke_df.sort_values(
            by=list(bespoke_df.columns)
        ).reset_index(drop=True)

        try:
            pd.testing.assert_frame_equal(
                reference_df_sorted,
                bespoke_df_sorted,
                check_dtype=False,
                check_column_type=False,
                check_index_type=False,
                check_exact=False,
                atol=1e-5,  # allow for some numerical tolerance in float comparisons
                rtol=1e-5,
            )
        except AssertionError as e:
            output = (
                f"Results do not match for query {inst.query_id} "
                f"with placeholders: {inst.placeholders}\n(SQL: {inst.sql}). "
                "Checking with set-semantic and rows are not equal.\n"
                f"Reference result:\n{reference_df_sorted}\n"
                f"Bespoke result:\n{bespoke_df_sorted}\n"
                f"Assert Dataframe Equal Error:\n{e}\n"
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
            else:
                logger.error(output)
                log_collector += output + "\n"

        # Check that ordering constraints are obeyed
        if inst.order_by_info is not None and len(inst.order_by_info) > 0:
            sort_cols = [col for col, _ in inst.order_by_info]

            # perform col rewrites
            rewritten_cols = []
            for c in sort_cols:
                if c.lower() == "count(*)":
                    rewritten_cols.append("count_star()")
                else:
                    rewritten_cols.append(c)

            # overwrite with rewritten cols
            sort_cols = rewritten_cols

            # ensure all sort cols are present
            for c in sort_cols:
                assert c in reference_df.columns, (
                    f"ORDER BY column {c} not in Reference result {reference_df.columns.tolist()}\n{inst.sql}\n{inst.placeholders}"
                )

            try:
                pd.testing.assert_frame_equal(
                    reference_df[sort_cols],
                    bespoke_df[sort_cols],
                    check_dtype=False,
                    check_column_type=False,
                    check_index_type=False,
                    check_exact=False,
                    atol=1e-5,
                    rtol=1e-5,
                )
            except AssertionError as e:
                output = (
                    f"Ordering constraints violated for query {inst.query_id} "
                    f"with placeholders: {inst.placeholders}\n(SQL: {inst.sql})\n"
                    f"Expected ORDER BY: {inst.order_by_info}\n"
                    f"Reference result:\n{reference_df[sort_cols]}\n"
                    f"Bespoke result:\n{bespoke_df[sort_cols]}\n"
                    f"Assert Dataframe Equal Error:\n{e}\n"
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
                else:
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

    umbra_rt_str = f"{total_umbra_rt:.2f}ms (Umbra)" if total_umbra_rt is not None else "N/A (Umbra)"
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
                "umbra_runtime_ms": [avg_umbra_rts.get(str(q)) for q in query_ids_executed],
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
