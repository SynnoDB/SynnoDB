"""Frontend protocol handling."""

from __future__ import annotations

import logging
from typing import Any, Optional

from .constants import CANCEL_REQUEST_CODE, FRONTEND_TAGS, GSSENC_REQUEST_CODE, SSL_REQUEST_CODE
from ...logging_utils import state_prefix, transaction_id_for_log
from ...query_capture import reset_result_tracking, start_query_tracking
from ...state import ColumnInfo, ParameterInfo, PortalBinding, SessionState
from .value_decoding import decode_binary_value, decode_text_value
from .wire import fmt_hex, i16, i32, parse_kv_cstrings, read_cstring

log = logging.getLogger("pgrouter")


def decode_parameter_value(format_code: int, type_oid: Optional[int], raw: bytes) -> Any:
    if format_code == 0:
        return decode_text_value(type_oid, raw)
    return decode_binary_value(type_oid or 0, raw)


def next_statement_execution_index(state: SessionState, statement_name: Optional[str]) -> Optional[int]:
    if statement_name is None:
        return None
    execution_index = state.prepared_execute_counts.get(statement_name, 0) + 1
    state.prepared_execute_counts[statement_name] = execution_index
    return execution_index


def apply_execute_context(
    state: SessionState,
    *,
    portal_name: str,
    statement_name: Optional[str],
    binding: Optional[PortalBinding],
) -> Optional[int]:
    statement_execution_index = next_statement_execution_index(state, statement_name)
    state.current_statement_name = statement_name
    state.current_portal_name = portal_name
    state.current_statement_execution_index = statement_execution_index
    if binding is not None:
        state.current_parameters = list(binding.parameters)
        state.current_result_format_codes = list(binding.result_formats)
    if statement_name is not None:
        cached_row_description = state.prepared_row_descriptions.get(statement_name)
        if cached_row_description is not None:
            state.row_description = apply_result_format_codes(cached_row_description, state.current_result_format_codes)
    return statement_execution_index


def apply_result_format_codes(columns: list[ColumnInfo], result_format_codes: list[int]) -> list[ColumnInfo]:
    if not result_format_codes:
        return [ColumnInfo(name=column.name, type_oid=column.type_oid, format_code=column.format_code) for column in columns]
    if len(result_format_codes) == 1:
        format_codes = [result_format_codes[0]] * len(columns)
    else:
        format_codes = [
            result_format_codes[index] if index < len(result_format_codes) else columns[index].format_code
            for index in range(len(columns))
        ]
    return [
        ColumnInfo(name=column.name, type_oid=column.type_oid, format_code=format_codes[index])
        for index, column in enumerate(columns)
    ]


def log_startup_packet(state: SessionState, packet: bytes, direction: str) -> int:
    ln = i32(packet, 0)
    code = i32(packet, 4)
    if code == SSL_REQUEST_CODE:
        log.debug("%s %s startup SSLRequest", state_prefix(state), direction)
        state.pending_ssl_response = True
        return code
    if code == GSSENC_REQUEST_CODE:
        log.debug("%s %s startup GSSENCRequest", state_prefix(state), direction)
        state.pending_gssenc_response = True
        return code
    if code == CANCEL_REQUEST_CODE:
        pid = i32(packet, 8)
        secret = packet[12:]
        log.debug("%s %s startup CancelRequest pid=%s secret_len=%d", state_prefix(state), direction, pid, len(secret))
        return code
    params = parse_kv_cstrings(packet[8:ln])
    state.startup_params = params
    major, minor = code >> 16, code & 0xFFFF
    log.debug("%s %s startup StartupMessage protocol=%d.%d params=%s", state_prefix(state), direction, major, minor, params)
    return code


