"""Re-export DuckDB's exception and type classes verbatim, plus SynnoDB's own.

A routing/engine failure never escapes: it falls back silently, and only a genuine
DuckDB error propagates, as DuckDB's *own* class, so ``except duckdb.CatalogException``
keeps working unchanged after ``import synnodb as duckdb``.

The one intentional SynnoDB-specific error is :class:`WriteNotSupportedError`, raised
when a write is issued while writes are disabled. It is a deliberate, user-facing "not
supported yet" signal (writes are not silently dropped), so it is meant to escape.

This module copies every public exception class (anything subclassing ``Exception``)
and the ``typing`` namespace out of ``duckdb`` into its own namespace, so both
``synnodb.SomeException`` and ``from synnodb.duckdb_compat.errors import ...`` work.
"""

from __future__ import annotations

import duckdb as _duckdb


class WriteNotSupportedError(NotImplementedError):
    """A write/DDL statement was issued on a SynnoConnection while writes are disabled.

    SynnoDB currently accelerates read-only queries only; writes are not yet supported.
    Subclasses ``NotImplementedError`` so it reads as "not implemented yet" and is not
    swallowed by ``except duckdb.Error``.
    """


def write_not_supported_message(sql: str) -> str:
    """The user-facing notice for a blocked write, naming the leading statement and the
    supported workarounds."""
    from synnodb.router.normalize import _leading_keyword

    kw = (_leading_keyword(sql) or "").upper() or "this statement"
    return (
        f"SynnoDB does not support writes yet ({kw}). It currently accelerates read-only "
        f"queries; writes and DDL are disabled. Run the statement on the underlying DuckDB "
        f"connection via the escape hatch, e.g. con.duckdb.execute(sql), or re-enable "
        f"passthrough with SYNNODB_BLOCK_WRITES=off (or RouterPolicy(block_writes=False))."
    )


# Names copied from duckdb (populated below); used by the package __init__.
exception_names: list[str] = []

for _name in dir(_duckdb):
    if _name.startswith("_"):
        continue
    _obj = getattr(_duckdb, _name)
    if isinstance(_obj, type) and issubclass(_obj, BaseException):
        globals()[_name] = _obj
        exception_names.append(_name)

# DuckDB's value-type namespace (BIGINT, VARCHAR, STRUCT, ...), if present.
if hasattr(_duckdb, "typing"):
    typing = _duckdb.typing  # noqa: F811  (intentional re-export)

__all__ = [
    "WriteNotSupportedError",
    *exception_names,
    *(["typing"] if hasattr(_duckdb, "typing") else []),
]
