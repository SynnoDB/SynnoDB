"""Query normalization helpers for repetition analysis."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Iterable

from sqlglot import exp, parse_one
from sqlglot.errors import ParseError

COMMUTATIVE_EXPRESSIONS = (exp.Add, exp.Mul, exp.Or, exp.NEQ)
SQL_DIALECT = "postgres"
NormalizationRule = Callable[[exp.Expression], exp.Expression]


@dataclass(frozen=True)
class NamedNormalizationRule:
    name: str
    apply: NormalizationRule


def parse_query(sql: str) -> exp.Expression | None:
    try:
        return parse_one(sql, read=SQL_DIALECT)
    except ParseError:
        return None


def _expression_sort_key(expression: exp.Expression) -> tuple[bool, str]:
    rendered = expression.sql(dialect=SQL_DIALECT, normalize=True)
    return (rendered in {"%s", "?"}, rendered)


def _flatten_binary_chain(node: exp.Expression, node_type: type[exp.Expression]) -> list[exp.Expression]:
    flattened: list[exp.Expression] = []

    def walk(current: exp.Expression) -> None:
        if isinstance(current, node_type):
            left = current.args.get("this")
            right = current.args.get("expression")
            if isinstance(left, exp.Expression):
                walk(left)
            if isinstance(right, exp.Expression):
                walk(right)
            return
        flattened.append(current)

    walk(node)
    return flattened


def _rebuild_binary_chain(expressions: list[exp.Expression], node_type: type[exp.Expression]) -> exp.Expression:
    if not expressions:
        raise ValueError("cannot rebuild empty expression chain")
    current = expressions[0]
    for expression in expressions[1:]:
        current = node_type(this=current, expression=expression)
    return current


def _normalize_literals_and_parameters(node: exp.Expression) -> exp.Expression:
    if isinstance(node, (exp.Literal, exp.Boolean, exp.Null, exp.Parameter)):
        return exp.Placeholder()
    return node


def _normalize_aliases(node: exp.Expression) -> exp.Expression:
    if isinstance(node, exp.Alias):
        return node.this
    return node


def _normalize_comparison_direction(node: exp.Expression) -> exp.Expression:
    if isinstance(node, exp.EQ):
        left = node.args.get("this")
        right = node.args.get("expression")
        if isinstance(left, exp.Expression) and isinstance(right, exp.Expression):
            if _expression_sort_key(right) < _expression_sort_key(left):
                node.set("this", right)
                node.set("expression", left)
    return node


def _normalize_and_predicates(node: exp.Expression) -> exp.Expression:
    if isinstance(node, exp.And):
        predicates = _flatten_binary_chain(node, exp.And)
        predicates.sort(key=_expression_sort_key)
        return _rebuild_binary_chain(predicates, exp.And)
    return node


def _normalize_in_lists(node: exp.Expression) -> exp.Expression:
    if isinstance(node, exp.In):
        expressions = node.args.get("expressions") or []
        if expressions:
            node.set("expressions", sorted(expressions, key=_expression_sort_key))
    return node


def _join_kind_name(join: exp.Join) -> str:
    kind = join.args.get("kind")
    if kind is None:
        return "INNER"
    return str(kind).upper()


def _normalize_inner_joins(node: exp.Expression) -> exp.Expression:
    if not isinstance(node, exp.Select):
        return node
    from_clause = node.args.get("from_")
    joins = node.args.get("joins") or []
    if not isinstance(from_clause, exp.From) or not joins:
        return node
    if any(not isinstance(join, exp.Join) or _join_kind_name(join) != "INNER" for join in joins):
        return node

    from_expression = from_clause.this
    if not isinstance(from_expression, exp.Expression):
        return node

    ordered_sources = sorted(
        [from_expression, *[join.this for join in joins if isinstance(join.this, exp.Expression)]],
        key=_expression_sort_key,
    )
    if len(ordered_sources) != len(joins) + 1:
        return node

    ordered_joins = [join.copy() for join in joins]
    ordered_joins.sort(key=lambda join: _expression_sort_key(join.this) if isinstance(join.this, exp.Expression) else (False, ""))
    for join, source in zip(ordered_joins, ordered_sources[1:]):
        join.set("this", source)

    updated_from = from_clause.copy()
    updated_from.set("this", ordered_sources[0])
    node.set("from_", updated_from)
    node.set("joins", ordered_joins)
    return node


def _normalize_commutative_expressions(node: exp.Expression) -> exp.Expression:
    if isinstance(node, COMMUTATIVE_EXPRESSIONS):
        left = node.args.get("this")
        right = node.args.get("expression")
        if isinstance(left, exp.Expression) and isinstance(right, exp.Expression):
            if _expression_sort_key(right) < _expression_sort_key(left):
                node.set("this", right)
                node.set("expression", left)
    return node


NORMALIZATION_RULES: tuple[NamedNormalizationRule, ...] = (
    NamedNormalizationRule("literal-and-parameter-placeholders", _normalize_literals_and_parameters),
    NamedNormalizationRule("remove-output-aliases", _normalize_aliases),
    NamedNormalizationRule("canonicalize-comparison-direction", _normalize_comparison_direction),
    NamedNormalizationRule("canonicalize-and-predicates", _normalize_and_predicates),
    NamedNormalizationRule("canonicalize-in-lists", _normalize_in_lists),
    NamedNormalizationRule("canonicalize-inner-joins", _normalize_inner_joins),
    NamedNormalizationRule("canonicalize-commutative-expressions", _normalize_commutative_expressions),
)


def normalize_expression(node: exp.Expression, rules: Iterable[NamedNormalizationRule] = NORMALIZATION_RULES) -> exp.Expression:
    normalized_children = dict(node.args)
    for key, value in list(normalized_children.items()):
        if isinstance(value, exp.Expression):
            normalized_children[key] = normalize_expression(value, rules)
        elif isinstance(value, list):
            normalized_children[key] = [
                normalize_expression(item, rules) if isinstance(item, exp.Expression) else item for item in value
            ]
    normalized = node.copy()
    for key, value in normalized_children.items():
        normalized.set(key, value)
    for rule in rules:
        normalized = rule.apply(normalized)
    return normalized


def normalize_query_structure(sql: str) -> str:
    parsed = parse_query(sql)
    if parsed is None:
        return " ".join(sql.split())
    normalized = normalize_expression(parsed, NORMALIZATION_RULES)
    return normalized.sql(dialect=SQL_DIALECT, normalize=True, pretty=False)


def normalize_query_structure_strict(sql: str) -> str:
    parsed = parse_query(sql)
    if parsed is None:
        raise ValueError(f"invalid query structure: {sql!r}")
    normalized = normalize_expression(parsed, NORMALIZATION_RULES)
    return normalized.sql(dialect=SQL_DIALECT, normalize=True, pretty=False)


def extract_record_values(record: dict[str, Any]) -> list[Any]:
    parameters = record.get("parameters") or []
    if parameters:
        return [param.get("value") for param in parameters]

    query = record.get("query")
    if not query:
        return []
    parsed = parse_query(query)
    if parsed is None:
        return []

    values: list[Any] = []
    for node in parsed.walk():
        if isinstance(node, exp.Literal):
            values.append(node.this)
        elif isinstance(node, exp.Boolean):
            values.append(bool(node.this))
        elif isinstance(node, exp.Null):
            values.append(None)
    return values