def handle_frontend_message(state: SessionState, tag: bytes, payload: bytes) -> None:
    name = FRONTEND_TAGS.get(tag, f"Unknown({tag!r})")
    if tag == b"Q":
        sql, _ = read_cstring(payload, 0)
        start_query_tracking(state, sql, "simple")
        log.info("%s qid=%s C->S Query tx=%s sql=%r", state_prefix(state), state.current_statement_id, transaction_id_for_log(state), sql)
        return
    if tag == b"P":
        off = 0
        stmt_name, off = read_cstring(payload, off)
        sql, off = read_cstring(payload, off)
        nparams = i16(payload, off)
        off += 2
        type_oids = [i32(payload, off + i * 4) for i in range(nparams)]
        key = stmt_name or "<unnamed>"
        state.prepared_sql[key] = sql
        state.prepared_param_types[key] = type_oids
        state.prepared_row_descriptions.pop(key, None)
        parse_count = state.prepared_execute_counts.get(key, 0)
        log.info(
            "%s C->S Parse stmt=%r sql=%r param_type_oids=%s prior_exec_count=%d",
            state_prefix(state),
            key,
            sql,
            type_oids,
            parse_count,
        )
        reset_result_tracking(state)
        return
    if tag == b"B":
        _handle_bind(state, payload)
        return
    if tag == b"E":
        _handle_execute(state, payload)
        return
    if tag == b"D":
        kind = chr(payload[0])
        obj, _ = read_cstring(payload, 1)
        log.debug("%s C->S Describe kind=%s name=%r", state_prefix(state), kind, obj)
        return
    if tag == b"S":
        log.debug("%s C->S Sync", state_prefix(state))
        return
    if tag == b"H":
        log.debug("%s C->S Flush", state_prefix(state))
        return
    if tag == b"C":
        kind = chr(payload[0])
        obj, _ = read_cstring(payload, 1)
        log.debug("%s C->S Close kind=%s name=%r", state_prefix(state), kind, obj)
        return
    if tag == b"X":
        log.debug("%s C->S Terminate", state_prefix(state))
        return
    log.debug("%s C->S %s payload=%s", state_prefix(state), name, fmt_hex(payload))


def _handle_bind(state: SessionState, payload: bytes) -> None:
    off = 0
    portal, off = read_cstring(payload, off)
    stmt, off = read_cstring(payload, off)
    key_stmt = stmt or "<unnamed>"
    key_portal = portal or "<unnamed>"
    format_count = i16(payload, off)
    off += 2
    param_formats = [i16(payload, off + i * 2) for i in range(format_count)]
    off += 2 * format_count
    nparams = i16(payload, off)
    off += 2
    param_type_oids = state.prepared_param_types.get(key_stmt, [])
    decoded_parameters: list[ParameterInfo] = []
    parameter_log_values: list[Any] = []
    for index in range(nparams):
        length = i32(payload, off)
        off += 4
        type_oid = param_type_oids[index] if index < len(param_type_oids) else None
        format_code = 0 if format_count == 0 else (param_formats[0] if format_count == 1 else param_formats[index])
        if length == -1:
            decoded_parameters.append(
                ParameterInfo(index=index + 1, format_code=format_code, type_oid=type_oid, value=None, length=None, is_null=True)
            )
            parameter_log_values.append(None)
            continue
        raw = payload[off:off + length]
        off += length
        value = decode_parameter_value(format_code, type_oid, raw)
        decoded_parameters.append(
            ParameterInfo(index=index + 1, format_code=format_code, type_oid=type_oid, value=value, length=length)
        )
        parameter_log_values.append(value)
    result_format_count = i16(payload, off)
    off += 2
    result_formats = [i16(payload, off + i * 2) for i in range(result_format_count)]
    state.portal_to_statement[key_portal] = key_stmt
    state.portal_bindings[key_portal] = PortalBinding(
        portal_name=key_portal,
        statement_name=key_stmt,
        sql=state.prepared_sql.get(key_stmt),
        parameter_formats=list(param_formats),
        parameters=decoded_parameters,
        result_formats=result_formats,
    )
    log.debug(
        "%s C->S Bind portal=%r stmt=%r sql=%r param_formats=%s params=%s result_formats=%s",
        state_prefix(state),
        key_portal,
        key_stmt,
        state.prepared_sql.get(key_stmt),
        param_formats,
        parameter_log_values,
        result_formats,
    )
    reset_result_tracking(state)


def _handle_execute(state: SessionState, payload: bytes) -> None:
    portal, off = read_cstring(payload, 0)
    max_rows = i32(payload, off)
    key_portal = portal or "<unnamed>"
    binding = state.portal_bindings.get(key_portal)
    stmt = binding.statement_name if binding else state.portal_to_statement.get(key_portal)
    sql = binding.sql if binding else state.prepared_sql.get(stmt or "")
    start_query_tracking(state, sql, "extended")
    statement_execution_index = apply_execute_context(
        state,
        portal_name=key_portal,
        statement_name=stmt,
        binding=binding,
    )
    log.debug(
        "%s qid=%s C->S Execute tx=%s portal=%r stmt=%r stmt_exec=%s sql=%r max_rows=%d",
        state_prefix(state),
        state.current_statement_id,
        transaction_id_for_log(state),
        key_portal,
        stmt,
        statement_execution_index,
        sql,
        max_rows,
    )
