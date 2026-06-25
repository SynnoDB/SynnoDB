import re

from workloads.workload_provider import format_args_element


def test_format_args_element_is_deterministic() -> None:
    placeholders = {"DELTA": "67", "SEGMENT": "BUILDING"}

    first = format_args_element("1", placeholders, request_disambiguator=0)
    second = format_args_element("1", placeholders, request_disambiguator=0)

    assert first == second
    query_id, req_id, args = first.split(maxsplit=2)
    assert query_id == "1"
    assert re.fullmatch(r"req_1_[0-9a-f]{12}", req_id)
    assert args == '"67" "BUILDING"'


def test_format_args_element_disambiguates_repetitions() -> None:
    placeholders = {"DELTA": "67"}

    rep0 = format_args_element("1", placeholders, request_disambiguator=0)
    rep1 = format_args_element("1", placeholders, request_disambiguator=1)

    assert rep0.split()[1] != rep1.split()[1]
    assert rep0.split(maxsplit=2)[2] == rep1.split(maxsplit=2)[2]


def test_format_args_element_preserves_in_lists() -> None:
    args = format_args_element(
        "12",
        {"SHIPMODE": "(MAIL,SHIP)", "DELTA": "67"},
        request_disambiguator=0,
    )

    assert args.endswith('(MAIL,SHIP) "67"')
