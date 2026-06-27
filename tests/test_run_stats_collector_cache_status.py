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

    def test_cache_status_falls_back_to_pending_flag_without_response_id(self):
        collector = _collector_for_cache_status_tests()

        self.assertTrue(collector._consume_llm_cache_status(_model_response(None)))


if __name__ == "__main__":
    unittest.main()
