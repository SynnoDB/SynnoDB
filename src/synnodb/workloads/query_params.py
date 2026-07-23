"""Fill templated workload queries with user-supplied parameter values.

A bring-your-own query can be a template with ``[PLACEHOLDER]`` holes (the TPC-H
convention). Each placeholder declares its value *space* as a typed spec, and the space is
sampled symbolically at run time (with the run's seeded RNG) - exactly the way the built-in
TPC-H generator draws ``randint``/``choice`` per execution.

A placeholder's spec is a discriminated union on ``type``:

  * ``{"type": "int",   "min": 60, "max": 120, "step": 1}`` - uniform int on the step grid;
  * ``{"type": "float", "min": 0.02, "max": 0.09, "step": 0.01}`` - uniform on the step grid
    (``step`` also fixes the rendered precision);
  * ``{"type": "date",  "min": "1993-01-01", "max": "1997-10-01"}`` -
    a uniform ISO date in the closed range;
  * ``{"type": "categorical", "values": ["ASIA", "EUROPE", ...]}`` - a uniform choice.

Correlated / distinct placeholders (a nation pair, a brand/quantity triple, a k-distinct
IN-list) are described by a *group* spec binding several placeholders jointly:

  * ``{"type": "tuples",  "placeholders": ["N1","N2"], "values": [["GERMANY","ROMANIA"], ...]}``
    - sample one row, assign its columns positionally;
  * ``{"type": "sample", "placeholders": ["I1", ..., "I7"], "domain": [...], "distinct": true}``
    - draw ``len(placeholders)`` values from a domain (distinct by default).

:func:`parse_param_space` validates a query's specs against its template (the typing layer);
:func:`ParamSpace.sample` draws one concrete assignment; :func:`substitute` fills it in.
"""

from __future__ import annotations

import datetime
import decimal
import logging
import os
import random
import re
from dataclasses import dataclass

logger = logging.getLogger(__name__)


def configure_byo_debug() -> bool:
    """Turn on verbose bring-your-own debug logging.

    Off by default. With ``SYNNODB_BYO_DEBUG=1`` registration logs, per query, the loaded
    parameter values and the SQL + args line the engine will receive. Same env-var pattern as
    SYNNODB_WORKER_LOG.
    """
    on = os.environ.get("SYNNODB_BYO_DEBUG", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )
    if on:
        for name in (
            "synnodb.workloads.query_params",
            "synnodb.workloads.byo_workload",
        ):
            logging.getLogger(name).setLevel(logging.DEBUG)
        root = logging.getLogger()
        if (
            not root.handlers
        ):  # standalone (registration before the framework logging is set up)
            h = logging.StreamHandler()
            h.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
            root.addHandler(h)
            root.setLevel(logging.DEBUG)
    return on


BYO_DEBUG = configure_byo_debug()

# Template placeholders are UPPERCASE by convention (DELTA, NATION1, INFO2, ...). Restricting the
# match to uppercase names avoids mistaking bracketed *data* literals for holes - e.g. IMDB's
# ``country_code`` values are genuinely ``'[cz]'`` / ``'[pa]'``, which would otherwise read as
# placeholders inside a static bring-your-own query. Legitimate placeholders may still sit inside a
# quoted string (TPC-H Q2's ``'%[TYPE]'``), so quote-awareness alone would not disambiguate.
_PLACEHOLDER_RE = re.compile(r"\[([A-Z][A-Z0-9_]*)\]")


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


# A single-quoted SQL string literal, with '' as the escaped embedded quote.
_QUOTED_LITERAL_RE = re.compile(r"'(?:[^']|'')*'")


def is_quoted_literal(value) -> bool:
    """True when ``value`` is a string shaped like a single-quoted SQL literal (``'scifi'``,
    ``'O''Reilly'``). Placeholder values must be bare - the template supplies the quotes
    (see :func:`render_value`). A quoted value substitutes into valid SQL, but its single
    quotes also travel to the engine args line, where the generated C++ parser's
    ``std::quoted`` strips only the wire double quotes - the single quotes leak into the
    string field and lookups miss. :func:`hoist_literal_quotes` converts such values."""
    return isinstance(value, str) and _QUOTED_LITERAL_RE.fullmatch(value) is not None


