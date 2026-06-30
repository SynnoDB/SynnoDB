"""Stage catalog for the ``manual`` debug entry point in main.py.

The normal entry point is the importable :class:`~synnodb.api.SynnoDB` API, whose
stages register themselves in ``synnodb.api``'s registry. This module maps a
stage name onto the corresponding stage so ``python main.py manual --stage X``
can resolve one. The ``scripted`` conversation has no API stage, so its
descriptor is defined here.
"""

from synnodb.api import Stage, all_stages
from synnodb.conversations.conversation_spec import FrameworkContext
from synnodb.cpp_runner.prepare_repo.prepare_olap import prepare_base


def _scripted_factory(ctx: FrameworkContext):
    from synnodb.conversations.scripted_conversation import ScriptedConversation

    return ScriptedConversation(**ctx.conv_args)


def _scripted_build_config(cfg, inputs):
    raise NotImplementedError(
        "the 'scripted' conversation has no API build_config; it is only reachable "
        "via the `manual` debug entry point (which supplies args directly)."
    )


# Not a registered API stage: scripted runs only through the manual entry point,
# so build_config/result/usecases are never exercised for it.
_SCRIPTED_SPEC = Stage(
    name="scripted",
    usecases=frozenset(),
    build_config=_scripted_build_config,
    prepare=prepare_base,
    needs_parallelism=False,
    be_relaxed_supervision=False,
    factory=_scripted_factory,
)


def get_spec(stage_name: str) -> Stage:
    catalog: dict = {st.name: st for st in all_stages()}
    catalog[_SCRIPTED_SPEC.name] = _SCRIPTED_SPEC
    if stage_name not in catalog:
        raise ValueError(
            f"Unknown stage '{stage_name}'. Known stages: {sorted(catalog)}"
        )
    return catalog[stage_name]
