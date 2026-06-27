"""Catalog lookup helpers for resolving type OIDs to readable names."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, Optional

from .constants import TYPE_OID_NAMES
from ...state import SessionState

log = logging.getLogger("pgrouter")

try:
    import psycopg
except ImportError:  # pragma: no cover - exercised only when psycopg is unavailable
    psycopg = None


def column_type_name(type_oid: int) -> Optional[str]:
    return TYPE_OID_NAMES.get(type_oid)


def build_catalog_lookup_connect_kwargs(state: SessionState) -> Optional[Dict[str, Any]]:
    user = state.startup_params.get("user")
    database = state.startup_params.get("database")
    if state.catalog_lookup_dsn:
        return {
            "conninfo": state.catalog_lookup_dsn,
            "application_name": "pg-router-type-lookup",
            "sslmode": "disable",
        }
    if not user or not database:
        return None
    kwargs: Dict[str, Any] = {
        "host": state.upstream_host,
        "port": state.upstream_port,
        "user": user,
        "dbname": database,
        "application_name": "pg-router-type-lookup",
    }
    if state.catalog_lookup_password is not None:
        kwargs["password"] = state.catalog_lookup_password
    return kwargs


def fetch_type_names_from_catalog_sync(state: SessionState, type_oids: list[int]) -> Dict[int, str]:
    if psycopg is None:
        raise RuntimeError("psycopg is required for authenticated catalog lookups")
    connect_kwargs = build_catalog_lookup_connect_kwargs(state)
    if not connect_kwargs or not type_oids:
        return {}
    with psycopg.connect(**connect_kwargs) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                select
                    t.oid::int4,
                    case
                        when n.nspname = 'pg_catalog' then t.typname
                        else n.nspname || '.' || t.typname
                    end as type_name
                from pg_catalog.pg_type t
                join pg_catalog.pg_namespace n on n.oid = t.typnamespace
                where t.oid = any(%s)
                order by t.oid
                """,
                (sorted(set(type_oids)),),
            )
            return {int(oid): str(type_name) for oid, type_name in cur.fetchall()}


async def fetch_type_names_from_catalog(state: SessionState, type_oids: list[int]) -> Dict[int, str]:
    return await asyncio.to_thread(fetch_type_names_from_catalog_sync, state, type_oids)


async def resolve_column_type_names(state: SessionState) -> None:
    unresolved = {
        col.type_oid
        for col in (state.row_description or [])
        if column_type_name(col.type_oid) is None
    }
    unresolved.update(
        param.type_oid
        for param in state.current_parameters
        if param.type_oid is not None and column_type_name(param.type_oid) is None
    )
    if not unresolved:
        return
    try:
        resolved = await fetch_type_names_from_catalog(state, sorted(unresolved))
    except Exception as exc:
        log.warning("Type lookup failed, falling back to OIDs only: %s", exc)
        return
    TYPE_OID_NAMES.update(resolved)