def hoist_literal_quotes(
    template: str, placeholders: list[str], rows: list[list]
) -> tuple[str, list[list]]:
    """Move SQL string quoting from parameter values into the template.

    Extraction-based workloads (Stack, MusicBrainz) record each varying literal exactly as it
    appears in the SQL, quotes included: values like ``'scifi'`` fill bare template holes
    (``site_name=[SITE_NAME]``). That substitutes into valid SQL but leaks the single quotes
    onto the engine args line (see :func:`is_quoted_literal`). The framework convention is
    the inverse: the template supplies the quotes (``'[NAME]'``) and values travel bare.

    This helper converts a ``tuples`` group to that convention: for every placeholder whose
    value in *each* row is a quoted literal, the quotes are stripped from the values and the
    template hole is wrapped as ``'[NAME]'``. Substituted SQL is unchanged; the engine now
    receives bare values. Numeric values and IN-list values are left untouched (IN-lists are
    fine as-is: ``parse_in_list`` strips their element quotes engine-side).

    Raises ValueError on a placeholder that mixes quoted and unquoted string values, or whose
    literal contains an embedded (escaped) quote - hoisting would substitute the bare value
    into a quoted template hole without re-escaping, so both need manual handling rather
    than a silent half-conversion.
    """
    hoisted_rows = [list(row) for row in rows]
    for col, name in enumerate(placeholders):
        values = [row[col] for row in hoisted_rows]
        quoted = [v for v in values if is_quoted_literal(v)]
        if not quoted:
            continue
        embedded = next((v for v in quoted if "'" in v[1:-1]), None)
        if embedded is not None:
            raise ValueError(
                f"placeholder '{name}': value {embedded!r} contains an embedded quote; "
                f"hoisting would substitute it unescaped into a quoted template hole. "
                f"Handle this placeholder manually."
            )
        if len(quoted) != len(values):
            offenders = [v for v in values if not is_quoted_literal(v)][:3]
            raise ValueError(
                f"placeholder '{name}': cannot hoist quotes - values mix quoted literals "
                f"with other shapes (e.g. {offenders!r}). A placeholder must be uniformly "
                f"quoted (hoistable) or uniformly bare/IN-list."
            )
        if f"[{name}]" not in template:
            raise ValueError(
                f"placeholder '{name}' does not occur in the template; cannot hoist its "
                f"quotes."
            )
        template = template.replace(f"[{name}]", f"'[{name}]'")
        for row in hoisted_rows:
            row[col] = row[col][1:-1]
    return template, hoisted_rows


# --- Typed parameter specs ---------------------------------------------------
#
# A scalar spec binds one placeholder; a group spec binds several jointly. Each spec knows
# how to (a) sample one value/row with an RNG and (b) describe itself as UI metadata (so a
# live dashboard can render a slider / dropdown / date-picker without re-deriving ranges).


@dataclass(frozen=True)
class IntSpec:
    min: int
    max: int
    step: int = 1

    def sample(self, rnd: random.Random) -> int:
        steps = (self.max - self.min) // self.step
        return self.min + self.step * rnd.randint(0, steps)

    def metadata(self) -> dict:
        return {"type": "int", "min": self.min, "max": self.max, "step": self.step}


@dataclass(frozen=True)
class FloatSpec:
    min: float
    max: float
    step: float

    def sample(self, rnd: random.Random) -> decimal.Decimal:
        # Step on an exact decimal grid so values render as "0.02" rather than 0.020000004;
        # the Decimal flows through coerce_for_engine as exact text.
        lo = decimal.Decimal(str(self.min))
        step = decimal.Decimal(str(self.step))
        steps = int((decimal.Decimal(str(self.max)) - lo) / step)
        return lo + step * rnd.randint(0, steps)

    def metadata(self) -> dict:
        return {"type": "float", "min": self.min, "max": self.max, "step": self.step}


@dataclass(frozen=True)
class DateSpec:
    min: datetime.date
    max: datetime.date

    def sample(self, rnd: random.Random) -> datetime.date:
        days = (self.max - self.min).days
        return self.min + datetime.timedelta(days=rnd.randint(0, days))

    def metadata(self) -> dict:
        return {
            "type": "date",
            "min": self.min.isoformat(),
            "max": self.max.isoformat(),
        }


@dataclass(frozen=True)
class CategoricalSpec:
    values: tuple

    def sample(self, rnd: random.Random):
        return rnd.choice(self.values)

    def metadata(self) -> dict:
        return {"type": "categorical", "values": list(self.values)}


