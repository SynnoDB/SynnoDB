from __future__ import annotations

from typing import List

from synnodb.workloads.workload_provider import Workload
from synnodb.workloads.workload_spec import get_workload_spec


def parse_query_ids(short_name: str, benchmark: Workload) -> List[str] | None:
    assert "v" not in short_name, (
        f"Unexpected 'v' in short name: {short_name}"
    )  # this was old logic to parse query ids from conversation name - we now pass the query short name directly as an argument, so this is no longer needed

    qnums = short_name.split("-")
    if len(qnums) == 1:
        return [qnums[0]]

    start_q, end_q = qnums[0], qnums[1]
    bench_value = benchmark.value if isinstance(benchmark, Workload) else str(benchmark)
    catalog = list(get_workload_spec(bench_value).all_query_ids)

    # Generic + workload-agnostic: when both endpoints are exact ids in the workload's
    # ordered query catalog, return the slice between them. Handles TPC-H ("1"-"22"),
    # exact CEB ranges ("1a"-"3b"), and any future workload with arbitrary id strings.
    if start_q in catalog and end_q in catalog:
        i, j = catalog.index(start_q), catalog.index(end_q)
        if i > j:
            i, j = j, i
        return catalog[i : j + 1]

    # Legacy CEB-only convenience: fuzzy ranges whose endpoints are not exact ids
    # (e.g. "2-9" meaning 2a..9b). Preserved for that workload; not needed for new ones.
    if bench_value == "ceb":
        return _parse_ceb_fuzzy_range(start_q, end_q, catalog)

    raise ValueError(
        f"Cannot parse query range '{short_name}' for workload '{bench_value}': "
        f"endpoints must be ids in the catalog {catalog}"
    )


def _parse_ceb_fuzzy_range(start_q: str, end_q: str, ceb_query_order: list[str]) -> List[str]:
    def parse_qstr(q: str, is_start: bool) -> str:
        if len(q) == 1:
            assert q.isdigit()
            q = f"0{q}a"
        elif len(q) == 2:
            if q[0].isdigit() and q[1].isdigit():
                if is_start:
                    q = f"{q}a"
                else:
                    # upper bound: append z
                    q = f"{q}z"
            elif q[0].isdigit() and q[1].isalpha():
                # prepend 0
                q = f"0{q}"
            else:
                raise Exception(f"Could not parse start query {q}")
        elif len(q) == 3:
            assert q[0].isdigit() and q[1].isdigit() and q[2].isalpha()
        else:
            raise Exception(f"Could not parse start query {q}")
        return q

    start_q = parse_qstr(start_q, is_start=True)
    end_q = parse_qstr(end_q, is_start=False)

    assert len(start_q) == 3, f"start_q: {start_q}"
    assert len(end_q) == 3, f"end_q: {end_q}"

    queries = []
    for q in ceb_query_order:
        q_str = f"{q}"
        if len(q) == 2:
            q_str = "0" + q_str

        assert len(q_str) == 3, f"q_str: {q_str}"
        assert q_str[0].isdigit() and q_str[1].isdigit() and q_str[2].isalpha(), (
            f"q_str: {q_str}"
        )

        if q_str >= start_q and q_str <= end_q:
            queries.append(q)

    return queries
