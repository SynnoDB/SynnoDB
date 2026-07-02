"""Stage-list builders of the built-in conversations.

Each module exposes ``build(ctx) -> list[StageItem]`` (parameterized builders
take their per-call inputs as extra keyword arguments). A ConversationPlan
references these directly; there are no conversation subclasses.
"""
