import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from agents import ModelSettings

from llm.llm_caching.cached_llm_helper import (
    LLMModelHelper,
    normalize_llm_cache_payload,
    remove_absolute_applypatch_paths,
)
from utils.utils import dump_pickle

sys.path.append(Path(__file__).parent.parent.parent.as_posix())


from openai.types.responses.response_function_tool_call import ResponseFunctionToolCall

DIFF = (
    "--- a/query9.cpp\n"
    "+++ b/query9.cpp\n"
    "@@ -136,7 +136,7 @@\n"
    "     const int32_t*  pk_sk   = li.partkey_sorted_suppkey.data();\n"
    "-    const int64_t*  pk_ep   = li.partkey_sorted_price.data();\n"
    "+    const int32_t*  pk_ep   = li.partkey_sorted_price.data();\n"
    "     const int32_t*  pk_disc = li.partkey_sorted_disc.data();\n"
)


def make_call(path: str) -> ResponseFunctionToolCall:
    import json

    return ResponseFunctionToolCall(
        call_id="toolu_011X8bi3Fc2wfer5tGsksuxf",
        name="apply_patch",
        type="function_call",
        arguments=json.dumps({"type": "update_file", "path": path, "diff": DIFF}),
    )


def make_response(*calls) -> SimpleNamespace:
    return SimpleNamespace(output=list(calls))


class DummyCacheType:
    def __init__(self, hash_payload: str):
        self.response = None
        self.hash_payload = hash_payload
        self.llm_time = 0.0


class TestCachedLLMHelper(unittest.TestCase):
    def test_relative_path_unchanged(self):
        call = make_call("query9.cpp")
        original_args = call.arguments
        resp = make_response(call)

        rewritten = remove_absolute_applypatch_paths(
            resp, working_dir=Path("/home/jwehrstein/bespoke_olap/output")
        )

        self.assertEqual(rewritten.output[0].arguments, original_args)

    def test_absolute_workspace_path_rewritten(self):
        call = make_call("/home/jwehrstein/bespoke_olap/output/query9.cpp")
        resp = make_response(call)

        rewritten = remove_absolute_applypatch_paths(
            resp, working_dir=Path("/home/jwehrstein/bespoke_olap/output")
        )

        import json as _json

        args = _json.loads(rewritten.output[0].arguments)
        self.assertEqual(args["path"], "query9.cpp")

    def test_absolute_path_outside_workspace_left_alone(self):
        call = make_call("/etc/passwd")
        original_args = call.arguments
        resp = make_response(call)

        rewritten = remove_absolute_applypatch_paths(
            resp, working_dir=Path("/home/jwehrstein/bespoke_olap/output")
        )

        # outside workspace — left for executor to reject
        self.assertEqual(rewritten.output[0].arguments, original_args)

    def test_normalizes_generated_request_ids(self):
        legacy_payload = (
            'Example args:\\n1 20260625_143239_29807 "67"\\n'
            "Output: result_20260625_143239_29807.csv"
        )
        stable_payload = (
            'Example args:\\n1 req_1_012345abcdef "67"\\n'
            "Output: result_req_1_012345abcdef.csv"
        )

        self.assertEqual(
            normalize_llm_cache_payload(legacy_payload),
            normalize_llm_cache_payload(stable_payload),
        )

    def test_resolve_cache_path_finds_legacy_equivalent_payload(self):
        legacy_payload = 'Example args:\\n1 20260625_143239_29807 "67"\\n'
        stable_payload = 'Example args:\\n1 req_1_012345abcdef "67"\\n'

        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp)
            legacy_path = cache_dir / "legacy.pkl"
            canonical_path = cache_dir / "canonical.pkl"
            dump_pickle(
                legacy_path,
                DummyCacheType(hash_payload=legacy_payload),
                do_not_cache=False,
            )

            helper = LLMModelHelper(
                model="test-model",
                cache_type=DummyCacheType,
                do_not_cache=False,
                config_kwargs={},
                is_litellm=False,
                working_dir=cache_dir,
            )
            resolved = helper.resolve_cache_path(
                cache_dir=cache_dir,
                cache_path=canonical_path,
                hash_payload=normalize_llm_cache_payload(stable_payload),
            )

            self.assertEqual(resolved, canonical_path)
            self.assertTrue(canonical_path.exists())

    def test_hash_payload_ignores_generated_request_id_shape(self):
        with tempfile.TemporaryDirectory() as tmp:
            helper = LLMModelHelper(
                model="test-model",
                cache_type=DummyCacheType,
                do_not_cache=False,
                config_kwargs={},
                is_litellm=False,
                working_dir=Path(tmp),
            )
            legacy_hash, legacy_payload = helper.hash_payload(
                system_instructions=None,
                input='Example args:\n1 20260625_143239_29807 "67"\n',
                model_settings=ModelSettings(),
                tools=[],
                output_schema=None,
                handoffs=[],
                previous_response_id=None,
                conversation_id=None,
                prompt=None,
            )
            stable_hash, stable_payload = helper.hash_payload(
                system_instructions=None,
                input='Example args:\n1 req_1_012345abcdef "67"\n',
                model_settings=ModelSettings(),
                tools=[],
                output_schema=None,
                handoffs=[],
                previous_response_id=None,
                conversation_id=None,
                prompt=None,
            )

            self.assertEqual(legacy_hash, stable_hash)
            self.assertEqual(legacy_payload, stable_payload)
            self.assertIn("<REQ_ID>", stable_payload)


if __name__ == "__main__":
    unittest.main()
