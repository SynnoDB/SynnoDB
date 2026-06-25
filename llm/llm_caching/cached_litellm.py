import asyncio
import copy
import logging
import time
from dataclasses import is_dataclass, replace
from pathlib import Path
from typing import Any, Dict, Optional

import litellm
from agents import ModelSettings
from agents.extensions.models.litellm_model import LitellmModel
from agents.models.reasoning_content_replay import ReasoningContentReplayContext
from litellm.exceptions import BadGatewayError, InternalServerError, RateLimitError

from llm.llm_caching.cached_llm_helper import LLMModelHelper
from observability.logging.run_stats_collector import RunStatsCollector, get_response_id
from synth_framework.git_snapshotter import GitSnapshotter
from synth_framework.runtime_tracker import RuntimeTracker
from tools.tool_call_error_logger import log_tool_call_error
from utils.utils import create_dir_and_set_permissions

logger = logging.getLogger(__name__)

_ANTHROPIC_CACHE_CONTROL_INJECTION_POINTS = [
    {"location": "message", "role": "system"},
    {"location": "message", "index": -1},
]


class LiteLLMCacheType:
    def __init__(
        self,
        response,
        hash_payload: str | None = None,
        llm_time: float | None = None,
    ):
        self.response = response
        self.hash_payload = hash_payload
        self.llm_time = llm_time


def _glm_should_replay_reasoning(ctx: ReasoningContentReplayContext) -> bool:
    return "GLM" in ctx.model


