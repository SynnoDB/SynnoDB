"""Plan catalog for the ``manual`` debug entry point in main.py.

The normal entry point is the importable :class:`~synnodb.api.SynnoDB` API,
whose named methods construct their plans directly. This module maps a stage
name onto the corresponding predefined plan so ``python main.py manual
--stage X`` can resolve one; parameterized plans take their inputs from the
parsed CLI args. The ``scripted`` conversation exists only here.
"""

import argparse

from synnodb.builtin_plans import (
    base_impl_plan,
    check_sf_plan,
    mt_plan,
    optim_plan,
    storage_plan_plan,
)
from synnodb.conversations.builders import scripted as _scripted_builder
from synnodb.cpp_runner.prepare_repo.prepare_features import PrepareFeatures
from synnodb.plan import ConversationPlan


def _scripted_plan() -> ConversationPlan:
    return ConversationPlan(
        name="scripted",
        prepare=PrepareFeatures.base(),
        stages=_scripted_builder.build,
        finish_interactive=True,
    )


def get_plan(stage_name: str, args: argparse.Namespace) -> ConversationPlan:
    if stage_name == "createStoragePlan":
        return storage_plan_plan()
    if stage_name == "createBaseImpl":
        return base_impl_plan()  # plan text resolved by main() from the config
    if stage_name == "runOptimLoop":
        return optim_plan(
            plan_source=getattr(args, "optimize_sample_plan_source", None) or "umbra"
        )
    if stage_name == "addMultiThreading":
        return mt_plan()
    if stage_name == "checkSfCorrectness":
        target_sf = getattr(args, "target_sf", None)
        assert target_sf is not None, "checkSfCorrectness requires --target_sf"
        return check_sf_plan(target_sf)
    if stage_name == "scripted":
        return _scripted_plan()
    known = [
        "createStoragePlan",
        "createBaseImpl",
        "runOptimLoop",
        "addMultiThreading",
        "checkSfCorrectness",
        "scripted",
    ]
    raise ValueError(f"Unknown stage '{stage_name}'. Known stages: {known}")
