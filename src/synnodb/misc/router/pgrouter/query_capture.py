"""Query lifecycle tracking and final capture logging."""

from __future__ import annotations

import datetime as dt
import logging
import time
from typing import Optional

from .output import abort_result_writer, append_query_record
from .state import SessionState

log = logging.getLogger("pgrouter")


def reset_result_tracking(state: SessionState) -> None:
    abort_result_writer(state)
    state.row_description = None
    state.current_row_count = 0
    state.current_command_tag = None
    state.current_error = None
    state.current_statement_id = None
    state.current_statement_name = None
    state.current_portal_name = None
    state.current_statement_execution_index = None
    state.current_statement_started_at = None
    state.current_statement_started_at_utc = None
    state.current_parameters = []
    state.current_result_format_codes = []
    state.current_transaction_id_for_statement = None
    state.current_transaction_status_for_statement = None


def start_query_tracking(state: SessionState, sql: Optional[str], source: str) -> None:
    reset_result_tracking(state)
    state.query_counter += 1
    state.current_statement_sequence = state.query_counter
    state.current_statement_id = f"{state.session_id}:q{state.current_statement_sequence:04d}"
    state.current_statement_sql = sql
    state.current_statement_source = source
    state.current_statement_started_at = time.perf_counter()
    state.current_statement_started_at_utc = dt.datetime.now(dt.UTC).isoformat()
    state.current_transaction_id_for_statement = state.current_transaction_id
    state.current_transaction_status_for_statement = state.current_transaction_status


async def finish_query_tracking(state: SessionState, command_tag: Optional[str] = None) -> None:
    if command_tag is not None:
        state.current_command_tag = command_tag
    duration_ms = None
    if state.current_statement_sql or state.current_statement_source:
        if state.current_statement_started_at is not None:
            duration_ms = round((time.perf_counter() - state.current_statement_started_at) * 1000, 3)
        log.info(
            "sid=%s qid=%s tx=%s source=%s stmt=%r portal=%r stmt_exec=%s rows=%d command=%r duration_ms=%s sql=%r",
            state.session_id,
            state.current_statement_id,
            state.current_transaction_id_for_statement or state.current_transaction_id,
            state.current_statement_source,
            state.current_statement_name,
            state.current_portal_name,
            state.current_statement_execution_index,
            state.current_row_count,
            state.current_command_tag,
            duration_ms,
            state.current_statement_sql,
        )
        await append_query_record(state, duration_ms=duration_ms)
    state.current_statement_sql = None
    state.current_statement_source = None
    reset_result_tracking(state)
