from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Callable, Optional

from workloads.workload_provider import ExecSettings


@dataclass
class StageConfig(ABC):
    """Abstract base class for all stage configurations."""

    descriptor: Optional[str] = None  # human-readable stage description for the LLM
    max_turns: Optional[int] = None  # max turns for this stage (None = no limit)

    def __post_init__(self):
        if self.descriptor is None:
            raise ValueError("descriptor is required")


@dataclass
class StaticStageConfig(StageConfig):
    """Stage configuration with a static prompt that is always executed as-is."""

    # Called just before the stage runs to build the prompt. Receives the stage's
    # exec settings (sourced from the workload provider's query batch, or None) and
    # the previous impl runtime in ms. Returns the full prompt string for this stage.
    get_prompt: Optional[Callable[[ExecSettings | None, float], str]] = None
    # Same as get_prompt, but additionally receives the tracing data string.
    get_prompt_with_tracing: Optional[
        Callable[[ExecSettings | None, float, str], str]
    ] = None

    def __post_init__(self):
        if self.get_prompt is None and self.get_prompt_with_tracing is None:
            raise ValueError("get_prompt is required")

    measure_performance_after_stage: bool = (
        True  # whether to measure performance immediately after this stage
    )
    measure_perf_qid: Optional[str] = (
        None  # which query to measure after this stage (None = use conversation's query_ids[0])
    )
    auto_revert_on_regression: bool = True  # automatically revert if no improvement (requires measure_performance_after_stage)
    feedback_on_incorrect: bool = False  # retry loop if implementation is incorrect (requires measure_performance_after_stage)
    throw_exception_on_incorrect: bool = False  # raise if incorrect after stage (requires measure_performance_after_stage)
    post_stage_validate: Optional[Callable[[], Optional[str]]] = (
        None  # called after stage; return None if valid, or a feedback string for the LLM
    )
    exec_settings: Optional[ExecSettings] = (
        None  # exec settings to use for providing current runtime statistics to the prompt
    )


class DynamicStageConfig(StageConfig, ABC):
    """Abstract base for stages with custom execution logic.

    Subclasses implement ``should_run`` and ``get_prompts`` to control whether
    the stage executes and which prompt(s) are sent to the LLM.  A single
    concrete step must be its own class that inherits from ``DynamicStageConfig``.
    """

    @abstractmethod
    def next_prompt(self) -> str:
        """Iteratively return the next prompt for the LLM.

        Returning None if nothing more to execute for this stage.
        """
        ...
