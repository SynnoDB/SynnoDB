"""Tests for ``hoist_literal_quotes`` (see its docstring for the convention it enforces) and
the ``parse_param_space`` warning on quoted-literal values. The invariant throughout: the
substituted SQL is byte-identical before and after hoisting."""

import json
import logging

import pytest

from synnodb.workloads.query_params import (
    hoist_literal_quotes,
    parse_param_space,
    substitute,
)


def _substituted(template: str, placeholders: list[str], rows: list[list]) -> list[str]:
    return [substitute(template, dict(zip(placeholders, row))) for row in rows]


class TestHoisting:
    def test_quoted_scalars_are_hoisted(self):
        template = "select count(*) from site where site_name=[SITE] and tag=[TAG]"
        rows = [["'scifi'", "'steins-gate'"], ["'math'", "'vector-spaces'"]]
        new_template, new_rows = hoist_literal_quotes(template, ["SITE", "TAG"], rows)
        assert (
            new_template
            == "select count(*) from site where site_name='[SITE]' and tag='[TAG]'"
        )
        assert new_rows == [["scifi", "steins-gate"], ["math", "vector-spaces"]]

    def test_substituted_sql_is_unchanged(self):
        template = (
            "select 1 where name=[NAME] and score > [SCORE] and url like [PATTERN] "
            "and site in [SITES] and d > x + [DATE]::interval"
        )
        placeholders = ["NAME", "SCORE", "PATTERN", "SITES", "DATE"]
        rows = [
            ["'Famous Question'", "8", "'%su%'", "('a','b')", "'7 months'"],
            ["'Nice Question'", "100", "'%io'", "('c')", "'11 months'"],
        ]
        new_template, new_rows = hoist_literal_quotes(template, placeholders, rows)
        assert _substituted(template, placeholders, rows) == _substituted(
            new_template, placeholders, new_rows
        )

    def test_numbers_and_in_lists_untouched(self):
        template = "select 1 where a in [TAGS] and b > [MIN]"
        rows = [["('x','y')", "10"], ["('z')", "20"]]
        new_template, new_rows = hoist_literal_quotes(template, ["TAGS", "MIN"], rows)
        assert new_template == template
        assert new_rows == rows

    def test_input_rows_not_mutated(self):
        rows = [["'a'"]]
        hoist_literal_quotes("x=[P]", ["P"], rows)
        assert rows == [["'a'"]]

    def test_empty_string_literal(self):
        new_template, new_rows = hoist_literal_quotes("a=[P]", ["P"], [["''"]])
        assert new_template == "a='[P]'"
        assert new_rows == [[""]]


class TestRejection:
    def test_mixed_quoting_raises(self):
        with pytest.raises(ValueError, match="mix quoted literals"):
            hoist_literal_quotes("a=[P]", ["P"], [["'x'"], ["y"]])

    def test_embedded_quote_raises(self):
        with pytest.raises(ValueError, match="embedded quote"):
            hoist_literal_quotes("a=[P]", ["P"], [["'O''Reilly'"]])

    def test_placeholder_missing_from_template_raises(self):
        with pytest.raises(ValueError, match="does not occur in the template"):
            hoist_literal_quotes("select 1", ["P"], [["'x'"]])


class TestParamSpaceWarning:
    """parse_param_space flags quoted-literal values for every spec form it can see."""

    def test_warns_on_quoted_tuples_values(self, caplog):
        with caplog.at_level(logging.WARNING, logger="synnodb.workloads.query_params"):
            parse_param_space(
                None,
                [
                    {
                        "type": "tuples",
                        "placeholders": ["NAME"],
                        "values": [["'scifi'"]],
                    }
                ],
                "select 1 where name=[NAME]",
            )
        assert "quoted SQL literal" in caplog.text

    def test_warns_on_quoted_categorical_values(self, caplog):
        with caplog.at_level(logging.WARNING, logger="synnodb.workloads.query_params"):
            parse_param_space(
                {"TYPE": {"type": "categorical", "values": ["'Person'", "'Group'"]}},
                None,
                "select 1 where t=[TYPE]",
            )
        assert "quoted SQL literal" in caplog.text

    def test_no_warning_for_bare_values(self, caplog):
        with caplog.at_level(logging.WARNING, logger="synnodb.workloads.query_params"):
            parse_param_space(
                {"TYPE": {"type": "categorical", "values": ["Person", "('a','b')"]}},
                None,
                "select 1 where t='[TYPE]' and x in [TYPE]",
            )
        assert "quoted SQL literal" not in caplog.text


class TestGeneratorWiring:
    """The stack and music_brainz generators must emit hoisted entries."""

    def test_stack_build_entry_hoists(self):
        from tutorials.workloads.stack.gen_stack_query import _build_entry

        entry = {
            "template": "select count(*) from t where site_name=[SITE_NAME]",
            "parameters": ["SITE_NAME"],
            "column_name_parameters": [],
            "operator_parameters": [],
            "queries": [
                {
                    "parameters": {"SITE_NAME": "'scifi'"},
                    "column_name_parameters": {},
                    "operator_parameters": {},
                },
                {
                    "parameters": {"SITE_NAME": "'math'"},
                    "column_name_parameters": {},
                    "operator_parameters": {},
                },
            ],
        }
        built = _build_entry(entry)
        assert built["sql"] == "select count(*) from t where site_name='[SITE_NAME]'"
        assert built["param_groups"][0]["values"] == [["scifi"], ["math"]]

    def test_musicbrainz_build_hoists(self, tmp_path):
        from tutorials.workloads.music_brainz._gen_musicbrainz_queries import (
            build_musicbrainz_queries_json,
        )

        templates = {
            "q1": {
                "template": (
                    "select 1 from a where t.name = [TYPE] and y >= [YEAR] "
                    "and tag.name in [TAGS]"
                ),
                "parameters": ["TYPE", "YEAR", "TAGS"],
                "generation_rules": {
                    "TYPE": {
                        "kind": "choice",
                        "values": ["Person", "Group"],
                        "quote": True,
                    },
                    "YEAR": {"kind": "int_uniform", "low": 1990, "high": 2000},
                    "TAGS": {
                        "kind": "choice_list",
                        "values": ["rock", "pop", "jazz"],
                        "n_min": 1,
                        "n_max": 2,
                        "quote": True,
                    },
                },
            }
        }
        templates_path = tmp_path / "templates.json"
        templates_path.write_text(json.dumps(templates))
        out = build_musicbrainz_queries_json(templates_path, num_instances=5, seed=0)
        entry = out["q1"]
        assert "t.name = '[TYPE]'" in entry["sql"]
        assert "y >= [YEAR]" in entry["sql"]  # numeric hole stays bare
        assert "tag.name in [TAGS]" in entry["sql"]  # IN-list hole stays bare
        for row in entry["param_groups"][0]["values"]:
            type_v, year_v, tags_v = row
            assert type_v in ("Person", "Group")  # bare, no quotes
            assert year_v.isdigit()
            assert tags_v.startswith("(") and "'" in tags_v  # list keeps element quotes
