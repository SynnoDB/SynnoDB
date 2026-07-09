"""The predefined ConversationPlans of the built-in pipeline stages.

Each built-in stage is an ordinary :class:`~synnodb.plan.ConversationPlan`
assembled from the same primitives available to user-defined plans - there is
no separate dispatch path. The ergonomic ``SynnoDB.createBaseImpl(...)``
methods construct these plans (baking in their per-call inputs) and hand them
to ``SynnoDB.run_synthesis``.
"""

from __future__ import annotations

import functools

from synnodb.conversations.examples import add_mt as _mt_builder
from synnodb.conversations.examples import base_impl as _base_impl_builder
from synnodb.conversations.examples import check_sf as _check_sf_builder
from synnodb.conversations.examples import optim as _optim_builder
from synnodb.conversations.examples import storage_plan as _storage_plan_builder
from synnodb.cpp_runner.prepare_repo.prepare_features import (
    Parallelism,
    PrepareFeatures,
)
from synnodb.plan import ConversationPlan, SupervisionPolicy
from synnodb.results import (
    build_base_impl,
    build_multithreaded,
    build_optimized,
    build_storage_plan,
    make_correctness_builder,
)


def storage_plan_plan() -> ConversationPlan:
    return ConversationPlan(
        name="createStoragePlan",
        prepare=PrepareFeatures.storage_plan(),
        stages=_storage_plan_builder.build,
        # A single document-writing stage with nothing to measure or revert;
        # there is no outcome for a supervisor to review.
        supervision=SupervisionPolicy.OFF,
        result=build_storage_plan,
    )


def base_impl_plan(storage_plan_text: str | None = None) -> ConversationPlan:
    return ConversationPlan(
        name="createBaseImpl",
        prepare=PrepareFeatures.base(storage_plan_text=storage_plan_text),
        stages=_base_impl_builder.build,
        supervision=SupervisionPolicy.STRICT,
        result=build_base_impl,
    )


def optim_plan(plan_source: str = "duckdb") -> ConversationPlan:
    return ConversationPlan(
        name="runOptimLoop",
        prepare=PrepareFeatures.optim(),
        stages=functools.partial(_optim_builder.build, plan_source=plan_source),
        parallelism=Parallelism.MULTI_THREADED,
        supervision=SupervisionPolicy.RELAXED,
        finish_interactive=True,
        offer_trace_option=True,  # collect fine-grained perf traces
        result=build_optimized,
    )


def mt_plan() -> ConversationPlan:
    return ConversationPlan(
        name="addMultiThreading",
        prepare=PrepareFeatures.mt(),
        stages=_mt_builder.build,
        parallelism=Parallelism.MULTI_THREADED,
        supervision=SupervisionPolicy.RELAXED,
        finish_interactive=True,
        offer_trace_option=True,
        result=build_multithreaded,
    )


def check_sf_plan(target_sf: float) -> ConversationPlan:
    return ConversationPlan(
        name="checkSfCorrectness",
        # replay the prepare record of the source snapshot; the run's
        # parallelism is the source run's recorded parallelism
        prepare=None,
        stages=functools.partial(_check_sf_builder.build, target_sf=target_sf),
        supervision=SupervisionPolicy.STRICT,
        finish_interactive=True,
        result=make_correctness_builder(target_sf),
    )
