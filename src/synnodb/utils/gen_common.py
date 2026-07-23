from __future__ import annotations

from typing import List

from synnodb.workloads.workload_provider import Workload
from synnodb.workloads.workload_spec import get_workload_spec


def parse_query_ids(
    short_name: str | None, benchmark: Workload | str
) -> List[str] | None:
    bench_value = benchmark.value if isinstance(benchmark, Workload) else str(benchmark)

    # No subset configured (SynnoConfig.query_subset=None): the workload's entire
    # query catalog.
    if short_name is None:
        return list(get_workload_spec(bench_value).all_query_ids)

    assert "v" not in short_name, (
        f"Unexpected 'v' in short name: {short_name}"
    )  # this was old logic to parse query ids from conversation name - we now pass the query short name directly as an argument, so this is no longer needed

    qnums = short_name.split("-")
    if len(qnums) == 1:
        return [qnums[0]]

    start_q, end_q = qnums[0], qnums[1]
    spec = get_workload_spec(bench_value)
    catalog = list(spec.all_query_ids)

    # Generic + workload-agnostic: when both endpoints are exact ids in the workload's
    # ordered query catalog, return the slice between them. Handles TPC-H ("1"-"22"),
    # exact CEB ranges ("1a"-"3b"), and any future workload with arbitrary id strings.
    if start_q in catalog and end_q in catalog:
        i, j = catalog.index(start_q), catalog.index(end_q)
        if i > j:
            i, j = j, i
        return catalog[i : j + 1]

    # Fuzzy ranges whose endpoints are not exact ids (e.g. CEB's "2-9" meaning 2a..9b).
    # The expansion is workload-specific and travels on the spec; a workload without one
    # (the default) only supports exact-catalog slicing.
    if spec.query_range_expander is not None:
        return spec.query_range_expander(start_q, end_q, catalog)

    raise ValueError(
        f"Cannot parse query range '{short_name}' for workload '{bench_value}': "
        f"endpoints must be ids in the catalog {catalog}"
    )
