"""Proactive-compaction trigger: compact before the context window is exhausted.

Kept dependency-free (no SDK/tooling imports) so the trigger logic is unit-testable
in isolation and importable from the wrapper without pulling heavy modules.
"""


# Trigger a proactive compaction once the context window is this full. Kept below
# 1.0 so we compact BEFORE a hard overflow rather than only reacting to one.
COMPACTION_TRIGGER_FRACTION = 0.90


def context_usage_at_or_above(run_stats_collector, threshold_fraction: float) -> bool:
    """True when the last recorded context-window occupancy is at/above threshold.

    Reads the fraction stashed by RunStatsCollector.on_llm_end each turn; returns
    False when there is no collector or no turn has completed yet.
    """
    if run_stats_collector is None:
        return False
    last_context_window_usage = getattr(
        run_stats_collector, "last_context_window_usage", 0.0
    )
    return last_context_window_usage >= threshold_fraction
