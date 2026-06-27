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

import functools
import re
from typing import Any, List, Optional, Sequence

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
        # A cast wrapping a single literal/placeholder is a typed literal (DATE 'x',
        # CAST(5 AS BIGINT)). Collapse it to a placeholder so an inline `date 'x'` shares
        # a structural key with a bare `?` in the template.
        if isinstance(node, exp.Cast) and isinstance(
            node.this, (exp.Literal, exp.Boolean, exp.Null, exp.Placeholder)
        ):
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


def _node_value(node: Any) -> Any:
    """Python value of a literal/boolean/null node (matches extract_literals)."""
    import sqlglot  # noqa: F401
    from sqlglot import expressions as exp

    if isinstance(node, exp.Literal):
        return node.to_py() if hasattr(node, "to_py") else node.this
    if isinstance(node, exp.Boolean):
        return bool(node.this)
    if isinstance(node, exp.Null):
        return None
    return None


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
        if isinstance(node, (exp.Literal, exp.Boolean, exp.Null)):
            values.append(_node_value(node))
    return values


def _loose_eq(a: Any, b: Any) -> bool:
    """Type-tolerant equality so a template constant ``'1'`` matches an incoming ``1``."""
    return a == b or str(a) == str(b)


def has_param_markers(template_sql: str) -> bool:
    """True if the template uses explicit ``?`` / ``$name`` placeholders (vs a concrete
    example query whose literals stand in for parameters). Selects the binding strategy:
    explicit markers → structural unification; concrete example → positional literals."""
    tree = _parse_cached(template_sql)
    if tree is None:
        return False
    from sqlglot import expressions as exp

    return bool(list(tree.find_all(exp.Placeholder)) or list(tree.find_all(exp.Parameter)))


@functools.lru_cache(maxsize=512)
def _parse_cached(sql: str) -> Any:
    try:
        import sqlglot
    except Exception:  # pragma: no cover
        return None
    try:
        return sqlglot.parse_one(sql, read=_DIALECT)
    except Exception:
        return None


def _value_of(node: Any) -> Any:
    """Value of an incoming sub-expression at a placeholder position: a bare literal, a
    signed number, the inner literal of a ``DATE 'x'`` cast, or, failing that, the
    rendered SQL of the sub-expression."""
    from sqlglot import expressions as exp

    while isinstance(node, exp.Paren):
        node = node.this
    if isinstance(node, exp.Neg):  # -5  →  Neg(Literal(5))
        inner = _value_of(node.this)
        try:
            return -inner  # type: ignore[operator]
        except TypeError:
            return node.sql(dialect=_DIALECT)
    if isinstance(node, (exp.Literal, exp.Boolean, exp.Null)):
        return _node_value(node)
    lits = list(node.find_all(exp.Literal))
    if len(lits) == 1:
        return _node_value(lits[0])
    return node.sql(dialect=_DIALECT)


def _record(name: str, value: Any, bound: dict) -> bool:
    if name in bound and not _loose_eq(bound[name], value):
        return False  # a repeated placeholder seen with two different values
    bound[name] = value
    return True


def _unify(t: Any, i: Any, names: Sequence[str], counter: List[int], bound: dict) -> bool:
    """Structurally unify a template node *t* with an incoming node *i*. A ``?`` /
    ``$name`` placeholder in the template is a wildcard that binds to the whole incoming
    sub-expression; everything else must match in type, and literals must match in value."""
    from sqlglot import expressions as exp

    # Redundant parens are not semantically meaningful for matching; unwrap on both sides
    # so `interval (?) day` unifies with `interval 90 day`.
    while isinstance(t, exp.Paren):
        t = t.this
    while isinstance(i, exp.Paren):
        i = i.this

    if isinstance(t, exp.Placeholder):
        nm = t.name
        if nm and nm != "?":  # named placeholder ($DATE, or the synthetic $__synpN)
            return _record(nm, _value_of(i), bound)
        # anonymous ? — normally rewritten to a named one before we get here
        if counter[0] >= len(names):
            return False
        name = names[counter[0]]
        counter[0] += 1
        return _record(name, _value_of(i), bound)
    if isinstance(t, exp.Parameter):  # some dialects represent $name as Parameter
        return _record(t.name, _value_of(i), bound)
    if type(t) is not type(i):
        return False
    if isinstance(t, (exp.Literal, exp.Boolean, exp.Null)):
        # Constants must match EXACTLY, type-sensitive: `1` and `'1'` are both Literal but
        # render differently, so we never accelerate a query that only *looks* similar.
        return t.sql(dialect=_DIALECT) == i.sql(dialect=_DIALECT)
    if set(t.args.keys()) != set(i.args.keys()):
        return False
    for key in t.args:
        if not _unify_arg(t.args[key], i.args[key], names, counter, bound):
            return False
    return True