@dataclass(frozen=True)
class TupleGroupSpec:
    """Several placeholders sampled jointly from enumerated rows (the index-zipped "list"
    form). Sampling picks one row and assigns its columns positionally, so correlated
    placeholders stay aligned."""

    placeholders: tuple[str, ...]
    rows: tuple[tuple, ...]

    def sample(self, rnd: random.Random) -> dict:
        return dict(zip(self.placeholders, rnd.choice(self.rows)))

    def column_metadata(self, ph: str) -> dict:
        i = self.placeholders.index(ph)
        seen: dict = {}
        for row in self.rows:
            seen.setdefault(row[i], None)
        return {"type": "categorical", "values": list(seen)}


@dataclass(frozen=True)
class SampleGroupSpec:
    """Draw ``len(placeholders)`` values from a shared domain (distinct by default) - the
    k-distinct IN-list case (Q7 nation pair, Q16 sizes, Q22 country codes)."""

    placeholders: tuple[str, ...]
    domain: tuple
    distinct: bool = True

    def sample(self, rnd: random.Random) -> dict:
        k = len(self.placeholders)
        if self.distinct:
            vals = rnd.sample(self.domain, k)
        else:
            vals = [rnd.choice(self.domain) for _ in range(k)]
        return dict(zip(self.placeholders, vals))

    def column_metadata(self, ph: str) -> dict:
        return {"type": "categorical", "values": list(self.domain)}


ScalarSpec = (IntSpec, FloatSpec, DateSpec, CategoricalSpec)
GroupSpec = (TupleGroupSpec, SampleGroupSpec)


@dataclass(frozen=True)
class ParamSpace:
    """A query's whole parameter space: per-placeholder scalar specs plus joint group specs,
    carrying the template's placeholder order so a sampled assignment is emitted in that order
    (the order the engine arg-parser expects)."""

    order: tuple[str, ...]
    scalars: dict[str, object]
    groups: tuple[object, ...]

    def is_empty(self) -> bool:
        return not self.scalars and not self.groups

    def sample(self, rnd: random.Random) -> dict[str, str]:
        """Draw one concrete assignment ``{placeholder: engine_string}`` in template order."""
        raw: dict[str, object] = {}
        for ph, spec in self.scalars.items():
            raw[ph] = spec.sample(rnd)
        for group in self.groups:
            raw.update(group.sample(rnd))
        return {ph: coerce_for_engine(raw[ph]) for ph in self.order}

    def metadata(self) -> dict[str, dict]:
        """Per-placeholder UI metadata in template order (slider / dropdown / date-picker)."""
        meta: dict[str, dict] = {}
        for ph, spec in self.scalars.items():
            meta[ph] = spec.metadata()
        for group in self.groups:
            for ph in group.placeholders:
                meta[ph] = group.column_metadata(ph)
        return {ph: meta[ph] for ph in self.order}


def _require(cond: bool, msg: str) -> None:
    if not cond:
        raise ValueError(msg)


def _is_simple_scalar(v) -> bool:
    """A single placeholder value the engine can read: a string or a number.

    Bools/None/dicts/lists are not simple scalars - placeholder values must render to the
    quoted string the generated C++ arg-parser reads into a ``std::string`` field. (A *list*
    is an IN-list, handled separately by :func:`_value_wire_shape`.)"""
    return isinstance(v, (str, int, float)) and not isinstance(v, bool)


def _value_wire_shape(label: str, v) -> str:
    """Classify one candidate placeholder value as ``"scalar"`` or ``"in_list"``.

    The shape fixes the generated C++ field type (``std::string`` vs
    ``std::vector<std::string>``), which is frozen from a single representative sample at
    code-gen time. A placeholder must therefore never be able to draw *both* shapes, so callers
    require one shape across a spec's whole value set. A list/tuple is an IN-list and must hold
    only simple scalars (no nesting); everything else must itself be a simple scalar."""
    if isinstance(v, (list, tuple)):
        _require(
            len(v) > 0 and all(_is_simple_scalar(e) for e in v),
            f"{label}: IN-list value {v!r} must be a non-empty list of simple scalars "
            f"(strings or numbers).",
        )
        return "in_list"
    _require(
        _is_simple_scalar(v),
        f"{label}: value {v!r} must be a string or number (simple datatypes only).",
    )
    return "scalar"


