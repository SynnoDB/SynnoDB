"""PostgreSQL protocol adapter."""

from __future__ import annotations

from ..base import ProtocolAdapter


async def _handle_client(*args: object, **kwargs: object) -> None:
    from .server import handle_client

    await handle_client(*args, **kwargs)


postgres_adapter = ProtocolAdapter(name="postgres", handle_client=_handle_client)

__all__ = ["postgres_adapter"]
