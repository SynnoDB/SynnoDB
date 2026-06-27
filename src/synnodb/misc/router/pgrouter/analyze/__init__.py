"""Public analyzer API."""

from .cli import analyze_main, main
from .normalization import NORMALIZATION_RULES, normalize_expression, normalize_query_structure
from .repetition import QueryOccurrence, QueryRepetition, find_repetitions

__all__ = [
    "NORMALIZATION_RULES",
    "QueryOccurrence",
    "QueryRepetition",
    "analyze_main",
    "find_repetitions",
    "main",
    "normalize_expression",
    "normalize_query_structure",
]
