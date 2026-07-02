import logging
from typing import Tuple

MODELS = {
    "openai/unsloth/MiniMax-M3": {
        "input": 0.0,
        "cached_input": 0.0,
        "output": 0.0,
        "context_window": 376_832,  # from /models endpoint: n_ctx_train
    },
    "openai/unsloth/MiniMax-M2.5": {
        "input": 0.0,
        "cached_input": 0.0,
        "output": 0.0,
        "context_window": 196_608,  # from /models endpoint: n_ctx_train
    },
    "openai/unsloth/MiniMax-M2.5-full": {
        "input": 0.0,
        "cached_input": 0.0,
        "output": 0.0,
        "context_window": 196_608,  # from /models endpoint: n_ctx_train
    },
    "openai/unsloth/GLM-5": {
        "input": 0.0,
        "cached_input": 0.0,
        "output": 0.0,
        "context_window": 202_752,  # from /models endpoint: n_ctx_train
    },
    "openai/unsloth/GLM-5.1": {  # currently the Q4 version due do KV size.
        "input": 0.0,
        "cached_input": 0.0,
        "output": 0.0,
        "context_window": 202_752,  # from /models endpoint: n_ctx_train
    },
    "openai/unsloth/Kimi-K2.6": {
        "input": 0.0,
        "cached_input": 0.0,
        "output": 0.0,
        "context_window": 202_752,  # from /models endpoint: n_ctx_train
    },
    "openai/unsloth/gemma-4-31B-it": {
        "input": 0.0,
        "cached_input": 0.0,
        "output": 0.0,
        "context_window": 262_144,  # from /models endpoint: n_ctx_train
    },
    "openai/unsloth/Qwen3.5-397B-A17B": {
        "input": 0.0,
        "cached_input": 0.0,
        "output": 0.0,
        "context_window": 262_144,  # from /models endpoint: n_ctx_train
    },
    # https://developers.openai.com/api/docs/models
    "gpt-5.1": {
        "input": 1.25 / 1_000_000,  # USD pro Token
        "cached_input": 0.125 / 1_000_000,  # USD pro Token
        "output": 10.00 / 1_000_000,  # USD pro Token
        "context_window": 400_000,
    },
    "gpt-5.1-codex": {
        "input": 1.25 / 1_000_000,  # USD pro Token
        "cached_input": 0.125 / 1_000_000,  # USD pro Token
        "output": 10.00 / 1_000_000,  # USD pro Token
        "context_window": 400_000,
    },
    "gpt-5.1-codex-max": {
        "input": 1.25 / 1_000_000,  # USD pro Token
        "cached_input": 0.125 / 1_000_000,  # USD pro Token
        "output": 10.00 / 1_000_000,  # USD pro Token
        "context_window": 400_000,
    },
    "gpt-5.2-codex": {
        "input": 1.75 / 1_000_000,  # USD pro Token
        "cached_input": 0.17 / 1_000_000,  # USD pro Token
        "output": 14.00 / 1_000_000,  # USD pro Token
        "context_window": 400_000,
    },
    "gpt-5.3-codex": {
        "input": 1.75 / 1_000_000,  # USD pro Token
        "cached_input": 0.175 / 1_000_000,  # USD pro Token
        "output": 14.00 / 1_000_000,  # USD pro Token
        "context_window": 400_000,
    },
    "gpt-5.4": {
        "input": 2.5 / 1_000_000,  # USD pro Token
        "cached_input": 0.25 / 1_000_000,  # USD pro Token
        "output": 15.00 / 1_000_000,  # USD pro Token
        "context_window": 1_050_000,
    },
    # https://openrouter.ai/z-ai/glm-5.2
    "openrouter/z-ai/glm-5.2": {
        "input": 1.2 / 1_000_000,  # USD pro Token
        "cached_input": 0.2 / 1_000_000,  # USD pro Token
        "output": 4.10 / 1_000_000,  # USD pro Token
        "context_window": 1_000_000,
    },
    "anthropic/claude-opus-4-20250514": {
        "input": 15.00 / 1_000_000,
        "cached_input": 1.50 / 1_000_000,
        "output": 75.00 / 1_000_000,
        "context_window": 200_000,
    },
    "anthropic/claude-opus-4-1-20250514": {
        "input": 15.00 / 1_000_000,
        "cached_input": 1.50 / 1_000_000,
        "output": 75.00 / 1_000_000,
        "context_window": 200_000,
    },
    "anthropic/claude-opus-4-5-20250514": {
        "input": 5.00 / 1_000_000,
        "cached_input": 0.50 / 1_000_000,
        "output": 25.00 / 1_000_000,
        "context_window": 200_000,
    },
    "anthropic/claude-opus-4-6-20250514": {
        "input": 5.00 / 1_000_000,
        "cached_input": 0.50 / 1_000_000,
        "output": 25.00 / 1_000_000,
        "context_window": 200_000,
    },
    "anthropic/claude-opus-4": {
        "input": 15.00 / 1_000_000,
        "cached_input": 1.50 / 1_000_000,
        "output": 75.00 / 1_000_000,
        "context_window": 200_000,
    },
    "anthropic/claude-opus-4-1": {
        "input": 15.00 / 1_000_000,
        "cached_input": 1.50 / 1_000_000,
        "output": 75.00 / 1_000_000,
        "context_window": 200_000,
    },
    "anthropic/claude-opus-4-5": {
        "input": 5.00 / 1_000_000,
        "cached_input": 0.50 / 1_000_000,
        "output": 25.00 / 1_000_000,
        "context_window": 200_000,
    },
    "anthropic/claude-opus-4-6": {
        "input": 5.00 / 1_000_000,
        "cached_input": 0.50 / 1_000_000,
        "output": 25.00 / 1_000_000,
        "context_window": 200_000,
    },
    "anthropic/claude-sonnet-4-20250514": {
        "input": 3.00 / 1_000_000,
        "cached_input": 0.30 / 1_000_000,
        "output": 15.00 / 1_000_000,
        "context_window": 200_000,
    },
    "anthropic/claude-sonnet-4-5-20250514": {
        "input": 3.00 / 1_000_000,
        "cached_input": 0.30 / 1_000_000,
        "output": 15.00 / 1_000_000,
        "context_window": 200_000,
    },
    "anthropic/claude-sonnet-4-20250514-5": {
        "input": 3.00 / 1_000_000,
        "cached_input": 0.30 / 1_000_000,
        "output": 15.00 / 1_000_000,
        "context_window": 200_000,
    },
    "anthropic/claude-sonnet-4": {
        "input": 3.00 / 1_000_000,
        "cached_input": 0.30 / 1_000_000,
        "output": 15.00 / 1_000_000,
        "context_window": 200_000,
    },
    "anthropic/claude-sonnet-4-5": {
        "input": 3.00 / 1_000_000,
        "cached_input": 0.30 / 1_000_000,
        "output": 15.00 / 1_000_000,
        "context_window": 200_000,
    },
    "anthropic/claude-sonnet-4-6": {
        "input": 3.00 / 1_000_000,
        "cached_input": 0.30 / 1_000_000,
        "output": 15.00 / 1_000_000,
        "context_window": 200_000,
    },
    "anthropic/claude-sonnet-5": {
        "input": 2.00 / 1_000_000,
        "cached_input": 0.20 / 1_000_000,
        "output": 10.00 / 1_000_000,
        "context_window": 1_000_000,
    },
}

