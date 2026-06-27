import json
import logging
from typing import List, Optional

from conversations.conversation import AbstractConversation
from utils.utils import create_dir_and_set_permissions

logger = logging.getLogger(__name__)


class ScriptedConversation(AbstractConversation):
    """
    Loads a JSON array of prompts and iterates through them interactively.

    callback(prompt: str, index: int) -> Awaitable[Any] | Any
    """

    def __init__(
        self,
        **kwargs,
    ):
        super().__init__(
            allowed_choices=("u", "r", "i", "c"),
            **kwargs,
        )
        self.prompts: List[str] = self._load()

    async def run(self) -> Optional[List[str]]:
        if self.replay:
            for i, prompt in enumerate(self.prompts):
                await self._maybe_await_callback(
                    prompt, prompt_descriptor=prompt[:20], index=i
                )
            return

        # reset used prompts to empty and start from the beginning of the conversation
        self.used = []
        idx = 0

        while idx < len(self.prompts):
            prompt = self.prompts[idx]

            choice, executed_prompt, last_outcome = await self.process_prompt(
                prompt, additional_out_str=str(idx)
            )

            # in case of use and replace, advance to next prompt. In others stay on the same prompt.
            if choice in ["u", "r"]:
                idx += 1

        # signal this is the end of the conversation - save the used prompts
        used = await self.ask_to_finish_and_save()

        return used

    # ---------- persistence ----------

    def _load(self) -> List[str]:
        if not self.conversation_json_path.exists():
            create_dir_and_set_permissions(self.conversation_json_path.parent)
            self._save([])
            return []

        with self.conversation_json_path.open("r", encoding="utf-8") as f:
            data = json.load(f)

        if not isinstance(data, list) or not all(isinstance(x, str) for x in data):
            raise ValueError("JSON file must contain an array of strings")

        return data
