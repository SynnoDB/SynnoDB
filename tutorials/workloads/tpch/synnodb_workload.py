"""The built-in TPC-H workload, expressed as a :class:`~synnodb.workloads.workload_spec.WorkloadSpec`
and registered into the core registry from the outside.

The core ``synnodb`` package is workload-agnostic: it knows how to drive *a* workload from a
spec, but ships no concrete workload. This module owns everything TPC-H-specific - the SQL
templates, schema DDL, per-query parameter generation, and declarative value spaces - and hands
it to the framework via :func:`register`. Import this module and call :func:`register` (or read
:data:`TPCH_SPEC`) before running the pipeline against ``"tpch"``.

The heavy / context-dependent parts are supplied as factories, so importing the spec does not pull
in the generator modules until they are actually used.
"""

from __future__ import annotations

from typing import Any

from synnodb.workloads.workload_spec import WorkloadSpec, register_workload

from tutorials.workloads.tpch.tpch_queries import tpc_h


def _tpch_schema() -> str:
    from tutorials.workloads.tpch.tpch_queries import tpc_h_schema

    return tpc_h_schema


def _tpch_query_gen_factory(provider: Any):
    from tutorials.workloads.tpch.gen_tpch_query import gen_query

    return gen_query


def _tpch_placeholders_factory(provider: Any, do_not_cache: bool = False):
    from tutorials.workloads.tpch.gen_tpch_query import gen_query

    def gen_placeholder_tpch(**kwargs):
        # we only need the placeholders dict
        return gen_query(**kwargs)[2]

    return gen_placeholder_tpch


def _tpch_param_space_factory(provider: Any):
    """Per-query typed value-space for TPC-H, from the declarative spec table.

    Used for live-UI widget metadata (slider/dropdown/date-picker). The run-time sampler
    stays ``gen_query`` (see ``_tpch_query_gen_factory``), so TPC-H run behavior is unchanged.
    """
    from synnodb.workloads.query_params import parse_param_space

    from tutorials.workloads.tpch.tpch_param_specs import TPCH_PARAM_SPECS

    def get(query_name: str):
        qid = query_name[1:] if query_name.startswith("Q") else query_name
        section = TPCH_PARAM_SPECS.get(qid)
        if section is None:
            return None
        return parse_param_space(
            section.get("params"), section.get("param_groups"), tpc_h[f"Q{qid}"]
        )

    return get


TPCH_SPEC = WorkloadSpec(
    name="tpch",
    tables=(
        "customer",
        "lineitem",
        "nation",
        "orders",
        "part",
        "partsupp",
        "region",
        "supplier",
    ),
    dataset_name="tpch",
    all_query_ids=tuple(str(i) for i in range(1, 23)),
    benchmark_sf=20,
    fast_check_sfs=(1, 2),
    exhaustive_sfs=(1, 2, 20),
    ingest_sfs=(20,),
    example_query="Q42",
    example_query_params="42",
    schema_example_table="lineitem",
    sql_dict_factory=lambda: tpc_h,
    schema_factory=_tpch_schema,
    query_gen_factory=_tpch_query_gen_factory,
    placeholders_factory=_tpch_placeholders_factory,
    param_space_factory=_tpch_param_space_factory,
    large_check_sf=100,
)


def register() -> WorkloadSpec:
    """Register the built-in TPC-H workload into the core registry. Idempotent."""
    register_workload(TPCH_SPEC)
    return TPCH_SPEC
