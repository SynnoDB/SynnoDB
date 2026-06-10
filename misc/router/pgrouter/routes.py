"""Route models and handler execution helpers for embedded use."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import (
    Any,
    AsyncIterable,
    AsyncIterator,
    Awaitable,
    Callable,
    Iterable,
    Literal,
    Mapping,
    Optional,
    Protocol,
    Sequence,
    TypeAlias,
)

from .analyze.normalization import extract_record_values, normalize_query_structure, normalize_query_structure_strict
from .protocols.postgres.pgtypes import BOOL_OID, BYTEA_OID, FLOAT8_OID, INT4_OID, JSONB_OID, TEXT_OID, UUID_OID
from .state import ColumnInfo, ParameterInfo

log = logging.getLogger("pgrouter")

ParameterValue: TypeAlias = Any
QuerySource: TypeAlias = Literal["simple", "extended"]
StatementHandler = Callable[["StatementContext"], "RouteResult | Awaitable[RouteResult]"]
RouteRowSource: TypeAlias = Sequence[Mapping[str, Any]] | Iterable[Mapping[str, Any]] | AsyncIterable[Mapping[str, Any]]


class TransactionHandler(Protocol):
    """Stateful handler for one intercepted explicit transaction."""

    def on_statement(self, context: "TransactionStatementContext") -> "RouteResult | Awaitable[RouteResult]":
        ...

    def on_commit(self, context: "TransactionControlContext") -> "None | RouteResult | Awaitable[None | RouteResult]":
        ...

    def on_rollback(self, context: "TransactionControlContext") -> "None | RouteResult | Awaitable[None | RouteResult]":
        ...


TransactionHandlerFactory = Callable[["TransactionStartContext"], "TransactionHandler | Awaitable[TransactionHandler]"]


@dataclass(frozen=True)
class ResultColumn:
    """Column metadata for handler-produced result sets."""

    name: str
    type_oid: int = TEXT_OID
    format_code: int = 0


@dataclass(frozen=True)
class Statement:
    """Shared statement definition used by query and transaction routes."""

    structure: str
    result_columns: Sequence[ResultColumn] | None = None
    name: Optional[str] = None
    normalized_structure: str = field(init=False)

    def __post_init__(self) -> None:
        structure = self.structure.strip()
        if not structure:
            raise ValueError("statement structure must not be empty")
        object.__setattr__(self, "normalized_structure", normalize_query_structure_strict(structure))


@dataclass(frozen=True)
class StatementRoute:
    """One embedded handler route matched by normalized SQL structure."""

    statement: Statement
    handler: StatementHandler

    @property
    def structure(self) -> str:
        return self.statement.structure

    @property
    def result_columns(self) -> Sequence[ResultColumn] | None:
        return self.statement.result_columns

    @property
    def name(self) -> Optional[str]:
        return self.statement.name

    @property
    def normalized_structure(self) -> str:
        return self.statement.normalized_structure


@dataclass(frozen=True)
class TransactionRoute:
    """One explicit transaction handler route matched by ordered normalized statements."""

    statements: Sequence[Statement]
    handler: TransactionHandlerFactory
    name: Optional[str] = None
    normalized_structures: tuple[str, ...] = field(init=False)

    def __post_init__(self) -> None:
        if not self.statements:
            raise ValueError("transaction route statements must not be empty")
        object.__setattr__(
            self,
            "normalized_structures",
            tuple(statement.normalized_structure for statement in self.statements),
        )


@dataclass(frozen=True)
class StatementContext:
    """Decoded statement context passed to one embedded handler."""

    query: str
    normalized_structure: str
    query_source: QuerySource
    parameter_values: tuple[ParameterValue, ...]
    parameters: tuple[ParameterInfo, ...]
    statement_name: Optional[str]
    portal_name: Optional[str]
    route: StatementRoute


@dataclass(frozen=True)
class RouteResult:
    """Rows returned by one embedded handler."""

    rows: RouteRowSource = ()
    columns: Sequence[ResultColumn] | None = None
    command_tag: Optional[str] = None


@dataclass(frozen=True)
class TransactionStartContext:
    """Context passed to a transaction handler factory when the route is selected."""

    transaction_id: str
    route: TransactionRoute


@dataclass(frozen=True)
class TransactionStatementContext:
    """Context passed to a transaction handler for one statement."""

    transaction_id: str
    statement_index: int
    query: str
    normalized_structure: str
    query_source: QuerySource
    parameter_values: tuple[ParameterValue, ...]
    parameters: tuple[ParameterInfo, ...]
    statement_name: Optional[str]
    portal_name: Optional[str]
    route: TransactionRoute
    statement: Statement


@dataclass(frozen=True)
class TransactionControlContext:
    """Context passed to a transaction handler on commit or rollback."""

    transaction_id: str
    statement_count: int
    route: TransactionRoute


@dataclass
class PortalInterceptState:
    statement_route: StatementRoute
    describe_sent: bool = False


def match_statement_route(statement_routes: Sequence[StatementRoute], query: Optional[str]) -> StatementRoute | None:
    if not query:
        return None
    normalized_query = normalize_query_structure(query)
    for statement_route in statement_routes:
        if statement_route.normalized_structure == normalized_query:
            return statement_route
    return None


def resolve_parameter_values(query: str, parameters: Sequence[ParameterInfo]) -> tuple[ParameterValue, ...]:
    if parameters:
        return tuple(parameter.value for parameter in parameters)
    extracted = extract_record_values({"query": query, "parameters": []})
    return tuple(extracted)


def command_tag_for_result(result: RouteResult, row_count: int) -> str:
    if result.command_tag:
        return result.command_tag
    return f"SELECT {row_count}"


def statement_route_description_columns(route: StatementRoute) -> list[ColumnInfo]:
    return build_column_info(route.result_columns or [], [])


def transaction_route_name(route: TransactionRoute) -> str:
    return route.name or " ; ".join(route.normalized_structures)


def statement_name_for_log(statement: Statement) -> str:
    return statement.name or statement.normalized_structure


def match_transaction_route(transaction_routes: Sequence[TransactionRoute], query: Optional[str]) -> TransactionRoute | None:
    if not query:
        return None
    normalized_query = normalize_query_structure(query)
    matches = [route for route in transaction_routes if route.normalized_structures[0] == normalized_query]
    if not matches:
        return None
    if len(matches) > 1:
        raise ValueError(f"multiple transaction routes match first statement: {normalized_query}")
    return matches[0]


async def invoke_statement_handler(
    statement_route: StatementRoute,
    *,
    query: str,
    query_source: QuerySource,
    parameters: Sequence[ParameterInfo],
    statement_name: Optional[str],
    portal_name: Optional[str],
) -> RouteResult:
    """Run one statement route handler and normalize the returned result object."""
    resolved_parameters = tuple(parameters)
    parameter_values = resolve_parameter_values(query, resolved_parameters)
    log.info(
        "Handler match route=%r source=%s stmt=%r portal=%r params=%s sql=%r",
        statement_route_name(statement_route),
        query_source,
        statement_name,
        portal_name,
        list(parameter_values),
        query,
    )
    context = StatementContext(
        query=query,
        normalized_structure=statement_route.normalized_structure,
        query_source=query_source,
        parameter_values=parameter_values,
        parameters=resolved_parameters,
        statement_name=statement_name,
        portal_name=portal_name,
        route=statement_route,
    )
    handler_result = statement_route.handler(context)
    if asyncio.iscoroutine(handler_result):
        handler_result = await handler_result
    if not isinstance(handler_result, RouteResult):
        raise TypeError("statement handler must return RouteResult")
    log.info(
        "Handler result route=%r source=%s stmt=%r portal=%r rows=%s command=%r",
        statement_route_name(statement_route),
        query_source,
        statement_name,
        portal_name,
        describe_route_row_source(handler_result.rows),
        handler_result.command_tag,
    )
    return handler_result


async def open_transaction_handler(
    transaction_route: TransactionRoute,
    *,
    transaction_id: str,
) -> TransactionHandler:
    log.info(
        "Transaction handler start route=%r tx=%s",
        transaction_route_name(transaction_route),
        transaction_id,
    )
    start_context = TransactionStartContext(transaction_id=transaction_id, route=transaction_route)
    handler_session = transaction_route.handler(start_context)
    if asyncio.iscoroutine(handler_session):
        handler_session = await handler_session
    if not hasattr(handler_session, "on_statement"):
        raise TypeError("transaction handler factory must return an object with on_statement(...)")
    return handler_session


async def invoke_transaction_statement_handler(
    transaction_route: TransactionRoute,
    handler_session: TransactionHandler,
    *,
    statement: Statement,
    transaction_id: str,
    statement_index: int,
    query: str,
    query_source: QuerySource,
    parameters: Sequence[ParameterInfo],
    statement_name: Optional[str],
    portal_name: Optional[str],
) -> RouteResult:
    resolved_parameters = tuple(parameters)
    parameter_values = resolve_parameter_values(query, resolved_parameters)
    context = TransactionStatementContext(
        transaction_id=transaction_id,
        statement_index=statement_index,
        query=query,
        normalized_structure=normalize_query_structure(query),
        query_source=query_source,
        parameter_values=parameter_values,
        parameters=resolved_parameters,
        statement_name=statement_name,
        portal_name=portal_name,
        route=transaction_route,
        statement=statement,
    )
    log.info(
        "Transaction handler statement route=%r statement=%r tx=%s index=%d stmt=%r portal=%r params=%s sql=%r",
        transaction_route_name(transaction_route),
        statement_name_for_log(statement),
        transaction_id,
        statement_index,
        statement_name,
        portal_name,
        list(parameter_values),
        query,
    )
    handler_result = handler_session.on_statement(context)
    if asyncio.iscoroutine(handler_result):
        handler_result = await handler_result
    if not isinstance(handler_result, RouteResult):
        raise TypeError("transaction handler on_statement must return RouteResult")
    return handler_result


async def invoke_transaction_control_handler(
    transaction_route: TransactionRoute,
    handler_session: TransactionHandler,
    *,
    transaction_id: str,
    statement_count: int,
    action: Literal["commit", "rollback"],
) -> RouteResult | None:
    context = TransactionControlContext(
        transaction_id=transaction_id,
        statement_count=statement_count,
        route=transaction_route,
    )
    method = handler_session.on_commit if action == "commit" else handler_session.on_rollback
    handler_result = method(context)
    if asyncio.iscoroutine(handler_result):
        handler_result = await handler_result
    if handler_result is None:
        return None
    if not isinstance(handler_result, RouteResult):
        raise TypeError(f"transaction handler on_{action} must return RouteResult or None")
    return handler_result


async def iterate_route_rows(rows: RouteRowSource) -> AsyncIterator[dict[str, Any]]:
    if hasattr(rows, "__aiter__"):
        async for row in rows:  # type: ignore[union-attr]
            yield dict(row)
        return
    for row in rows:  # type: ignore[not-an-iterable]
        yield dict(row)


async def prepare_route_result_rows(
    default_columns: Sequence[ResultColumn] | None,
    result: RouteResult,
    format_codes: Sequence[int],
) -> tuple[list[ColumnInfo], AsyncIterator[dict[str, Any]]]:
    source_columns = result.columns or default_columns
    row_iter = iterate_route_rows(result.rows)
    if source_columns:
        return build_column_info(source_columns, format_codes), row_iter
    try:
        first_row = await row_iter.__anext__()
    except StopAsyncIteration:
        return [], row_iter
    inferred_columns = [ResultColumn(name=name, type_oid=infer_type_oid(value)) for name, value in first_row.items()]

    async def with_first() -> AsyncIterator[dict[str, Any]]:
        yield first_row
        async for row in row_iter:
            yield row

    return build_column_info(inferred_columns, format_codes), with_first()


def validate_transaction_route_set(transaction_routes: Sequence[TransactionRoute]) -> None:
    first_structure_to_route: dict[str, TransactionRoute] = {}
    for route in transaction_routes:
        first_structure = route.normalized_structures[0]
        existing = first_structure_to_route.get(first_structure)
        if existing is not None:
            raise ValueError(
                f"transaction routes must have unique first statements: {transaction_route_name(existing)!r} and {transaction_route_name(route)!r}"
            )
        first_structure_to_route[first_structure] = route


def statement_route_name(route: StatementRoute) -> str:
    return route.name or route.normalized_structure


def describe_route_row_source(rows: RouteRowSource) -> str:
    if isinstance(rows, Sequence):
        return str(len(rows))
    return "stream"


def build_column_info(columns: Sequence[ResultColumn], format_codes: Sequence[int]) -> list[ColumnInfo]:
    resolved: list[ColumnInfo] = []
    for index, column in enumerate(columns):
        resolved.append(
            ColumnInfo(
                name=column.name,
                type_oid=column.type_oid,
                format_code=resolve_format_code(column.format_code, index, format_codes),
            )
        )
    return resolved


def resolve_format_code(default_format_code: int, index: int, format_codes: Sequence[int]) -> int:
    if not format_codes:
        return default_format_code
    if len(format_codes) == 1:
        return format_codes[0]
    return format_codes[index]


def infer_type_oid(value: Any) -> int:
    if isinstance(value, bool):
        return BOOL_OID
    if isinstance(value, int):
        return INT4_OID
    if isinstance(value, float):
        return FLOAT8_OID
    if isinstance(value, (bytes, bytearray)):
        return BYTEA_OID
    if isinstance(value, dict) or isinstance(value, list):
        return JSONB_OID
    try:
        import uuid

        if isinstance(value, uuid.UUID):
            return UUID_OID
    except Exception:  # pragma: no cover - defensive only
        pass
    return TEXT_OID