logger = logging.getLogger(__name__)


def request_cost_usd(
    model, input_tokens: int, cached_tokens: int, output_tokens: int
) -> float:
    """
    assert str(model) in MODELS, (
        f"Model {model} not found in pricing table. Known models: {list(MODELS.keys())}"
    )
    """
    if str(model) not in MODELS:
        logger.debug(
            f"Model {model} not found in pricing table. Known models: {list(MODELS.keys())}. Returning cost of 0."
        )
        return 0.0
    prices = MODELS[str(model)]

    billable_input_tokens = input_tokens - cached_tokens

    return (
        billable_input_tokens * prices["input"]
        + cached_tokens * prices["cached_input"]
        + output_tokens * prices["output"]
    )


def context_window_usage(model, used_tokens: int) -> Tuple[str, float]:
    model_info = MODELS.get(str(model))
    if model_info is None:
        logger.warning(
            f"Unknown model {model} for context window tracking, assuming 128K"
        )
        window_size = 131_072
    else:
        window_size = model_info["context_window"]

    used_pct = (used_tokens / window_size) * 100
    left_pct = 100 - used_pct

    def fmt_k(n: int) -> str:
        return f"{n / 1000:.1f}K" if n >= 1000 else str(n)

    return (
        f"{left_pct:.0f}% left ({fmt_k(used_tokens)} used / {fmt_k(window_size)})"
    ), used_pct / 100
