import logging
import time
from pathlib import Path
from typing import Any, Optional, Tuple

from agents import TResponseInputItem, custom_span
from agents.memory.openai_responses_compaction_session import (
    OpenAIResponsesCompactionSession,
    _normalize_compaction_output_items,
    _strip_orphaned_assistant_ids,
    select_compaction_candidate_items,
)
from agents.memory.session import OpenAIResponsesCompactionArgs

from llm.anthropic.claude_compaction_helper import ClaudeCompactionHelper
from observability.logging.run_stats_collector import RunStatsCollector
from synth_framework.runtime_tracker import RuntimeTracker
from utils import utils

logger = logging.getLogger(__name__)


class CompactCacheType:
    def __init__(
        self,
        response_id: str,
        output_items: list[TResponseInputItem],
        hash_payload: str,
        runtime_seconds: float,
    ):
        self.response_id = response_id
        self.output_items = output_items
        self.hash_payload = hash_payload
        self.runtime_seconds = runtime_seconds


class CachedOpenAIResponsesCompactionSession(OpenAIResponsesCompactionSession):
    def __init__(
        self,
        do_not_cache: bool,
        cache_dir: Path,
        run_stats_collector: RunStatsCollector | None,
        runtime_tracker: Optional[RuntimeTracker] = None,
        use_claude_compaction: bool = False,
        claude_compaction_model: Optional[str] = "claude-sonnet-4-5-20250929",
        compaction_api_base: Optional[str] = None,
        **kwargs,
    ):
        self.cache_dir = cache_dir
        utils.create_dir_and_set_permissions(self.cache_dir)
        self.run_stats_collector = run_stats_collector
        self.runtime_tracker = runtime_tracker
        self.do_not_cache = do_not_cache
        self.use_claude_compaction = use_claude_compaction
        self.claude_compaction_helper = None

        if self.use_claude_compaction:
            assert claude_compaction_model is not None, (
                "claude_compaction_model must be provided when use_claude_compaction is True"
            )

            if claude_compaction_model.startswith("anthropic/"):
                # skip the "anthropic/" prefix if provided, since the client library expects model names without it
                claude_compaction_model = claude_compaction_model[len("anthropic/") :]

            self.claude_compaction_helper = ClaudeCompactionHelper(
                claude_compaction_model=claude_compaction_model,
                api_base=compaction_api_base,
            )

        # call original openai compaction session init
        super().__init__(**kwargs)

        # store the compaction model name for later use
        self.compaction_model_name = (
            claude_compaction_model if use_claude_compaction else self.model
        )

    async def run_compaction(
        self, args: OpenAIResponsesCompactionArgs | None = None
    ) -> None:
        """Run compaction using responses.compact API."""

        # Updated to AgentsSDK v05/05/2026 (f84ef7f649d1bb3ba7ca86e41509081e7e779bc6)
        # https://openai.github.io/openai-agents-python
        # updates: https://github.com/openai/openai-agents-python/commits/main/src/agents/memory/openai_responses_compaction_session.py

        # >>> ADDED
        with custom_span(f'Compaction ("{self.model}")', {}):
            # <<< ADDED-END

            if args and args.get("response_id"):
                self._response_id = args["response_id"]  # type: ignore
            requested_mode = args.get("compaction_mode") if args else None
            if args and "store" in args:
                store = args["store"]
                if store is False and self._response_id:
                    self._last_unstored_response_id = self._response_id
                elif (
                    store is True
                    and self._response_id == self._last_unstored_response_id
                ):
                    self._last_unstored_response_id = None
            else:
                store = None
            resolved_mode = self._resolve_compaction_mode_for_response(
                response_id=self._response_id,
                store=store,
                requested_mode=requested_mode,
            )

            if resolved_mode == "previous_response_id" and not self._response_id:
                raise ValueError(
                    "OpenAIResponsesCompactionSession.run_compaction requires a response_id "
                    "when using previous_response_id compaction."
                )

            (
                compaction_candidate_items,
                session_items,
            ) = await self._ensure_compaction_candidates()

            force = args.get("force", False) if args else False
            should_compact = force or self.should_trigger_compaction(
                {
                    "response_id": self._response_id,
                    "compaction_mode": resolved_mode,
                    "compaction_candidate_items": compaction_candidate_items,
                    "session_items": session_items,
                }
            )

            if not should_compact:
                # logger.debug(
                #     f"skip: decision hook declined compaction for {self._response_id}"
                # )
                return

            self._deferred_response_id = None
            logger.debug(
                f"compact: start for {self._response_id} using {self.compaction_model_name} (mode={resolved_mode})"
            )
            compact_kwargs: dict[str, Any] = {"model": self.compaction_model_name}
            if resolved_mode == "previous_response_id":
                compact_kwargs["previous_response_id"] = self._response_id
            else:
                compact_kwargs["input"] = session_items

            # <<< ADDED
            output_items = None  # type: ignore
            # try to get output_items from cache
            cache_path, stable_payload = self._get_cache_path()
            if cache_path.exists():
                logger.debug(
                    f"Retrieving compaction from cache for response_id: {self._response_id} (model: {self.compaction_model_name})"
                )
                cached = utils.load_pickle(cache_path, CompactCacheType)
                if cached is not None:
                    output_items = cached.output_items
                    assert cached.response_id == self._response_id

                    if self.runtime_tracker is not None:
                        self.runtime_tracker.add_skipped_time(cached.runtime_seconds)

            # fallback if not successfully loaded from cache
            if output_items is None:
                start_time = time.perf_counter()

                if self.use_claude_compaction:
                    assert self.claude_compaction_helper is not None, (
                        "claude_compaction_helper must be initialized when use_claude_compaction is True"
                    )
                    output_items = (
                        await self.claude_compaction_helper.compact_with_claude(
                            session_items
                        )
                    )
                else:
                    # <<< original
                    compacted = await self.client.responses.compact(**compact_kwargs)

                    # >>> added log
                    logger.debug(
                        f"Running compaction. Model: {self.compaction_model_name} for response_id: {self._response_id}"
                    )
                    # <<< added log end
                    output_items = _normalize_compaction_output_items(
                        compacted.output or []
                    )
                    # >>> original end

                # write to cache
                if cache_path is not None and not self.do_not_cache:
                    utils.dump_pickle(
                        cache_path,
                        CompactCacheType(
                            self._response_id,
                            output_items,
                            hash_payload=stable_payload,
                            runtime_seconds=time.perf_counter() - start_time,
                        ),
                        do_not_cache=self.do_not_cache,
                    )
            # <<< ADDED

            # <<< MOVED DOWN
            await self.underlying_session.clear_session()
            # >>> MOVED DOWN-END

            output_items = _strip_orphaned_assistant_ids(output_items)

            if output_items:
                await self.underlying_session.add_items(output_items)

            # ADDED EXCEPTION:
            else:
                raise Exception(
                    f"Compaction returned no output items for response_id {self._response_id} - cannot proceed with empty session"
                )
            # <<< EXCEPTION

            self._compaction_candidate_items = select_compaction_candidate_items(
                output_items
            )
            self._session_items = output_items

            logger.debug(
                f"compact: done for {self._response_id} "
                f"(mode={resolved_mode}, output={len(output_items)}, "
                f"candidates={len(self._compaction_candidate_items)})"
            )
            # >>> ADDED

            if self.run_stats_collector is not None:
                # log compaction stats
                self.run_stats_collector.log_metrics_callback(
                    {
                        "type": "compaction",
                        "compaction/output_items": len(output_items),
                        "compaction/candidate_items": len(
                            self._compaction_candidate_items
                        ),
                    },
                    log_and_increment=True,
                )
            # <<< ADDED-END

    def _get_cache_path(self) -> Tuple[Path, str]:
        payload = {
            "response_id": self._response_id,
            "model": str(self.compaction_model_name),
        }
        stable_payload = utils.stable_json(payload)

        hash = utils.sha256(stable_payload)
        return self._cache_path_for(hash), stable_payload

    def _cache_path_for(self, hash: str) -> Path:
        return self.cache_dir / f"{hash}.pkl"
