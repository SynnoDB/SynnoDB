"""The activity-summary line the supervisor reads used to collapse EVERY non-compile
run failure to "incorrect query output" - so a crash, timeout, or OOM was reported as
a wrong answer, and the supervisor then faulted the agent for calling a transient infra
failure exactly what it was. _run_outcome_label distinguishes a genuine correctness
mismatch (validation/error=False) from an execution failure (validation/error=True) and
names a timeout / OOM / crash when the captured output shows it. Pin that mapping."""

from synnodb.tools.run import _run_outcome_label


def test_success_is_success():
    assert (
        _run_outcome_label(True, {}, "", "", "Query results are correct") == "success"
    )


def test_answer_mismatch_is_incorrect_output():
    # a clean run whose output disagrees with the reference: validation/error is False
    label = _run_outcome_label(
        False,
        {"validation/error": False},
        "",
        "",
        "Results do not match for query 3",
    )
    assert label == "incorrect query output"


def test_signal_kill_is_a_crash_not_incorrect_output():
    label = _run_outcome_label(
        False,
        {"validation/error": True},
        "",
        "ERROR: query child killed by signal 11 (SIGSEGV)",
        "Error: result_1.arrow not found",
    )
    assert label == "run failed (crash)"


def test_bad_alloc_is_out_of_memory():
    label = _run_outcome_label(
        False,
        {"validation/error": True},
        "terminate called after throwing an instance of 'std::bad_alloc'",
        "",
        "Error",
    )
    assert label == "run failed (out of memory)"


def test_timeout_is_named():
    label = _run_outcome_label(
        False, {"validation/error": True}, "", "query timed out after 10s", "Error"
    )
    assert label == "run failed (timeout)"


def test_generic_execution_error_when_no_keyword():
    label = _run_outcome_label(
        False, {"validation/error": True}, "", "", "Error: something went wrong"
    )
    assert label == "run failed (execution error)"


def test_missing_error_flag_defaults_to_incorrect_output():
    # If a failure carries no error flag it is not an execution crash; treat it as a
    # correctness failure rather than inventing a crash category.
    assert (
        _run_outcome_label(False, {}, "", "", "some message")
        == "incorrect query output"
    )
