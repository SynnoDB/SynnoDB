import json
import math
import mimetypes
import os
import socketserver
import threading
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from synnodb.observability.logging.run_stats_drain import DataDrain, _duckdb_col_value
from synnodb.settings import DEFAULT_WANDB_ENTITY, DEFAULT_WANDB_PROJECT

_UI_DIR = Path(__file__).parent

# Directories never surfaced by the code inspector — VCS metadata, caches and
# dependency trees would only bury the generated code in noise.
_WORKSPACE_SKIP_DIRS = frozenset(
    {".git", "__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache", "node_modules"}
)
# Cap a single file's served content so a stray multi-MB artifact can't stall the browser.
_WORKSPACE_MAX_BYTES = 2_000_000


def _list_workspace_files(root: Path) -> list[str]:
    """Return sorted workspace-relative paths of every regular file under root.

    Skip-dirs are pruned before descent (so ``.git``/caches are never walked),
    and symlinks are ignored entirely so the listing can only ever name files
    that physically live inside the workspace.
    """
    root = root.resolve()
    files: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = [d for d in dirnames if d not in _WORKSPACE_SKIP_DIRS]
        base = Path(dirpath)
        for name in filenames:
            path = base / name
            if path.is_symlink() or not path.is_file():
                continue
            files.append(path.relative_to(root).as_posix())
    files.sort()
    return files


def _read_workspace_file(root: Path, rel: str) -> dict | None:
    """Read one workspace file. Returns None if the path escapes root or is missing.

    ``resolve()`` collapses any symlinks/``..`` before the containment check, so
    a request can never read outside the workspace. Paths that descend through a
    skip-dir (``.git``, caches, ``node_modules``) are rejected too — those are
    pruned from ``/api/files`` and must not be reachable by guessing a rel. At
    most ``_WORKSPACE_MAX_BYTES`` (+1 sentinel) is read into memory regardless of
    the real file size.
    """
    root = root.resolve()
    target = (root / rel).resolve()
    if not target.is_relative_to(root) or not target.is_file():
        return None
    if _WORKSPACE_SKIP_DIRS.intersection(target.relative_to(root).parts):
        return None
    size = target.stat().st_size
    with target.open("rb") as fh:
        head = fh.read(_WORKSPACE_MAX_BYTES + 1)
    if b"\x00" in head[:8192]:
        return {"path": rel, "binary": True, "size": size}
    truncated = len(head) > _WORKSPACE_MAX_BYTES
    text = head[:_WORKSPACE_MAX_BYTES].decode("utf-8", errors="replace")
    return {"path": rel, "content": text, "size": size, "truncated": truncated}


def _local_files_payload(root: "Path | None") -> bytes:
    """JSON body for /api/files backed by a local workspace directory."""
    if root is None or not root.is_dir():
        return json.dumps({"available": False, "files": []}).encode()
    return json.dumps(
        {"available": True, "root": str(root), "files": _list_workspace_files(root)}
    ).encode()


def _local_file_payload(root: "Path | None", rel: str) -> "bytes | None":
    """JSON body for /api/file backed by a local workspace directory (None → 404)."""
    if root is None or not root.is_dir() or not rel:
        return None
    result = _read_workspace_file(root, rel)
    return json.dumps(result).encode() if result is not None else None

# A single live-dashboard HTTP server is shared by every stage that runs in ONE process, so the
# dashboard URL keeps following the active stage. Without this, each stage's LiveDashboardDrain
# started its own server and, because the previous stage's server still held the port, hopped to
# the next one (8765 -> 8766 -> ...) - stranding the dashboard on the first stage (the storage
# plan) while later stages (base impl) served on a port nobody was watching. The first drain binds
# the server; later drains reuse it and retarget ``_ACTIVE_SNAPSHOT["fn"]`` at their own data.
_SHARED_SERVER: "tuple[socketserver.TCPServer, int] | None" = None
_SHARED_SERVER_LOCK = threading.Lock()
_ACTIVE_SNAPSHOT: dict = {
    "fn": lambda: json.dumps({"meta": {}, "steps": [], "data": {}}),
    "workspace": lambda: None,
}


