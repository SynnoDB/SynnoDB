"""Fill templated workload queries with user-supplied parameter values.

A bring-your-own query can be a template with ``[PLACEHOLDER]`` holes (the TPC-H
convention). The user supplies the values explicitly - this module only fills them in. There
is no inference: each placeholder's candidate values come from the workload file, exactly as
the built-in workloads bring their own (TPC-H from a generator, CEB from stored instances).

A query entry carries, per placeholder, the list of possible values. :func:`expand_param_grid`
turns those per-placeholder lists into concrete instantiations by index-zipping them (so
correlated placeholders - e.g. a pair of nations, or a brand/quantity triple - stay aligned),
broadcasting any length-1 list across the sweep.
"""
from __future__ import annotations

import datetime
import decimal
import logging
import os
import re

logger = logging.getLogger(__name__)


def configure_byo_debug() -> bool:
    """Turn on verbose bring-your-own debug logging.

    Off by default. With ``SYNNODB_BYO_DEBUG=1`` registration logs, per query, the loaded
    parameter values and the SQL + args line the engine will receive. Same env-var pattern as
    SYNNODB_WORKER_LOG.
    """
    on = os.environ.get("SYNNODB_BYO_DEBUG", "").strip().lower() in ("1", "true", "yes", "on")
    if on:
        for name in ("synnodb.workloads.query_params", "synnodb.workloads.byo_workload"):
            logging.getLogger(name).setLevel(logging.DEBUG)
        root = logging.getLogger()
        if not root.handlers:  # standalone (registration before the framework logging is set up)
            h = logging.StreamHandler()
            h.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
            root.addHandler(h)
            root.setLevel(logging.DEBUG)
    return on


BYO_DEBUG = configure_byo_debug()

_PLACEHOLDER_RE = re.compile(r"\[([A-Za-z_][A-Za-z0-9_]*)\]")


def find_placeholders(sql: str) -> list[str]:
    """Distinct placeholder names in template order (e.g. ['DELTA'] / ['NATION1','NATION2'])."""
    seen: dict[str, None] = {}
    for m in _PLACEHOLDER_RE.finditer(sql):
        seen.setdefault(m.group(1), None)
    return list(seen)


def _render_in_list(values) -> str:
    """Render a list/tuple as a SQL IN-list literal ``(a, b, c)``.

    String elements are single-quoted so the text is valid both substituted into the SQL and
    passed to the engine (where ``format_args_element`` leaves a ``(``-prefixed value
    unquoted)."""
    parts = []
    for v in values:
        if isinstance(v, datetime.date):
            parts.append(f"date '{v.isoformat()}'")
        elif isinstance(v, decimal.Decimal):
            parts.append(format(v, "f"))
        elif isinstance(v, (int, float)) and not isinstance(v, bool):
            parts.append(repr(v) if isinstance(v, float) else str(v))
        else:
            parts.append("'" + str(v).replace("'", "''") + "'")
    return "(" + ", ".join(parts) + ")"


def render_value(v) -> str:
    """Render a value as the bare literal that goes where ``[PH]`` sat (the template already
    supplies surrounding quotes / ``date`` / ``interval`` syntax). A list/tuple is an IN-list
    and is rendered ``(a, b, c)`` with its own element quoting."""
    if isinstance(v, (list, tuple)):
        return _render_in_list(v)
    if isinstance(v, datetime.date):
        return v.isoformat()
    if isinstance(v, (decimal.Decimal, float)):
        return format(v, "f") if isinstance(v, decimal.Decimal) else repr(v)
    return str(v)


def coerce_for_engine(v) -> str:
    """Render a value as a string for the engine.

    The framework passes all query placeholder values as strings (the built-in TPC-H
    generator returns DELTA='68', QUANTITY='25'): format_args_element quotes them and the
    generated arg-parser reads them with std::quoted into std::string fields, then the engine
    converts. Passing a native int/float here produces an args line of ``"2520"`` that the
    parser, expecting an unquoted ``2520``, rejects with "Q1: failed to parse DELTA".
    render_value is unaffected, so the engine arg and the substituted SQL stay consistent.

    A list/tuple (an IN-list) renders to a single ``(a, b, c)`` string; format_args_element
    detects the leading ``(`` and passes it unquoted."""
    if isinstance(v, (list, tuple)):
        return _render_in_list(v)
    if isinstance(v, datetime.date):
        return v.isoformat()
    if isinstance(v, decimal.Decimal):
        return format(v, "f")  # exact decimal text, e.g. "0.09"
    if isinstance(v, float):
        return repr(v)
    return str(v)


def substitute(template: str, assignment: dict[str, object]) -> str:
    sql = template
    for ph, val in assignment.items():
        sql = sql.replace(f"[{ph}]", render_value(val))
    return sql


def expand_param_grid(template: str, param_lists: dict[str, list]) -> list[dict]:
    """Turn per-placeholder value lists into concrete instantiations.

    ``param_lists`` maps each placeholder to the list of values it should take across the
    sweep. Instantiation *i* takes the *i*-th value of every placeholder (index-zip), so
    correlated placeholders stay aligned. A length-1 list broadcasts to every instantiation;
    any other list must share the common length. Values are coerced to the engine's string
    form (a nested list becomes a ``(a, b, c)`` IN-list).

    Raises ValueError if the supplied placeholders do not match the template's, or if the
    list lengths are inconsistent.
    """
    phs = find_placeholders(template)
    if not phs:
        if param_lists:
            raise ValueError(
                f"Query has no placeholders but params were supplied for {sorted(param_lists)}."
            )
        return [{}]

    keys = set(param_lists)
    if keys != set(phs):
        missing = set(phs) - keys
        extra = keys - set(phs)
        raise ValueError(
            f"params placeholder mismatch: missing={sorted(missing)} extra={sorted(extra)} "
            f"(expected {phs})."
        )

    # Normalize each placeholder's values to a list of per-instantiation values.
    columns: dict[str, list] = {}
    for ph in phs:
        v = param_lists[ph]
        columns[ph] = list(v) if isinstance(v, list) else [v]
        if not columns[ph]:
            raise ValueError(f"params for placeholder '{ph}' is empty; supply at least one value.")

    lengths = {len(c) for c in columns.values()}
    n = max(lengths)
    bad = {ph: len(c) for ph, c in columns.items() if len(c) not in (1, n)}
    if bad:
        raise ValueError(
            f"inconsistent number of values across placeholders: {bad}; each list must have "
            f"length 1 (broadcast) or {n}."
        )

    out: list[dict] = []
    for i in range(n):
        out.append({ph: coerce_for_engine(col[i] if len(col) == n else col[0])
                    for ph, col in columns.items()})
    return out
