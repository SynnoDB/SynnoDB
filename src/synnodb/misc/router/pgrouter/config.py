"""Shared router runtime configuration."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Sequence

from .protocols.base import ProtocolAdapter
from .routes import StatementRoute, TransactionRoute, validate_transaction_route_set


@dataclass(frozen=True)
class RouterConfig:
    """Runtime configuration for standalone and embedded router use."""

    listen_host: str = "127.0.0.1"
    listen_port: int = 5432
    upstream_host: str = "127.0.0.1"
    upstream_port: int = 15433
    jsonl_path: str = "queries.jsonl"
    catalog_lookup_dsn: Optional[str] = None
    catalog_lookup_password: Optional[str] = None
    results_dir: str = "results"
    result_file_format: str = "json"
    capture_enabled: bool = False
    append: bool = False
    protocol_adapter: ProtocolAdapter | None = None
    statement_routes: Sequence[StatementRoute] = field(default_factory=tuple)
    transaction_routes: Sequence[TransactionRoute] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if self.protocol_adapter is None:
            from .protocols.postgres import postgres_adapter

            object.__setattr__(self, "protocol_adapter", postgres_adapter)
        validate_transaction_route_set(self.transaction_routes)
