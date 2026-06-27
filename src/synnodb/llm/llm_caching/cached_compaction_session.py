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
        self,
        args: OpenAIResponsesCompactionArgs | None = None,
        *,
        reanchor: bool = True,
    ) -> None:
        """Run compaction using responses.compact API.

        `reanchor` controls whether the active stage prompt is reinserted into the
        post-compaction output so the agent does not drift after its history is
        summarized away. It defaults to True because the SDK's proactive/near-limit
        compaction calls this method directly (with no caller to re-issue the task).
        Our own wrapper passes reanchor=False on the <<COMPACTION>> marker and the
        reactive context-overflow retry, where the caller re-issues the prompt
        itself.

        Reinsertion happens only on the local/claude path; OpenAI server-side
        compaction ignores it. The reinserted text is read from ambient state
        (run_stats_collector.current_stage_prompt), since the SDK-triggered path
        cannot pass it as an argument."""

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
            served_from_cache = False

            # Decide whether to reinsert the active stage prompt into the compacted
            # output. Only the proactive/SDK path (reanchor=True) does this, and only
            # on the local/claude path; the prompt is read from ambient state because
            # the SDK-triggered call cannot pass it. Caller-initiated compactions
            # (reanchor=False) re-issue the task themselves, so they reinsert nothing.
            stage_prompt_for_reanchor = (
                self.run_stats_collector.current_stage_prompt
                if self.run_stats_collector is not None
                else None
            )
            prompt_to_reinsert_after_compaction = (
                stage_prompt_for_reanchor
                if (
                    reanchor
                    and self.use_claude_compaction
                    and stage_prompt_for_reanchor
                )
                else None
            )

            # try to get output_items from cache. The key is content-addressed
            # (transcript + mode + reinserted prompt): litellm/local models report
            # response_id=None, so a {response_id, model}-only key collapses every
            # compaction onto ONE stale entry. See _get_cache_path.
            cache_path, stable_payload = self._get_cache_path(
                session_items=session_items,
                resolved_mode=resolved_mode,
                reanchor_prompt=prompt_to_reinsert_after_compaction,
            )
            if not self.do_not_cache and cache_path.exists():
                logger.debug(
                    f"Retrieving compaction from cache for response_id: {self._response_id} (model: {self.compaction_model_name})"
                )
                cached = utils.load_pickle(cache_path, CompactCacheType)
                if cached is not None:
                    output_items = cached.output_items
                    served_from_cache = True

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
                            session_items,
                            resume_prompt=prompt_to_reinsert_after_compaction,
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

            output_items = _strip_orphaned_assistant_ids(output_items)

            # Validate BEFORE clearing: a degenerate (empty) compaction must not
            # wipe the live session and leave the caller running on empty history.
            if not output_items:
                raise Exception(
                    f"Compaction returned no output items for response_id "
                    f"{self._response_id} - refusing to clear the session."
                )

            await self.underlying_session.clear_session()
            await self.underlying_session.add_items(output_items)

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
                # Make the compaction visible in the per-stage debug.log: without
                # this a compaction leaves an unexplained gap between turns and
                # you cannot tell what (if anything) was re-anchored.
                debug_logger = self.run_stats_collector.debug_logger
                if debug_logger is not None:
                    # reanchor=True is the proactive/SDK-initiated path; reanchor=
                    # False is a caller-initiated compaction (marker / reactive).
                    compaction_kind = "PROACTIVE" if reanchor else "CALLER"
                    cache_source = "cache HIT" if served_from_cache else "fresh"
                    stage_descriptor = (
                        self.run_stats_collector.current_prompt_descriptor or "<none>"
                    )
                    produced_text = "\n\n".join(
                        str(it.get("content", it)) if isinstance(it, dict) else str(it)
                        for it in output_items
                    )
                    debug_logger.log_event(
                        f"{compaction_kind} COMPACTION (stage={stage_descriptor}, "
                        f"mode={resolved_mode}, {cache_source}, "
                        f"output_items={len(output_items)})",
                        f"--- Compaction produced ({len(output_items)} item(s)) ---\n"
                        f"{produced_text}\n\n"
                        "--- Reinserted task prompt ---\n"
                        + (
                            prompt_to_reinsert_after_compaction
                            or "<none - reinsert only on proactive near-limit compaction>"
                        ),
                    )
            # <<< ADDED-END

    def _get_cache_path(
        self,
        session_items: Any,
        resolved_mode: str | None,
        reanchor_prompt: str | None = None,
    ) -> Tuple[Path, str]:
        # Content-address the compaction. response_id alone is NOT enough: for
        # litellm/local models it is always None, so the key would be constant
        # per model and every compaction (within a run and across runs) would
        # reuse one stale entry. Hash the transcript being compacted, the mode,
        # and the reinserted prompt (which is embedded into the output) so each
        # distinct compaction point maps to its own entry - while replay of the
        # same conversation still reproduces identical keys (deterministic).
        try:
            session_digest = utils.sha256(utils.stable_json(session_items))
        except (TypeError, ValueError):
            # Non-JSON-serialisable items: fall back to a repr-based digest. repr
            # can vary across processes (object addresses), so this degrades to
            # "always recompute", never a wrong hit.
            session_digest = utils.sha256(repr(session_items))
        payload = {
            "response_id": self._response_id,
            "model": str(self.compaction_model_name),
            "mode": resolved_mode,
            "session_digest": session_digest,
            "reanchor_prompt": reanchor_prompt,
        }
        stable_payload = utils.stable_json(payload)

        cache_key_hash = utils.sha256(stable_payload)
        return self._cache_path_for(cache_key_hash), stable_payload

    def _cache_path_for(self, hash: str) -> Path:
        return self.cache_dir / f"{hash}.pkl"
