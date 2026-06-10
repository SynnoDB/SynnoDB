import logging
import os
import pickle
import random
import re
from collections import defaultdict
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Regex patterns for template extraction
NUMERIC_RE = re.compile(r"\b\d+(?:\.\d+)?\b")
IN_LIST_RE = re.compile(r"IN\s*\((('[^']*',?|[^,')]*,?)+)\)", re.IGNORECASE)
ILIKE_PATTERN_RE = re.compile(r"ILIKE\s+'([^']*)'", re.IGNORECASE)
COLUMN_RE = re.compile(
    r"([a-zA-Z_][a-zA-Z0-9_]*(?:\.[a-zA-Z_][a-zA-Z0-9_]*)*(?:::[a-zA-Z_][a-zA-Z0-9_]*)?)\s*(<=|>=|<|>|=|IN|ILIKE)",
    re.IGNORECASE,
)
LITERAL_OP_COLUMN_RE = re.compile(
    r"\b\d+(?:\.\d+)?\b\s*(<=|>=|<|>|=)\s+([a-zA-Z_][a-zA-Z0-9_]*(?:\.[a-zA-Z_][a-zA-Z0-9_]*)*(?:::[a-zA-Z_][a-zA-Z0-9_]*)?)",
    re.IGNORECASE,
)
QUOTED_NUMERIC_RE = re.compile(r"'(\d+(?:\.\d+)?)'")

# Column name to parameter name mapping
COLUMN_TO_PARAM = {
    "production_year": "YEAR",
    "keyword": "KEYWORD",
    "kind_id": "KIND",
    "title": "TITLE",
    "season_nr": "SEASON",
    "episode_nr": "EPISODE",
    "series_years": "SERIES_YEARS",
    "name": "NAME",
    "imdb_index": "IMDB_INDEX",
    "imdb_id": "IMDB_ID",
    "gender": "GENDER",
    "name_pcode_cf": "NAME_PCODE_CF",
    "name_pcode_nf": "NAME_PCODE_NF",
    "surname_pcode": "SURNAME_PCODE",
    "country_code": "COUNTRY",
    "kind": "KIND",
    "role": "ROLE",
    "info": "INFO",
    "note": "NOTE",
    "id": "ID",
}


def _camel_placeholder(column: str, counter: int | None = None) -> str:
    """
    Convert column name to TPC-H-style placeholder using mapping dictionary.
    Example:
      t.production_year -> YEAR
      k.keyword         -> KEYWORD
      mii2.info::float  -> INFO
    """
    base = column.split(".")[-1]
    # Strip type casting annotations (e.g., ::float, ::int)
    base = base.split("::")[0]
    if base in COLUMN_TO_PARAM:
        param_name = COLUMN_TO_PARAM[base]
    else:
        logging.warning(f"No mapping for column '{base}', using uppercased name.")
        param_name = base.upper()
    return f"{param_name}{counter}" if counter is not None else param_name


def _extract_literal(line: str) -> Any:
    """Extract literal value(s) from a filter line.

    Returns tuple of (value, is_in_clause, is_ilike) where:
    - value: The extracted literal(s)
    - is_in_clause: True if extracted from IN clause
    - is_ilike: True if extracted from ILIKE clause
    """
    in_match = IN_LIST_RE.search(line)
    if in_match:
        raw = in_match.group(1)
        return (tuple(v.strip().strip("'") for v in raw.split(",")), True, False)

    # Check for ILIKE patterns
    ilike_match = ILIKE_PATTERN_RE.search(line)
    if ilike_match:
        pattern = ilike_match.group(1)
        return ((pattern,), False, True)

    # Ignore numeric tokens inside non-numeric quoted strings (e.g. regex patterns)
    quoted_parts = re.findall(r"'([^']*)'", line)
    quoted_nums = [q for q in quoted_parts if re.fullmatch(r"\d+(?:\.\d+)?", q)]
    line_no_quotes = re.sub(r"'[^']*'", "''", line)

    nums = NUMERIC_RE.findall(line_no_quotes)
    if quoted_nums or nums:
        values = []
        for q in quoted_nums:
            values.append(float(q) if "." in q else int(q))
        for n in nums:
            values.append(float(n) if "." in n else int(n))
        return (tuple(values), False, False)

    return None


