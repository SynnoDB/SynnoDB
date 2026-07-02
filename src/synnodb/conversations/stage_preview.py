"""Best-effort previews of a conversation's not-yet-executed prompt stages.

The live dashboard's prompts pane reconstructs the stages that have already run
from emitted metric rows. To also surface the *scheduled* stages, we walk the
conversation's declarative stage list - known in full the moment
``Conversation.build_items`` returns - and render a lightweight preview of every
prompt-bearing stage: its descriptor and, where the prompt can be rendered ahead
of time, the prompt text with the values that are only known at runtime left as
explicit placeholders.

Only prompt-bearing stages are previewed (``PromptStage``, ``DynamicStageConfig``
and the ``PerQueryLoop`` composite): they are the items that surface as sections
in the pane. Marker/control items (compaction, benchmark, validation toggles,
supervision horizons) and the silent composites (``AssertCorrect`` /
``MeasureBaselines``) run no LLM turn and so never appear as a section.
"""

from __future__ import annotations

import logging

from synnodb.conversations.stage_items import (
    DynamicStageConfig,
    PerQueryLoop,
    PromptStage,
    StageItem,
)

logger = logging.getLogger(__name__)

# Substituted for the tracing/profiling blob a stage prompt closes over, so a
# scheduled prompt reads as a template with its live data clearly marked as
# not-yet-known instead of silently rendering as an empty or stale value.
TRACING_PLACEHOLDER = "«tracing / profiling output - collected when this stage runs»"

# The previous-implementation runtime handed to the prompt builders. Only a few
# builders interpolate it (and only for some models); 0 keeps the arithmetic
# they perform on it (``int()``, division) well-defined while rendering.
_RT_PLACEHOLDER_MS = 0.0


def _render_prompt_stage_preview(stage: PromptStage) -> tuple[str | None, bool]:
    """Render a :class:`PromptStage`'s prompt ahead of execution.

    Returns ``(preview_text, has_runtime_placeholder)``. ``preview_text`` is
    None when the prompt cannot be rendered without running the stage.
    ``has_runtime_placeholder`` is True when the rendered text embeds a value
    that is only truly known at execution time (the tracing/profiling blob).
    """
    try:
        if stage.get_prompt is not None:
            return stage.get_prompt(stage.exec_settings, _RT_PLACEHOLDER_MS), False
        if stage.get_prompt_with_tracing is not None:
            text = stage.get_prompt_with_tracing(
                stage.exec_settings, _RT_PLACEHOLDER_MS, TRACING_PLACEHOLDER
            )
            return text, True
    except Exception as exc:  # noqa: BLE001 - a preview must never break a run
        logger.debug("Could not render preview for stage %r: %s", stage.descriptor, exc)
    return None, False


def build_stage_previews(items: list[StageItem]) -> list[dict]:
    """Return an ordered preview entry for every prompt-bearing stage in items.

    Each entry is a JSON-serializable dict with ``descriptor``,
    ``prompt_preview`` (str or None), ``has_runtime_placeholder`` and
    ``dynamic`` (the prompt is generated at runtime and cannot be previewed).
    """
    previews: list[dict] = []
    for item in items:
        if isinstance(item, PromptStage):
            preview, has_placeholder = _render_prompt_stage_preview(item)
            previews.append(
                {
                    "descriptor": item.descriptor,
                    "prompt_preview": preview,
                    "has_runtime_placeholder": has_placeholder,
                    "dynamic": False,
                }
            )
        elif isinstance(item, (PerQueryLoop, DynamicStageConfig)):
            # The concrete prompts of these stages are decided while they run
            # (per query, or iteratively), so only the descriptor is known now.
            previews.append(
                {
                    "descriptor": item.descriptor,
                    "prompt_preview": None,
                    "has_runtime_placeholder": True,
                    "dynamic": True,
                }
            )
    return previews
