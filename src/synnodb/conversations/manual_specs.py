"""Stage catalog for the ``manual`` debug entry point in main.py.

The normal entry point is the importable :class:`~synnodb.api.SynnoDB` API, whose
stages register themselves in ``synnodb.api``'s registry. This module maps a
``conv_mode`` string onto the corresponding stage so ``python main.py manual
--conv_mode X`` can resolve one. The ``scripted`` conversation has no API stage,
so its descriptor is defined here.
"""

from synnodb.api import Stage, all_stages
from synnodb.conversations.conversation_spec import FrameworkContext
from synnodb.cpp_runner.prepare_repo.prepare_olap import prepare_base
from synnodb.utils.conv_name_utils import ConvMode


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
    conv_mode=ConvMode.SCRIPTED,
    usecases=frozenset(),
    build_config=_scripted_build_config,
    prepare=prepare_base,
    needs_parallelism=False,
    be_relaxed_supervision=False,
    factory=_scripted_factory,
)


def get_spec(conv_mode: str) -> Stage:
    catalog: dict = {st.conv_mode: st for st in all_stages()}
    catalog[ConvMode.SCRIPTED] = _SCRIPTED_SPEC
    if conv_mode not in catalog:
        raise ValueError(
            f"Unknown conv_mode '{conv_mode}'. Known modes: {sorted(catalog)}"
        )
    return catalog[conv_mode]
