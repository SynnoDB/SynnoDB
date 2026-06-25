"""Conversation-spec catalog for the ``manual`` debug entry point in main.py.

The normal entry points (``run_*.py``) each construct their own
:class:`ConversationSpec` and pass it straight to ``run_conv_wrapper``; they do
NOT depend on this module. It exists solely so ``python main.py manual
--conv_mode X`` can resolve a spec by name.

The ``scripted`` conversation has no run script, so its spec is defined here.
"""

from conversations.conversation_spec import ConversationSpec, FrameworkContext
from cpp_runner.prepare_repo.prepare_olap import prepare_base
from run_add_multi_threading import SPEC as _MT_SPEC
from run_check_sf_correctness import SPEC as _CHECK_SF_SPEC
from run_gen_base_impl import SPEC as _BASE_SPEC
from run_gen_storage_plan import SPEC as _STORAGE_PLAN_SPEC
from run_optim_loop import SPEC as _OPTIM_SPEC
from utils.conv_name_utils import ConvMode


def _scripted_factory(ctx: FrameworkContext):
    from conversations.scripted_conversation import ScriptedConversation

    return ScriptedConversation(**ctx.conv_args)


_SCRIPTED_SPEC = ConversationSpec(
    prepare=prepare_base,
    needs_parallelism=False,
    be_relaxed_supervision=False,
    factory=_scripted_factory,
)


_CATALOG: dict[str, ConversationSpec] = {
    ConvMode.STORAGE_PLAN: _STORAGE_PLAN_SPEC,
    ConvMode.BASE: _BASE_SPEC,
    ConvMode.OPTIM: _OPTIM_SPEC,
    ConvMode.MAKE_MT: _MT_SPEC,
    ConvMode.CHECK_SF: _CHECK_SF_SPEC,
    ConvMode.SCRIPTED: _SCRIPTED_SPEC,
}


def get_spec(conv_mode: str) -> ConversationSpec:
    if conv_mode not in _CATALOG:
        raise ValueError(
            f"Unknown conv_mode '{conv_mode}'. Known modes: {sorted(_CATALOG)}"
        )
    return _CATALOG[conv_mode]