def _normalize_line(line: str, placeholder: str) -> str:
    """Replace literal with placeholder."""
    line = IN_LIST_RE.sub(f"IN {placeholder}", line)
    line = ILIKE_PATTERN_RE.sub(f"ILIKE {placeholder}", line)
    # Replace quoted numeric literals with placeholder (without quotes)
    line = QUOTED_NUMERIC_RE.sub(placeholder, line)

    # Replace numeric literals outside of quoted strings only
    parts = re.split(r"('(?:[^']*)')", line)
    for i, part in enumerate(parts):
        if part.startswith("'") and part.endswith("'"):
            continue
        parts[i] = NUMERIC_RE.sub(placeholder, part)
    line = "".join(parts)
    return line


def _move_is_null_to_in_clause(
    query: List[str], target_rows_cols: List[Tuple[int, str]]
) -> List[str] | None:
    for row, col in target_rows_cols:
        if len(query) <= row:
            return None  # row index out of range, cannot rewrite
        assert row < len(query), (
            f"Row index {row} out of range for query with {len(query)} lines\nQuery:\n{query}"
        )
        target_line = query[row]

        assert col in target_line, (
            f"Expected column '{col}' in line {row}: {target_line}\n{query}"
        )

        if (
            f"{col} in" in target_line or f"{col} IN" in target_line
        ) and f"{col} IS NULL" in target_line:
            query[row] = _rewrite_is_null_to_in_clause(target_line, col)
        elif f"AND ({col} IS NULL)" == target_line.strip():
            query[row] = f"AND ({col} in (NULL))"
        elif (
            f"{col} in" in target_line or f"{col} IN" in target_line
        ) and "IS NULL" not in target_line:
            # no rewrite needed, already in correct format
            pass
        else:
            raise ValueError(f"Unexpected formatting in query: {target_line} / {col}")

        # print(f"{target_line}--> {query[row]}")  # log the rewrite

    return query


def _rewrite_is_null_to_in_clause(line: str, column: str) -> str:
    # extract in clause
    assert " OR " in line, f"Expected OR clause in line: {line}"
    assert line.startswith("AND ("), f"Expected line to start with 'AND (': {line}"
    assert line.endswith(")"), f"Expected line to end with ')': {line}"

    # remove AND ( and trailing )
    content = line[len("AND (") : -1]

    # split in clause and IS NULL part
    assert " OR " in content, f"Expected OR clause in line content: {content}"
    in_part, is_null_part = content.split(" OR ", 1)
    assert f"{column} IS NULL" in is_null_part, (
        f"Expected IS NULL part for column '{column}' in line: {line}"
    )

    # verify in_part contains the expected column
    assert f"{column} in" in in_part or f"{column} IN" in in_part, (
        f"Expected IN clause for column '{column}' in line: {line}"
    )

    # add NULL to in clause
    assert in_part.endswith(")"), f"Expected IN clause to end with ')': {in_part}"
    in_part = in_part[:-1] + ",NULL)"

    return f"AND ({in_part})"


