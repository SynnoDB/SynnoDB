"""PostgreSQL router package.

Public embedded API:
- ``RouterConfig`` configures standalone or embedded router instances.
- ``EmbeddedRouter`` runs the router in a background thread.
- ``StatementRoute`` registers one normalized statement structure and its handler.
- ``TransactionRoute`` registers one explicit transaction pattern and its stateful handler.
- ``StatementContext`` and ``RouteResult`` define the handler call contract.
"""

from .config import RouterConfig
from .routes import (
    RouteResult,
    ResultColumn,
    StatementContext,
    StatementRoute,
    Statement,
    TransactionControlContext,
    TransactionRoute,
    TransactionStartContext,
    TransactionStatementContext,
)
from .service import EmbeddedRouter

__all__ = [
    "EmbeddedRouter",
    "RouteResult",
    "ResultColumn",
    "RouterConfig",
    "Statement",
    "StatementContext",
    "StatementRoute",
    "TransactionControlContext",
    "TransactionRoute",
    "TransactionStartContext",
    "TransactionStatementContext",
]
