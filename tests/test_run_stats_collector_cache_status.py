import sys
import unittest
from pathlib import Path

from agents import ModelResponse
from agents.usage import Usage

from synnodb.observability.logging.run_stats_collector import RunStatsCollector

sys.path.append(Path(__file__).parent.parent.as_posix())


def _collector_for_cache_status_tests() -> RunStatsCollector:
    collector = object.__new__(RunStatsCollector)
    collector._llm_answered_from_cache_by_response_id = {}
    collector.last_llm_hash = None
    return collector


def _model_response(response_id: str | None) -> ModelResponse:
    return ModelResponse(output=[], usage=Usage(), response_id=response_id)


class TestRunStatsCollectorCacheStatus(unittest.TestCase):
    def test_response_specific_cache_status_overrides_stale_pending_flag(self):
        collector = _collector_for_cache_status_tests()
        collector.record_llm_cache_status(
            False, response_id="real-response", request_hash="real-request"
        )

        self.assertFalse(
            collector._consume_llm_cache_status(_model_response("real-response"))
        )
        self.assertEqual(collector.last_llm_hash, "real-request")

    def test_cache_status_defaults_to_not_cached_without_response_id(self):
        collector = _collector_for_cache_status_tests()

        # No response id and no recorded status: conservatively treated as a real (uncached)
        # call, so cost accounting never under-counts an unidentifiable turn.
        self.assertFalse(collector._consume_llm_cache_status(_model_response(None)))

    def test_record_apply_patch_rejected_marks_step_and_keeps_reason(self):
        collector = object.__new__(RunStatsCollector)
        # per-tool-call state, as reset in on_tool_start
        collector.apply_patch_rejected = False
        collector.apply_patch_failed = []
        collector.apply_patch_files = set()
        collector.apply_patch_cached = False

        collector.record_apply_patch_rejected(
            "db_loader.cpp", "missing required field(s): type"
        )

        # A schema-rejected call is neither a cache hit nor a real edit: it must be
        # flagged rejected (so the live-ui can distinguish it from a cache miss),
        # carry the reason, and name the targeted file - without recording a hit.
        self.assertTrue(collector.apply_patch_rejected)
        self.assertFalse(collector.apply_patch_cached)
        self.assertEqual(len(collector.apply_patch_failed), 1)
        self.assertIn(
            "missing required field(s): type", collector.apply_patch_failed[0]
        )

        self.assertIn("db_loader.cpp", collector.apply_patch_files)

    def test_record_apply_patch_rejected_tolerates_missing_path(self):
        collector = object.__new__(RunStatsCollector)
        collector.apply_patch_rejected = False
        collector.apply_patch_failed = []
        collector.apply_patch_files = set()

        collector.record_apply_patch_rejected(None, "invalid json")

        self.assertTrue(collector.apply_patch_rejected)
        self.assertEqual(collector.apply_patch_files, set())


if __name__ == "__main__":
    unittest.main()
