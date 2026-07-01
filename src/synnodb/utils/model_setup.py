import json
import logging
import os
from typing import Any, Optional

import litellm
from openai import AsyncOpenAI

logger = logging.getLogger(__name__)


def resolve_model_extra_body(
    explicit: "str | dict[str, Any] | None",
) -> dict[str, Any] | None:
    """Resolve a host-independent ``extra_body`` object to merge into every request."""
    if isinstance(explicit, dict):
        return explicit
    raw = explicit if explicit is not None else os.environ.get("MODEL_EXTRA_BODY")
    if not raw or not raw.strip():
        return None
    try:
        parsed = json.loads(raw)
    except ValueError as exc:
        raise ValueError(
            f"model_extra_body must be a JSON object, got invalid JSON: {raw!r}"
        ) from exc
    if not isinstance(parsed, dict):
        raise ValueError(
            f"model_extra_body must be a JSON object, got {type(parsed).__name__}: {raw!r}"
        )
    return parsed


# Silence LiteLLM's raw stdout print ("Provider List: https://...")
litellm.suppress_debug_info = True


def _model_is_anthropic(model_name: str) -> bool:
    """Whether the model is served by Anthropic (and thus needs ANTHROPIC_API_KEY).

    Mirrors the check in ClaudeCompactionHelper: a LiteLLM ``anthropic/...`` model
    or a bare ``claude-...`` model. These are the cases where both the main model
    call and Claude compaction read ANTHROPIC_API_KEY.
    """
    return model_name.startswith("anthropic/") or model_name.startswith("claude-")


def _model_is_openrouter(model_name: str) -> bool:
    """Whether the model is served via OpenRouter (and thus needs OPENROUTER_API_KEY).

    A LiteLLM ``openrouter/...`` model, e.g. ``openrouter/z-ai/glm5.2``.
    """
    return model_name.startswith("openrouter/")


def validate_model_credentials(model_name: str) -> None:
    """Fail fast — and with an actionable message — when the API key the model
    needs is missing.

    Called early (at SynnoDB construction) so the user learns about a missing key
    up front, not several stages later when compaction first fires. The previous
    behaviour raised only inside the compaction helper with a message that both
    blamed "compaction" (misleading — the key is needed for every model call) and
    omitted how to fix it.
    """
    if _model_is_anthropic(model_name) and not os.environ.get("ANTHROPIC_API_KEY"):
        raise ValueError(
            f"ANTHROPIC_API_KEY is not set, but the model {model_name!r} needs it. "
            "Add it to a .env file in your project root "
            "(a line `ANTHROPIC_API_KEY=sk-ant-...`) or export it in your shell "
            "before running."
        )
    if _model_is_openrouter(model_name) and not os.environ.get("OPENROUTER_API_KEY"):
        raise ValueError(
            f"OPENROUTER_API_KEY is not set, but the model {model_name!r} needs it. "
            "Add it to a .env file in your project root "
            "(a line `OPENROUTER_API_KEY=sk-or-...`) or export it in your shell "
            "before running."
        )


def setup_model_config(
    model_arg: str,
    api_base_override: str | None = None,
) -> tuple[bool, str, str | None, Optional[AsyncOpenAI], str | None]:
    model_name = model_arg

    use_litellm = not model_name.startswith("gpt-")
    if use_litellm:
        # ensure the correct syntax
        assert "/" in model_name, (
            "Litellm model names must be prefixed with the provider, e.g. 'anthropic/claude-sonnet-4-6' or 'openai/unsloth/MiniMax-M2.5'"
        )
        provider, _ = model_name.split("/", 1)

        logger.info(f"Using LiteLLM model: {model_name} (provider: {provider})")
        if provider == "openrouter":
            # OpenRouter has its own key; LiteLLM routes to https://openrouter.ai/api/v1
            api_key = os.environ.get("OPENROUTER_API_KEY")
        else:
            api_key = (
                os.environ.get("LITELLM_API_KEY")
                or os.environ.get("ANTHROPIC_API_KEY")
                or os.environ.get("OPENAI_API_KEY")
                or "dummy"  # local llm
            )
        api_base = (
            api_base_override  # CLI --api_base takes priority
            or os.environ.get("LLM_API_BASE")  # generic name for local/custom endpoints
            or os.environ.get("OPENAI_API_BASE")  # also read by litellm internally
            or os.environ.get("LITELLM_API_BASE")
        )
        # Default to DGX local model endpoint for non-cloud providers (llama is listening on all interfaces not just localhost)
        if not api_base and provider not in (
            "anthropic",
            "azure",
            "bedrock",
            "vertex_ai",
            "openrouter",
        ):
            api_base = "http://dgx02:13506/v1"
            logger.info(
                f"No LLM_API_BASE set, defaulting to local model endpoint: {api_base}"
            )
        openai_client = None
    else:
        openai_api_key = os.environ.get("OPENAI_API_KEY")
        assert model_name.startswith("gpt-"), (
            "Only gpt- models with OpenAI responses model. If you want to use other models via litellm wrapper, please prefix the model name with 'litellm/'."
        )
        api_key = openai_api_key
        api_base = None
        openai_client = AsyncOpenAI(api_key=openai_api_key)
    return use_litellm, model_name, api_key, openai_client, api_base