def _parse_number(ph: str, raw: dict, key: str, kind: str):
    _require(key in raw, f"'{ph}' ({kind} spec): missing required field '{key}'.")
    v = raw[key]
    _require(
        isinstance(v, (int, float)) and not isinstance(v, bool),
        f"'{ph}' ({kind} spec): '{key}' must be a number, got {v!r}.",
    )
    return v


def _parse_scalar_spec(ph: str, raw: object):
    _require(
        isinstance(raw, dict) and "type" in raw,
        f"'{ph}': a parameter spec must be an object with a 'type' field, got {raw!r}.",
    )
    t = raw["type"]
    if t == "int":
        lo = _parse_number(ph, raw, "min", "int")
        hi = _parse_number(ph, raw, "max", "int")
        step = raw.get("step", 1)
        _require(
            all(isinstance(x, int) and not isinstance(x, bool) for x in (lo, hi, step)),
            f"'{ph}' (int spec): min/max/step must be integers.",
        )
        _require(step > 0, f"'{ph}' (int spec): step must be > 0.")
        _require(lo <= hi, f"'{ph}' (int spec): min ({lo}) must be <= max ({hi}).")
        return IntSpec(lo, hi, step)
    if t == "float":
        lo = _parse_number(ph, raw, "min", "float")
        hi = _parse_number(ph, raw, "max", "float")
        step = _parse_number(ph, raw, "step", "float")
        _require(step > 0, f"'{ph}' (float spec): step must be > 0.")
        _require(lo <= hi, f"'{ph}' (float spec): min ({lo}) must be <= max ({hi}).")
        return FloatSpec(float(lo), float(hi), float(step))
    if t == "date":
        _require(
            "granularity" not in raw,
            f"'{ph}' (date spec): 'granularity' is no longer supported; use min/max only.",
        )
        lo = _parse_date(ph, raw, "min")
        hi = _parse_date(ph, raw, "max")
        _require(lo <= hi, f"'{ph}' (date spec): min ({lo}) must be <= max ({hi}).")
        return DateSpec(lo, hi)
    if t == "categorical":
        vals = raw.get("values")
        _require(
            isinstance(vals, list) and len(vals) > 0,
            f"'{ph}' (categorical spec): 'values' must be a non-empty list.",
        )
        shapes = {_value_wire_shape(f"'{ph}' (categorical spec)", v) for v in vals}
        _require(
            len(shapes) == 1,
            f"'{ph}' (categorical spec): all values must share one shape - either all scalars "
            f"or all IN-lists, not a mix (one wire type is frozen per placeholder at code-gen).",
        )
        return CategoricalSpec(tuple(vals))
    raise ValueError(
        f"'{ph}': unknown parameter spec type {t!r}. Expected one of int/float/date/categorical."
    )


def _parse_date(ph: str, raw: dict, key: str) -> datetime.date:
    _require(key in raw, f"'{ph}' (date spec): missing required field '{key}'.")
    v = raw[key]
    _require(
        isinstance(v, str), f"'{ph}' (date spec): '{key}' must be an ISO date string."
    )
    try:
        return datetime.date.fromisoformat(v)
    except ValueError as e:
        raise ValueError(
            f"'{ph}' (date spec): '{key}'={v!r} is not a valid ISO date."
        ) from e


