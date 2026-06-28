"""Unit tests for template filling + explicit parameter expansion
(synnodb.workloads.query_params)."""
from __future__ import annotations

import datetime
import decimal

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


def test_expand_no_placeholders():
    assert qp.expand_param_grid("select count(*) from t", {}) == [{}]


def test_expand_zips_into_instantiations():
    out = qp.expand_param_grid("a=[X] and b=[Y]", {"X": [1, 2, 3], "Y": ["p", "q", "r"]})
    assert out == [{"X": "1", "Y": "p"}, {"X": "2", "Y": "q"}, {"X": "3", "Y": "r"}]


def test_expand_preserves_correlation():
    # Distinct-nation pair: index i must keep (NATION1[i], NATION2[i]) aligned.
    out = qp.expand_param_grid(
        "n1='[NATION1]' or n2='[NATION2]'",
        {"NATION1": ["FRANCE", "GERMANY"], "NATION2": ["GERMANY", "FRANCE"]},
    )
    assert out == [
        {"NATION1": "FRANCE", "NATION2": "GERMANY"},
        {"NATION1": "GERMANY", "NATION2": "FRANCE"},
    ]


def test_expand_length_one_broadcasts():
    out = qp.expand_param_grid("a=[X] and b=[Y]", {"X": ["fixed"], "Y": [1, 2, 3]})
    assert [a["X"] for a in out] == ["fixed", "fixed", "fixed"]
    assert [a["Y"] for a in out] == ["1", "2", "3"]


def test_expand_scalar_normalized_to_single():
    out = qp.expand_param_grid("a=[X]", {"X": "FRANCE"})
    assert out == [{"X": "FRANCE"}]


def test_expand_in_list_nested():
    out = qp.expand_param_grid("s in [SIZES]", {"SIZES": [["1", "3", "5"]]})
    assert out == [{"SIZES": "('1', '3', '5')"}]


def test_expand_inconsistent_lengths_raise():
    with pytest.raises(ValueError, match="inconsistent number of values"):
        qp.expand_param_grid("a=[X] and b=[Y]", {"X": [1, 2], "Y": [1, 2, 3]})


def test_expand_missing_and_extra_keys_raise():
    with pytest.raises(ValueError, match="missing="):
        qp.expand_param_grid("a=[X] and b=[Y]", {"X": [1]})
    with pytest.raises(ValueError, match="extra="):
        qp.expand_param_grid("a=[X]", {"X": [1], "Z": [2]})


def test_expand_empty_value_list_raises():
    with pytest.raises(ValueError, match="empty"):
        qp.expand_param_grid("a=[X]", {"X": []})


def test_expand_params_for_static_query_raises():
    with pytest.raises(ValueError, match="no placeholders"):
        qp.expand_param_grid("select 1", {"X": [1]})
