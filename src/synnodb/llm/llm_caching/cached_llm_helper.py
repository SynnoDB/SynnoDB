import json
import logging
from dataclasses import is_dataclass
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from agents import (
    AgentOutputSchemaBase,
    ApplyPatchTool,
    Handoff,
    ModelSettings,
    ShellTool,
    Tool,
)
from agents.usage import RequestUsage, Usage
from anthropic import BaseModel
from openai.types.responses.response_function_tool_call import ResponseFunctionToolCall

from synnodb.llm.sdk.agents_sdk.openai_token_usage import (
    openai_get_tokens_context_and_dollar_info,
)
from synnodb.synth_framework.git_snapshotter import GitSnapshotter
from synnodb.synth_framework.runtime_tracker import RuntimeTracker
from synnodb.tools.tool_call_error_logger import log_tool_call_error
from synnodb.utils.utils import dump_pickle, load_pickle, sha256, stable_json

logger = logging.getLogger(__name__)


first_invocation = True


class LLMModelHelper:
    def __init__(
        self,
        model: str,
        cache_type,
        do_not_cache: bool,
        config_kwargs: dict[str, Any],
        is_litellm: bool,
        working_dir: Path,
        snapshotter: GitSnapshotter | None = None,
        runtime_tracker: Optional[RuntimeTracker] = None,
        tools_loaded_deferred: bool = False,
    ):
        self.model = model
        self.runtime_tracker = runtime_tracker
        self.do_not_cache = do_not_cache
        self.config_kwargs = config_kwargs
        self.cache_type = cache_type
        self.snapshotter = snapshotter
        self.is_litellm = is_litellm
        self.tools_loaded_deferred = tools_loaded_deferred
        self.working_dir = working_dir

    def load_llm_entry_from_cache(
        self, cache_path: Path
    ) -> tuple[Optional[object], float, bool]:
        cached = load_pickle(cache_path, self.cache_type)
        if cached is not None:
            # logger.info(f'Found in cache: {cache_path}')
            resp = cached.response

            if self.is_litellm:
                # could maybe also be applied to openai
                litellm_ensure_usage_entries(resp.usage)

            assert resp.usage is not None
            cost = openai_get_tokens_context_and_dollar_info(
                resp.usage, self.model, last_entry_only=True, log=False
            )["cost"]
            logger.debug(f"Saved: ${cost:0.6f} / Cache: {cache_path}")

            # Add the LLM time to the runtime tracker as skipped time, since we are skipping the actual LLM call by using the cache
            if self.runtime_tracker is not None:
                self.runtime_tracker.add_skipped_time(cached.llm_time)

            return resp, cost, True

        return None, 0, False

    def process_llm_response(
        self,
        resp,
        llm_time,
        cache_path: Optional[Path],
        hash_payload: str,
    ):
        if self.is_litellm:
            # could maybe also be applied to openai
            litellm_ensure_usage_entries(resp.usage)

        # extract cost
        assert resp.usage is not None
        stats = openai_get_tokens_context_and_dollar_info(
            resp.usage, self.model, last_entry_only=True, log=False
        )
        cost = stats["cost"]

        # rewrite absolute paths in apply_patch tool calls to relative paths to avoid cache misses due to different absolute paths on different machines
        # !IMPORTANT: do this before the caching - otherwise the cache would store the absolute paths and would not be hit on other machines with different absolute paths
        resp = remove_absolute_applypatch_paths(
            resp, self.working_dir, model=str(self.model)
        )

        logger.debug(
            f"Cost: ${cost:0.6f} / Time: {llm_time:0.2f}s / Cache: {cache_path} / input tokens: {stats['input_tokens']} (cached: {stats['cached_tokens']})"
        )

        if cache_path is not None and not self.do_not_cache:
            dump_pickle(
                cache_path,
                self.cache_type(
                    resp,
                    hash_payload=hash_payload,
                    llm_time=llm_time,
                ),
                do_not_cache=self.do_not_cache,
            )

        self.llm_was_cached = False

        return resp

    def hash_payload(
        self,
        system_instructions: str | None,
        input: Any,
        model_settings: ModelSettings,
        tools: list[Tool],
        output_schema: AgentOutputSchemaBase | None,
        handoffs: list[Handoff],
        previous_response_id: str | None,
        conversation_id: str | None,
        prompt: Any | None,
        stream: Optional[bool] = None,
    ) -> Tuple[str, str]:
        if handoffs:
            raise RuntimeError("Handoffs are not supported with caching.")

        # serialize config args dict () - use key sorting to ensure stable order
        config_kwargs_serialized = ",".join(
            f"{k}={v}" for k, v in sorted(self.config_kwargs.items())
        )

        tools_serialized = serialize_tools(tools)

        global first_invocation
        if first_invocation:
            logger.debug(f"Tools encoded for hashing: {tools_serialized}")
            first_invocation = False

        payload = {
            "model": str(self.model),
            "system_instructions": system_instructions,
            "input": input,
            "model_settings": model_settings.to_json_dict(),
            "tools": tools_serialized,
            "output_schema": (
                output_schema.json_schema() if output_schema is not None else None
            ),
            # "handoffs": [h.model_dump() if hasattr(h, "model_dump") else repr(h) for h in handoffs],
            "conversation_id": conversation_id,
            "previous_response_id": previous_response_id,
            "prompt": prompt,
            "config_kwargs": config_kwargs_serialized,
        }

        # stream is only used for openai
        if stream is not None:
            payload["stream"] = stream

        if self.tools_loaded_deferred:
            payload["tools_loaded_deferred"] = True

        stable_payload = stable_json(payload)
        req_hash = sha256(stable_payload)

        return req_hash, stable_payload


