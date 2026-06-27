import asyncio
import inspect
import json
import logging
import time
from abc import abstractmethod
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from prompt_toolkit import PromptSession
from prompt_toolkit.filters import is_multiline
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.key_binding import KeyBindings

from synnodb.llm.sdk.sdk_wrapper import SDKWrapper
from synnodb.observability.logging.notify import send_notification
from synnodb.synth_framework.runtime_tracker import RuntimeTracker
from synnodb.utils.utils import atomic_write, create_parent_and_set_permissions

COMPACTION_MARKER = "<<COMPACTION>>"
BENCHMARK_MARKER = "<<BENCHMARK>>"
VALIDATE_ON = "<<VALIDATE_ON>>"
VALIDATE_OFF = "<<VALIDATE_OFF>>"
VALIDATE_OUTPUT_STDOUT_ON = "<<VALIDATE_OUTPUT_STDOUT_ON>>"
VALIDATE_OUTPUT_STDOUT_OFF = "<<VALIDATE_OUTPUT_STDOUT_OFF>>"
NOTIFY_AFTER_SEC = 60

# Display labels for each choice key (order is preserved in the prompt).
_CHOICE_LABELS: dict[str, str] = {
    "u": "<b>[u]</b>se",
    "r": "<b>[r]</b>eplace",
    "i": "<b>[i]</b>nsert before",
    "c": "<b>[c]</b>ompaction",
}

logger = logging.getLogger(__name__)