def _make_http_server(
    host: str,
    start_port: int,
    snapshot_fn,
    post_handlers: dict | None = None,
    file_list_fn=None,
    file_read_fn=None,
) -> tuple[int, "socketserver.TCPServer"]:
    """Bind an HTTP server that calls snapshot_fn() for /api/stats.

    post_handlers maps path → callable(body: bytes) → bytes for POST endpoints.
    file_list_fn, if given, is a callable returning the JSON body (bytes) for
    /api/files; file_read_fn(rel) returns the JSON body (bytes) for /api/file or
    None for 404. Together they back the code inspector. When file_list_fn is
    None the inspector endpoints report the workspace as unavailable.
    Returns (port, server) — caller is responsible for starting the serve thread.
    """
    import http.server

    class _Server(socketserver.ThreadingMixIn, socketserver.TCPServer):
        allow_reuse_address = True
        daemon_threads = True

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            path = urlparse(self.path).path
            if path == "/api/stats":
                self._reply(snapshot_fn().encode(), "application/json")
                return
            if path == "/api/files":
                body = (
                    file_list_fn()
                    if file_list_fn is not None
                    else json.dumps({"available": False, "files": []}).encode()
                )
                self._reply(body, "application/json")
                return
            if path == "/api/file":
                rel = (parse_qs(urlparse(self.path).query).get("path") or [""])[0]
                body = file_read_fn(rel) if file_read_fn is not None else None
                if body is None:
                    self.send_error(404)
                    return
                self._reply(body, "application/json")
                return
            rel = "index.html" if path in ("/", "") else path.lstrip("/")
            file = _UI_DIR / rel
            if not file.is_file() or not file.resolve().is_relative_to(
                _UI_DIR.resolve()
            ):
                self.send_error(404)
                return
            ct = mimetypes.guess_type(file.name)[0] or "application/octet-stream"
            self._reply(file.read_bytes(), ct)

        def do_POST(self):  # noqa: N802
            path = urlparse(self.path).path
            if post_handlers and path in post_handlers:
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length) if length else b""
                result = post_handlers[path](body)
                self._reply(result, "application/json")
            else:
                self.send_error(404)

        def _reply(self, body: bytes, ct: str) -> None:
            try:
                self.send_response(200)
                self.send_header("Content-Type", ct)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                # Client disconnected mid-response (e.g. browser tab closed or
                # navigated away while polling). Nothing to do but stop writing.
                self.close_connection = True

        def log_message(self, fmt, *args) -> None:  # noqa: N802
            pass

    for port in range(start_port, start_port + 10):
        try:
            server = _Server((host, port), _Handler)
            return port, server
        except OSError:
            continue
    raise RuntimeError(f"Could not bind to any port in {start_port}-{start_port + 9}")


def _duckdb_snapshot(db_path: Path) -> str:
    """Read run_metrics from a DuckDB file and return a /api/stats JSON string."""
    import duckdb

    con = duckdb.connect(str(db_path), read_only=True)
    try:
        col_info = con.execute("PRAGMA table_info(run_metrics)").fetchall()
        cols = [row[1] for row in col_info]
        rows = con.execute("SELECT * FROM run_metrics ORDER BY step").fetchall()
    finally:
        con.close()

    data: dict[str, dict] = {}
    meta: dict = {
        "run_name": None,
        "wandb_run_id": None,
        "system_name": None,
        "start_time": None,
    }

    for raw in rows:
        row_dict = dict(zip(cols, raw))
        step = int(row_dict.pop("step"))
        parsed: dict = {}
        for k, v in row_dict.items():
            if v is None:
                continue
            if isinstance(v, str):
                try:
                    decoded = json.loads(v)
                    parsed[k] = decoded
                except (json.JSONDecodeError, ValueError):
                    parsed[k] = v
            elif isinstance(v, float) and not math.isfinite(v):
                pass
            else:
                parsed[k] = v
        # Harvest meta fields if present in metrics
        for mk in ("run_name", "wandb_run_id", "system_name"):
            if meta[mk] is None and mk in parsed:
                meta[mk] = parsed[mk]
        data[str(step)] = parsed

    # Infer run_name from file stem when not embedded in metrics
    if meta["run_name"] is None:
        meta["run_name"] = db_path.stem

    steps = [int(k) for k in data]
    return json.dumps({"meta": meta, "steps": steps, "data": data})


