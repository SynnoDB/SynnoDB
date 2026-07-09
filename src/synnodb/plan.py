"""The user-facing description of a conversation: :class:`ConversationPlan`.

A plan is the single, self-contained description of one synthesis run:

    plan = ConversationPlan(
        name="myTuningPass",                    # run identity: naming, logging, caching
        prepare=PrepareFeatures(tracing=True),  # what the workspace must provide
        stages=my_stages,                       # ConvContext -> list[StageItem]
    )
    result = db.run_synthesis(plan, start=base_impl)

There is no call-site overriding: variations are expressed by constructing a
different plan (``dataclasses.replace(plan, name=...)`` or a plan factory such
as ``check_sf_plan(target_sf=100)``), so a plan value always means the same
run, wherever it appears. The chain token (``start``) is deliberately not part
of the reusable plan; it is the only per-invocation argument of
``run_synthesis``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Callable

from synnodb.cpp_runner.prepare_repo.prepare_features import PrepareFeatures
from synnodb.results import ResultBuilder, build_artifact

if TYPE_CHECKING:
    from synnodb.conversations.conv_context import ConvContext
    from synnodb.conversations.stage_items import StageItem

_PLAN_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")


class SupervisionPolicy(Enum):
    """Whether (and how strictly) a supervisor agent reviews every stage outcome."""

    OFF = "off"
    STRICT = "strict"
    # relaxed: the supervisor does not insist on the runtime goal being reached
    RELAXED = "relaxed"


@dataclass(frozen=True)
class ConversationPlan:
    """A complete, reusable description of one conversation run."""

    # Run identity: feeds the conversation name, log files, the DuckDB drain,
    # the W&B stage_name tag, and the per-run debug-log category.
    name: str
    # What the prepared workspace must provide. None replays the prepare record
    # of the start snapshot (the checkSfCorrectness case).
    prepare: PrepareFeatures | None
    # Builds the declarative stage list this conversation executes.
    stages: "Callable[[ConvContext], list[StageItem]]" = field(repr=False)
    supervision: SupervisionPolicy = SupervisionPolicy.RELAXED
    # Whether the run ends with the interactive add-more-prompts loop (a no-op
    # under auto_finish).
    finish_interactive: bool = False
    # Whether the run tool offers the trace_mode flag to the agent (used by
    # conversations whose prompts consume execution traces).
    offer_trace_option: bool = False
    # Builds the typed domain artifact from the finished run's workspace.
    result: ResultBuilder = build_artifact

    def __post_init__(self) -> None:
        if not _PLAN_NAME_RE.match(self.name):
            raise ValueError(
                f"Invalid plan name {self.name!r}: must start with a letter and "
                "contain only letters, digits, '_' or '-' (it feeds run names, "
                "log files, and the W&B stage tag)."
            )
