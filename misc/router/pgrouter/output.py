"""Filesystem output helpers for query capture."""

from __future__ import annotations

import json
import shutil
import time
from pathlib import Path
from dataclasses import asdict
from typing import Any, Callable, Dict, Optional

import pandas as pd

from .protocols.postgres.catalog import column_type_name, resolve_column_type_names
from .protocols.postgres.wire import format_code_name
from .state import CapturedStatement, SessionState, TransactionCapture, ParameterInfo


class ResultWriter:
    def __init__(self, output_path: Path) -> None:
        self.output_path = output_path
        self.closed = False

    def append_row(self, row: Dict[str, Any]) -> None:  # pragma: no cover - interface only
        raise NotImplementedError

    def finalize(self) -> Optional[str]:  # pragma: no cover - interface only
        raise NotImplementedError

    def abort(self) -> None:
        self.closed = True


class JsonResultWriter(ResultWriter):
    def __init__(self, output_path: Path) -> None:
        super().__init__(output_path)
        self._tmp_path = output_path.with_name(f".{output_path.name}.tmp")
        self._fh = None
        self._row_count = 0

    def append_row(self, row: Dict[str, Any]) -> None:
        if self.closed:
            raise RuntimeError("result writer is closed")
        if self._fh is None:
            self.output_path.parent.mkdir(parents=True, exist_ok=True)
            self._fh = open(self._tmp_path, "w", encoding="utf-8")
            self._fh.write("[")
        if self._row_count:
            self._fh.write(",")
        self._fh.write(json.dumps(row, ensure_ascii=True))
        self._row_count += 1

    def finalize(self) -> Optional[str]:
        if self.closed:
            return str(self.output_path) if self.output_path.exists() else None
        self.closed = True
        if self._row_count == 0:
            self.abort()
            return None
        assert self._fh is not None
        self._fh.write("]\n")
        self._fh.close()
        self._fh = None
        self._tmp_path.replace(self.output_path)
        return str(self.output_path)

    def abort(self) -> None:
        if self.closed and not self._tmp_path.exists():
            return
        self.closed = True
        if self._fh is not None:
            self._fh.close()
            self._fh = None
        if self._tmp_path.exists():
            self._tmp_path.unlink()


class PickleResultWriter(ResultWriter):
    def __init__(self, output_path: Path) -> None:
        super().__init__(output_path)
        self._rows: list[Dict[str, Any]] = []

    def append_row(self, row: Dict[str, Any]) -> None:
        if self.closed:
            raise RuntimeError("result writer is closed")
        self._rows.append(row)

    def finalize(self) -> Optional[str]:
        if self.closed:
            return str(self.output_path) if self.output_path.exists() else None
        self.closed = True
        if not self._rows:
            return None
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        pd.DataFrame(self._rows).to_pickle(self.output_path)
        return str(self.output_path)

    def abort(self) -> None:
        self.closed = True
        self._rows.clear()


ResultWriterFactory = Callable[[Path], ResultWriter]
RESULT_WRITER_FACTORIES: dict[str, tuple[str, ResultWriterFactory]] = {
    "json": (".json", JsonResultWriter),
    "pickle": (".pkl", PickleResultWriter),
}


def register_result_writer(name: str, suffix: str, factory: ResultWriterFactory) -> None:
    RESULT_WRITER_FACTORIES[name] = (suffix, factory)


def result_file_formats() -> list[str]:
    return sorted(RESULT_WRITER_FACTORIES)


def serialize_parameter(param: ParameterInfo) -> Dict[str, Any]:
    return {
        "index": param.index,
        "format": format_code_name(param.format_code),
        "type_oid": param.type_oid,
        "type_name": column_type_name(param.type_oid) if param.type_oid is not None else None,
        "length": param.length,
        "is_null": param.is_null,
        "value": param.value,
    }


def result_writer_entry(result_file_format: str) -> tuple[str, ResultWriterFactory]:
    entry = RESULT_WRITER_FACTORIES.get(result_file_format)
    if entry is None:
        raise ValueError(f"unsupported result file format: {result_file_format}")
    return entry


def result_file_suffix(result_file_format: str) -> str:
    suffix, _factory = result_writer_entry(result_file_format)
    return suffix


def result_output_path(state: SessionState) -> Path:
    query_id = state.current_statement_id or f"{state.session_id}:q{state.current_statement_sequence:04d}"
    return Path(state.results_dir) / f"{query_id}{result_file_suffix(state.result_file_format)}"


