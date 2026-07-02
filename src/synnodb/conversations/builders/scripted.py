"""Stage list of the manual `scripted` conversation.

Re-expresses the legacy ScriptedConversation as a plan builder: the JSON prompt
array persisted at the conversation path becomes a list of PromptStages
(compaction markers map to Compact items). New prompts can be appended through
the plan's finish_interactive loop.
"""

import json
import logging

from synnodb.conversations.conv_context import ConvContext
from synnodb.conversations.stage_items import (
    COMPACTION_MARKER,
    Compact,
    PromptStage,
    StageItem,
)

logger = logging.getLogger(__name__)


def build(ctx: ConvContext) -> list[StageItem]:
    path = ctx.conversation_json_path
    if path is None or not path.exists():
        return []

    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list) or not all(isinstance(x, str) for x in data):
        raise ValueError("JSON file must contain an array of strings")

    items: list[StageItem] = []
    for prompt in data:
        if prompt == COMPACTION_MARKER:
            items.append(Compact())
            continue
        items.append(
            PromptStage(
                descriptor=prompt[:20],
                get_prompt=lambda _exec_settings, _rt, prompt=prompt: prompt,
                measure_performance_after_stage=False,
                auto_revert_on_regression=False,
            )
        )
    return items
