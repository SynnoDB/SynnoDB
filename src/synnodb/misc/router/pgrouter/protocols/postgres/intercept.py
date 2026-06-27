"""Protocol handling for locally intercepted statement routes."""

from __future__ import annotations

import asyncio
import logging
from typing import Sequence

from ...analyze.normalization import normalize_query_structure
from ...logging_utils import state_prefix
from ...output import append_result_row, ensure_transaction_id
from ...query_capture import finish_query_tracking
from ...routes import (
    PortalInterceptState,
    RouteResult,
    StatementRoute,
    command_tag_for_result,
    invoke_transaction_control_handler,
    invoke_transaction_statement_handler,
    invoke_statement_handler,
    match_statement_route,
    match_transaction_route,
    open_transaction_handler,
    prepare_route_result_rows,
    statement_route_description_columns,
    statement_route_name,
    TransactionRoute,
)
from ...state import ColumnInfo, ParameterInfo, SessionState
from .encoding import (
    encode_bind_complete,
    encode_close_complete,
    encode_command_complete,
    encode_data_row,
    encode_error_response,
    encode_no_data,
    encode_parameter_description,
    encode_parse_complete,
    encode_ready_for_query,
    encode_row_description,
)
from .wire import read_cstring

log = logging.getLogger("pgrouter")

UNSUPPORTED_TRANSACTION_STATES = frozenset({"T", "E"})
ABORTED_TRANSACTION_ERROR_CODE = "25P02"
ABORTED_TRANSACTION_ERROR_MESSAGE = "current transaction is aborted, commands ignored until end of transaction block"


async def maybe_handle_intercepted_message(
    state: SessionState,
    tag: bytes,
    payload: bytes,
    client_writer: asyncio.StreamWriter,
    *,
    statement_routes: Sequence[StatementRoute],
    transaction_routes: Sequence[TransactionRoute],
) -> bool:
    if tag == b"Q":
        if await _handle_transaction_simple_intercept(state, client_writer, transaction_routes):
            return True
        if not statement_routes:
            return False
        return await _handle_simple_query_intercept(state, client_writer, statement_routes)
    if tag == b"E" and state.local_transaction_active:
        return await _handle_transaction_execute_intercept(state, payload, client_writer, transaction_routes)
    if tag == b"S":
        return await _handle_sync_intercept(state, client_writer)
    if tag == b"C":
        return await _handle_close_intercept(state, payload, client_writer)
    if tag == b"H":
        return state.local_sync_pending
    if not statement_routes:
        return False
    if tag == b"P":
        return await _handle_parse_intercept(state, client_writer, statement_routes)
    if tag == b"B":
        return await _handle_bind_intercept(state, client_writer)
    if tag == b"D":
        return await _handle_describe_intercept(state, payload, client_writer)
    if tag == b"E":
        return await _handle_execute_intercept(state, payload, client_writer)
    return False


async def _handle_simple_query_intercept(
    state: SessionState,
    client_writer: asyncio.StreamWriter,
    statement_routes: Sequence[StatementRoute],
) -> bool:
    statement_route = match_statement_route(statement_routes, state.current_statement_sql)
    if statement_route is None or state.current_statement_sql is None:
        return False
    if _skip_intercept_for_transaction(state, statement_route, statement_name=None, portal_name=None):
        return False
    result = await invoke_statement_handler(
        statement_route,
        query=state.current_statement_sql,
        query_source=state.current_statement_source or "simple",
        parameters=state.current_parameters,
        statement_name=None,
        portal_name=None,
    )
    await _send_intercepted_query_response(state, client_writer, result, default_columns=statement_route.result_columns)
    return True


async def _handle_transaction_simple_intercept(
    state: SessionState,
    client_writer: asyncio.StreamWriter,
    transaction_routes: Sequence[TransactionRoute],
) -> bool:
    if state.current_statement_sql is None:
        return False
    command = _simple_transaction_command(state.current_statement_sql)
    if not state.local_transaction_active:
        if command not in {"BEGIN", "START TRANSACTION"} or not transaction_routes:
            return False
        await _send_local_transaction_begin(state, client_writer)
        return True

    if command == "ROLLBACK":
        await _send_local_transaction_end(state, client_writer, action="rollback")
        return True
    if command in {"COMMIT", "END"}:
        if state.current_transaction_status == "E":
            await _send_local_transaction_error(
                state,
                client_writer,
                ABORTED_TRANSACTION_ERROR_MESSAGE,
                code=ABORTED_TRANSACTION_ERROR_CODE,
            )
            return True
        await _send_local_transaction_end(state, client_writer, action="commit")
        return True
    if state.current_transaction_status == "E":
        await _send_local_transaction_error(
            state,
            client_writer,
            ABORTED_TRANSACTION_ERROR_MESSAGE,
            code=ABORTED_TRANSACTION_ERROR_CODE,
        )
        return True
    await _handle_transaction_statement_intercept(
        state,
        client_writer,
        transaction_routes=transaction_routes,
        query=state.current_statement_sql,
        query_source=state.current_statement_source or "simple",
        parameters=state.current_parameters,
        statement_name=None,
        portal_name=None,
        result_formats=[],
    )
    return True


