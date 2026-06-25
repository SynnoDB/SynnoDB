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
        payload_a = (
            'Example args:\\n1 req_1_012345abcdef "67"\\n'
            "Output: result_req_1_012345abcdef.csv"
        )
        payload_b = (
            'Example args:\\n1 req_2_fedcba543210 "67"\\n'
            "Output: result_req_2_fedcba543210.csv"
        )

        self.assertEqual(
            normalize_llm_cache_payload(payload_a),
            normalize_llm_cache_payload(payload_b),
        )

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
            hash_a, payload_a = helper.hash_payload(
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
            hash_b, payload_b = helper.hash_payload(
                system_instructions=None,
                input='Example args:\n1 req_2_fedcba543210 "67"\n',
                model_settings=ModelSettings(),
                tools=[],
                output_schema=None,
                handoffs=[],
                previous_response_id=None,
                conversation_id=None,
                prompt=None,
            )

            self.assertEqual(hash_a, hash_b)
            self.assertEqual(payload_a, payload_b)
            self.assertIn("<REQ_ID>", payload_a)


if __name__ == "__main__":
    unittest.main()