def ensure_result_writer(state: SessionState) -> ResultWriter:
    if not state.capture_enabled:
        raise RuntimeError("result writers are unavailable when capture is disabled")
    if state.current_result_writer is None:
        _suffix, factory = result_writer_entry(state.result_file_format)
        state.current_result_writer = factory(result_output_path(state))
    return state.current_result_writer


def append_result_row(state: SessionState, row: Dict[str, Any]) -> None:
    if not state.capture_enabled:
        return
    writer = ensure_result_writer(state)
    writer.append_row(row)


def finalize_result_writer(state: SessionState) -> Optional[str]:
    writer = state.current_result_writer
    state.current_result_writer = None
    if writer is None:
        return None
    return writer.finalize()


def abort_result_writer(state: SessionState) -> None:
    writer = state.current_result_writer
    state.current_result_writer = None
    if writer is not None:
        writer.abort()


def ensure_transaction_id(state: SessionState) -> str:
    if state.current_transaction_id is None:
        state.transaction_counter += 1
        state.current_transaction_id = f"{state.session_id}:tx{state.transaction_counter:04d}"
    return state.current_transaction_id


def query_duration_ms(state: SessionState) -> Optional[float]:
    if state.current_statement_started_at is None:
        return None
    return round((time.perf_counter() - state.current_statement_started_at) * 1000, 3)


def serialize_result_types(state: SessionState) -> list[Dict[str, Any]]:
    return [
        {
            "name": col.name,
            "type_oid": col.type_oid,
            "type_name": column_type_name(col.type_oid),
            "format": "text" if col.format_code == 0 else "binary",
        }
        for col in (state.row_description or [])
    ]


def statement_reused(state: SessionState) -> bool:
    return state.current_statement_execution_index is not None and state.current_statement_execution_index > 1


EXPLICIT_TRANSACTION_COMMANDS = frozenset({"BEGIN", "START TRANSACTION", "COMMIT", "ROLLBACK"})
ACTIVE_TRANSACTION_STATES = frozenset({"T", "E"})


def query_belongs_to_explicit_transaction(state: SessionState) -> bool:
    return (
        state.current_command_tag in EXPLICIT_TRANSACTION_COMMANDS
        or state.current_transaction_id_for_statement is not None
        or state.current_transaction_status_for_statement in ACTIVE_TRANSACTION_STATES
        or state.current_transaction_capture is not None
    )


def display_transaction_status(status: str | None) -> str | None:
    if status is None:
        return None
    if status == "I":
        return "idle"
    if status == "E":
        return "failed_transaction"
    if status == "T":
        return "open"
    return status


def build_query_record(state: SessionState, *, result_file: Optional[str], duration_ms: Optional[float]) -> Dict[str, Any]:
    transaction_id = state.current_transaction_id_for_statement
    transaction_status = display_transaction_status(state.current_transaction_status_for_statement)
    if state.current_command_tag in ("BEGIN", "START TRANSACTION"):
        transaction_id = ensure_transaction_id(state)
        transaction_status = "open"

    record: Dict[str, Any] = {
        "session_id": state.session_id,
        "transaction_id": transaction_id,
        "transaction_status": transaction_status,
        "query_id": state.current_statement_id,
        "query": state.current_statement_sql,
        "query_source": state.current_statement_source,
        "statement_name": state.current_statement_name,
        "portal_name": state.current_portal_name,
        "statement_execution_index": state.current_statement_execution_index,
        "statement_reused": statement_reused(state),
        "client_addr": state.client_addr,
        "backend_pid": state.backend_pid,
        "started_at": state.current_statement_started_at_utc,
        "duration_ms": duration_ms,
        "row_count": state.current_row_count,
        "parameters": [serialize_parameter(param) for param in state.current_parameters],
        "result_file": result_file,
        "result_types": serialize_result_types(state),
        "command": state.current_command_tag,
    }
    if state.current_error:
        record["error"] = state.current_error
    return record


def build_statement_record(state: SessionState, *, duration_ms: Optional[float]) -> CapturedStatement:
    result_file = finalize_result_writer(state)
    return CapturedStatement(
        query_id=state.current_statement_id or f"{state.session_id}:q{state.current_statement_sequence:04d}",
        query=state.current_statement_sql,
        query_source=state.current_statement_source,
        statement_name=state.current_statement_name,
        portal_name=state.current_portal_name,
        statement_execution_index=state.current_statement_execution_index,
        statement_reused=statement_reused(state),
        started_at=state.current_statement_started_at_utc,
        duration_ms=duration_ms,
        row_count=state.current_row_count,
        parameters=[serialize_parameter(param) for param in state.current_parameters],
        result_types=serialize_result_types(state),
        result_file=result_file,
        command=state.current_command_tag,
        error=state.current_error,
    )


