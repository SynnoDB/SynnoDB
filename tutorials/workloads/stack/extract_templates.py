#!/usr/bin/env python3
"""Extract a parameterized SQL template for each query class in so_queries/.

Each subdirectory ``q1`` .. ``q16`` holds many concrete SQL files that share the
same join skeleton but differ in the values used inside their filter predicates
(string literals, numbers, ``IN (...)`` lists and, in a few classes, the filtered
column or the comparison operator itself).

For every class this script:

  1. Tokenizes each query so that the join skeleton lines up position for position
     across all queries in the class (an ``IN (...)`` list is kept as a single
     value token, and the ``IN`` keyword is separated from it so that it aligns
     with the ``ILIKE '<pattern>'`` variant used by some queries).
  2. Finds the token positions whose value changes between queries. Those become
     the template's parameters; everything constant becomes fixed template text.
  3. Emits, per class, the template string with ``[name]`` placeholders, the
     ordered list of parameter names, and - for every concrete query - the
     dictionary mapping each placeholder name to the value it takes.

The raw ``so_queries/`` log (6191 tiny ``.sql`` files) is not checked in; it is
downloaded + cached on first use from :data:`SO_QUERIES_URL` (see
:func:`ensure_so_queries`). :func:`build_templates` returns the per-class
extraction as an in-memory dict; running this file as a script also writes it to
``stack_templates.json`` for inspection.
"""

import io
import json
import re
import shutil
import tarfile
import tempfile
import urllib.request
from collections import Counter
from pathlib import Path

import zstandard

ROOT = Path(__file__).resolve().parent
QUERY_DIR = ROOT / "so_queries"
OUTPUT = ROOT / "stack_templates.json"

# The raw workload log, published as a ~540 KB ``.tar.zst`` whose sole top-level entry is a
# ``so_queries/`` directory of ``q1`` .. ``q16`` subfolders. Not checked in - fetched on demand.
SO_QUERIES_URL = "https://rmarcus.info/so_queries.tar.zst"

# A quoted string, an IN (...) list, a number, an identifier/keyword, a
# comparison operator, or a single structural punctuation character.
TOKEN_RE = re.compile(
    r"""
      '(?:[^']|'')*'                 # single-quoted string literal
    | \b[Ii][Nn]\s*\([^)]*\)         # IN (...) list  (split into keyword + list below)
    | \d+\.\d+ | \d+                 # numeric literal
    | [A-Za-z_][A-Za-z_0-9]*         # identifier / keyword
    | >=|<=|<>|!=|=|>|<              # comparison operator
    | [(),.;*]                       # structural punctuation
    """,
    re.VERBOSE,
)

OPERATORS = {">=", "<=", "<>", "!=", "=", ">", "<"}
OPERATOR_WORDS = {"in", "ilike", "like", "not"}


def mask_comments(sql: str) -> str:
    """Blank out ``-- ...`` comments while preserving character offsets."""
    return re.sub(r"--[^\n]*", lambda m: " " * len(m.group()), sql)


def tokenize(sql: str):
    """Return ``(text, start, end)`` tuples for every token in ``sql``.

    ``IN (...)`` is split into the ``IN`` keyword and the parenthesized list so
    that it aligns with the ``ILIKE '<pattern>'`` form (keyword + value).
    """
    masked = mask_comments(sql)
    tokens = []
    for m in TOKEN_RE.finditer(masked):
        text = m.group()
        if re.match(r"^[Ii][Nn]\s*\(", text):
            paren = text.index("(")
            tokens.append(("in", m.start(), m.start() + 2))
            tokens.append((text[paren:], m.start() + paren, m.end()))
        else:
            tokens.append((text, m.start(), m.end()))
    return tokens


def classify(tokens, pos: int) -> str:
    """Classify a varying token position as an operator, column-name, or value."""
    texts = [t[0] for t in tokens]
    tok = texts[pos]
    if tok in OPERATORS or tok.lower() in OPERATOR_WORDS:
        return "operator"
    if re.match(r"^[A-Za-z_]", tok) and pos >= 1 and texts[pos - 1] == ".":
        return "column"
    return "value"


def parameter_name(tokens, pos: int, used: Counter) -> str:
    """Derive a stable, unique placeholder name for a varying token position."""
    texts = [t[0] for t in tokens]
    kind = classify(tokens, pos)

    if kind == "operator":
        base = "op"
    elif kind == "column":
        # The filtered column itself varies (e.g. score vs view_count).
        alias = texts[pos - 2] if pos >= 2 else "t"
        base = f"{alias}_column"
    else:
        # A literal value: name it after the column it is compared against.
        base = "value"
        j = pos - 1
        while j >= 1:
            if re.match(r"^[A-Za-z_]", texts[j]) and texts[j - 1] == ".":
                base = texts[j]
                break
            j -= 1

    base = base.upper()
    used[base] += 1
    return base if used[base] == 1 else f"{base}_{used[base]}"


