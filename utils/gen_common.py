from __future__ import annotations

from typing import List

from workloads.workload_provider import Workload
from workloads.workload_provider_bff import BFFWorkload
from workloads.workload_provider_olap import OLAPWorkload


def parse_query_ids(short_name: str, benchmark: Workload) -> List[str] | None:
    assert "v" not in short_name, (
        f"Unexpected 'v' in short name: {short_name}"
    )  # this was old logic to parse query ids from conversation name - we now pass the query short name directly as an argument, so this is no longer needed

    qnums = short_name.split("-")
    if len(qnums) == 1:
        return [qnums[0]]
    start_q = qnums[0]
    end_q = qnums[1]

    if benchmark in (
        OLAPWorkload.TPCH,
        BFFWorkload.TPCH,
        BFFWorkload.TPCH_ST,
    ):
        start_q = int(start_q)
        end_q = int(end_q)

        qids = list(range(start_q, end_q + 1))

        # convert to strings
        return [str(qid) for qid in qids]

    elif benchmark == OLAPWorkload.CEB:
        ceb_query_order = [
            "1a",
            "2a",
            "2b",
            "2c",
            "3a",
            "3b",
            "4a",
            "5a",
            "6a",
            "7a",
            "8a",
            "9a",
            "9b",
            "10a",
            "11a",
            "11b",
        ]

        def parse_qstr(q: str, is_start: bool) -> str:
            # prepent start q
            if len(q) == 1:
                assert q.isdigit()
                q = f"0{q}a"
            elif len(q) == 2:
                # check if both 1&2 are digits
                if q[0].isdigit() and q[1].isdigit():
                    # prepend a
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
                pass
            else:
                raise Exception(f"Could not parse start query {q}")
            return q

        start_q = parse_qstr(start_q, is_start=True)
        end_q = parse_qstr(end_q, is_start=False)

        assert len(start_q) == 3, f"start_q: {start_q}"
        assert len(end_q) == 3, f"end_q: {end_q}"

        queries = []

        for q in ceb_query_order:
            # prepend with 0 if single digit
            q_str = f"{q}"
            if len(q) == 2:
                q_str = "0" + q_str

            assert len(q_str) == 3, f"q_str: {q_str}"
            assert q_str[0].isdigit() and q_str[1].isdigit() and q_str[2].isalpha(), (
                f"q_str: {q_str}"
            )

            # perform lexicographical comparison
            if q_str >= start_q and q_str <= end_q:
                queries.append(q)

        return queries
    else:
        raise ValueError(f"Unknown benchmark: {benchmark}")