def _unify_arg(tv: Any, iv: Any, names: Sequence[str], counter: List[int], bound: dict) -> bool:
    from sqlglot import expressions as exp

    if isinstance(tv, list) or isinstance(iv, list):
        tl = tv if isinstance(tv, list) else [tv]
        il = iv if isinstance(iv, list) else [iv]
        if len(tl) != len(il):
            return False
        return all(_unify_arg(a, b, names, counter, bound) for a, b in zip(tl, il))
    if isinstance(tv, exp.Expression):
        return isinstance(iv, exp.Expression) and _unify(tv, iv, names, counter, bound)
    return tv == iv  # plain scalar arg (a flag/keyword) must match


def _name_anonymous_placeholders(
    sql: str, names: Sequence[str]
) -> tuple[Optional[str], dict]:
    """Rewrite each anonymous ``?`` (in true *source* order, skipping string/identifier
    literals) into a uniquely-named ``$__synpN`` parameter, returning the rewritten SQL
    and ``{__synpN: user_name}``. This removes any dependence on AST traversal order for
    mapping ``?`` to the user's positional placeholder names. Returns ``(None, {})`` if
    the number of ``?`` does not match ``names``."""
    out: List[str] = []
    mapping: dict = {}
    i = 0
    pi = 0
    n = len(sql)
    quote: Optional[str] = None
    while i < n:
        c = sql[i]
        if quote is not None:
            out.append(c)
            if c == quote:
                if i + 1 < n and sql[i + 1] == quote:  # escaped '' or ""
                    out.append(sql[i + 1])
                    i += 2
                    continue
                quote = None
            i += 1
            continue
        if c in ("'", '"'):
            quote = c
            out.append(c)
            i += 1
            continue
        if c == "?":
            syn = f"__synp{pi}"
            out.append("$" + syn)
            if pi < len(names):
                mapping[syn] = names[pi]
            pi += 1
            i += 1
            continue
        out.append(c)
        i += 1
    if pi != len(names):
        return None, {}
    return "".join(out), mapping


def unify_and_bind(
    template_sql: str, incoming_sql: str, placeholder_names: Sequence[str] = ()
) -> Optional[dict]:
    """Bind an incoming query's values to a template's placeholders by structurally
    matching the two parse trees. The template may use anonymous ``?`` (mapped to
    ``placeholder_names`` in source order) and/or named ``$name`` parameters, and a name
    may repeat (e.g. TPC-H Q6's ``[DATE]`` twice).

    Returns ``{name: value}`` (a repeated placeholder collapsed to one value) or ``None``
    when the incoming query does not match the template: a differing constant, a
    structural difference, an arity mismatch, or a repeated placeholder seen with two
    different values. The constants in the template are matched as constants, so an inline
    query like ``... interval 90 day`` binds the parameter and not a constant.
    """
    rewritten, mapping = _name_anonymous_placeholders(template_sql, placeholder_names)
    if rewritten is None:
        return None  # `?` count != len(placeholder_names)

    t = _parse_cached(rewritten)
    i = _parse_cached(incoming_sql)
    if t is None or i is None:
        return None
    bound: dict = {}
    if not _unify(t, i, (), [0], bound):
        return None

    # Remap synthetic ?-names to the user's names, enforcing that a placeholder used in
    # several positions resolved to one consistent value.
    final: dict = {}
    for key, val in bound.items():
        user = mapping.get(key, key)
        if user in final and not _loose_eq(final[user], val):
            return None
        final[user] = val

    # Every distinct user placeholder name must have been bound.
    distinct = set(mapping.values()) if mapping else set(placeholder_names)
    if distinct - set(final):
        return None
    return final