def _extract_template(
    queries: List[str],
    query_name: str,
) -> Tuple[str, List[str], List[Dict[str, Any]]]:
    """
    Extract templates and bindings from a list of SQL queries.
    Uses the logic from test.ipynb to extract parameterized templates.
    """
    split_queries = [q.strip().splitlines() for q in queries]

    # Filter out queries with literal-op-column formatting when possible
    filtered_queries = [
        q
        for q in split_queries
        if not any(LITERAL_OP_COLUMN_RE.search(line) for line in q)
    ]
    if filtered_queries:
        split_queries = filtered_queries
    else:
        logging.warning(
            f"All queries of {query_name} contain literal-op-column formatting. Template may be less stable."
        )

    # filter out edge cases
    tmp_queries = []
    for q in split_queries:
        if query_name == "8a":
            # check that filter in line 29 is on n.name_pcode_cf and not some other column - different templates are mixed up!
            if "n.name_pcode_cf" not in q[29]:
                continue
        elif query_name == "3b":
            # check that filter in line 24 is on n.name_pcode_nf and not name_pcode_cf - different templates are mixed up!
            if "n.name_pcode_nf" not in q[24]:
                continue

        tmp_queries.append(q)

    if len(tmp_queries) < len(split_queries):
        logging.warning(
            f"Only {len(tmp_queries)}/{len(split_queries)} queries passed edge case filtering for {query_name}. Other queries have diverging filter-columns! This is violating template definition.\nWTF dear dataset creators??"
        )

    split_queries = tmp_queries

    # rewrite columns where IN and IS NULL are mixed to have NULL in the IN clause instead, to unify formatting across queries. This affects:
    # - (n.gender IS NULL)
    # - (n.gender in ('f','m'))
    # - (n.gender in ('m') OR n.gender IS NULL)

    # rewrite this to:
    # - (n.gender in ('m',NULL))
    query_col_null_in_dict = {
        "1a": [(25, "n.gender")],
        "2a": [(29, "n.gender")],
        "2b": [(29, "n.gender")],
        "2c": [(25, "n.gender")],
        "4a": [(17, "n.gender"), (19, "ci.note")],
        "6a": [(45, "n.gender"), (46, "n.name_pcode_nf"), (47, "ci.note")],
        "7a": [(45, "n.gender"), (46, "n.name_pcode_nf"), (47, "ci.note")],
        "8a": [(28, "n.gender"), (29, "n.name_pcode_cf")],
    }

    if query_name in query_col_null_in_dict:
        rewritten_queries = []
        for q in split_queries:
            copied_q = q.copy()

            out = _move_is_null_to_in_clause(
                copied_q, query_col_null_in_dict[query_name]
            )
            if out is None:
                logging.warning(
                    f"Failed to rewrite IS NULL to IN clause for query {query_name}\n{copied_q}"
                )
            else:
                rewritten_queries.append(out)

        if len(rewritten_queries) < len(split_queries):
            logging.warning(
                f"Only {len(rewritten_queries)}/{len(split_queries)} queries were successfully rewritten for {query_name}. Some queries may have unexpected formatting."
            )
    else:
        rewritten_queries = split_queries

    # Prefer a base query without literal-op-column formatting (keeps stable template shape)
    base_query_idx = 0
    for idx, q in enumerate(rewritten_queries):
        if not any(LITERAL_OP_COLUMN_RE.search(line) for line in q):
            base_query_idx = idx
            break

    assert rewritten_queries, "No valid queries to extract template from"

    num_lines = len(rewritten_queries[0])

    # Collect literals per line
    line_literals = defaultdict(list)
    line_columns = {}
    line_is_in_clause = {}
    line_is_ilike = {}

    for i in range(num_lines):
        for q in rewritten_queries:
            lit_result = _extract_literal(q[i])
            if lit_result is not None:
                lit_value, is_in_clause, is_ilike = lit_result
                line_literals[i].append(lit_value)
                # Record if this is an IN clause or ILIKE (use the first one found)
                if i not in line_is_in_clause:
                    line_is_in_clause[i] = is_in_clause
                if i not in line_is_ilike:
                    line_is_ilike[i] = is_ilike

        # Try to extract column name from any query
        if i in line_literals:
            for q in rewritten_queries:
                m = COLUMN_RE.search(q[i])
                if m:
                    line_columns[i] = m.group(1)
                    break
                m = LITERAL_OP_COLUMN_RE.search(q[i])
                if m:
                    line_columns[i] = m.group(2)
                    break

    template_lines = []
    bindings = defaultdict(dict)
    placeholder_counters = defaultdict(int)

    # First pass: count how many times each parameter appears
    param_counts = defaultdict(int)
    for i in range(num_lines):
        if i in line_literals:
            col = line_columns.get(i, "UNKNOWN")
            base_param = _camel_placeholder(col, None)
            param_counts[base_param] += 1

    for i in range(num_lines):
        if i in line_literals:
            values = line_literals[i]
            col = line_columns.get(i, "UNKNOWN")
            # Get the base parameter name first
            base_param = _camel_placeholder(col, None)

            should_parameterize = len(set(values)) != 1 or param_counts[base_param] > 1
            if not should_parameterize:
                # Literal does NOT vary and appears once → use as-is
                template_lines.append(rewritten_queries[base_query_idx][i].strip())
                continue

            # Track counter based on parameter name, not column name
            placeholder_counters[base_param] += 1
            # Add suffix if this parameter appears more than once
            suffix = (
                placeholder_counters[base_param]
                if param_counts[base_param] > 1
                else None
            )
            placeholder = _camel_placeholder(col, suffix)

            normalized = _normalize_line(
                rewritten_queries[base_query_idx][i].strip(), placeholder
            )
            template_lines.append(normalized)

            # Format bindings based on whether it's an IN clause or ILIKE
            is_in = line_is_in_clause.get(i, False)
            is_ilike = line_is_ilike.get(i, False)
            for qid, lit_tuple in enumerate(values):
                if is_ilike:
                    # Format ILIKE pattern as quoted string: '%pattern%'
                    bindings[qid][placeholder] = f"'{lit_tuple[0]}'"
                elif is_in or len(lit_tuple) > 1:
                    # For IN clauses, store as list and serialize with tuple syntax
                    # Format NULL values as '<<NULL>>' string
                    items = []
                    for v in lit_tuple:
                        if v is None:
                            items.append(
                                "<<NULL>>"
                            )  # use special string to represent NULL in IN lists, will be serialized over strings and parsed in C++ args parser
                        elif isinstance(v, str):
                            items.append(v)
                        else:
                            raise ValueError(
                                f"Unexpected non-string, non-NULL value in IN clause: {v} (type {type(v)})"
                            )
                    # Serialize as tuple syntax for C++ parsing
                    formatted_items = [f"'{v}'" for v in items]
                    formatted = "(" + ", ".join(formatted_items) + ")"
                    bindings[qid][placeholder] = formatted
                else:
                    # Single value: convert to string
                    bindings[qid][placeholder] = str(lit_tuple[0])
        else:
            # No literals on this line → include as-is
            template_lines.append(rewritten_queries[base_query_idx][i].strip())

    # build sql
    queries = ["\n".join(q) for q in rewritten_queries]

    template_str = "\n".join(template_lines)
    bindings_list = [bindings[i] for i in range(len(bindings))]

    # make sure every entry has same amount of parameters (some queries may be missing some parameters if they don't have a literal on that line)
    all_placeholders = set()
    for b in bindings_list:
        all_placeholders.update(b.keys())

    diff_cols = []
    for b in bindings_list:
        if set(b.keys()) != all_placeholders:
            diff_cols.append(all_placeholders - set(b.keys()))

            logging.error(
                f"Query {query_name} has inconsistent placeholders across bindings:"
            )
            break

    # print for diverging placeholders all values
    for c in diff_cols:
        # serach def lines of diff_colls
        line_nrs = [
            line
            for line, col in line_columns.items()
            if _camel_placeholder(col, None) in c
        ]

        assert len(line_nrs) == 1, (
            f"Expected exactly one line for column with inconsistent placeholders, got {len(line_nrs)} for {c}"
        )

        logging.warning(f"Missing placeholders: {c} (line: {line_nrs[0]})")
        for q, b in zip(queries, bindings_list):
            for p in c:
                if p not in b:
                    found_line = q.split("\n")[line_nrs[0]]
                    logging.warning(f"  Missing {p}. Found: {found_line}")
                else:
                    logging.warning(f"  {p} = {b[p]}")

    assert len(diff_cols) == 0, f"Some queries have missing placeholders: {diff_cols}"

    return (template_str, queries, bindings_list)


