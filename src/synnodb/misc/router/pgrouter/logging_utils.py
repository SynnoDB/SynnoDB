"""Small helpers for consistent session/query logging."""

from __future__ import annotations

from .state import SessionState


def state_prefix(state: SessionState) -> str:
    return f"sid={state.session_id}"


def transaction_id_for_log(state: SessionState) -> str:
    return state.current_transaction_id or "-"
