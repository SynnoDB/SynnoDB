# ============================================================================
# Data Models
# ============================================================================


import re
from dataclasses import dataclass, field
from typing import Callable, Dict, List, NamedTuple, Set

import numpy as np
import pandas as pd


@dataclass
class ErrorSpan:
    """Represents a span where validation had errors."""

    start: int
    end: int


@dataclass
class WorkedOnSpan:
    """Represents a span where LLM worked on specific queries."""

    start: int
    end: int
    queries: str
    section: str = ""
    query_set: Set[str] = field(default_factory=set)


@dataclass
class QueryImplementationTracker:
    """Tracks which queries have been implemented at each turn."""

    turn: pd.Series
    num_implemented: pd.Series
    query_dict: Dict[str, pd.Series] = field(default_factory=dict)


# ============================================================================
# Data Cleaning Pipeline
# ============================================================================


# Rules identifying the start of each worked-on section via simple substring
# checks on the 'current_prompt' column. Rules are mutually exclusive: a given
# prompt row will match at most one predicate. The section_label applies from
# the first matching row onward; all rows before the first rule fires are
# labeled "storage".
class SectionRule(NamedTuple):
    label: str
    predicate: Callable[[str], bool]
    display_label: str = ""
    query_level: bool = False  # whether this section should be further split by query in extract_worked_on_queries


SECTION_RULES: List[SectionRule] = [
    SectionRule(
        "storage",
        lambda s: "produce a creative in-memory storage-layout" in s,
        "1&2:\nStorage",
        query_level=False,
    ),
    SectionRule(
        "implement queries",
        lambda s: "Start with quer" in s,
        "3: Basic\nQuery Impl.",
        query_level=True,
    ),
    SectionRule(
        "pin & trace",
        lambda s: "pin the query-execution" in s,
        "4&5:\nPin &\nTracing",
        query_level=True,
    ),
    # SectionRule("optimization", lambda s: "Optimize the implementation of" in s),
    SectionRule(
        "optim card",
        lambda s: "sample execution plan" in s,
        "4.1: Optim\nw/ Data",
        query_level=True,
    ),
    SectionRule(
        "optim trace",
        lambda s: "using the tracing/profiling data collected with sf" in s,
        "4.2: Optim w/\nSelf-Tracing",
        query_level=True,
    ),
    SectionRule(
        "optim expert",
        lambda s: " using the expert knowledge" in s,
        "4.3: Optim w/\nExp. Knowledge",
        query_level=True,
    ),
    SectionRule(
        "optim human",
        lambda s: "Jasny" in s,
        "4.4: Optim w/\nHuman Reference",
        query_level=True,
    ),
    SectionRule(
        "add mt",
        lambda s: "Parallelize the implementation" in s,
        "5: Add\nMulti-Threading",
        query_level=True,
    ),
]


def get_section_from_title(s: str) -> SectionRule:
    """Helper to determine section label for a given prompt string."""
    for rule in SECTION_RULES:
        if rule.label == s or rule.display_label == s:
            return rule
    raise ValueError(f"No section rule matched for string: {s}")