class AbstractConversation:
    def __init__(
        self,
        conversation_json_path: Path,  # where to persist the conversation (list of accepted prompts)
        agent_sdk_wrapper: SDKWrapper,
        callback: Callable[[str, Optional[str], int, Optional[int], bool], str],
        all_query_ids: List[str],
        replay: bool = False,
        notify: bool = False,
        auto_finish: bool = False,
        allowed_choices: Tuple[str, ...] = (
            "u",
            "r",
            "i",
            "c",
        ),  # use, replace, insert-before, compaction
        auto_u: bool = False,
        replay_cache: bool = False,
        runtime_tracker: Optional[RuntimeTracker] = None,
    ):
        self.conversation_json_path = conversation_json_path
        self.callback = callback
        self.replay = replay
        self.notify = notify
        self.auto_finish = auto_finish
        self.allowed_choices = allowed_choices
        self.runtime_tracker = runtime_tracker
        self.agent_sdk_wrapper = agent_sdk_wrapper
        self.all_query_ids = all_query_ids

        # create cache dir if not existing
        create_parent_and_set_permissions(self.conversation_json_path)

        # create auto mode callbacks
        if auto_u:
            logger.warning(
                "Auto-U mode enabled: automatically proceeding with all prompts without asking for user confirmation. Make sure this is what you want!"
            )
            assert not replay_cache, "auto_u and replay_cache cannot both be enabled"
            assert "u" in allowed_choices, (
                "auto_u requires 'u' to be in allowed_choices"
            )
            self.get_choice = lambda: "u"
        elif replay_cache:
            # auto-approve if last LLM response was cached, otherwise ask user (same as auto_u but only for cached responses - executes only the cached prompts and the first non-cached prompt, then stops and waits for user input for the rest)
            self.get_choice = lambda: (
                "u" if self.agent_sdk_wrapper.last_llm_call_was_cached() else None
            )
        else:
            self.get_choice = None

        self._session = self._create_session()

        # for type hinting clarity - will be initialized in run()
        self.used: Optional[List[str]] = None

    @abstractmethod
    async def run(self) -> Optional[List[str]]:
        pass

    # ---------- interaction ----------

    async def process_prompt(
        self,
        prompt: str,
        prompt_descriptor: Optional[
            str
        ] = None,  # short description of the prompt, used for logging and callbacks
        max_turns: Optional[int] = None,
        additional_out_str: Optional[str] = None,
    ) -> Tuple[str, str, str | None]:
        """
        Handle one interaction round for `prompt`.

        Resolves the user choice by consulting `self.get_choice` first (set by
        auto_u / replay_cache modes), then falling back to interactive input.
        Executes the chosen action, appends to `used`, and persists via `_save`.

        Returns
        -------
        ("advance", last_output)  – caller should move to the next prompt
        ("stay",    last_output)  – caller should re-show the same prompt
                                    (insert-before and compaction cases)
        """

        # Show the prompt before asking for the choice, so user can see what they're acting on while deciding.
        self._show_prompt(prompt, additional_out_str)

        choice = self.get_choice() if self.get_choice else None
        if choice is None:
            t1 = time.time()
            choice = await self._ask_choice(prompt)
            if self.runtime_tracker is not None:
                self.runtime_tracker.add_wait_time(
                    time.time() - t1
                )  # if user took 30s to respond, add 30s to wait time so that it's not counted in the agent's runtime

        assert self.used is not None, (
            "self.used should have been initialized in run() by children class by now"
        )

        last_output = None
        if choice == "u":
            self.used.append(prompt)
            last_output = await self._maybe_await_callback(
                prompt,
                prompt_descriptor,
                len(self.used) - 1,
                max_turns,
                prompt_already_printed=True,
            )

        elif choice == "r":
            new_prompt = await self._ask_multiline("Replacement (Ctrl+D to submit)")
            if new_prompt.strip():
                self.used.append(new_prompt)
                last_output = await self._maybe_await_callback(
                    new_prompt, new_prompt[:20], len(self.used) - 1, max_turns
                )

        elif choice == "i":
            new_prompt = await self._ask_multiline(
                "Insert before (Ctrl+D to submit)",
            )
            if new_prompt.strip():
                self.used.append(new_prompt)
                self._save(self.used)  # save progress before the callback
                last_output = await self._maybe_await_callback(
                    new_prompt, new_prompt[:20], len(self.used) - 1, max_turns
                )

        elif choice == "c":
            self.used.append(COMPACTION_MARKER)
            self._save(self.used)  # save progress before the callback
            last_output = await self._maybe_await_callback(
                COMPACTION_MARKER, "compaction", len(self.used) - 1, max_turns
            )

        else:
            raise ValueError(f"Unexpected choice: {choice!r}")

        # Save progress after each accepted prompt.
        self._save(self.used)

        # return choice, last prompt, last output
        return choice, self.used[-1], last_output

    async def ask_to_finish_and_save(self) -> List[str]:
        assert self.used is not None
        if not self.auto_finish:
            logger.info(
                "\nAdd new prompts (Ctrl+D to submit, empty submits nothing and finishes):"
            )
            while True:
                text = await self._ask_multiline("> ")
                if not text.strip():
                    break
                self.used.append(text)
                self._save(self.used)
                await self._maybe_await_callback(text, text[:20], len(self.used) - 1)

            self._save(self.used)

        return self.used

    # ---------- persistence ----------

    def _save(self, prompts: List[str]) -> None:
        atomic_write(
            path=self.conversation_json_path,
            data=(json.dumps(prompts, ensure_ascii=False, indent=2) + "\n").encode(
                "utf-8"
            ),
        )

    # ---------- UI ----------

    def _create_session(self) -> PromptSession:
        kb = KeyBindings()

        @kb.add("c-d", filter=is_multiline)
        def _(event):
            event.app.current_buffer.validate_and_handle()

        return PromptSession(key_bindings=kb)

    def _show_prompt(self, prompt: str, additional_info: Optional[str] = None) -> None:
        logger.info(
            "=" * 20
            + f" Prompt {additional_info if additional_info is not None else ''}"
            + "=" * 20
        )
        logger.info(prompt)
        logger.info("=" * 60)

    async def _ask_choice(self, prompt: str) -> str:
        labels = " / ".join(
            _CHOICE_LABELS[c] for c in self.allowed_choices if c in _CHOICE_LABELS
        )
        prompt_text = HTML(f"{labels} ? ")

        notified = False
        notify_msg = (
            f"**LLM requires action on prompt:**\n```quote\n{prompt[:1000]}\n```"
        )

        while True:
            if not notified and self.notify:
                send_notification(notify_msg, check_tmux=True)

            prompt_task = asyncio.create_task(self._session.prompt_async(prompt_text))

            while True:
                try:
                    raw = await asyncio.wait_for(
                        asyncio.shield(prompt_task),
                        timeout=NOTIFY_AFTER_SEC,
                    )
                except asyncio.TimeoutError:
                    if self.notify and not notified:
                        send_notification(notify_msg, check_tmux=False)
                        notified = True
                    continue

                choice = (raw or "").strip().lower()
                if choice in self.allowed_choices:
                    return choice

                # invalid input: restart a fresh prompt
                notified = False
                break

    async def _ask_multiline(self, label: str) -> str:
        text = await self._session.prompt_async(
            HTML(f"<b>{label}</b> "),
            multiline=True,
        )
        return text.strip()

    async def _maybe_await_callback(
        self,
        prompt: str,
        prompt_descriptor: Optional[str],  # short description of the prompt
        index: int,
        max_turns: Optional[int] = None,
        prompt_already_printed: bool = False,  # whether the prompt has already been printed in the current flow (to avoid duplicate prints in some flows, e.g. use)
    ) -> str:
        res = self.callback(
            text=prompt,  # type: ignore
            short_desc=prompt_descriptor,
            idx=index,
            max_turns=max_turns,
            prompt_already_printed=prompt_already_printed,
        )
        if inspect.iscoroutine(res):
            return await res  # type: ignore
        return res
