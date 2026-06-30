"""Unit tests for template filling + typed parameter specs
(synnodb.workloads.query_params)."""
from __future__ import annotations

import datetime
import decimal
import random

import pytest

from synnodb.workloads import query_params as qp


def test_find_placeholders_order_and_dedup():
    assert qp.find_placeholders("a [DELTA] b") == ["DELTA"]
    assert qp.find_placeholders("[N1] x [N2] y [N1]") == ["N1", "N2"]
    assert qp.find_placeholders("select count(*) from t") == []


def test_render_value_scalar_date_decimal():
    assert qp.render_value("FRANCE") == "FRANCE"
    assert qp.render_value(datetime.date(1995, 3, 15)) == "1995-03-15"
    assert qp.render_value(decimal.Decimal("0.09")) == "0.09"


def test_render_value_in_list():
    assert qp.render_value(["A", "B"]) == "('A', 'B')"
    assert qp.render_value([1, 2, 3]) == "(1, 2, 3)"
    assert qp.render_value(["O'Brien"]) == "('O''Brien')"  # quote-escaped


def test_coerce_for_engine_stringifies():
    assert qp.coerce_for_engine(90) == "90"
    assert qp.coerce_for_engine(decimal.Decimal("0.09")) == "0.09"
    assert qp.coerce_for_engine(datetime.date(1995, 3, 15)) == "1995-03-15"
    assert qp.coerce_for_engine(["1", "3", "5"]) == "('1', '3', '5')"


def test_substitute_fills_template():
    sql = qp.substitute("x <= date '1998-12-01' - interval '[DELTA]' day", {"DELTA": "90"})
    assert "[DELTA]" not in sql and "interval '90' day" in sql


# --- parse_param_space: coverage / validation -------------------------------


def test_no_placeholders_empty_space():
    space = qp.parse_param_space(None, None, "select count(*) from t")
    assert space.is_empty()
    assert space.sample(random.Random(0)) == {}


def test_no_placeholders_but_params_raises():
    with pytest.raises(ValueError, match="no placeholders"):
        qp.parse_param_space({"X": {"type": "int", "min": 1, "max": 2}}, None, "select 1")


def test_missing_and_extra_placeholders_raise():
    with pytest.raises(ValueError, match="missing="):
        qp.parse_param_space({"X": {"type": "int", "min": 1, "max": 2}}, None, "a=[X] b=[Y]")
    with pytest.raises(ValueError, match="extra="):
        qp.parse_param_space(
            {"X": {"type": "int", "min": 1, "max": 2}, "Z": {"type": "int", "min": 1, "max": 2}},
            None,
            "a=[X]",
        )


def test_double_cover_raises():
    with pytest.raises(ValueError, match="more than one spec"):
        qp.parse_param_space(
            {"A": {"type": "categorical", "values": ["x"]}},
            [{"type": "sample", "placeholders": ["A", "B"], "domain": ["x", "y"]}],
            "a=[A] b=[B]",
        )


def test_unknown_type_raises():
    with pytest.raises(ValueError, match="unknown parameter spec type"):
        qp.parse_param_space({"X": {"type": "wat"}}, None, "a=[X]")


# --- scalar specs: sampling ---------------------------------------------------


def test_int_spec_samples_on_grid():
    space = qp.parse_param_space({"X": {"type": "int", "min": 60, "max": 120}}, None, "v=[X]")
    rnd = random.Random(0)
    vals = {int(space.sample(rnd)["X"]) for _ in range(200)}
    assert vals and all(60 <= v <= 120 for v in vals)


def test_int_spec_step():
    space = qp.parse_param_space(
        {"X": {"type": "int", "min": 0, "max": 10, "step": 5}}, None, "v=[X]"
    )
    rnd = random.Random(1)
    assert {space.sample(rnd)["X"] for _ in range(50)} <= {"0", "5", "10"}


def test_int_spec_min_gt_max_raises():
    with pytest.raises(ValueError, match="min .* <= max"):
        qp.parse_param_space({"X": {"type": "int", "min": 5, "max": 1}}, None, "v=[X]")


def test_int_spec_bad_step_raises():
    with pytest.raises(ValueError, match="step must be > 0"):
        qp.parse_param_space({"X": {"type": "int", "min": 1, "max": 5, "step": 0}}, None, "v=[X]")


def test_float_spec_exact_decimal_text():
    space = qp.parse_param_space(
        {"D": {"type": "float", "min": 0.02, "max": 0.09, "step": 0.01}}, None, "d=[D]"
    )
    rnd = random.Random(3)
    vals = {space.sample(rnd)["D"] for _ in range(200)}
    assert vals <= {"0.02", "0.03", "0.04", "0.05", "0.06", "0.07", "0.08", "0.09"}


def test_date_spec_day_in_range():
    space = qp.parse_param_space(
        {"DT": {"type": "date", "min": "1995-03-01", "max": "1995-03-31"}},
        None,
        "d=[DT]",
    )
    rnd = random.Random(4)
    for _ in range(50):
        d = datetime.date.fromisoformat(space.sample(rnd)["DT"])
        assert datetime.date(1995, 3, 1) <= d <= datetime.date(1995, 3, 31)


