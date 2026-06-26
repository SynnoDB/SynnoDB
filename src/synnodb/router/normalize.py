"""SQL normalization & classification for template matching (sqlglot-based).

Two jobs:

* ``statement_kind`` — cheaply classify a statement as read-only / mutating /
  unknown. Drives the v1 read-only block.
* ``normalize_sql`` — canonicalize a SELECT into a structural key by replacing
  literals with placeholders and rendering a deterministic form, so two queries
  that differ only in constants map to the same template. ``extract_literals``
  pulls those constants back out in order to bind them to the engine's placeholders.

``sqlglot`` is imported lazily so ``import synnodb`` stays cheap for users who never
trigger routing.
"""
from __future__ import annotations

import re
from typing import Any, List, Optional

_DIALECT = "duckdb"

# Leading keyword → read-only? Used as a fast path and a fallback when parsing fails.
_READ_KEYWORDS = {"select", "with", "explain", "describe", "desc", "show", "summarize", "values", "pragma", "table", "from"}
_WRITE_KEYWORDS = {
    "insert", "update", "delete", "merge", "create", "drop", "alter", "truncate",
    "replace", "attach", "detach", "copy", "import", "export", "set", "begin",
    "commit", "rollback", "checkpoint", "vacuum", "call", "use", "load", "install",
}

_LEADING_WORD = re.compile(r"\s*(?:--[^\n]*\n|/\*.*?\*/|\s)*([A-Za-z_]+)", re.DOTALL)


def _leading_keyword(sql: str) -> Optional[str]:
    m = _LEADING_WORD.match(sql)
    return m.group(1).lower() if m else None


def statement_kind(sql: str) -> str:
    """Return ``"read"``, ``"write"`` or ``"unknown"`` for *sql*.

    Conservative: a ``COPY``/``SET``/``PRAGMA`` that could mutate is treated as
    write so the router never accelerates something with side effects. ``PRAGMA``
    is a special case — most are read-only, but we keep it out of the routed path
    regardless (the router only ever routes plain SELECTs).
    """
    kw = _leading_keyword(sql)
    if kw is None:
        return "unknown"
    if kw in _WRITE_KEYWORDS:
        # COPY ... TO <file> is an export (read-only) but COPY ... FROM writes; treat
        # the whole COPY family as write for safety.
        return "write"
    if kw in _READ_KEYWORDS:
        return "read"
    return "unknown"


def is_select(sql: str) -> bool:
    """True only for plain SELECT / WITH...SELECT statements (the routable shape)."""
    try:
        import sqlglot
        from sqlglot import expressions as exp
    except Exception:  # pragma: no cover - sqlglot is a core dep
        return _leading_keyword(sql) in {"select", "with"}
    try:
        tree = sqlglot.parse_one(sql, read=_DIALECT)
    except Exception:
        return False
    return isinstance(tree, (exp.Select, exp.Union, exp.Subquery)) or (
        isinstance(tree, exp.With) and isinstance(tree.this, (exp.Select, exp.Union))
    )


def normalize_sql(sql: str) -> Optional[str]:
    """Canonical structural key for *sql*, or ``None`` if it cannot be parsed.

    Replaces literals (and bind parameters) with a single placeholder token so that
    queries differing only in constants share a key, and renders a deterministic,
    comment-free, identifier-normalized SQL string.
    """
    try:
        import sqlglot
        from sqlglot import expressions as exp
    except Exception:  # pragma: no cover
        return None
    try:
        tree = sqlglot.parse_one(sql, read=_DIALECT)
    except Exception:
        return None
    if tree is None:
        return None

    def _placeholder(node: "exp.Expression") -> "exp.Expression":
        if isinstance(node, (exp.Literal, exp.Boolean, exp.Null, exp.Parameter, exp.Placeholder)):
            return exp.Placeholder()
        return node

    try:
        normalized = tree.transform(_placeholder)
        return normalized.sql(dialect=_DIALECT, normalize=True, comments=False)
    except Exception:
        return None


def has_order_by(sql: str) -> bool:
    """True if the statement's top-level result is explicitly ordered (``ORDER BY``).

    Only a top-level ORDER BY makes the row order meaningful for comparison; an
    ORDER BY inside a subquery does not. Best-effort (``False`` on parse failure).
    """
    try:
        import sqlglot
        from sqlglot import expressions as exp
    except Exception:  # pragma: no cover
        return False
    try:
        tree = sqlglot.parse_one(sql, read=_DIALECT)
    except Exception:
        return False
    if tree is None:
        return False
    if isinstance(tree, exp.With):
        tree = tree.this
    return bool(isinstance(tree, exp.Select) and tree.args.get("order"))


def tables_in(sql: str) -> List[str]:
    """Lower-cased table names referenced by *sql* (best-effort; ``[]`` on parse fail).

    Used to mark engine-bound tables dirty when a write/DDL touches them.
    """
    try:
        import sqlglot
        from sqlglot import expressions as exp
    except Exception:  # pragma: no cover
        return []
    try:
        tree = sqlglot.parse_one(sql, read=_DIALECT)
    except Exception:
        return []
    if tree is None:
        return []
    names: List[str] = []
    for table in tree.find_all(exp.Table):
        if table.name:
            names.append(table.name.lower())
    return names


def extract_literals(sql: str) -> List[Any]:
    """Literal constants in *sql*, in document order (to bind to placeholders)."""
    try:
        import sqlglot
        from sqlglot import expressions as exp
    except Exception:  # pragma: no cover
        return []
    try:
        tree = sqlglot.parse_one(sql, read=_DIALECT)
    except Exception:
        return []
    if tree is None:
        return []
    values: List[Any] = []
    for node in tree.walk():
        node = node[0] if isinstance(node, tuple) else node
        if isinstance(node, exp.Literal):
            values.append(node.to_py() if hasattr(node, "to_py") else node.this)
        elif isinstance(node, exp.Boolean):
            values.append(bool(node.this))
        elif isinstance(node, exp.Null):
            values.append(None)
    return values