@lru_cache(maxsize=None)
def _load_query_templates(
    data_dir: str, query_name: str
) -> Tuple[str, List[str], List[Dict[str, Any]]]:
    """
    Load all query templates from a directory of pickle files.
    Returns tuple of (template_string, bindings_list).
    """

    if not os.path.exists(data_dir):
        raise FileNotFoundError(f"Data directory not found: {data_dir}")

    # Load all pickle files and group by query
    sql_list = []

    for filename in sorted(os.listdir(data_dir)):
        if not filename.endswith(".pkl"):
            continue

        filepath = os.path.join(data_dir, filename)
        try:
            with open(filepath, "rb") as f:
                data = pickle.load(f)

            if "sql" in data:
                sql_list.append(data["sql"])
        except (pickle.UnpicklingError, KeyError, IOError):
            continue

    return _extract_template(sql_list, query_name=query_name)


def gen_query_single_only(**kwargs) -> Tuple[str, str, Dict[str, Any]]:
    template, sql_queries, bindings_list = gen_query(**kwargs)
    assert len(sql_queries) == 1, f"Expected single query, got {len(sql_queries)}"
    return template, sql_queries[0], bindings_list[0]


def gen_query(
    ceb_dir: Path,
    query_name: str = "1a",
    rnd: Optional[random.Random] = None,
    seed: int = 42,
    num_queries: int = 1,
) -> Tuple[str, List[str], List[Dict[str, Any]]]:
    """
    Generate parameterized CEB queries with random bindings.

    Args:
        ceb_dir: Path to the CEB data directory
        query_name: The query name/ID (e.g., "1a", "2b", etc.)
        rnd: Random number generator instance
        seed: Seed for random number generation
        num_queries: Number of query variants to generate (default: 1)

    Returns:
        Tuple of (sql_queries_list, template_string, bindings_list)
        - sql_queries_list: List of concrete SQL queries with substituted values
        - template_string: The parameterized template with placeholders
        - bindings_list: List of binding dictionaries, one per generated query
    """

    if rnd is None:
        rnd = random.Random(seed)

    assert ceb_dir.exists(), f"CEB directory does not exist: {ceb_dir}"

    # navigate to query dir
    if query_name.lower().startswith("q"):
        cleaned_query_name = query_name[1:]
    else:
        cleaned_query_name = query_name

    query_dir = ceb_dir / f"{cleaned_query_name.lower()}"
    assert query_dir.exists(), f"CEB query directory does not exist: {query_dir}"

    # Load templates
    template_str, sql_list, all_bindings = _load_query_templates(
        query_dir.as_posix(), query_name=cleaned_query_name
    )

    # Generate multiple query variants with random bindings
    sql_queries = []
    bindings_list = []

    # Randomly select bindings
    selected_indices = list(range(len(all_bindings)))
    rnd.shuffle(selected_indices)
    selected_indices = selected_indices[:num_queries]

    for idx in selected_indices:
        selected_bindings = all_bindings[idx]
        selected_sql = sql_list[idx]
        assert isinstance(selected_sql, str), (
            f"Expected SQL string, got {type(selected_sql)}"
        )

        sql_queries.append(selected_sql)
        bindings_list.append(selected_bindings)

    return template_str, sql_queries, bindings_list


if __name__ == "__main__":
    # Example usage
    CEB_DIR = Path("/mnt/labstore/bespoke_olap/datasets/ceb/imdb")
    template, sqls, bindings = gen_query(CEB_DIR, query_name="Q1a", num_queries=100)
