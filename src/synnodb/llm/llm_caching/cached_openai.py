import logging
import time
from pathlib import Path
from typing import Any, Dict, Literal, Optional, overload

from agents.agent_output import AgentOutputSchemaBase
from agents.handoffs import Handoff
from agents.model_settings import ModelSettings
from agents.models.openai_responses import OpenAIResponsesModel
from agents.tool import Tool
from openai import BadRequestError
from openai.types.responses import Response

from synnodb.llm.llm_caching.cached_llm_helper import LLMModelHelper
from synnodb.observability.logging.run_stats_collector import RunStatsCollector, get_response_id
from synnodb.synth_framework.git_snapshotter import GitSnapshotter
from synnodb.synth_framework.runtime_tracker import RuntimeTracker
from synnodb.utils.utils import create_dir_and_set_permissions

logger = logging.getLogger(__name__)


class LLMCacheType:
    def __init__(
        self,
        response: Response,
        hash_payload: str,
        llm_time: float | None = None,
    ):
        self.response = response
        self.hash_payload = hash_payload
        self.llm_time = llm_time


class CachedOpenAIResponsesModel(OpenAIResponsesModel):
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
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self.cache_dir = llm_cache_dir
        create_dir_and_set_permissions(self.cache_dir)
        self.stop_on_cache_miss = stop_on_cache_miss
        self.total_saved = 0.0
        self.llm_was_cached = False
        self.run_stats_collector = run_stats_collector

        self.llm_model_helper = LLMModelHelper(
            model=self.model,
            cache_type=LLMCacheType,
            snapshotter=snapshotter,
            runtime_tracker=runtime_tracker,
            config_kwargs=config_kwargs,
            do_not_cache=do_not_cache,
            is_litellm=False,
            tools_loaded_deferred=tools_loaded_deferred,
            working_dir=working_dir,
        )

    def _cache_path_for(self, hash: str) -> Path:
        return self.cache_dir / f"{hash}.pkl"

    def __str__(self):
        return str(self.model)

    @overload
    async def _fetch_response(
        self,
        system_instructions: str | None,
        input: Any,
        model_settings: ModelSettings,
        tools: list[Tool],
        output_schema: AgentOutputSchemaBase | None,
        handoffs: list[Handoff],
        previous_response_id: str | None,
        conversation_id: str | None,
        stream: Literal[False],
        prompt: Any | None = None,
    ) -> Response: ...

    @overload
    async def _fetch_response(
        self,
        system_instructions: str | None,
        input: Any,
        model_settings: ModelSettings,
        tools: list[Tool],
        output_schema: AgentOutputSchemaBase | None,
        handoffs: list[Handoff],
        previous_response_id: str | None,
        conversation_id: str | None,
        stream: Literal[True],
        prompt: Any | None = None,
    ) -> Any: ...

    async def _fetch_response(  # type: ignore[override]
        self,
        system_instructions: str | None,
        input: Any,
        model_settings: ModelSettings,
        tools: list[Tool],
        output_schema: AgentOutputSchemaBase | None,
        handoffs: list[Handoff],
        previous_response_id: str | None,
        conversation_id: str | None,
        stream: bool,
        prompt: Any | None = None,
    ):
        assert not stream, "stream not supported"

        req_hash, hash_payload = self.llm_model_helper.hash_payload(
            system_instructions,
            input,
            model_settings,
            tools,
            output_schema,
            handoffs,
            previous_response_id,
            conversation_id,
            prompt,
            stream,
        )

        # store the hash for later
        if self.run_stats_collector is not None:
            self.run_stats_collector.record_llm_cache_status(
                False, request_hash=req_hash
            )

        cache_path = self._cache_path_for(req_hash)

        if cache_path.exists():
            resp, saved_cost, self.llm_was_cached = (
                self.llm_model_helper.load_llm_entry_from_cache(cache_path)
            )
            if resp is not None:
                self.total_saved += saved_cost
                if self.run_stats_collector is not None:
                    self.run_stats_collector.record_llm_cache_status(
                        True,
                        response_id=get_response_id(resp),
                        request_hash=req_hash,
                    )

                return resp

        if self.stop_on_cache_miss:
            raise Exception(
                f"Stop on cache miss. Did not found in cache: {cache_path}\nPayload hash: {req_hash}\nPayload: {hash_payload}"
            )

        async def _exec(fetch_response, suffix: Optional[str] = None):
            if suffix is not None:
                logger.info(
                    f"Retrying LLM call with modified input due to error: {suffix}"
                )
                modified_input = f"{input}\n\n{suffix}"
            else:
                modified_input = input

            # measure time to fetch response from LLM
            time_start = time.perf_counter()

            resp = await fetch_response(
                system_instructions=system_instructions,
                input=modified_input,
                model_settings=model_settings,
                tools=tools,
                output_schema=output_schema,
                handoffs=handoffs,
                previous_response_id=previous_response_id,
                conversation_id=conversation_id,
                stream=stream,
                prompt=prompt,
            )

            # compute total llm call time
            time_end = time.perf_counter()
            llm_time = time_end - time_start

            return resp, llm_time

        try:
            resp, llm_time = await _exec(super()._fetch_response)
        except BadRequestError as e:
            if "violating our usage policy" in str(e):
                logger.warning(
                    f"BadRequestError due to content policy violation: {e}. Attempting to retry with modified input."
                )
                # Modify the input to try to avoid the content policy violation
                modified_input = (
                    "This input has been modified to avoid content policy violations."
                )
                resp, llm_time = await _exec(
                    super()._fetch_response, suffix=modified_input
                )
            else:
                raise e

        # process response and cache it
        self.llm_model_helper.process_llm_response(
            resp=resp,
            llm_time=llm_time,
            cache_path=cache_path,
            hash_payload=hash_payload,
        )
        if self.run_stats_collector is not None:
            self.run_stats_collector.record_llm_cache_status(
                False,
                response_id=get_response_id(resp),
                request_hash=req_hash,
            )
        self.llm_was_cached = False

        return resp