class DataCleaner:
    """Handles data extraction and transformation from W&B history."""

    @staticmethod
    def extract_error_spans(history: pd.DataFrame) -> List[ErrorSpan]:
        """
        Extract error spans from validation history.
        Spans from first False to following True in 'validation/correct' column.
        """
        if "validation/correct" not in history.columns:
            return []

        validate_history = history[["validation/correct"]]
        error_spans = []
        step = None

        for idx, c in enumerate(validate_history.values):
            if pd.isna(c):
                continue
            elif not c and step is None:
                step = idx
            elif not c:
                continue
            elif c and step is not None:
                error_spans.append(ErrorSpan(start=step, end=idx))
                step = None

        return error_spans

    @staticmethod
    def extract_queries_implemented(
        history: pd.DataFrame,
    ) -> QueryImplementationTracker:
        """
        Extract number of queries implemented at each turn.
        Uses cumulative max to track implementation progress.
        """
        # Count queries from validation/query_ids_executed
        if "validation/query_ids_executed" in history.columns:
            num_implemented = history["validation/query_ids_executed"].apply(
                lambda x: len(x) if isinstance(x, list) else 0
            )
            num_implemented = num_implemented.cummax()
        else:
            num_implemented = pd.Series(0, index=history.index)

        # Extract per-query implementation status
        query_dict = {}
        regex = r"validation/query_([a-zA-Z0-9]+)/speedup"

        for col in history.columns:
            match = re.match(regex, col)
            if match:
                query_id = match.group(1)
                implemented = history[col].notnull().cummax()
                query_dict[query_id] = implemented

        return QueryImplementationTracker(
            turn=history.get(
                "turn", pd.Series(range(len(history)), index=history.index)
            ),
            num_implemented=num_implemented,
            query_dict=query_dict,
        )

    @classmethod
    def extract_worked_on_queries(
        cls, history: pd.DataFrame, drill_down_to_query_level: bool = True
    ) -> pd.Series:
        """
        Extract which queries the LLM worked on in each turn.

        First assigns a section label to every row using SECTION_RULES.  Rows
        before the earliest matching section are labelled "storage".

        When *drill_down_to_query_level* is True and the section is one of
        ``_QUERY_LEVEL_SECTIONS``, the label is refined to
        ``"<section> <Q-string>"`` (e.g. ``"implement queries Q3"``).
        Otherwise the bare section label is returned.

        Args:
            drill_down_to_query_level: If True, append per-query info for
                "implement queries" and "optimization" sections.
        """
        if "validation/query_ids_executed" not in history.columns:
            return pd.Series("storage", index=history.index)

        # --- Build per-row query-change sets ---
        worked_on_queries: List = []
        last_queries: Set[str] = set()

        # extract the "query_id_not_recognized" column if it exists, which indicates cases where the llm named a query that does not exist.
        not_recognized_col = "validation/query_id_not_recognized"
        not_recognized = (
            history[not_recognized_col]
            if not_recognized_col in history.columns
            else None
        )

        for i, queries in enumerate(history["validation/query_ids_executed"]):
            if not_recognized is not None:
                val = not_recognized.iloc[i]

                # check if the query-id was not recognized and hence we cannot extract what was worked on from this element
                if isinstance(val, str) and val:
                    worked_on_queries.append(None)
                    continue

            if queries is None or (isinstance(queries, float) and pd.isna(queries)):
                worked_on_queries.append(None)
            else:
                if (
                    isinstance(queries, str)
                    and queries.startswith("[")
                    and queries.endswith("]")
                ):
                    try:
                        queries = eval(queries)
                    except Exception as e:
                        raise ValueError(
                            f"Failed to parse query list from string: {queries}"
                        ) from e

                assert isinstance(queries, list) or isinstance(queries, np.ndarray), (
                    f"Expected list or numpy array, got {type(queries)}: {queries}"
                )

                # rewrite query strings: remove leading q
                queries = [
                    q[1:] if isinstance(q, str) and q.startswith("q") else q
                    for q in queries
                ]

                query_set = set(queries)
                if not query_set.issuperset(last_queries):
                    worked_on_queries.append(query_set)
                elif query_set == last_queries:
                    worked_on_queries.append(query_set)
                else:
                    # New queries have been added since last turn, so we consider the LLM to have "worked on" these new queries in the current turn.
                    worked_on_queries.append(query_set - last_queries)
                last_queries = query_set

        query_series = pd.Series(worked_on_queries, index=history.index)
        query_series = query_series.bfill().ffill()

        # --- Determine section boundaries from current_prompt ---
        section_starts: Dict[str, int] = {}
        col = "current_prompt"
        if col in history.columns:
            for rule in SECTION_RULES:
                mask = history[col].apply(
                    lambda s: rule.predicate(s) if isinstance(s, str) else False
                )
                if mask.any():
                    idx = mask[mask].index[0]
                    section_starts[rule.label] = idx

        if section_starts:
            first_idx = min(section_starts.values())

        # Sorted list of (start_index, label) for range lookup.
        sorted_sections = sorted(section_starts.items(), key=lambda x: x[1])

        def _section_at(row_idx: int) -> str:
            label = "storage"
            for sec_label, sec_start in sorted_sections:
                if row_idx >= sec_start:
                    label = sec_label
            return label

        # --- Fuse section label with optional query drill-down ---
        # Always returns a (section, query_str) tuple so callers retain both pieces.
        def _fuse(row_idx: int, query_val) -> tuple:
            section = _section_at(row_idx)
            if (
                drill_down_to_query_level
                and get_section_from_title(section).query_level
            ):
                q_str = (
                    cls.format_query_string(query_val, omit_leading_q=True)
                    if isinstance(query_val, set)
                    else ""
                )
                return (section, q_str)
            return (section, "")

        return pd.Series(
            [_fuse(idx, val) for idx, val in zip(query_series.index, query_series)],
            index=history.index,
        )

    @staticmethod
    def format_query_string(
        query_set: Set[str], max_queries: int = 22, omit_leading_q: bool = False
    ) -> str:
        """Format a set of queries into a readable string."""
        if not isinstance(query_set, set) or len(query_set) == 0:
            if isinstance(query_set, str):
                return query_set
            return ""

        if len(query_set) == max_queries:
            return "all"

        if len(query_set) < 5:
            try:
                sorted_x = sorted(query_set, key=lambda x: int(x))
                x_int = [int(e) for e in sorted_x]
                min_x, max_x = min(x_int), max(x_int)

                if x_int == list(range(min_x, max_x + 1)):
                    if min_x == max_x:
                        return f"Q{min_x}" if not omit_leading_q else f"{min_x}"
                    return (
                        f"Q {min_x}-{max_x}"
                        if not omit_leading_q
                        else f"{min_x}-{max_x}"
                    )
            except (ValueError, TypeError):
                pass

            return "Q " + ", ".join(sorted(query_set))

        return "multiple Q."

    @classmethod
    def extract_worked_on_spans(
        cls, worked_on_queries: pd.Series
    ) -> List[WorkedOnSpan]:
        """
        Convert worked-on query series into continuous spans with consistent queries.

        Expects a series of (section, query_str) tuples as produced by
        extract_worked_on_queries.  Each WorkedOnSpan carries both the section
        label and the per-query string so the plotter can render them on
        separate lines.
        """
        spans = []
        current_span = None

        for i, value in enumerate(worked_on_queries):
            # Support both legacy plain-string values and the current (section, q_str) tuples.
            if isinstance(value, tuple):
                section, queries = value
            else:
                section, queries = value, ""

            key = (section, queries)
            if current_span is None:
                current_span = {
                    "start": i,
                    "section": section,
                    "queries": queries,
                    "key": key,
                }
            elif key != current_span["key"]:
                current_span["end"] = i - 1
                spans.append(current_span)
                current_span = {
                    "start": i,
                    "section": section,
                    "queries": queries,
                    "key": key,
                }

        if current_span is not None:
            current_span["end"] = len(worked_on_queries) - 1
            spans.append(current_span)

        return [
            WorkedOnSpan(
                start=s["start"],
                end=s["end"],
                section=s["section"],
                queries=s["queries"],
            )
            for s in spans
        ]

    # deprecated: we now track code size directly via cloc tool (excluding comments, blank lines, etc.) rather than inferring it from apply-patch added/deleted lines counts.
    # @classmethod
    # def extract_code_size(cls, history: pd.DataFrame) -> pd.Series:
    #     # added loc
    #     loc_diff = history.apply(
    #         lambda row: (
    #             row.get("apply_patch/added_loc_count", 0)
    #             - row.get("apply_patch/deleted_loc_count", 0)
    #         ),
    #         axis=1,
    #     )

    #     # do prefix sum
    #     code_size = loc_diff.cumsum()

    #     return code_size