def _wandb_snapshot(run_id: str, entity: str | None, project: str | None) -> str:
    """Fetch W&B run history and return a /api/stats JSON string."""
    from synnodb.observability.plots.utils.wandb_utils import get_wandb_run

    # Delegate path construction so a missing entity falls back to the caller's
    # own default entity instead of a literal "None/<project>/<run_id>" path.
    run = get_wandb_run(run_id=run_id, entity=entity, project=project)

    meta = {
        "run_name": run.name,
        "wandb_run_id": run_id,
        "system_name": run.config.get("system_name"),
        "start_time": run.created_at,
    }

    history = run.history(samples=10000)  # pandas DataFrame
    data: dict[str, dict] = {}
    steps: list[int] = []

    for _, row in history.iterrows():
        step = int(row.get("_step", len(steps)))
        steps.append(step)
        row_dict: dict = {}
        for k, v in row.items():
            if k.startswith("_"):
                continue
            if v is None:
                continue
            if isinstance(v, float):
                if not math.isfinite(v):
                    continue
                row_dict[k] = v
            else:
                row_dict[k] = v
        data[str(step)] = row_dict

    return json.dumps({"meta": meta, "steps": steps, "data": data})


def _normalize_stats_url(api_url: str) -> str:
    """Return a URL that points at a dashboard /api/stats endpoint."""
    api_url = api_url.strip()
    if not urlparse(api_url).scheme:
        api_url = f"http://{api_url}"
    api_url = api_url.rstrip("/")
    if not api_url.endswith("/api/stats"):
        api_url = f"{api_url}/api/stats"
    return api_url


def _remote_api_snapshot(api_url: str) -> str:
    """Fetch a live dashboard /api/stats JSON string from another host."""
    from urllib.error import URLError
    from urllib.request import urlopen

    stats_url = _normalize_stats_url(api_url)
    try:
        with urlopen(stats_url, timeout=10) as response:  # noqa: S310
            raw = response.read().decode()
    except URLError as exc:
        return json.dumps(
            {
                "meta": {
                    "_source_type": "remote",
                    "_source_ref": api_url,
                    "_error": str(exc.reason),
                },
                "steps": [],
                "data": {},
            }
        )

    payload = json.loads(raw)
    meta = payload.setdefault("meta", {})
    meta["_source_type"] = "remote"
    meta["_source_ref"] = api_url
    return json.dumps(payload)


def _remote_base_url(api_url: str) -> str:
    """Return the remote dashboard's base URL (without the /api/stats suffix)."""
    return _normalize_stats_url(api_url)[: -len("/api/stats")]


def _remote_files_payload(api_url: str) -> bytes:
    """Proxy /api/files from a remote live dashboard so its workspace shows here."""
    from urllib.error import URLError
    from urllib.request import urlopen

    try:
        with urlopen(  # noqa: S310
            _remote_base_url(api_url) + "/api/files", timeout=10
        ) as response:
            return response.read()
    except URLError:
        return json.dumps({"available": False, "files": []}).encode()


def _remote_file_payload(api_url: str, rel: str) -> "bytes | None":
    """Proxy /api/file from a remote live dashboard (None → 404)."""
    if not rel:
        return None
    from urllib.error import URLError
    from urllib.parse import quote
    from urllib.request import urlopen

    try:
        with urlopen(  # noqa: S310
            _remote_base_url(api_url) + "/api/file?path=" + quote(rel), timeout=10
        ) as response:
            return response.read()
    except URLError:  # includes HTTPError (e.g. remote 404)
        return None