def litellm_ensure_usage_entries(usage: Usage) -> None:
    if usage.request_usage_entries:
        return
    if usage.total_tokens <= 0:
        return
    request = RequestUsage(
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        total_tokens=usage.total_tokens,
        input_tokens_details=usage.input_tokens_details,
        output_tokens_details=usage.output_tokens_details,
    )
    usage.request_usage_entries.append(request)


def prune_llm_cache_key_dict(data: Dict):
    # keep only selected  attributes - this preserves old cached if OpenAI updates the tool objects
    args_to_keep = [
        "description",
        "is_enabled",
        "name",
        "needs_approval",
        "params_json_schema",
        "strict_json_schema",
        "tool_input_guardrails",
        "tool_output_guardrails",
        "defer_loading",
    ]

    cleaned = {k: data[k] for k in args_to_keep if k in data}

    return cleaned


def serialize_tools(tools: list[Tool]) -> list[str]:
    tools_serialized = []
    try:
        for t in tools:
            if isinstance(t, ApplyPatchTool) or isinstance(t, ShellTool):
                data = t.name
            elif isinstance(t, BaseModel):
                # response pydanctic model
                data = prune_llm_cache_key_dict(t.to_dict())

                data = stable_json(data)
            elif is_dataclass(t):
                # dataclass object
                data = t.__dict__.copy()

                # cleanup the args of the tools to keep only selected attributes
                data = prune_llm_cache_key_dict(data)

                data = stable_json(data)
            elif isinstance(t, list):
                # call recusively
                data = serialize_tools(t)
            else:
                raise Exception(f"Cannot hash tool of type {type(t)}")

            # check that no memory addresses are present in the serialized data
            assert "0x" not in data, (
                f"Cannot hash tool with non-deterministic data. Discovered likely a function or object reference in the tool definition: {data}"
            )

            tools_serialized.append(data)
    except Exception as e:
        logger.debug(f"Error serializing tools for hashing: {e}\n{str(t)}")
        raise Exception(f"Error serializing tools for hashing: {e}")

    return tools_serialized


LEGACY_WORKSPACE_DIR = "/home/jwehrstein/bespoke_olap/output"


def remove_absolute_applypatch_paths(
    model_output, working_dir: Path, model: str | None = None
):
    candidates = [working_dir.resolve()]
    legacy = Path(LEGACY_WORKSPACE_DIR).resolve()
    if legacy not in candidates:
        candidates.append(legacy)

    for call in model_output.output:
        if not isinstance(call, ResponseFunctionToolCall):
            continue
        if call.type != "function_call" or call.name != "apply_patch":
            continue

        try:
            args_dict = json.loads(call.arguments)
        except json.JSONDecodeError:
            logger.warning(
                "Skipping path rewrite for apply_patch with malformed JSON args (will be caught downstream)"
            )
            log_tool_call_error(
                error_type="JSONDecodeError",
                error=Exception(
                    f"Malformed JSON in apply_patch args: {call.arguments[:200]}"
                ),
                model=str(model),
            )
            continue

        file_path = args_dict.get("path")
        if not file_path or not Path(file_path).is_absolute():
            continue

        resolved = Path(file_path).resolve()
        ws = next((c for c in candidates if resolved.is_relative_to(c)), None)
        if ws is None:
            # outside workspace — leave for executor to reject
            continue

        args_dict["path"] = resolved.relative_to(ws).as_posix()
        call.arguments = json.dumps(args_dict)

        rewritten = args_dict["path"]
        if any(str(c) in rewritten for c in candidates):
            raise RuntimeError(
                f"Absolute workspace path leaked after rewrite in apply_patch path={rewritten!r}; call={call}"
            )

    return model_output
