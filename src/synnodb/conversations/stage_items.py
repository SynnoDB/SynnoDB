"""Typed items a conversation stage list is authored from.

A stage list is a flat ``list[StageItem]``. Prompt-bearing stages are
:class:`PromptStage` (a declarative prompt + measurement/revert flags) or a
:class:`DynamicStageConfig` subclass (custom prompt-generation logic). Control
steps that used to be authored as raw marker strings (``"<<COMPACTION>>"`` &
co.) are typed marker items here.

The legacy marker strings remain the wire/persistence format: the engine lowers
each marker item to its string when executing (``handle_prompt``, the
conversation JSON, and the supervision skip set are unchanged). The strings just
stop being the authoring format.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Awaitable, Callable, Literal, Optional, Union

from synnodb.workloads.workload_provider import ExecSettings

# The legacy marker strings: the wire/persistence format the typed marker items
# lower to (handle_prompt and the conversation JSON operate on these).
COMPACTION_MARKER = "<<COMPACTION>>"
BENCHMARK_MARKER = "<<BENCHMARK>>"
VALIDATE_ON = "<<VALIDATE_ON>>"
VALIDATE_OFF = "<<VALIDATE_OFF>>"
VALIDATE_OUTPUT_STDOUT_ON = "<<VALIDATE_OUTPUT_STDOUT_ON>>"
VALIDATE_OUTPUT_STDOUT_OFF = "<<VALIDATE_OUTPUT_STDOUT_OFF>>"


class StageItem:
    """Base of every entry in a conversation stage list.

    ``benchmark_sf`` is available on every item: when set, the engine applies
    ``set_benchmark_sf`` on the workload provider before executing the item and
    restores the previous value afterwards (also on exception). ``"large_check"``
    resolves to the workload's ``large_check_sf`` (falling back to the default
    benchmark SF when the workload declares none).
    """

    benchmark_sf: float | Literal["large_check"] | None = None


# --------------------------------- markers -----------------------------------
@dataclass(frozen=True)
class MarkerItem(StageItem):
    """A no-prompt control step, lowered to its legacy marker string at execution."""

    @property
    def marker(self) -> str:
        raise NotImplementedError


@dataclass(frozen=True)
class Compact(MarkerItem):
    """Compact the conversation history."""

    @property
    def marker(self) -> str:
        return COMPACTION_MARKER


@dataclass(frozen=True)
class Benchmark(MarkerItem):
    """Run a full benchmark across all queries."""

    benchmark_sf: float | Literal["large_check"] | None = None

    @property
    def marker(self) -> str:
        return BENCHMARK_MARKER


@dataclass(frozen=True)
class ValidateOn(MarkerItem):
    """Enable output parsing and validation in the run tool."""

    @property
    def marker(self) -> str:
        return VALIDATE_ON


@dataclass(frozen=True)
class ValidateOff(MarkerItem):
    """Disable output parsing and validation in the run tool."""

    @property
    def marker(self) -> str:
        return VALIDATE_OFF


@dataclass(frozen=True)
class ValidateStdoutOn(MarkerItem):
    """Include stdout/stderr in validation results."""

    @property
    def marker(self) -> str:
        return VALIDATE_OUTPUT_STDOUT_ON


@dataclass(frozen=True)
class ValidateStdoutOff(MarkerItem):
    """Exclude stdout/stderr from validation results."""

    @property
    def marker(self) -> str:
        return VALIDATE_OUTPUT_STDOUT_OFF


@dataclass(frozen=True)
class SupervisionHorizon(MarkerItem):
    """Cut the supervision agent's past/future stage visibility at this point.

    Not an executable step: the runner skips it; only the supervision agent's
    stage-overview scoping reads it.
    """

    @property
    def marker(self) -> str:
        # Import here to avoid a module cycle (supervision_agent imports items).
        from synnodb.conversations.supervision_agent import (
            SUPERVISION_STAGE_VISIBILITY_MARKER,
        )

        return SUPERVISION_STAGE_VISIBILITY_MARKER


# ------------------------------ prompt stages ---------------------------------
@dataclass
class StageConfig(StageItem, ABC):
    """Abstract base class for all prompt-bearing stage configurations."""

    descriptor: Optional[str] = None  # human-readable stage description for the LLM
    max_turns: Optional[int] = None  # max turns for this stage (None = no limit)
    benchmark_sf: float | Literal["large_check"] | None = None  # see StageItem

    def __post_init__(self):
        if self.descriptor is None:
            raise ValueError("descriptor is required")


@dataclass
class PromptStage(StageConfig):
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
    post_stage_validate: Optional[
        Union[Callable[[], Optional[str]], Callable[[], Awaitable[Optional[str]]]]
    ] = None  # called after stage (sync or async); return/await None if valid, or a feedback string for the LLM
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


# ------------------------------ composite items -------------------------------
@dataclass(frozen=True)
class AssertCorrect(StageItem):
    """Assert that the current implementation produces correct results.

    Runs the validation benchmark for ``query_ids`` (None = all queries of the
    conversation) and raises if the results are incorrect. No LLM interaction.
    """

    query_ids: tuple[str, ...] | None = None

    @property
    def descriptor(self) -> str:
        return "Assert correctness of current implementation"


@dataclass(frozen=True)
class MeasureBaselines(StageItem):
    """Measure the current per-query runtimes and store them on the conversation.

    Runs a full benchmark and stores ``{query_id: runtime_ms}`` under the
    attribute named by ``into``, so later stage prompts can close over the
    baseline (e.g. the single-threaded runtimes before the MT tuning round).
    """

    into: str = "single_threaded_rt_ms"

    @property
    def descriptor(self) -> str:
        return f"Measure per-query baseline runtimes (into {self.into})"


@dataclass
class PerQueryLoop(StageItem):
    """Per-query optimization loop: one conversation branch per query, stages
    executed stage-major across all queries.

    ``build(query_id, ctx)`` returns the ordered stage list for one query; all
    queries must yield the same number of stages. The engine executes ring by
    ring (stage 0 for every query, then stage 1, ...), measuring the runtime
    before each stage, collecting tracing data for stages that set
    ``get_prompt_with_tracing``, compacting after each stage, and running a
    full benchmark at the end of each ring (``end_of_ring_benchmark``).

    Conversation branching is fully owned by the loop, including the branch
    anchor: the SDK's ``create_branch_from_turn`` copies turns strictly before
    the branch turn (see tests/test_sdk_branch_semantics.py), so the turn at
    the branch point is sacrificed from every per-query branch. With
    ``branch_anchor=True`` the loop first emits a disposable no-op anchor turn
    so only that turn is excluded, never a meaningful one - required when the
    conversation has no disposable turn at the branch point (the
    multi-threading round); round 1 deliberately branches off its last
    pre-optimization turn instead.
    """

    build: "Callable[[str, object], list[StageItem]]"
    conversation_branching: bool = True
    end_of_ring_benchmark: bool = True
    branch_anchor: bool = False
    benchmark_sf: float | Literal["large_check"] | None = None

    @property
    def descriptor(self) -> str:
        return "Per-query optimization loop"
