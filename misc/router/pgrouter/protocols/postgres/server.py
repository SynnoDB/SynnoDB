"""Asyncio server and relay loop."""

from __future__ import annotations

import asyncio
import logging
import uuid

from .constants import CANCEL_REQUEST_CODE, GSSENC_REQUEST_CODE, SSL_REQUEST_CODE
from .backend import handle_backend_message
from .frontend import handle_frontend_message, log_startup_packet
from .intercept import maybe_handle_intercepted_message
from .wire import i32
from ...config import RouterConfig
from ...state import SessionState

log = logging.getLogger("pgrouter")


async def relay_startup(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    state: SessionState,
    c2s: bool,
) -> int:
    header = await reader.readexactly(4)
    ln = i32(header, 0)
    rest = await reader.readexactly(ln - 4)
    packet = header + rest
    writer.write(packet)
    await writer.drain()
    if c2s:
        return log_startup_packet(state, packet, "C->S")
    return 0


async def relay_message_loop(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    peer_writer: asyncio.StreamWriter,
    state: SessionState,
    from_client: bool,
    config: RouterConfig,
) -> None:
    while True:
        if state.tls_active or state.gss_active:
            data = await reader.read(65536)
            if not data:
                break
            writer.write(data)
            await writer.drain()
            continue

        tag = await reader.readexactly(1)
        len_bytes = await reader.readexactly(4)
        ln = i32(len_bytes, 0)
        payload = await reader.readexactly(ln - 4)

        if from_client:
            handle_frontend_message(state, tag, payload)
            if await maybe_handle_intercepted_message(
                state,
                tag,
                payload,
                peer_writer,
                statement_routes=config.statement_routes,
                transaction_routes=config.transaction_routes,
            ):
                continue
            writer.write(tag + len_bytes + payload)
            await writer.drain()
        else:
            writer.write(tag + len_bytes + payload)
            await writer.drain()
            await handle_backend_message(state, tag, payload)


async def handle_ssl_or_gss_response(
    server_reader: asyncio.StreamReader,
    client_writer: asyncio.StreamWriter,
    state: SessionState,
) -> None:
    if state.pending_ssl_response:
        b = await server_reader.readexactly(1)
        client_writer.write(b)
        await client_writer.drain()
        state.pending_ssl_response = False
        if b == b"S":
            state.tls_active = True
            log.warning("sid=%s S->C SSL accepted; subsequent traffic is encrypted and will only be relayed", state.session_id)
        else:
            log.info("sid=%s S->C SSL response=%r", state.session_id, b)
        return
    if state.pending_gssenc_response:
        b = await server_reader.readexactly(1)
        client_writer.write(b)
        await client_writer.drain()
        state.pending_gssenc_response = False
        if b == b"G":
            state.gss_active = True
            log.warning("sid=%s S->C GSS encryption accepted; subsequent traffic is encrypted and will only be relayed", state.session_id)
        else:
            log.info("sid=%s S->C GSSENC response=%r", state.session_id, b)


async def handle_client(
    client_reader: asyncio.StreamReader,
    client_writer: asyncio.StreamWriter,
    config: RouterConfig,
) -> None:
    peer = client_writer.get_extra_info("peername")
    state = SessionState(
        client_addr=str(peer),
        jsonl_path=config.jsonl_path,
        upstream_host=config.upstream_host,
        upstream_port=config.upstream_port,
        catalog_lookup_dsn=config.catalog_lookup_dsn,
        catalog_lookup_password=config.catalog_lookup_password,
        results_dir=config.results_dir,
        result_file_format=config.result_file_format,
        session_id=uuid.uuid4().hex[:12],
        capture_enabled=config.capture_enabled,
    )
    log.info("sid=%s Accepted client %s", state.session_id, peer)

    server_writer: asyncio.StreamWriter | None = None
    try:
        server_reader, server_writer = await asyncio.open_connection(config.upstream_host, config.upstream_port)
        first_code = await relay_startup(client_reader, server_writer, state, c2s=True)

        if first_code in (SSL_REQUEST_CODE, GSSENC_REQUEST_CODE):
            await handle_ssl_or_gss_response(server_reader, client_writer, state)
            if not state.tls_active and not state.gss_active:
                await relay_startup(client_reader, server_writer, state, c2s=True)
        elif first_code == CANCEL_REQUEST_CODE:
            data = await server_reader.read()
            if data:
                client_writer.write(data)
                await client_writer.drain()
            server_writer.close()
            await server_writer.wait_closed()
            client_writer.close()
            await client_writer.wait_closed()
            return

        async def client_to_server() -> None:
            try:
                await relay_message_loop(client_reader, server_writer, client_writer, state, from_client=True, config=config)
            finally:
                server_writer.close()
                try:
                    await server_writer.wait_closed()
                except Exception:
                    pass

        async def server_to_client() -> None:
            try:
                await relay_message_loop(server_reader, client_writer, server_writer, state, from_client=False, config=config)
            finally:
                client_writer.close()
                try:
                    await client_writer.wait_closed()
                except Exception:
                    pass

        await asyncio.gather(client_to_server(), server_to_client())
    except asyncio.IncompleteReadError:
        log.info("sid=%s Connection closed client=%s backend_pid=%s queries=%d", state.session_id, peer, state.backend_pid, state.query_counter)
    except Exception:
        log.exception("sid=%s Router error client=%s backend_pid=%s", state.session_id, peer, state.backend_pid)
    finally:
        try:
            client_writer.close()
            await client_writer.wait_closed()
        except Exception:
            pass
        if server_writer is not None:
            try:
                server_writer.close()
                await server_writer.wait_closed()
            except Exception:
                pass
