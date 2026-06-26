"""Re-export DuckDB's exception and type classes verbatim.

The drop-in must never invent error types that escape to the user: engine/routing
failures fall back silently, and only a genuine DuckDB error propagates — and it is
DuckDB's *own* class, so ``except duckdb.CatalogException`` keeps working unchanged
after ``import synnodb as duckdb``.

This module copies every public exception class (anything subclassing ``Exception``)
and the ``typing`` namespace out of ``duckdb`` into its own namespace, so both
``synnodb.SomeException`` and ``from synnodb.duckdb_compat.errors import ...`` work.
"""
from __future__ import annotations

import duckdb as _duckdb

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

__all__ = [*exception_names, *(["typing"] if hasattr(_duckdb, "typing") else [])]