async def _handle_parse_intercept(
    state: SessionState,
    client_writer: asyncio.StreamWriter,
    statement_routes: Sequence[StatementRoute],
) -> bool:
    statement_name = next(reversed(state.prepared_sql), None)
    if statement_name is None:
        return False
    statement_route = match_statement_route(statement_routes, state.prepared_sql.get(statement_name))
    if statement_route is None:
        state.prepared_intercepts.pop(statement_name, None)
        return False
    state.prepared_intercepts[statement_name] = statement_route
    state.local_sync_pending = True
    client_writer.write(encode_parse_complete())
    await client_writer.drain()
    log.info("sid=%s C->S Parse intercepted stmt=%r route=%s", state.session_id, statement_name, statement_route_name(statement_route))
    return True


async def _handle_bind_intercept(
    state: SessionState,
    client_writer: asyncio.StreamWriter,
) -> bool:
    portal_name = next(reversed(state.portal_bindings), None)
    if portal_name is None:
        return False
    binding = state.portal_bindings[portal_name]
    statement_route = state.prepared_intercepts.get(binding.statement_name)
    if statement_route is None or binding.sql is None:
        state.portal_intercepts.pop(portal_name, None)
        return False
    state.portal_intercepts[portal_name] = PortalInterceptState(statement_route=statement_route, describe_sent=False)
    state.local_sync_pending = True
    client_writer.write(encode_bind_complete())
    await client_writer.drain()
    log.info("sid=%s C->S Bind intercepted portal=%r stmt=%r route=%s", state.session_id, portal_name, binding.statement_name, statement_route_name(statement_route))
    return True


async def _handle_describe_intercept(state: SessionState, payload: bytes, client_writer: asyncio.StreamWriter) -> bool:
    kind = chr(payload[0])
    name, _ = read_cstring(payload, 1)
    key_name = name or "<unnamed>"
    if kind == "P":
        portal_state = state.portal_intercepts.get(key_name)
        if portal_state is None:
            return False
        columns = statement_route_description_columns(portal_state.statement_route)
        client_writer.write(encode_row_description(columns) if columns else encode_no_data())
        await client_writer.drain()
        portal_state.describe_sent = True
        state.local_sync_pending = True
        return True
    if kind == "S":
        statement_route = state.prepared_intercepts.get(key_name)
        if statement_route is None:
            return False
        type_oids = state.prepared_param_types.get(key_name, [])
        client_writer.write(encode_parameter_description(type_oids))
        columns = statement_route_description_columns(statement_route)
        client_writer.write(encode_row_description(columns) if columns else encode_no_data())
        await client_writer.drain()
        state.local_sync_pending = True
        return True
    return False


async def _handle_execute_intercept(
    state: SessionState,
    payload: bytes,
    client_writer: asyncio.StreamWriter,
) -> bool:
    portal_name, _off = read_cstring(payload, 0)
    key_portal = portal_name or "<unnamed>"
    portal_state = state.portal_intercepts.get(key_portal)
    if portal_state is None:
        return False
    binding = state.portal_bindings.get(key_portal)
    if binding is None or binding.sql is None:
        return False
    if _skip_intercept_for_transaction(
        state,
        portal_state.statement_route,
        statement_name=binding.statement_name,
        portal_name=key_portal,
    ):
        state.portal_intercepts.pop(key_portal, None)
        return False
    result = await invoke_statement_handler(
        portal_state.statement_route,
        query=binding.sql,
        query_source="extended",
        parameters=binding.parameters,
        statement_name=binding.statement_name,
        portal_name=key_portal,
    )
    await _send_intercepted_query_response(
        state,
        client_writer,
        result,
        default_columns=portal_state.statement_route.result_columns,
        result_formats=binding.result_formats,
        send_description=not portal_state.describe_sent,
    )
    state.local_sync_pending = True
    return True


