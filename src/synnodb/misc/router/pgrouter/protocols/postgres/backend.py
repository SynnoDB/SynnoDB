"""Backend protocol handling."""

from __future__ import annotations

import logging

from .constants import BACKEND_TAGS
from ...logging_utils import state_prefix, transaction_id_for_log
from ...output import append_result_row
from ...query_capture import finish_query_tracking, reset_result_tracking
from ...state import SessionState
from .wire import (
    data_row_to_object,
    decode_error_or_notice,
    decode_row_description,
    format_code_name,
    fmt_hex,
    i32,
    read_cstring,
    summarize_data_row,
)

log = logging.getLogger("pgrouter")

AUTH_CODE_NAMES = {
    0: "Ok",
    2: "KerberosV5",
    3: "CleartextPassword",
    5: "MD5Password",
    7: "GSS",
    8: "GSSContinue",
    9: "SSPI",
    10: "SASL",
    11: "SASLContinue",
    12: "SASLFinal",
}


async def handle_backend_message(state: SessionState, tag: bytes, payload: bytes) -> None:
    name = BACKEND_TAGS.get(tag, f"Unknown({tag!r})")
    if tag == b"R":
        auth_code = i32(payload, 0)
        log.debug("%s S->C Authentication code=%d name=%s", state_prefix(state), auth_code, AUTH_CODE_NAMES.get(auth_code, "Unknown"))
        return
    if tag == b"S":
        key, off = read_cstring(payload, 0)
        value, _ = read_cstring(payload, off)
        log.debug("%s S->C ParameterStatus %s=%r", state_prefix(state), key, value)
        return
    if tag == b"K":
        pid = i32(payload, 0)
        secret = payload[4:]
        state.backend_pid = pid
        log.debug("%s S->C BackendKeyData pid=%d secret_len=%d", state_prefix(state), pid, len(secret))
        return
    if tag == b"Z":
        await _handle_ready_for_query(state, payload)
        return
    if tag == b"T":
        cols = decode_row_description(payload)
        state.row_description = cols
        if state.current_statement_name is not None:
            state.prepared_row_descriptions[state.current_statement_name] = [
                type(col)(name=col.name, type_oid=col.type_oid, format_code=col.format_code) for col in cols
            ]
        summary = [(col.name, col.type_oid, format_code_name(col.format_code)) for col in cols]
        log.debug("%s qid=%s S->C RowDescription cols=%s", state_prefix(state), state.current_statement_id, summary)
        return
    if tag == b"D":
        state.current_row_count += 1
        append_result_row(state, data_row_to_object(payload, state.row_description))
        log.debug("%s qid=%s S->C DataRow %s", state_prefix(state), state.current_statement_id, summarize_data_row(payload, state.row_description))
        return
    if tag == b"C":
        command_tag, _ = read_cstring(payload, 0)
        log.debug("%s qid=%s S->C CommandComplete tag=%r", state_prefix(state), state.current_statement_id, command_tag)
        await finish_query_tracking(state, command_tag)
        return
    if tag == b"E":
        await _handle_error(state, payload)
        return
    if tag == b"N":
        fields = decode_error_or_notice(payload)
        log.info(
            "%s qid=%s S->C NoticeResponse severity=%r message=%r",
            state_prefix(state),
            state.current_statement_id,
            fields.get("S") or fields.get("V"),
            fields.get("M"),
        )
        return
    if tag == b"1":
        log.debug("%s S->C ParseComplete", state_prefix(state))
        return
    if tag == b"2":
        log.debug("%s S->C BindComplete", state_prefix(state))
        return
    if tag == b"s":
        log.debug("%s S->C PortalSuspended", state_prefix(state))
        return
    if tag == b"n":
        log.debug("%s S->C NoData", state_prefix(state))
        return
    log.debug("%s S->C %s payload=%s", state_prefix(state), name, fmt_hex(payload))


async def _handle_ready_for_query(state: SessionState, payload: bytes) -> None:
    status = chr(payload[0])
    if status in ("T", "E"):
        if state.current_transaction_id is None:
            state.transaction_counter += 1
            state.current_transaction_id = f"{state.session_id}:tx{state.transaction_counter:04d}"
        state.current_transaction_status = status
    else:
        state.current_transaction_id = None
        state.current_transaction_status = status
    log.debug("%s S->C ReadyForQuery tx_status=%s tx=%s", state_prefix(state), status, transaction_id_for_log(state))
    state.startup_done = True
    if state.current_statement_sql or state.current_statement_source or state.current_row_count:
        await finish_query_tracking(state)
    else:
        reset_result_tracking(state)


async def _handle_error(state: SessionState, payload: bytes) -> None:
    fields = decode_error_or_notice(payload)
    log.info(
        "%s qid=%s S->C ErrorResponse severity=%r code=%r message=%r detail=%r",
        state_prefix(state),
        state.current_statement_id,
        fields.get("S") or fields.get("V"),
        fields.get("C"),
        fields.get("M"),
        fields.get("D"),
    )
    state.current_error = fields
    await finish_query_tracking(state, "ERROR")