def _parse_group_spec(idx: int, raw: object):
    label = f"param_groups[{idx}]"
    _require(
        isinstance(raw, dict) and "type" in raw,
        f"{label}: a group spec must be an object with a 'type' field, got {raw!r}.",
    )
    phs = raw.get("placeholders")
    _require(
        isinstance(phs, list) and len(phs) > 0 and all(isinstance(p, str) for p in phs),
        f"{label}: 'placeholders' must be a non-empty list of placeholder names.",
    )
    phs_t = tuple(phs)
    t = raw["type"]
    if t == "tuples":
        rows = raw.get("values")
        _require(
            isinstance(rows, list) and len(rows) > 0,
            f"{label} (tuples): 'values' must be a non-empty list of rows.",
        )
        out_rows = []
        for r in rows:
            _require(
                isinstance(r, list) and len(r) == len(phs_t),
                f"{label} (tuples): each row must be a list of {len(phs_t)} value(s) "
                f"matching {phs_t}, got {r!r}.",
            )
            for cell in r:
                _require(
                    _is_simple_scalar(cell),
                    f"{label} (tuples): row value {cell!r} must be a simple scalar (string or "
                    f"number); each correlated column binds one scalar placeholder.",
                )
            out_rows.append(tuple(r))
        return TupleGroupSpec(phs_t, tuple(out_rows))
    if t == "sample":
        domain = raw.get("domain")
        _require(
            isinstance(domain, list) and len(domain) > 0,
            f"{label} (sample): 'domain' must be a non-empty list.",
        )
        for d in domain:
            _require(
                _is_simple_scalar(d),
                f"{label} (sample): domain value {d!r} must be a simple scalar (string or "
                f"number).",
            )
        distinct = raw.get("distinct", True)
        _require(
            isinstance(distinct, bool),
            f"{label} (sample): 'distinct' must be a boolean.",
        )
        if distinct:
            # "distinct" means distinct *values*: dedupe by value (random.sample draws by
            # position, so a domain like ["A","A","B"] could otherwise hand two placeholders
            # the same value). Order-preserving so sampling stays deterministic.
            domain = list(dict.fromkeys(domain))
            _require(
                len(domain) >= len(phs_t),
                f"{label} (sample): distinct draw of {len(phs_t)} needs a domain of at least "
                f"{len(phs_t)} distinct values, got {len(domain)}.",
            )
        return SampleGroupSpec(phs_t, tuple(domain), distinct)
    raise ValueError(
        f"{label}: unknown group spec type {t!r}. Expected 'tuples' or 'sample'."
    )


def _warn_quoted_literal_values(space: ParamSpace) -> None:
    """Warn once per placeholder whose declared values look like quoted SQL literals.

    Such values substitute into valid SQL, so the mistake is otherwise invisible until
    engine-side lookups silently miss. Metadata already flattens every spec form
    (categorical, tuples columns, sample domains) to a per-placeholder value list, so this
    covers them all; numeric specs carry no values and are skipped."""
    for ph, meta in space.metadata().items():
        example = next(
            (v for v in meta.get("values", ()) if is_quoted_literal(v)), None
        )
        if example is not None:
            logger.warning(
                "placeholder '%s': value %r looks like a quoted SQL literal; the single "
                "quotes will reach the engine inside the string value and lookups will "
                "miss. Put the quotes in the template ('[%s]') and supply bare values "
                "(see synnodb.workloads.query_params.hoist_literal_quotes).",
                ph,
                example,
                ph,
            )


def parse_param_space(
    raw_params: dict | None, raw_groups: list | None, template: str
) -> ParamSpace:
    """Validate a query's typed specs against its template and build a :class:`ParamSpace`.

    ``raw_params`` maps each scalar placeholder to a typed spec; ``raw_groups`` is a list of
    group specs. Every ``[PLACEHOLDER]`` in the template must be covered by exactly one scalar
    spec or one group column - no missing, extra, or doubly-covered placeholders. Raises
    ValueError (the typing/validation layer) on any violation.
    """
    phs = find_placeholders(template)
    raw_params = raw_params or {}
    raw_groups = raw_groups or []

    if not phs:
        if raw_params or raw_groups:
            raise ValueError(
                f"Query has no placeholders but params/param_groups were supplied "
                f"({sorted(raw_params)}, {len(raw_groups)} group(s))."
            )
        return ParamSpace((), {}, ())

    _require(
        isinstance(raw_params, dict),
        "params must be an object mapping placeholder -> spec.",
    )
    _require(
        isinstance(raw_groups, list), "param_groups must be a list of group specs."
    )

    scalars = {ph: _parse_scalar_spec(ph, raw) for ph, raw in raw_params.items()}
    groups = [_parse_group_spec(i, raw) for i, raw in enumerate(raw_groups)]

    covered: list[str] = list(scalars)
    for g in groups:
        covered.extend(g.placeholders)
    dupes = sorted({p for p in covered if covered.count(p) > 1})
    if dupes:
        raise ValueError(
            f"placeholder(s) {dupes} are covered by more than one spec; each placeholder must "
            f"be bound exactly once."
        )

    covset = set(covered)
    missing = sorted(set(phs) - covset)
    extra = sorted(covset - set(phs))
    if missing or extra:
        raise ValueError(
            f"params placeholder mismatch: missing={missing} extra={extra} (expected {phs})."
        )

    ordered_scalars = {ph: scalars[ph] for ph in phs if ph in scalars}
    space = ParamSpace(tuple(phs), ordered_scalars, tuple(groups))
    _warn_quoted_literal_values(space)
    return space