async def _handle_transaction_execute_intercept(
    state: SessionState,
    payload: bytes,
    client_writer: asyncio.StreamWriter,
    transaction_routes: Sequence[TransactionRoute],
) -> bool:
    portal_name, _off = read_cstring(payload, 0)
    key_portal = portal_name or "<unnamed>"
    binding = state.portal_bindings.get(key_portal)
    if binding is None or binding.sql is None:
        return False
    if state.current_transaction_status == "E":
        await _send_local_transaction_error(
            state,
            client_writer,
            ABORTED_TRANSACTION_ERROR_MESSAGE,
            code=ABORTED_TRANSACTION_ERROR_CODE,
            with_sync=True,
        )
        return True
    await _handle_transaction_statement_intercept(
        state,
        client_writer,
        transaction_routes=transaction_routes,
        query=binding.sql,
        query_source="extended",
        parameters=binding.parameters,
        statement_name=binding.statement_name,
        portal_name=key_portal,
        result_formats=binding.result_formats,
    )
    state.local_sync_pending = True
    return True


async def _handle_sync_intercept(state: SessionState, client_writer: asyncio.StreamWriter) -> bool:
    if not state.local_sync_pending:
        return False
    client_writer.write(encode_ready_for_query(state.current_transaction_status))
    await client_writer.drain()
    state.local_sync_pending = False
    return True


def _skip_intercept_for_transaction(
    state: SessionState,
    statement_route: StatementRoute,
    *,
    statement_name: str | None,
    portal_name: str | None,
) -> bool:
    if state.current_transaction_status not in UNSUPPORTED_TRANSACTION_STATES:
        return False
    log.warning(
        "%s qid=%s Hook route=%r skipped because explicit transactions are not supported tx_status=%s stmt=%r portal=%r",
        state_prefix(state),
        state.current_statement_id,
        statement_route_name(statement_route),
        state.current_transaction_status,
        statement_name,
        portal_name,
    )
    return True


async def _handle_close_intercept(state: SessionState, payload: bytes, client_writer: asyncio.StreamWriter) -> bool:
    kind = chr(payload[0])
    name, _ = read_cstring(payload, 1)
    key_name = name or "<unnamed>"
    handled = False
    if kind == "P" and key_name in state.portal_intercepts:
        state.portal_intercepts.pop(key_name, None)
        state.portal_bindings.pop(key_name, None)
        handled = True
    if kind == "S" and key_name in state.prepared_intercepts:
        state.prepared_intercepts.pop(key_name, None)
        state.prepared_sql.pop(key_name, None)
        state.prepared_param_types.pop(key_name, None)
        state.prepared_row_descriptions.pop(key_name, None)
        handled = True
    if handled:
        client_writer.write(encode_close_complete())
        await client_writer.drain()
    return handled


def _simple_transaction_command(query: str) -> str | None:
    normalized = query.strip().rstrip(";").upper()
    if normalized in {"BEGIN", "START TRANSACTION", "COMMIT", "ROLLBACK", "END"}:
        return normalized
    return None


def _reset_local_transaction_state(state: SessionState) -> None:
    state.local_transaction_active = False
    state.local_transaction_route = None
    state.local_transaction_session = None
    state.local_transaction_statement_index = 0


async def _send_local_transaction_begin(state: SessionState, client_writer: asyncio.StreamWriter) -> None:
    client_writer.write(encode_command_complete("BEGIN"))
    await client_writer.drain()
    await finish_query_tracking(state, "BEGIN")
    ensure_transaction_id(state)
    state.current_transaction_status = "T"
    state.local_transaction_active = True
    state.local_transaction_route = None
    state.local_transaction_session = None
    state.local_transaction_statement_index = 0
    client_writer.write(encode_ready_for_query("T"))
    await client_writer.drain()


async def _send_local_transaction_end(
    state: SessionState,
    client_writer: asyncio.StreamWriter,
    *,
    action: str,
) -> None:
    route = state.local_transaction_route
    handler_session = state.local_transaction_session
    if action == "commit" and route is not None and state.local_transaction_statement_index < len(route.normalized_structures):
        await _send_local_transaction_error(
            state,
            client_writer,
            "transaction handler route is incomplete; rollback is required",
            code="0A000",
        )
        return
    if route is not None and handler_session is not None:
        control_result = await invoke_transaction_control_handler(
            route,
            handler_session,
            transaction_id=ensure_transaction_id(state),
            statement_count=state.local_transaction_statement_index,
            action="commit" if action == "commit" else "rollback",
        )
        if control_result is not None and control_result.command_tag not in {None, "COMMIT", "ROLLBACK"}:
            raise ValueError("transaction control hooks must not override COMMIT/ROLLBACK command tags")
    command_tag = "COMMIT" if action == "commit" else "ROLLBACK"
    client_writer.write(encode_command_complete(command_tag))
    await client_writer.drain()
    await finish_query_tracking(state, command_tag)
    _reset_local_transaction_state(state)
    state.current_transaction_status = "I"
    state.current_transaction_id = None
    client_writer.write(encode_ready_for_query("I"))
    await client_writer.drain()


