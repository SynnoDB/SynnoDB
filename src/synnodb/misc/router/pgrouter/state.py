"""Session state for the router."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Dict, List, Optional

if TYPE_CHECKING:
    from .routes import PortalInterceptState, StatementRoute, TransactionHandler, TransactionRoute


@dataclass
class ColumnInfo:
    name: str
    type_oid: int
    format_code: int


@dataclass
class ParameterInfo:
    index: int
    format_code: int
    type_oid: Optional[int]
    value: Any
    length: Optional[int]
    is_null: bool = False


@dataclass
class PortalBinding:
    portal_name: str
    statement_name: str
    sql: Optional[str]
    parameter_formats: List[int] = field(default_factory=list)
    parameters: List[ParameterInfo] = field(default_factory=list)
    result_formats: List[int] = field(default_factory=list)


@dataclass
class CapturedStatement:
    query_id: str
    query: Optional[str]
    query_source: Optional[str]
    statement_name: Optional[str]
    portal_name: Optional[str]
    statement_execution_index: Optional[int]
    statement_reused: bool
    started_at: Optional[str]
    duration_ms: Optional[float]
    row_count: int
    parameters: List[Dict[str, Any]] = field(default_factory=list)
    result_types: List[Dict[str, Any]] = field(default_factory=list)
    result_file: Optional[str] = None
    command: Optional[str] = None
    error: Optional[Dict[str, str]] = None


@dataclass
class TransactionCapture:
    transaction_id: str
    started_at: Optional[str]
    started_at_perf: Optional[float]
    statements: List[CapturedStatement] = field(default_factory=list)


@dataclass
class SessionState:
    client_addr: str
    jsonl_path: str
    upstream_host: str
    upstream_port: int
    catalog_lookup_dsn: Optional[str]
    catalog_lookup_password: Optional[str]
    results_dir: str
    result_file_format: str
    session_id: str
    capture_enabled: bool = True
    tls_active: bool = False
    gss_active: bool = False
    startup_done: bool = False
    pending_ssl_response: bool = False
    pending_gssenc_response: bool = False
    prepared_sql: Dict[str, str] = field(default_factory=dict)
    prepared_param_types: Dict[str, List[int]] = field(default_factory=dict)
    prepared_row_descriptions: Dict[str, List[ColumnInfo]] = field(default_factory=dict)
    portal_to_statement: Dict[str, str] = field(default_factory=dict)
    portal_bindings: Dict[str, PortalBinding] = field(default_factory=dict)
    prepared_intercepts: Dict[str, StatementRoute] = field(default_factory=dict)
    portal_intercepts: Dict[str, PortalInterceptState] = field(default_factory=dict)
    prepared_execute_counts: Dict[str, int] = field(default_factory=dict)
    local_transaction_active: bool = False
    local_transaction_route: Optional[TransactionRoute] = None
    local_transaction_session: Optional[TransactionHandler] = None
    local_transaction_statement_index: int = 0
    local_sync_pending: bool = False
    row_description: Optional[List[ColumnInfo]] = None
    current_statement_sql: Optional[str] = None
    current_statement_id: Optional[str] = None
    current_statement_source: Optional[str] = None
    current_statement_name: Optional[str] = None
    current_portal_name: Optional[str] = None
    current_statement_execution_index: Optional[int] = None
    current_statement_started_at: Optional[float] = None
    current_statement_started_at_utc: Optional[str] = None
    current_row_count: int = 0
    current_result_writer: Any = None
    current_command_tag: Optional[str] = None
    current_error: Optional[Dict[str, str]] = None
    current_parameters: List[ParameterInfo] = field(default_factory=list)
    current_result_format_codes: List[int] = field(default_factory=list)
    current_statement_sequence: int = 0
    current_transaction_id_for_statement: Optional[str] = None
    current_transaction_status_for_statement: Optional[str] = None
    startup_params: Dict[str, str] = field(default_factory=dict)
    backend_pid: Optional[int] = None
    query_counter: int = 0
    transaction_counter: int = 0
    current_transaction_id: Optional[str] = None
    current_transaction_status: str = "I"
    current_transaction_capture: Optional[TransactionCapture] = None