def test_date_spec_granularity_rejected():
    with pytest.raises(ValueError, match="granularity.*no longer supported"):
        qp.parse_param_space(
            {"DT": {"type": "date", "min": "1993-01-01", "max": "1994-01-01", "granularity": "month"}},
            None,
            "d=[DT]",
        )


def test_date_spec_bad_iso_raises():
    with pytest.raises(ValueError, match="not a valid ISO date"):
        qp.parse_param_space(
            {"DT": {"type": "date", "min": "1993-13-99", "max": "1994-01-01"}}, None, "d=[DT]"
        )


def test_categorical_sample_and_empty_raises():
    space = qp.parse_param_space(
        {"R": {"type": "categorical", "values": ["ASIA", "EUROPE"]}}, None, "r=[R]"
    )
    assert {space.sample(random.Random(7))["R"] for _ in range(20)} <= {"ASIA", "EUROPE"}
    with pytest.raises(ValueError, match="non-empty list"):
        qp.parse_param_space({"R": {"type": "categorical", "values": []}}, None, "r=[R]")


# --- group specs -------------------------------------------------------------


def test_tuples_group_stays_aligned():
    space = qp.parse_param_space(
        None,
        [{"type": "tuples", "placeholders": ["A", "B"],
          "values": [["FRANCE", "GERMANY"], ["CHINA", "JAPAN"]]}],
        "a='[A]' b='[B]'",
    )
    pairs = {(space.sample(random.Random(s))["A"], space.sample(random.Random(s))["B"])
             for s in range(20)}
    assert pairs <= {("FRANCE", "GERMANY"), ("CHINA", "JAPAN")}  # never crossed


def test_tuples_row_arity_mismatch_raises():
    with pytest.raises(ValueError, match="row must be a list"):
        qp.parse_param_space(
            None,
            [{"type": "tuples", "placeholders": ["A", "B"], "values": [["only-one"]]}],
            "a=[A] b=[B]",
        )


def test_sample_group_distinct():
    space = qp.parse_param_space(
        None,
        [{"type": "sample", "placeholders": ["I1", "I2", "I3"], "domain": ["a", "b", "c", "d"]}],
        "[I1] [I2] [I3]",
    )
    assign = space.sample(random.Random(8))
    assert len(set(assign.values())) == 3  # distinct draw


def test_sample_group_distinct_domain_too_small_raises():
    with pytest.raises(ValueError, match="needs a domain of at least"):
        qp.parse_param_space(
            None,
            [{"type": "sample", "placeholders": ["I1", "I2", "I3"], "domain": ["a", "b"]}],
            "[I1] [I2] [I3]",
        )


def test_sample_group_non_distinct_allows_repeats():
    space = qp.parse_param_space(
        None,
        [{"type": "sample", "placeholders": ["A", "B"], "domain": ["x"], "distinct": False}],
        "[A] [B]",
    )
    assert space.sample(random.Random(9)) == {"A": "x", "B": "x"}


# --- ordering / determinism / metadata --------------------------------------


def test_sample_is_in_template_order():
    space = qp.parse_param_space(
        {"Y": {"type": "int", "min": 1, "max": 1}, "X": {"type": "int", "min": 2, "max": 2}},
        None,
        "first=[X] second=[Y]",  # template order is X, then Y
    )
    assert list(space.sample(random.Random(0))) == ["X", "Y"]


def test_sampling_is_deterministic_per_seed():
    space = qp.parse_param_space(
        {"X": {"type": "int", "min": 1, "max": 100}, "R": {"type": "categorical", "values": list("abcdef")}},
        None,
        "v=[X] r=[R]",
    )
    seq1 = [space.sample(random.Random(42)) for _ in range(5)]
    seq2 = [space.sample(random.Random(42)) for _ in range(5)]
    assert seq1 == seq2


def test_metadata_shapes_for_widgets():
    space = qp.parse_param_space(
        {
            "N": {"type": "int", "min": 60, "max": 120},
            "R": {"type": "categorical", "values": ["ASIA", "EUROPE"]},
            "D": {"type": "date", "min": "1993-01-01", "max": "1997-01-01"},
        },
        None,
        "n=[N] r=[R] d=[D]",
    )
    meta = space.metadata()
    assert meta["N"] == {"type": "int", "min": 60, "max": 120, "step": 1}
    assert meta["R"] == {"type": "categorical", "values": ["ASIA", "EUROPE"]}
    assert meta["D"] == {"type": "date", "min": "1993-01-01", "max": "1997-01-01"}


def test_group_metadata_is_per_column_categorical():
    space = qp.parse_param_space(
        None,
        [{"type": "sample", "placeholders": ["I1", "I2"], "domain": ["13", "31", "23"]}],
        "[I1] [I2]",
    )
    meta = space.metadata()
    assert meta["I1"] == {"type": "categorical", "values": ["13", "31", "23"]}
    assert meta["I2"]["values"] == ["13", "31", "23"]