class CachedLitellmModel(LitellmModel):
    def __init__(
        self,
        *args,
        llm_cache_dir: Path,
        do_not_cache: bool,
        working_dir: Path,
        snapshotter: GitSnapshotter | None = None,
        stop_on_cache_miss: bool = False,
        config_kwargs: Dict[str, Any] = {},
        runtime_tracker: Optional[RuntimeTracker] = None,
        tools_loaded_deferred: bool = False,
        run_stats_collector: RunStatsCollector | None = None,
        glm_thinking_enabled: bool = False,
        **kwargs,
    ):
        self.glm_thinking_enabled = glm_thinking_enabled
        if glm_thinking_enabled:
            kwargs.setdefault(
                "should_replay_reasoning_content", _glm_should_replay_reasoning
            )
        super().__init__(*args, **kwargs)
        self.cache_dir = llm_cache_dir
        create_dir_and_set_permissions(self.cache_dir)
        self.stop_on_cache_miss = stop_on_cache_miss
        self.total_saved = 0.0
        self.llm_was_cached = False
        self.last_tool_call_args = None  # store raw tool call args for error logging
        self.last_xml_tool_calls: list | None = (
            None  # XML tool calls found in GLM reasoning block
        )
        self.run_stats_collector = run_stats_collector
        self.llm_model_helper = LLMModelHelper(
            model=self.model,
            cache_type=LiteLLMCacheType,
            snapshotter=snapshotter,
            runtime_tracker=runtime_tracker,
            config_kwargs=config_kwargs,
            do_not_cache=do_not_cache,
            is_litellm=True,
            tools_loaded_deferred=tools_loaded_deferred,
            working_dir=working_dir,
        )

    def _cache_path_for(self, hash: str) -> Path:
        return self.cache_dir / f"{hash}.pkl"

    def __str__(self) -> str:
        return str(self.model)

    def _is_anthropic_model(self) -> bool:
        return str(self.model).startswith("anthropic/")

    def _is_glm_model(self) -> bool:
        return "GLM" in str(self.model)

    def _augment_model_settings_for_glm_thinking(
        self, model_settings: ModelSettings
    ) -> Any:
        if not self.glm_thinking_enabled or not self._is_glm_model():
            return model_settings
        if model_settings is None:
            return model_settings
        try:
            existing_body = getattr(model_settings, "extra_body", None) or {}
            if isinstance(existing_body, dict) and "thinking" in existing_body:
                return model_settings
            updated_body = (
                dict(existing_body) if isinstance(existing_body, dict) else {}
            )
            updated_body["thinking"] = {"type": "enabled", "clear_thinking": False}
            if is_dataclass(model_settings):
                return replace(model_settings, extra_body=updated_body)
            copied = copy.deepcopy(model_settings)
            setattr(copied, "extra_body", updated_body)
            return copied
        except Exception as exc:
            logger.warning("Failed to enable GLM thinking on model settings: %s", exc)
            return model_settings

    def _augment_model_settings_for_anthropic_prompt_caching(
        self, model_settings: ModelSettings
    ) -> Any:
        if not self._is_anthropic_model():
            return model_settings

        if model_settings is None:
            return model_settings

        # extra_body was deprecated by anthropic on 4/18/2026. Switching to writing to extra-args field

        try:
            extra_args = getattr(model_settings, "extra_args", None) or {}
            injection_points = extra_args.get("cache_control_injection_points")
            if injection_points:
                return model_settings

            updated_extra_args = dict(extra_args)
            updated_extra_args["cache_control_injection_points"] = (
                _ANTHROPIC_CACHE_CONTROL_INJECTION_POINTS
            )

            if is_dataclass(model_settings):
                return replace(model_settings, extra_args=updated_extra_args)  # type: ignore

            copied = copy.deepcopy(model_settings)
            setattr(copied, "extra_args", updated_extra_args)
            return copied
        except Exception as exc:
            logger.warning(
                "Failed to enable Anthropic prompt caching hints on model settings: %s",
                exc,
            )
            return model_settings

    def _scan_reasoning_for_xml_calls(self, resp) -> None:
        """Scan resp.output for GLM reasoning items containing XML tool calls."""
        self.last_xml_tool_calls = None
        if not self._is_glm_model():
            return
        try:
            from openai.types.responses.response_reasoning_item import (
                ResponseReasoningItem,
            )

            from llm.glm.glm_xml_tool_call_parser import parse_xml_tool_calls

            for item in resp.output:
                if isinstance(item, ResponseReasoningItem):
                    for summary in item.summary or []:
                        text = getattr(summary, "text", "") or ""
                        calls = parse_xml_tool_calls(text)
                        if calls:
                            self.last_xml_tool_calls = calls
                            return
        except Exception as exc:
            logger.debug("GLM XML scan failed: %s", exc)

    async def get_response(self, *args, **kwargs):
        system_instructions = kwargs.get("system_instructions")
        input = kwargs.get("input")
        model_settings = kwargs.get("model_settings")
        tools = kwargs.get("tools") or []
        output_schema = kwargs.get("output_schema")
        handoffs = kwargs.get("handoffs") or []
        previous_response_id = kwargs.get("previous_response_id")
        conversation_id = kwargs.get("conversation_id")
        prompt = kwargs.get("prompt")

        assert model_settings is not None, "model_settings is required for caching"

        req_hash, hash_payload = self.llm_model_helper.hash_payload(
            system_instructions,
            input,
            model_settings,  # type: ignore
            tools=tools,
            output_schema=output_schema,
            handoffs=handoffs,
            previous_response_id=previous_response_id,
            conversation_id=conversation_id,
            prompt=prompt,
        )

        # store the hash for later
        if self.run_stats_collector is not None:
            self.run_stats_collector.record_llm_cache_status(
                answered_from_cache=False, request_hash=req_hash
            )

        cache_path = self._cache_path_for(req_hash)

        cache_hit_path = self.llm_model_helper.resolve_cache_path(
            self.cache_dir, cache_path, hash_payload
        )

        if cache_hit_path is not None:
            resp, saved_cost, self.llm_was_cached = (
                self.llm_model_helper.load_llm_entry_from_cache(cache_hit_path)
            )
            if resp is not None:
                self.total_saved += saved_cost
                if self.run_stats_collector is not None:
                    resp_id = get_response_id(resp)
                    if resp_id is not None:
                        self.run_stats_collector.record_llm_cache_status(
                            answered_from_cache=True,
                            response_id=resp_id,
                            request_hash=req_hash,
                        )
                # store raw tool call arguments for error logging (same as non-cached path)
                try:
                    from agents.models.interface import ResponseFunctionToolCall

                    self.last_tool_call_args = [
                        {"name": item.name, "arguments": item.arguments}
                        for item in resp.output
                        if isinstance(item, ResponseFunctionToolCall)
                    ]
                except Exception:
                    self.last_tool_call_args = None
                self._scan_reasoning_for_xml_calls(resp)
                return resp

        if self.stop_on_cache_miss:
            # logger.debug(hash_payload)
            raise Exception(
                f"Stop on cache miss. Did not found in cache: {cache_path}\nPayload hash: {req_hash}\nPayload: {hash_payload}"
            )

        # add cache control injection points for Anthropic models to enable prompt caching
        kwargs["model_settings"] = (
            self._augment_model_settings_for_anthropic_prompt_caching(model_settings)
        )
        # inject thinking parameter for GLM models
        kwargs["model_settings"] = self._augment_model_settings_for_glm_thinking(
            kwargs["model_settings"]
        )

        try:
            time_start = time.perf_counter()
            resp = await super().get_response(*args, **kwargs)
            llm_time = time.perf_counter() - time_start
        except RateLimitError as e:
            # wait (rate limit cooldown - at least one min)
            wait_min = 1  # minutes
            logger.warning(
                f"Rate limit error encountered: {e}. Waiting for {wait_min} minutes before retrying."
            )
            await asyncio.sleep(120)

            # try again
            time_start = time.perf_counter()
            resp = await super().get_response(*args, **kwargs)
            llm_time = time.perf_counter() - time_start
        except BadGatewayError as e:
            # wait
            wait_min = 1  # minutes
            logger.warning(
                f"Bad gateway error encountered: {e}. Waiting for {wait_min} minutes before retrying."
            )
            await asyncio.sleep(120)

            # try again
            time_start = time.perf_counter()
            resp = await super().get_response(*args, **kwargs)
            llm_time = time.perf_counter() - time_start
        except InternalServerError as e:
            if "overloaded_error" in str(e).lower():
                wait_min = 1  # minutes
                last_err: Exception = e
                for attempt in range(3):
                    logger.warning(
                        f"Model server overloaded error encountered (attempt {attempt + 1}/3): {last_err}. Waiting for {wait_min} minutes before retrying."
                    )
                    await asyncio.sleep(120)

                    try:
                        time_start = time.perf_counter()
                        resp = await super().get_response(*args, **kwargs)
                        llm_time = time.perf_counter() - time_start
                        break
                    except InternalServerError as retry_err:
                        if "overloaded_error" not in str(retry_err).lower():
                            raise
                        last_err = retry_err
                else:
                    # not hitting break
                    raise last_err
            elif "failed to parse" in str(e).lower():
                logger.warning(
                    f"Model generated malformed tool call: {e}. Retrying after short delay."
                )
                log_tool_call_error(
                    error_type="InternalServerError",
                    error=e,
                    model=str(self.model),
                    turn=self.run_stats_collector.last_turn
                    if self.run_stats_collector
                    else None,
                )
                await asyncio.sleep(5)

                # try again
                time_start = time.perf_counter()
                resp = await super().get_response(*args, **kwargs)
                llm_time = time.perf_counter() - time_start
            elif "anthropic error" in str(e).lower():
                logger.warning(
                    f"Anthropic error encountered: {e}. This may be due to a transient issue with the model server. Retrying once after a short delay."
                )
                await asyncio.sleep(30)

                # try again
                time_start = time.perf_counter()
                resp = await super().get_response(*args, **kwargs)
                llm_time = time.perf_counter() - time_start
            else:
                raise e
        except litellm.exceptions.Timeout as e:
            logger.warning(
                f"Timeout error encountered: {e}. Retrying once with doubled timeout."
            )
            await asyncio.sleep(30)

            # Retry with double the timeout so long-running generations can complete.
            # The default litellm timeout is 600s; double it to 1200s for the retry.
            retry_kwargs = dict(kwargs)
            ms = retry_kwargs.get("model_settings")
            if ms is not None:
                existing_extra_args = getattr(ms, "extra_args", None) or {}
                updated_extra_args = dict(existing_extra_args)
                updated_extra_args["timeout"] = int(
                    updated_extra_args.get("timeout", 600) * 2
                )
                try:
                    if is_dataclass(ms):
                        retry_kwargs["model_settings"] = replace(
                            ms, extra_args=updated_extra_args
                        )
                    else:
                        ms_copy = copy.deepcopy(ms)
                        setattr(ms_copy, "extra_args", updated_extra_args)
                        retry_kwargs["model_settings"] = ms_copy
                except Exception:
                    pass  # fall back to original model_settings if patching fails

            time_start = time.perf_counter()
            resp = await super().get_response(*args, **retry_kwargs)
            llm_time = time.perf_counter() - time_start
        except litellm.ServerUnavailableError as e:
            logger.warning(
                f"Model server unavailable error encountered: {e}. This may be due to a transient issue with the model server. Retrying once after a short delay."
            )
            await asyncio.sleep(30)

            # try again
            time_start = time.perf_counter()
            resp = await super().get_response(*args, **kwargs)
            llm_time = time.perf_counter() - time_start

        # process response and cache it
        self.llm_model_helper.process_llm_response(
            resp=resp,
            llm_time=llm_time,
            cache_path=cache_path,
            hash_payload=hash_payload,
        )
        if self.run_stats_collector is not None:
            resp_id = get_response_id(resp)
            if resp_id is not None:
                self.run_stats_collector.record_llm_cache_status(
                    False,
                    response_id=resp_id,
                    request_hash=req_hash,
                )

        self.llm_was_cached = False

        # store raw tool call arguments for error logging
        try:
            from agents.models.interface import ResponseFunctionToolCall

            self.last_tool_call_args = [
                {"name": item.name, "arguments": item.arguments}
                for item in resp.output
                if isinstance(item, ResponseFunctionToolCall)
            ]
        except Exception:
            self.last_tool_call_args = None

        self._scan_reasoning_for_xml_calls(resp)

        return resp