async def _send_local_transaction_error(
    state: SessionState,
    client_writer: asyncio.StreamWriter,
    message: str,
    *,
    code: str,
    with_sync: bool = False,
) -> None:
    client_writer.write(encode_error_response(message, code=code))
    await client_writer.drain()
    state.current_error = {"C": code, "M": message, "S": "ERROR"}
    await finish_query_tracking(state, "ERROR")
    state.current_transaction_status = "E"
    state.local_transaction_active = True
    if with_sync:
        state.local_sync_pending = True
        return
    client_writer.write(encode_ready_for_query("E"))
    await client_writer.drain()


async def _handle_transaction_statement_intercept(
    state: SessionState,
    client_writer: asyncio.StreamWriter,
    *,
    transaction_routes: Sequence[TransactionRoute],
    query: str,
    query_source: str,
    parameters: Sequence[ParameterInfo],
    statement_name: str | None,
    portal_name: str | None,
    result_formats: Sequence[int],
) -> None:
    route = state.local_transaction_route
    if route is None:
        route = match_transaction_route(transaction_routes, query)
        if route is None:
            await _send_local_transaction_error(
                state,
                client_writer,
                f"no transaction handler route matches statement: {query}",
                code="0A000",
                with_sync=(query_source == "extended"),
            )
            return
        state.local_transaction_route = route
        state.local_transaction_session = await open_transaction_handler(route, transaction_id=ensure_transaction_id(state))
        state.local_transaction_statement_index = 0

    expected_index = state.local_transaction_statement_index
    if expected_index >= len(route.normalized_structures):
        await _send_local_transaction_error(
            state,
            client_writer,
            f"transaction handler route has no statement for extra query: {query}",
            code="0A000",
            with_sync=(query_source == "extended"),
        )
        return

    statement = route.statements[expected_index]
    normalized_query = statement.normalized_structure
    actual_normalized_query = normalize_query_structure(query)
    if actual_normalized_query != normalized_query:
        await _send_local_transaction_error(
            state,
            client_writer,
            f"transaction handler route expected {normalized_query!r} but got {actual_normalized_query!r}",
            code="0A000",
            with_sync=(query_source == "extended"),
        )
        return

    result = await invoke_transaction_statement_handler(
        route,
        state.local_transaction_session,
        statement=statement,
        transaction_id=ensure_transaction_id(state),
        statement_index=expected_index + 1,
        query=query,
        query_source=query_source,
        parameters=parameters,
        statement_name=statement_name,
        portal_name=portal_name,
    )
    state.local_transaction_statement_index += 1
    await _send_intercepted_query_response(
        state,
        client_writer,
        result,
        default_columns=statement.result_columns or (),
        result_formats=result_formats,
        send_ready=query_source == "simple",
        send_description=True,
    )


async def _emit_rows_and_capture(
    state: SessionState,
    client_writer: asyncio.StreamWriter,
    result: RouteResult,
    *,
    default_columns: Sequence,
    result_formats: Sequence[int],
    send_description: bool,
    send_ready: bool,
) -> None:
    columns, rows = await prepare_route_result_rows(default_columns, result, result_formats)
    if send_description:
        client_writer.write(encode_row_description(columns) if columns else encode_no_data())
    state.row_description = list(columns)
    row_count = 0
    async for row in rows:
        row_count += 1
        state.current_row_count += 1
        append_result_row(state, row)
        client_writer.write(encode_data_row(row, columns))
    command_tag = command_tag_for_result(result, row_count)
    client_writer.write(encode_command_complete(command_tag))
    await client_writer.drain()
    await finish_query_tracking(state, command_tag)
    if send_ready:
        client_writer.write(encode_ready_for_query(state.current_transaction_status))
        await client_writer.drain()


async def _send_intercepted_query_response(
    state: SessionState,
    client_writer: asyncio.StreamWriter,
    result: RouteResult,
    *,
    default_columns: Sequence,
    result_formats: Sequence[int] = (),
    send_description: bool = True,
    send_ready: bool = True,
) -> None:
    try:
        await _stream_query_result(
            state,
            client_writer,
            result,
            default_columns=default_columns,
            result_formats=result_formats,
            send_description=send_description,
            send_ready=send_ready,
        )
    except Exception as exc:
        client_writer.write(encode_error_response(str(exc)))
        await client_writer.drain()
        raise


async def _stream_query_result(
    state: SessionState,
    client_writer: asyncio.StreamWriter,
    result: RouteResult,
    *,
    default_columns: Sequence,
    result_formats: Sequence[int],
    send_description: bool,
    send_ready: bool,
) -> None:
    await _emit_rows_and_capture(
        state,
        client_writer,
        result,
        default_columns=default_columns,
        result_formats=result_formats,
        send_description=send_description,
        send_ready=send_ready,
    )