class StandaloneDashboard:
    """HTTP dashboard server that reads from DuckDB, W&B, or a remote live API.

    DuckDB mode re-reads the database on every /api/stats request, so it works
    both for completed runs and as a live observer of an ongoing run writing to the
    same .duckdb file.  W&B mode fetches the run history once on the first request
    and caches it for subsequent polls.  Remote API mode proxies another live
    dashboard's /api/stats endpoint.

    Call serve_forever() to block the calling thread, or keep the object alive and
    let it serve in background threads.
    """

    def __init__(
        self,
        host: str = "0.0.0.0",
        port: int = 8765,
        *,
        db_path: str | Path | None = None,
        wandb_run_id: str | None = None,
        api_url: str | None = None,
        wandb_entity: str | None = DEFAULT_WANDB_ENTITY,
        wandb_project: str = DEFAULT_WANDB_PROJECT,
    ) -> None:
        import logging
        import threading

        self._db_path = Path(db_path) if db_path else None
        self._wandb_run_id = wandb_run_id
        self._api_url = api_url
        self._wandb_entity = wandb_entity
        self._wandb_project = wandb_project
        self._wandb_entity_default = wandb_entity
        self._wandb_project_default = wandb_project
        self._wandb_cache: str | None = None
        self._lock = threading.Lock()

        port, self._server = _make_http_server(
            host,
            port,
            self._snapshot,
            post_handlers={
                "/api/reload": self._handle_reload,
                "/api/switch": self._handle_switch,
            },
            file_list_fn=self._files_payload,
            file_read_fn=self._file_payload,
        )
        self._port = port
        t = threading.Thread(target=self._server.serve_forever, daemon=True)
        t.start()
        logging.getLogger(__name__).info(
            "\033[1;32m[StandaloneDashboard] http://localhost:%d\033[0m", self._port
        )

    @property
    def port(self) -> int:
        return self._port

    def _files_payload(self) -> bytes:
        """Code inspector is only available when proxying a live dashboard (api_url).

        DuckDB / W&B sources have no live workspace on this host, so the feature
        is deactivated for them — surfacing this machine's local ``./output``
        would mislead by attributing unrelated files to the selected source.
        """
        with self._lock:
            api_url = self._api_url
        if api_url:
            return _remote_files_payload(api_url)
        return json.dumps({"available": False, "files": []}).encode()

    def _file_payload(self, rel: str) -> "bytes | None":
        with self._lock:
            api_url = self._api_url
        if api_url:
            return _remote_file_payload(api_url, rel)
        return None

    def serve_forever(self) -> None:
        import time

        while True:
            time.sleep(1)

    def _snapshot(self) -> str:
        with self._lock:
            if (
                self._db_path is None
                and self._wandb_run_id is None
                and self._api_url is None
            ):
                return json.dumps(
                    {
                        "meta": {"_source_type": "standalone", "_source_ref": None},
                        "steps": [],
                        "data": {},
                    }
                )
            if self._db_path is not None:
                raw = json.loads(_duckdb_snapshot(self._db_path))
                raw["meta"]["_source_type"] = "db"
                raw["meta"]["_source_ref"] = str(self._db_path)
                return json.dumps(raw)
            if self._api_url is not None:
                return _remote_api_snapshot(self._api_url)
            if self._wandb_cache is None:
                assert self._wandb_run_id is not None
                raw = json.loads(
                    _wandb_snapshot(
                        self._wandb_run_id,
                        self._wandb_entity,
                        self._wandb_project,
                    )
                )
                raw["meta"]["_source_type"] = "wandb"
                raw["meta"]["_source_ref"] = self._wandb_run_id
                self._wandb_cache = json.dumps(raw)
            return self._wandb_cache

    def _handle_reload(self, _body: bytes) -> bytes:
        with self._lock:
            self._wandb_cache = None
        return b'{"ok":true}'

    def _handle_switch(self, body: bytes) -> bytes:
        try:
            req = json.loads(body) if body else {}
            wandb_run_id = (req.get("wandb_run_id") or "").strip()
            db_path = (req.get("db_path") or "").strip()
            api_url = (req.get("api_url") or "").strip()
            with self._lock:
                if api_url:
                    self._api_url = api_url
                    self._db_path = None
                    self._wandb_run_id = None
                    self._wandb_cache = None
                elif wandb_run_id:
                    self._wandb_run_id = wandb_run_id
                    # Always reset to the constructor defaults when the request
                    # omits these — otherwise a previous switch's custom entity
                    # silently sticks across to the next run, which can route
                    # the fetch to the wrong wandb project.
                    self._wandb_entity = (
                        req.get("wandb_entity") or self._wandb_entity_default
                    )
                    self._wandb_project = (
                        req.get("wandb_project") or self._wandb_project_default
                    )
                    self._db_path = None
                    self._api_url = None
                    self._wandb_cache = None
                elif db_path:
                    self._db_path = Path(db_path)
                    self._wandb_run_id = None
                    self._api_url = None
                    self._wandb_cache = None
                else:
                    return b'{"ok":false,"error":"no source specified"}'
            return b'{"ok":true}'
        except Exception as e:
            return json.dumps({"ok": False, "error": str(e)}).encode()