def build_class(files):
    """Build the template and per-query parameter dicts for one query class."""
    tokenized = {f: tokenize(f.read_text()) for f in files}

    lengths = Counter(len(t) for t in tokenized.values())
    if len(lengths) != 1:
        raise ValueError(f"non-uniform token lengths: {dict(lengths)}")
    n_tokens = next(iter(lengths))

    # A position is a parameter iff its token text is not identical everywhere.
    varying = [
        pos for pos in range(n_tokens) if len({tokenized[f][pos][0] for f in files}) > 1
    ]

    representative = min(files)  # deterministic; formatting is identical class-wide
    rep_tokens = tokenized[representative]
    rep_text = representative.read_text()

    # Assign a stable name to each varying position (based on the representative).
    used = Counter()
    names = {pos: parameter_name(rep_tokens, pos, used) for pos in varying}

    # Split parameters by kind so column-name and operator parameters are
    # recorded separately from ordinary value parameters.
    kinds = {pos: classify(rep_tokens, pos) for pos in varying}
    value_parameters = [names[pos] for pos in varying if kinds[pos] == "value"]
    column_name_parameters = [names[pos] for pos in varying if kinds[pos] == "column"]
    operator_parameters = [names[pos] for pos in varying if kinds[pos] == "operator"]

    # Build the template by replacing the representative's varying spans, right
    # to left so earlier character offsets stay valid.
    template = rep_text
    for pos in sorted(varying, reverse=True):
        _, start, end = rep_tokens[pos]
        template = template[:start] + f"[{names[pos]}]" + template[end:]

    queries = []
    for f in sorted(files):
        toks = tokenized[f]
        by_kind = {"value": {}, "column": {}, "operator": {}}
        for pos in varying:
            by_kind[kinds[pos]][names[pos]] = toks[pos][0]
        queries.append(
            {
                "file": f"{f.parent.name}/{f.name}",
                "parameters": by_kind["value"],
                "column_name_parameters": by_kind["column"],
                "operator_parameters": by_kind["operator"],
            }
        )

    return {
        "template": template,
        "parameters": value_parameters,
        "column_name_parameters": column_name_parameters,
        "operator_parameters": operator_parameters,
        "num_queries": len(files),
        "queries": queries,
    }


def substitute(template: str, params: dict) -> str:
    """Re-instantiate a query from a template and its parameter dict."""
    out = template
    for name, value in params.items():
        out = out.replace(f"[{name}]", value, 1)
    return out


def ensure_so_queries(dest: Path = QUERY_DIR, url: str = SO_QUERIES_URL) -> Path:
    """Return the raw ``so_queries/`` SQL log, downloading + caching it on first use.

    The workload (6191 ``.sql`` files, ~540 KB compressed) is not checked in, so this fetches the
    ``.tar.zst`` from ``url`` once and caches it at ``dest``; later calls reuse the cached copy.
    The archive's own top-level ``so_queries/`` directory becomes ``dest``. Extraction is pure
    Python (``zstandard`` + ``tarfile``), so no system ``tar``/``zstd`` is required.
    """
    if dest.is_dir() and any(dest.glob("q*/*.sql")):
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=dest.parent) as tmp:
        staging = Path(tmp)
        with urllib.request.urlopen(url, timeout=120) as resp:
            compressed = resp.read()
        reader = zstandard.ZstdDecompressor().stream_reader(io.BytesIO(compressed))
        with tarfile.open(fileobj=reader, mode="r|") as tf:
            tf.extractall(
                staging, filter="data"
            )  # filter="data" blocks path-traversal entries
        extracted = staging / "so_queries"
        if not extracted.is_dir():
            raise RuntimeError(
                f"{url} did not contain a top-level 'so_queries/' directory"
            )
        if dest.exists():
            shutil.rmtree(dest)
        extracted.replace(dest)  # atomic move within dest.parent's filesystem
    return dest


def build_templates(query_dir: Path = QUERY_DIR, *, download: bool = True) -> dict:
    """Build the per-class template extraction (the ``stack_templates.json`` mapping) in memory.

    With ``download=True`` (default) the raw ``so_queries/`` log is fetched + cached if absent.
    Deterministic: files are processed in sorted order and each class's template is taken from its
    lexicographically smallest query, so the result is identical every run.
    """
    if download:
        ensure_so_queries(query_dir)
    result: dict = {}
    for c in range(1, 17):
        cls = f"q{c}"
        files = sorted((query_dir / cls).glob("*.sql"))
        if files:
            result[cls] = build_class(files)
    return result


def main():
    result = build_templates()

    OUTPUT.write_text(json.dumps(result, indent=2))

    # Validation: every query must round-trip from its template + parameters
    # (comparing on whitespace-normalized text, since only values differ).
    def norm(s):
        return re.sub(r"\s+", " ", mask_comments(s)).strip()

    total = mismatches = 0
    for cls, data in result.items():
        for q in data["queries"]:
            total += 1
            original = (QUERY_DIR / q["file"]).read_text()
            all_params = {
                **q["parameters"],
                **q["column_name_parameters"],
                **q["operator_parameters"],
            }
            rebuilt = substitute(data["template"], all_params)
            if norm(original) != norm(rebuilt):
                mismatches += 1

    print(f"Wrote {OUTPUT} ({len(result)} classes, {total} queries).")
    print(f"Round-trip check: {total - mismatches}/{total} queries reproduced exactly.")
    print()
    print(
        f"{'class':>6}  {'#lit':>4}  {'#col':>4}  {'#op':>3}  "
        f"column_name_parameters / operator_parameters"
    )
    for cls, data in result.items():
        extras = data["column_name_parameters"] + data["operator_parameters"]
        print(
            f"{cls:>6}  {len(data['parameters']):>4}  "
            f"{len(data['column_name_parameters']):>4}  "
            f"{len(data['operator_parameters']):>3}  "
            f"{', '.join(extras) if extras else '-'}"
        )


if __name__ == "__main__":
    main()
