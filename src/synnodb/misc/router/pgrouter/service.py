"""Reusable router service entrypoints for CLI and embedded use."""

from __future__ import annotations

import asyncio
import logging
import signal
import threading
from dataclasses import dataclass
from typing import Optional

from .config import RouterConfig
from .output import initialize_capture_outputs

log = logging.getLogger("pgrouter")


@dataclass
class _ShutdownController:
    server: asyncio.AbstractServer
    stop_event: asyncio.Event
    active_tasks: set[asyncio.Task[None]]
    request_count: int = 0

    def request_shutdown(self) -> None:
        self.request_count += 1
        if self.request_count == 1:
            log.info(
                "Shutdown requested. Stopping listener and waiting for %d active session(s). Press Ctrl-C again to force.",
                len(self.active_tasks),
            )
            self.server.close()
            self.stop_event.set()
            return
        log.warning("Force shutdown requested. Cancelling %d active session(s).", len(self.active_tasks))
        for task in list(self.active_tasks):
            task.cancel()
        self.stop_event.set()


def _install_signal_handlers(
    loop: asyncio.AbstractEventLoop,
    controller: _ShutdownController,
) -> bool:
    if threading.current_thread() is not threading.main_thread():
        return False
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, controller.request_shutdown)
        except NotImplementedError:  # pragma: no cover - platform specific
            pass
    return True


def _remove_signal_handlers(loop: asyncio.AbstractEventLoop) -> None:
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.remove_signal_handler(sig)
        except NotImplementedError:  # pragma: no cover - platform specific
            pass


async def serve_router(
    config: RouterConfig,
    *,
    shutdown_event: asyncio.Event | None = None,
    ready_event: threading.Event | None = None,
) -> None:
    if config.capture_enabled:
        initialize_capture_outputs(config.jsonl_path, config.results_dir, config.append)

    active_tasks: set[asyncio.Task[None]] = set()
    stop_event = shutdown_event or asyncio.Event()
    loop = asyncio.get_running_loop()

    async def client_handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        assert task is not None
        active_tasks.add(task)
        try:
            await config.protocol_adapter.handle_client(reader, writer, config)
        finally:
            active_tasks.discard(task)

    server = await asyncio.start_server(client_handler, config.listen_host, config.listen_port)

    addrs = ", ".join(str(sock.getsockname()) for sock in (server.sockets or []))
    log.info(
        "Listening on %s, protocol=%s, forwarding to %s:%d, capture=%s, statement_routes=%d, transaction_routes=%d",
        addrs,
        config.protocol_adapter.name,
        config.upstream_host,
        config.upstream_port,
        "enabled" if config.capture_enabled else "disabled",
        len(config.statement_routes),
        len(config.transaction_routes),
    )
    if ready_event is not None:
        ready_event.set()

    shutdown_controller = _ShutdownController(server=server, stop_event=stop_event, active_tasks=active_tasks)
    install_signal_handlers = _install_signal_handlers(loop, shutdown_controller)

    try:
        async with server:
            await stop_event.wait()
            server.close()
            await server.wait_closed()
            if active_tasks:
                await asyncio.gather(*active_tasks, return_exceptions=True)
    finally:
        if install_signal_handlers:
            _remove_signal_handlers(loop)


def run_router(config: RouterConfig) -> None:
    asyncio.run(serve_router(config))


class EmbeddedRouter:
    """Run the router in-process on a dedicated background thread."""

    def __init__(self, config: RouterConfig) -> None:
        self.config = config
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._shutdown_event: asyncio.Event | None = None
        self._error: BaseException | None = None
        self._ready_event = threading.Event()
        self._reset_runtime_state()

    def __enter__(self) -> "EmbeddedRouter":
        self.start()
        return self

    def __exit__(self, _exc_type: object, _exc: object, _tb: object) -> None:
        self.stop()

    def _reset_runtime_state(self) -> None:
        self._thread = None
        self._loop = None
        self._shutdown_event = None
        self._error = None
        self._ready_event.clear()

    def _ensure_stopped(self) -> None:
        if self._thread is not None:
            raise RuntimeError("embedded router is already running")

    def _thread_main(self) -> None:
        loop: asyncio.AbstractEventLoop | None = None
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            shutdown_event = asyncio.Event()
            self._loop = loop
            self._shutdown_event = shutdown_event
            loop.run_until_complete(serve_router(self.config, shutdown_event=shutdown_event, ready_event=self._ready_event))
        except BaseException as exc:  # pragma: no cover - surfaced back to caller
            self._error = exc
            self._ready_event.set()
        finally:
            if loop is not None:
                try:
                    loop.run_until_complete(loop.shutdown_asyncgens())
                    loop.run_until_complete(loop.shutdown_default_executor())
                except Exception:
                    pass
                asyncio.set_event_loop(None)
                loop.close()
            self._loop = None

    @property
    def is_running(self) -> bool:
        """Return ``True`` while the embedded router thread is active."""
        return self._thread is not None and self._thread.is_alive()

    def _wait_until_ready(self, timeout_s: float) -> None:
        if not self._ready_event.wait(timeout_s):
            raise TimeoutError(f"embedded router did not start within {timeout_s} seconds")
        if self._error is not None:
            raise RuntimeError("embedded router failed to start") from self._error

    def start(self, *, timeout_s: float = 10.0) -> None:
        """Start the embedded router and wait until it is listening."""
        self._ensure_stopped()
        self._ready_event.clear()
        self._error = None
        self._thread = threading.Thread(target=self._thread_main, name="pgrouter-embedded", daemon=True)
        self._thread.start()
        self._wait_until_ready(timeout_s)

    def _request_stop(self) -> None:
        if self._loop is not None and self._shutdown_event is not None:
            self._loop.call_soon_threadsafe(self._shutdown_event.set)

    def stop(self, *, timeout_s: float = 10.0) -> None:
        """Stop the embedded router and wait for its thread to exit."""
        if self._thread is None:
            return
        self._request_stop()
        self._thread.join(timeout_s)
        if self._thread.is_alive():
            raise TimeoutError(f"embedded router did not stop within {timeout_s} seconds")
        if self._error is not None:
            raise RuntimeError("embedded router stopped with an error") from self._error
        self._reset_runtime_state()
