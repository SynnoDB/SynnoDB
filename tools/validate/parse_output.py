from typing import List

from tools.validate.run_and_check_queries import Measurement


def parse_output(
    stdout: str,
    stderr: str,
    resp: str,
) -> List[Measurement] | str:
    lines = stdout.strip().split("\n")

    # search for line matching "<run> | Q<query_id> | Execution ms: <num>"
    timing_lines = [
        line for line in lines if line.count("|") == 2 and "Execution ms:" in line
    ]

    if len(timing_lines) == 0:
        return (
            "Error: no timing lines found in program stdout. "
            "Expected lines like: '<run> | Q<query_id> | Execution ms: <num>'.\n"
            + f"STDERR:\n{stderr}\nSTDOUT:\n{stdout}\nResp:\n{resp}"
        )

    measurements = []
    for timing_line in timing_lines:
        run_num, query_id, exec_time = timing_line.split("|")

        run_num = run_num.strip()
        query_id = query_id.strip()
        exec_time = exec_time.strip()

        assert exec_time.startswith("Execution ms:"), (
            f"Unexpected exec time format: \"{exec_time}\" Expected to start with 'Execution ms:'"
        )
        exec_time = exec_time[len("Execution ms:") :].strip().strip(":").strip()

        if not run_num.isdigit():
            return (
                "Error: timing line run number is not an integer.\n"
                + f"Bad line: {timing_line}\nSTDERR:\n{stderr}\nSTDOUT:\n{stdout}\nResp:\n{resp}"
            )

        if not query_id.startswith("Q"):
            return (
                "Error: timing line query id does not start with 'Q'.\n"
                + f"Bad line: {timing_line}\nSTDERR:\n{stderr}\nSTDOUT:\n{stdout}\nResp:\n{resp}"
            )

        query_id = query_id[1:]  # remove leading "Q"

        measurements.append(
            Measurement(
                run_nr=int(run_num), query_id=query_id, exec_time=float(exec_time)
            )
        )

    return measurements