def ensure_transaction_capture(state: SessionState, transaction_id: str) -> TransactionCapture:
    capture = state.current_transaction_capture
    if capture is not None:
        return capture
    capture = TransactionCapture(
        transaction_id=transaction_id,
        started_at=state.current_statement_started_at_utc,
        started_at_perf=state.current_statement_started_at,
    )
    state.current_transaction_capture = capture
    return capture


def transaction_outcome(capture: TransactionCapture) -> str:
    if not capture.statements:
        return "unknown"
    final_command = capture.statements[-1].command
    if final_command == "COMMIT":
        return "committed"
    if final_command == "ROLLBACK":
        return "rolled_back"
    return "open"


def build_transaction_record(
    state: SessionState,
    capture: TransactionCapture,
    *,
    duration_ms: Optional[float],
) -> Dict[str, Any]:
    joined_query = "; ".join(statement.query for statement in capture.statements if statement.query)
    total_rows = sum(statement.row_count for statement in capture.statements)
    final_command = capture.statements[-1].command if capture.statements else None
    return {
        "session_id": state.session_id,
        "transaction_id": capture.transaction_id,
        "transaction_status": transaction_outcome(capture),
        "query_id": capture.transaction_id,
        "query": joined_query,
        "query_source": "transaction",
        "statement_name": None,
        "portal_name": None,
        "statement_execution_index": None,
        "statement_reused": False,
        "client_addr": state.client_addr,
        "backend_pid": state.backend_pid,
        "started_at": capture.started_at,
        "duration_ms": duration_ms,
        "row_count": total_rows,
        "statement_count": len(capture.statements),
        "parameters": [],
        "result_file": None,
        "result_types": [],
        "command": final_command,
        "statements": [asdict(statement) for statement in capture.statements],
    }


def maybe_finalize_transaction_capture(
    state: SessionState,
    *,
    command_tag: Optional[str],
    duration_ms: Optional[float],
) -> Optional[Dict[str, Any]]:
    capture = state.current_transaction_capture
    if capture is None or command_tag not in {"COMMIT", "ROLLBACK"}:
        return None
    started_at_perf = capture.started_at_perf
    total_duration_ms = duration_ms
    if started_at_perf is not None and state.current_statement_started_at is not None:
        total_duration_ms = round((time.perf_counter() - started_at_perf) * 1000, 3)
    state.current_transaction_capture = None
    return build_transaction_record(state, capture, duration_ms=total_duration_ms)


def append_jsonl_record(jsonl_path: str, record: Dict[str, Any]) -> None:
    with open(jsonl_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=True) + "\n")


async def append_query_record(state: SessionState, *, duration_ms: Optional[float] = None) -> None:
    if not state.capture_enabled:
        return
    if not state.current_statement_sql:
        return
    await resolve_column_type_names(state)
    resolved_duration_ms = query_duration_ms(state) if duration_ms is None else duration_ms
    if query_belongs_to_explicit_transaction(state):
        transaction_id = state.current_transaction_id_for_statement
        if state.current_command_tag in ("BEGIN", "START TRANSACTION"):
            transaction_id = ensure_transaction_id(state)
        if transaction_id is None:
            transaction_id = ensure_transaction_id(state)
        capture = ensure_transaction_capture(state, transaction_id)
        capture.statements.append(build_statement_record(state, duration_ms=resolved_duration_ms))
        transaction_record = maybe_finalize_transaction_capture(
            state,
            command_tag=state.current_command_tag,
            duration_ms=resolved_duration_ms,
        )
        if transaction_record is not None:
            append_jsonl_record(state.jsonl_path, transaction_record)
        return
    result_file = finalize_result_writer(state)
    record = build_query_record(
        state,
        result_file=result_file,
        duration_ms=resolved_duration_ms,
    )
    append_jsonl_record(state.jsonl_path, record)


def initialize_capture_outputs(jsonl_path: str, results_dir: str, append: bool) -> None:
    jsonl = Path(jsonl_path)
    results = Path(results_dir)
    if append:
        jsonl.parent.mkdir(parents=True, exist_ok=True)
        jsonl.touch(exist_ok=True)
        results.mkdir(parents=True, exist_ok=True)
        return
    jsonl.parent.mkdir(parents=True, exist_ok=True)
    with open(jsonl, "w", encoding="utf-8"):
        pass
    if results.exists():
        shutil.rmtree(results)
    results.mkdir(parents=True, exist_ok=True)