class LiveDashboardDrain(DataDrain):
    """In-process HTTP server that serves a live run-stats dashboard.

    Starts a background daemon thread on construction; bind address is
    0.0.0.0 so the UI is reachable from any host on the network.
    """

    def __init__(
        self,
        host: str = "0.0.0.0",
        port: int = 8765,
        *,
        run_name: str | None = None,
        wandb_run_id: str | None = None,
        system_name: str | None = None,
        workspace_dir: str | Path,
    ) -> None:
        import logging
        import threading

        # The run process knows its own workspace and passes it in; the dashboard
        # is never told paths out of band. The code inspector serves only files
        # physically under this directory.
        self._workspace_dir = Path(workspace_dir).resolve()
        self._data: dict[int, dict] = {}
        self._meta = {
            "run_name": run_name,
            "wandb_run_id": wandb_run_id,
            "system_name": system_name,
            "start_time": datetime.now().isoformat(timespec="seconds"),
        }
        self._lock = threading.Lock()
        self._port = self._start_server(host, port)
        logging.getLogger(__name__).info(
            "\033[1;32m[LiveDashboard] http://localhost:%d\033[0m", self._port
        )

    # ------------------------------------------------------------------ emit --

    def emit(self, metrics: dict, step: int) -> None:
        with self._lock:
            row = self._data.setdefault(step, {})
            for k, v in metrics.items():
                coerced = _duckdb_col_value(v)
                if coerced is not None:
                    row[k] = coerced

    # -------------------------------------------------------------- internals --

    def _workspace_root(self) -> "Path | None":
        """The generated-code workspace this run operates in (passed in by main)."""
        return self._workspace_dir

    def _snapshot(self) -> str:
        with self._lock:
            snapshot = {k: dict(v) for k, v in sorted(self._data.items())}

        def _clean(v):
            if isinstance(v, float) and not math.isfinite(v):
                return None
            return v

        return json.dumps(
            {
                "meta": self._meta,
                "steps": list(snapshot.keys()),
                "data": {
                    str(k): {ck: _clean(cv) for ck, cv in v.items()}
                    for k, v in snapshot.items()
                },
            }
        )

    def _start_server(self, host: str, start_port: int) -> int:
        # Retarget the shared server at THIS stage's data, binding the server only the first time.
        # Reading _ACTIVE_SNAPSHOT["fn"] per request (via the closure below) means a later stage's
        # data appears on the same URL with no port change, so the dashboard follows the run.
        global _SHARED_SERVER
        _ACTIVE_SNAPSHOT["fn"] = self._snapshot
        _ACTIVE_SNAPSHOT["workspace"] = self._workspace_root
        with _SHARED_SERVER_LOCK:
            if _SHARED_SERVER is not None:
                return _SHARED_SERVER[1]
            port, server = _make_http_server(
                host,
                start_port,
                lambda: _ACTIVE_SNAPSHOT["fn"](),
                file_list_fn=lambda: _local_files_payload(_ACTIVE_SNAPSHOT["workspace"]()),
                file_read_fn=lambda rel: _local_file_payload(
                    _ACTIVE_SNAPSHOT["workspace"](), rel
                ),
            )
            t = threading.Thread(target=server.serve_forever, daemon=True)
            t.start()
            _SHARED_SERVER = (server, port)
            return port
