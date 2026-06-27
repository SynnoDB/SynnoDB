"""Protocol adapter interface for router transports."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Awaitable, Callable

if TYPE_CHECKING:
    import asyncio

    from ..config import RouterConfig


ClientHandler = Callable[["asyncio.StreamReader", "asyncio.StreamWriter", "RouterConfig"], Awaitable[None]]


@dataclass(frozen=True)
class ProtocolAdapter:
    """Concrete protocol transport/parser implementation used by the router."""

    name: str
    handle_client: ClientHandler
