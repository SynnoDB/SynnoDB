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
from typing import Any, List, Optional, Sequence, Tuple

_DIALECT = "duckdb"

# Leading keyword → read-only? Used as a fast path and a fallback when parsing fails.
_READ_KEYWORDS = {
    "select",
    "with",
    "explain",
    "describe",
    "desc",
    "show",
    "summarize",
    "values",
    "pragma",
    "table",
    "from",
}
_WRITE_KEYWORDS = {
    "insert",
    "update",
    "delete",
    "merge",
    "create",
    "drop",
    "alter",
    "truncate",
    "replace",
    "attach",
    "detach",
    "copy",
    "import",
    "export",
    "set",
    "begin",
    "commit",
    "rollback",
    "checkpoint",
    "vacuum",
    "call",
    "use",
    "load",
    "install",
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


def is_read_only_query(sql: str) -> bool:
    """True if *sql* is a read-only query, and therefore allowed when writes are blocked.

    Read queries (SELECT/WITH/EXPLAIN/SHOW/DESCRIBE/SUMMARIZE/VALUES and read-only PRAGMA
    introspection) pass through, and a SELECT may route. Anything that mutates data, changes
    session/catalog state, or manages transactions/extensions is a write.

    The cheap leading-keyword check is enough for a single statement, but two shapes can hide
    a write behind a read-looking start: a CTE-led statement (``WITH ... DELETE``) and a
    multi-statement string (``SELECT 1; DROP TABLE t``). Those are parsed and allowed only if
    every statement is a SELECT-family query. An unrecognized single keyword is confirmed by a
    SELECT parse, so a parenthesized or comment-led SELECT is still allowed.
    """
    kind = statement_kind(sql)
    if kind == "write":
        return False
    if kind == "read":
        if _leading_keyword(sql) == "with" or _has_multiple_statements(sql):
            return _all_statements_are_select(sql)
        return True
    return is_select(sql)


def _has_multiple_statements(sql: str) -> bool:
    """Cheap, conservative test for more than one statement. A ``;`` inside a string literal
    is a false positive, which only costs a parse downstream, never correctness."""
    s = sql.strip()
    if s.endswith(";"):
        s = s[:-1]
    return ";" in s


def _all_statements_are_select(sql: str) -> bool:
    """True only if *sql* parses into one or more statements that are all SELECT-family
    queries. Used to clear a CTE-led or multi-statement string; anything that does not parse,
    or contains a non-SELECT statement, is treated as not read-only (so it is blocked)."""
    try:
        import sqlglot
        from sqlglot import expressions as exp
    except Exception:  # pragma: no cover - sqlglot is a core dep
        return False
    try:
        statements = sqlglot.parse(sql, read=_DIALECT)
    except Exception:
        return False
    if not statements:
        return False
    for stmt in statements:
        if stmt is None:
            continue
        inner = stmt.this if isinstance(stmt, exp.With) else stmt
        if not isinstance(inner, (exp.Select, exp.Union, exp.Subquery)):
            return False
    return True


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
        if isinstance(
            node, (exp.Literal, exp.Boolean, exp.Null, exp.Parameter, exp.Placeholder)
        ):
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


def order_by_key_indices(sql: str, output_names: Sequence[str]) -> Optional[List[int]]:
    """Output-column indices of the top-level ``ORDER BY`` keys, for a tie-aware ordered
    cross-check.

    A query with ``ORDER BY`` constrains the order only by its key columns; rows that tie on the
    keys may appear in any order, and a correct engine may legitimately break those ties
    differently from DuckDB. Comparing such a result strictly position-by-position false-rejects
    the engine and quarantines it. To compare correctly we need to know which OUTPUT columns are
    the sort keys: then the keys must match positionally (the real ordering contract) while tied
    rows are compared as a multiset.

    Returns the list of output-column indices for the keys, or ``None`` when there is no top-level
    ``ORDER BY`` or any key is not a plain reference to a single output column / a positional
    ordinal (an arbitrary ``ORDER BY`` expression, or a key that is not projected). ``None`` tells
    the caller to fall back to a strict positional comparison, which is conservative: it can only
    over-reject, never accept a wrongly ordered result.
    """
    tree = _parse_cached(sql)
    if tree is None:
        return None
    from sqlglot import expressions as exp

    if isinstance(tree, exp.With):
        tree = tree.this
    if not isinstance(tree, exp.Select):
        return None
    order = tree.args.get("order")
    if not order:
        return None
    lower = [n.lower() for n in output_names]
    indices: List[int] = []
    for ordered in order.expressions:
        key = ordered.this
        # ORDER BY <ordinal> (1-based positional reference into the SELECT list).
        if isinstance(key, exp.Literal) and not key.is_string:
            try:
                pos = int(key.this) - 1
            except (TypeError, ValueError):
                return None
            if 0 <= pos < len(output_names):
                indices.append(pos)
                continue
            return None
        # ORDER BY <column or output alias>. Resolve only an unambiguous, unqualified name that
        # is exactly one output column; anything qualified, ambiguous, or not projected -> strict.
        if isinstance(key, exp.Column) and not key.table:
            nm = key.name.lower()
            if lower.count(nm) == 1:
                indices.append(lower.index(nm))
                continue
        return None
    return indices or None


def top_level_limit_offset(sql: str) -> Tuple[Optional[int], int]:
    """The statement's top-level ``LIMIT`` / ``OFFSET`` row counts as ``(limit, offset)``.

    Only the top-level clause bounds the result set, so a ``LIMIT`` inside a subquery or CTE is
    ignored. ``limit`` is ``None`` when the result is not truncated - no top-level ``LIMIT``, a
    count that is not a plain integer literal, or a parse failure. That default is the safe one: it
    makes the caller compare the full result strictly, which can only over-reject.
    """
    tree = _parse_cached(sql)
    if tree is None:
        return None, 0
    from sqlglot import expressions as exp

    if isinstance(tree, exp.With):
        tree = tree.this
    if not isinstance(tree, exp.Select):
        return None, 0

    def _count(node: Any) -> Optional[int]:
        if node is None:
            return None
        value = node.expression if isinstance(node, (exp.Limit, exp.Offset)) else node
        if not isinstance(value, exp.Literal) or value.is_string:
            return None
        try:
            return int(value.this)
        except (TypeError, ValueError):
            return None

    return _count(tree.args.get("limit")), _count(tree.args.get("offset")) or 0


def widened_query(sql: str, limit: int) -> Optional[str]:
    """*sql* re-aimed at the first *limit* rows of its ranking, or ``None`` if it cannot be
    rewritten.

    The top-level ``LIMIT`` becomes *limit* and any top-level ``OFFSET`` is dropped, so the result
    starts at rank 1 and - once *limit* is large enough - is a superset of every row the original
    window could legitimately have contained. Everything else (the ORDER BY, the predicates, any
    nested clause) is untouched, so the widened query ranks rows exactly as the original does.

    This is how :func:`adapt.candidate_superset` turns an ambiguous ``ORDER BY k LIMIT n`` into
    something checkable: the engine's rows must be members of this superset rather than a
    reproduction of the arbitrary pick DuckDB made at the cut.
    """
    tree = _parse_cached(sql)
    if tree is None:
        return None
    from sqlglot import expressions as exp

    # _parse_cached hands back a shared cached tree; mutating it in place would corrupt every
    # later use of that SQL.
    tree = tree.copy()
    target = tree.this if isinstance(tree, exp.With) else tree
    if not isinstance(target, exp.Select) or target.args.get("limit") is None:
        return None
    try:
        target.set("limit", exp.Limit(expression=exp.Literal.number(limit)))
        target.set("offset", None)
        return tree.sql(dialect=_DIALECT)
    except Exception:
        return None


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
    # A CTE is referenced as a Table node but is not a real table; exclude CTE names so a
    # query like ``WITH revenue AS (...) SELECT ... FROM lineitem, revenue`` reports only the
    # real tables (``lineitem``), not the CTE alias.
    cte_names = {
        cte.alias_or_name.lower() for cte in tree.find_all(exp.CTE) if cte.alias_or_name
    }
    names: List[str] = []
    for table in tree.find_all(exp.Table):
        if table.name and table.name.lower() not in cte_names:
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

    return bool(
        list(tree.find_all(exp.Placeholder)) or list(tree.find_all(exp.Parameter))
    )


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


def _unify(
    t: Any, i: Any, names: Sequence[str], counter: List[int], bound: dict
) -> bool:
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
        if nm and nm != "?":  # named placeholder ($DATE, or the synthetic :synpN)
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


def _unify_arg(
    tv: Any, iv: Any, names: Sequence[str], counter: List[int], bound: dict
) -> bool:
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


def scan_string_literals(sql: str) -> List[tuple[int, int, str]]:
    """Every quoted string/identifier as ``(open_index, close_index, inner_text)``, by a single
    quote-aware left-to-right scan. Both ``'`` and ``"`` delimit; a doubled quote (``''`` / ``""``)
    is an escape, not a boundary, and ``inner_text`` has those escapes collapsed.

    A regex cannot do this: it has no notion of which ``'`` opens vs closes a literal, so it would
    pair a *closing* quote with the next *opening* quote and treat the gap between two unrelated
    literals as one string - sweeping up whatever sits between them."""
    spans: List[tuple[int, int, str]] = []
    i, n = 0, len(sql)
    while i < n:
        q = sql[i]
        if q not in ("'", '"'):
            i += 1
            continue
        j = i + 1
        buf: List[str] = []
        while j < n:
            if sql[j] == q:
                if j + 1 < n and sql[j + 1] == q:  # escaped '' or ""
                    buf.append(q)
                    j += 2
                    continue
                break
            buf.append(sql[j])
            j += 1
        spans.append((i, j, "".join(buf)))
        i = j + 1
    return spans


def _name_anonymous_placeholders(
    sql: str, names: Sequence[str]
) -> tuple[Optional[str], dict]:
    """Rewrite each anonymous ``?`` (in true *source* order, skipping string/identifier
    literals) into a uniquely-named ``:synpN`` placeholder, returning the rewritten SQL
    and ``{synpN: user_name}``. This removes any dependence on AST traversal order for
    mapping ``?`` to the user's positional placeholder names. Returns ``(None, {})`` if
    the number of ``?`` does not match ``names``.

    The ``:name`` form (not ``$name``) is deliberate: sqlglot's duckdb tokenizer rejects a
    ``$name`` parameter inside an ``IN (...)`` list (e.g. TPC-H Q22's country-code list), whereas
    ``:name`` parses as a Placeholder in every position a value can appear."""
    out: List[str] = []
    mapping: dict = {}
    pi = 0
    last = 0

    def rewrite_outside(segment: str) -> None:
        # A `?` only ever appears outside a quoted literal here, so split is safe.
        nonlocal pi
        pieces = segment.split("?")
        out.append(pieces[0])
        for piece in pieces[1:]:
            if pi < len(names):
                mapping[f"synp{pi}"] = names[pi]
            out.append(f":synp{pi}")
            pi += 1
            out.append(piece)

    for open_i, close_i, _ in scan_string_literals(sql):
        rewrite_outside(sql[last:open_i])
        out.append(sql[open_i : close_i + 1])  # literal copied verbatim
        last = close_i + 1
    rewrite_outside(sql[last:])
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


def binding_groups(specs: Sequence[Any]) -> List[List[Any]]:
    """Partition placeholder *specs* into binding groups, in order - one group per SQL ``?``.

    A standalone parameter (``group == -1``) is its own group; consecutive parameters that share a
    non-negative ``group`` id come from one string literal (e.g. Q13's two words) and bind from a
    single ``?``. The result drives both the marker/value arity and the per-literal splitting."""
    groups: List[List[Any]] = []
    for s in specs:
        if s.group >= 0 and groups and groups[-1][0].group == s.group:
            groups[-1].append(s)
        else:
            groups.append([s])
    return groups


def split_literal(value: Any, group: Sequence[Any]) -> Optional[dict]:
    """Recover the parameter(s) a single bound literal carries, as ``{name: value}``.

    *group* is the ordered specs sharing the literal; each carries the constant ``prefix`` before
    it and ``suffix`` after it (the delimiter to the next parameter, or the literal's tail for the
    last). Returns ``None`` when *value* does not carry exactly those constants around
    wildcard-free cores - the correctness boundary. Two lookalikes share the coarse structural key
    ``like <ph>`` but are different queries that must not route here: ``like 'BRASS'`` (missing
    the template's ``%``), and ``like '%y%'`` against a ``'%[TYPE]'`` template (the core ``y%``
    is a *pattern* to SQL but would reach the engine as a literal word).
    """
    if len(group) == 1 and not group[0].prefix and not group[0].suffix:
        return {group[0].name: value}  # plain whole-literal parameter (any type)
    if not isinstance(value, str):
        return None
    # One anchored regex does the whole check: the constants must appear verbatim, and each
    # recovered core must be free of LIKE wildcards (which also makes the split unambiguous).
    consts = [group[0].prefix] + [s.suffix for s in group]
    m = re.fullmatch("([^%_]*)".join(re.escape(c) for c in consts), value)
    if m is None:
        return None
    out: dict = {}
    for s, core in zip(group, m.groups()):
        if s.name in out and out[s.name] != core:
            return None  # one literal using the same parameter twice, inconsistently
        out[s.name] = core
    return out


def merge_split(
    groups: Sequence[Sequence[Any]], values: Sequence[Any]
) -> Optional[dict]:
    """Split each group's bound *value* into its parameter(s) and merge into one ``{name: value}``,
    rejecting (``None``) a repeated placeholder that resolves to two different values or a value
    that does not carry its literal's constants. Shared by the two binding paths (a query's inline
    literals via :func:`bind_template`, and caller-supplied ``parameters`` in the router)."""
    out: dict = {}
    for group, value in zip(groups, values):
        parts = split_literal(value, group)
        if parts is None:
            return None
        for name, val in parts.items():
            if name in out and not _loose_eq(out[name], val):
                return None
            out[name] = val
    return out


def bind_template(
    template_sql: str, incoming_sql: str, specs: Sequence[Any]
) -> Optional[dict]:
    """Bind *incoming_sql* against *template_sql*, unpacking any string-embedded parameters.

    A wrapper over :func:`unify_and_bind` that also handles parameters living inside a string
    literal (see :class:`PlaceholderSpec`). Each binding group is one ``?``: it binds the whole
    literal, then :func:`split_literal` recovers the parameter(s) inside. Returns ``{name: value}``
    or ``None`` when the query does not match the template or a literal lacks its constants.
    """
    groups = binding_groups(specs)
    # One synthetic name per group so the ``?`` count matches even when a literal packs several
    # parameters (Q13: two words, one ``?``). unify binds the whole literal to that name.
    group_names = [f"__grp{i}" for i in range(len(groups))]
    raw = unify_and_bind(template_sql, incoming_sql, group_names)
    if raw is None:
        return None
    return merge_split(groups, [raw.get(n) for n in group_names])
