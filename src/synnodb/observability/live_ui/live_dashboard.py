import gzip
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

_UI_DIR = Path(__file__).parent

# Directories never surfaced by the code inspector — VCS metadata, caches and
# dependency trees would only bury the generated code in noise.
_WORKSPACE_SKIP_DIRS = frozenset(
    {
        ".git",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        "node_modules",
    }
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


# A single live-dashboard HTTP server is bound once per process and shared by every
# stage, so the dashboard URL stays put (no 8765 -> 8766 -> ... hopping that would
# strand the dashboard on the first stage while later stages serve on a port nobody
# watches). The shared drain (below) binds it on the first stage; ``_ACTIVE_SNAPSHOT``
# is the indirection the serve thread reads per request so the bound server always
# renders the current in-memory store.
_SHARED_SERVER: "tuple[socketserver.TCPServer, int] | None" = None
_SHARED_SERVER_LOCK = threading.Lock()
_ACTIVE_SNAPSHOT: dict = {
    "fn": lambda since=None: json.dumps(
        {"meta": {}, "steps": [], "data": {}, "latest": None, "count": 0}
    ),
    "workspace": lambda: None,
    "body": lambda step=None: None,
}


def _parse_since(raw: "str | None") -> "int | None":
    """Parse the ``since`` query arg of /api/stats into a step cursor (None → full).

    The client sends the highest step id it already holds; the server replies with
    only steps at or beyond it (the boundary step is re-sent because the current,
    highest turn can still accumulate fields after the client first saw it). A
    missing or malformed value means "send the full snapshot".
    """
    if raw is None or raw == "":
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


# Heavy per-step text fields. These dominate the /api/stats payload (a single
# shell/llm turn can be tens of KB) yet are only ever read to fill the activity
# log's expanded body view. They are stripped from every snapshot and served on
# demand, one step at a time, via /api/step_body — so the browser parses and
# holds only what it renders up front, and each poll delta stays small on a run
# that is streaming output continuously.
_BODY_FIELDS = (
    "llm/output_text",
    "shell/outputs",
    "data_inspect/output",
    "apply_patch/string",
)


def _strip_bodies(data: dict) -> dict:
    """Return a copy of a ``{step: fields}`` map with _BODY_FIELDS removed.

    The input is never mutated (a cached source dict must stay intact); each row
    is shallow-copied without its body fields.
    """
    return {
        step: {k: v for k, v in row.items() if k not in _BODY_FIELDS}
        for step, row in data.items()
    }


def _body_fields_payload(data: dict, step: "str | int") -> "bytes | None":
    """JSON body for /api/step_body: the _BODY_FIELDS of one step (None → 404).

    ``data`` is the full (unstripped) ``{step: fields}`` store keyed by string
    step id. Only the body fields actually present on that step are returned.
    """
    row = data.get(str(step))
    if row is None:
        return None
    fields = {k: row[k] for k in _BODY_FIELDS if k in row}
    return json.dumps({"step": str(step), "fields": fields}).encode()


def _finalize_snapshot(payload: dict, since: "int | None", *, strip: bool = True) -> str:
    """Attach the incremental-protocol fields to a full snapshot dict and serialize.

    ``latest``/``count`` are the max step id and total step count of the *full*
    store, so the client can detect when it has drifted out of sync (e.g. the run
    reset its timeline) and refetch. When ``since`` is given, only steps at or
    beyond it are kept and ``incremental`` is set; the untouched dict is never
    mutated so a cached source dict stays intact.

    ``strip`` removes the heavy body fields (served separately via
    /api/step_body). It is turned off only for the remote proxy, which passes an
    upstream dashboard's already-shaped payload through verbatim.
    """
    data = payload.get("data", {})
    ids = sorted(int(k) for k in data)
    latest = ids[-1] if ids else None
    count = len(ids)
    prep = _strip_bodies if strip else (lambda d: d)
    if since is None:
        out = {**payload, "data": prep(data), "latest": latest, "count": count}
    else:
        kept = {k: v for k, v in data.items() if int(k) >= since}
        out = {
            **payload,
            "data": prep(kept),
            "steps": sorted(int(k) for k in kept),
            "latest": latest,
            "count": count,
            "incremental": True,
        }
    return json.dumps(out)


# A single LiveDashboardDrain instance is shared by every stage that runs in ONE
# process (e.g. the chained SynnoDB notebook pipeline: createStoragePlan ->
# createBaseImpl -> runOptimLoop -> ...). Each stage's RunStatsCollector restarts
# its turn counter at 0 and its cumulative ``total/*`` / ``tool/*_count`` metrics at
# ~0, so naively pointing the dashboard at a fresh per-stage store would reset the
# timeline every stage. Instead all stages emit into this one drain: incoming steps
# are offset past the previous stage's last step, and cumulative metrics carry over,
# producing one continuous journey. ``get_or_create_live_drain`` creates it on the
# first stage and opens a new stage on every later one; ``reset_live_dashboard``
# wipes the accumulated data so a new pipeline starts clean (server stays bound).
_SHARED_DRAIN: "LiveDashboardDrain | None" = None
_SHARED_DRAIN_LOCK = threading.Lock()


def _make_http_server(
    host: str,
    start_port: int,
    snapshot_fn,
    post_handlers: dict | None = None,
    file_list_fn=None,
    file_read_fn=None,
    body_fn=None,
) -> tuple[int, "socketserver.TCPServer"]:
    """Bind an HTTP server that calls snapshot_fn() for /api/stats.

    post_handlers maps path → callable(body: bytes) → bytes for POST endpoints.
    file_list_fn, if given, is a callable returning the JSON body (bytes) for
    /api/files; file_read_fn(rel) returns the JSON body (bytes) for /api/file or
    None for 404. Together they back the code inspector. When file_list_fn is
    None the inspector endpoints report the workspace as unavailable.
    body_fn(step), if given, returns the JSON body (bytes) for /api/step_body —
    the heavy per-step body fields stripped from /api/stats — or None for 404.
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
                since = _parse_since(
                    (parse_qs(urlparse(self.path).query).get("since") or [None])[0]
                )
                self._reply(snapshot_fn(since).encode(), "application/json")
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
            if path == "/api/step_body":
                step = (parse_qs(urlparse(self.path).query).get("step") or [""])[0]
                body = body_fn(step) if body_fn is not None else None
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
                headers = [("Content-Type", ct)]
                # The stats feed is JSON text and compresses ~5x; gzip it (and any
                # other text body) whenever the client accepts it and the payload is
                # big enough to be worth the CPU. Tiny idle deltas fall below the
                # threshold and are sent as-is. Already-compressed binaries (images)
                # are left untouched. The urllib-based remote proxy does not send
                # Accept-Encoding, so proxied fetches stay uncompressed end to end.
                compressible = (
                    ct.startswith("application/json")
                    or ct.startswith("text/")
                    or "javascript" in ct
                )
                accepts_gzip = "gzip" in self.headers.get("Accept-Encoding", "")
                if compressible and accepts_gzip and len(body) >= 1400:
                    body = gzip.compress(body, 5)
                    headers.append(("Content-Encoding", "gzip"))
                self.send_response(200)
                for key, value in headers:
                    self.send_header(key, value)
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
        "num_threads": None,
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
        # The serving thread count is stored per row under run/num_threads; lift the
        # first one seen into run metadata (it is constant for the whole run).
        if meta["num_threads"] is None and "run/num_threads" in parsed:
            meta["num_threads"] = parsed["run/num_threads"]
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
        "num_threads": None,
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
        # The serving thread count rides along every metric row; lift the first
        # one seen into run metadata (constant for the whole run).
        if meta["num_threads"] is None and "run/num_threads" in row_dict:
            meta["num_threads"] = row_dict["run/num_threads"]
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


def _remote_body_payload(api_url: str, step: str) -> "bytes | None":
    """Proxy /api/step_body from a remote live dashboard (None → 404)."""
    if not step:
        return None
    from urllib.error import URLError
    from urllib.parse import quote
    from urllib.request import urlopen

    try:
        with urlopen(  # noqa: S310
            _remote_base_url(api_url) + "/api/step_body?step=" + quote(str(step)),
            timeout=10,
        ) as response:
            return response.read()
    except URLError:  # includes HTTPError (e.g. remote 404 / older build)
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
        wandb_entity: str | None = None,
        wandb_project: str | None = None,
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
            body_fn=self._body_payload,
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

    def _body_payload(self, step: str) -> "bytes | None":
        """JSON body for /api/step_body, dispatched by the current source.

        DuckDB re-reads the file (it re-reads on every stats poll anyway); W&B
        reads the cached history (building it if this is the first request);
        remote proxies upstream. The body fields were stripped from /api/stats,
        so this endpoint is how the client fetches an expanded entry's text.
        """
        with self._lock:
            db_path = self._db_path
            api_url = self._api_url
            wandb_run_id = self._wandb_run_id
            wandb_cache = self._wandb_cache
        if api_url:
            return _remote_body_payload(api_url, step)
        if db_path is not None:
            data = json.loads(_duckdb_snapshot(db_path)).get("data", {})
            return _body_fields_payload(data, step)
        if wandb_run_id:
            if wandb_cache is None:
                self._snapshot()  # populates _wandb_cache (unstripped history)
                with self._lock:
                    wandb_cache = self._wandb_cache
            if wandb_cache is None:
                return None
            data = json.loads(wandb_cache).get("data", {})
            return _body_fields_payload(data, step)
        return None

    def serve_forever(self) -> None:
        import time

        while True:
            time.sleep(1)

    def _snapshot(self, since: "int | None" = None) -> str:
        with self._lock:
            if (
                self._db_path is None
                and self._wandb_run_id is None
                and self._api_url is None
            ):
                return _finalize_snapshot(
                    {
                        "meta": {"_source_type": "standalone", "_source_ref": None},
                        "steps": [],
                        "data": {},
                    },
                    since,
                )
            if self._db_path is not None:
                raw = json.loads(_duckdb_snapshot(self._db_path))
                raw["meta"]["_source_type"] = "db"
                raw["meta"]["_source_ref"] = str(self._db_path)
                return _finalize_snapshot(raw, since)
            if self._api_url is not None:
                # The upstream dashboard already stripped bodies (or, if it is an
                # older build, inlined them); pass its payload through verbatim and
                # proxy body fetches to it rather than re-shaping here.
                return _finalize_snapshot(
                    json.loads(_remote_api_snapshot(self._api_url)), since, strip=False
                )
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
            return _finalize_snapshot(json.loads(self._wandb_cache), since)

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

    A single instance accumulates across every stage of a chained pipeline that
    runs in one process (see ``get_or_create_live_drain``). ``emit`` offsets each
    stage's steps past the previous stage and carries cumulative metrics forward,
    so the whole journey appears on one continuous timeline rather than resetting
    per stage.
    """

    def __init__(
        self,
        host: str = "0.0.0.0",
        port: int = 8765,
        *,
        run_name: str | None = None,
        wandb_run_id: str | None = None,
        system_name: str | None = None,
        workspace_dir: str | Path | None = None,
    ) -> None:
        import logging
        import threading

        # The run process knows its own workspace and passes it in; the dashboard
        # is never told paths out of band. The code inspector serves only files
        # physically under this directory. When no workspace is supplied (e.g. the
        # dashboard is started eagerly before any run exists) the inspector simply
        # reports the workspace as unavailable.
        self._workspace_dir = Path(workspace_dir).resolve() if workspace_dir else None
        self._data: dict[int, dict] = {}
        self._lock = threading.Lock()
        # Monotonic revision bumped on every mutation of _data/_meta. The full
        # snapshot is expensive to serialize (all steps + their bodies), so it is
        # cached and only rebuilt when the revision moves. Incremental deltas are
        # cheap and always built fresh. See _snapshot.
        self._rev = 0
        self._cache_full: str | None = None
        self._cache_rev = -1
        # Per-stage accumulation bookkeeping.
        self._stage_base = 0  # global-step offset for the active stage
        self._carry: dict[
            str, float
        ] = {}  # cumulative-metric baseline for the active stage
        self._last_global: dict[
            str, float
        ] = {}  # latest global value of each cumulative metric
        self._stages: list[dict] = []  # one entry per stage, with its base step
        self._meta = {
            "run_name": run_name,
            "wandb_run_id": wandb_run_id,
            "system_name": system_name,
            "start_time": datetime.now().isoformat(timespec="seconds"),
            # The run's resolved serving thread count, lifted from the first metric
            # row that carries run/num_threads (see emit). None until then.
            "num_threads": None,
            "stages": self._stages,
            # Previews of the currently-running conversation's scheduled stages,
            # populated by register_planned_stages; None until a conversation
            # registers its stage list. See the prompts pane in the live UI.
            "planned_stages": None,
            # Set by report_error when a run aborts with an unrecovered exception.
            # None while the run is healthy; a dict with the message + traceback
            # once it fails. The UI raises an alert banner and freezes its timer
            # the moment this is populated. Cleared when the next stage begins.
            "error": None,
        }
        # Construction starts the server but opens no stage yet — the first stage to
        # emit calls begin_stage(). This lets the driver start the dashboard eagerly
        # (so the URL can be shown up front) before any run_name exists.
        self._port = self._start_server(host, port)
        logging.getLogger(__name__).info(
            "\033[1;32m[LiveDashboard] http://localhost:%d\033[0m", self._port
        )

    @property
    def port(self) -> int:
        return self._port

    # ----------------------------------------------------------- stage hooks --

    @staticmethod
    def _is_cumulative(key: str) -> bool:
        """True for metrics that accumulate within a stage and so must carry over
        across stages to stay monotonic (token/cost/runtime totals, tool counts)."""
        return key.startswith("total/") or (
            key.startswith("tool/") and key.endswith("_count")
        )

    def begin_stage(
        self,
        *,
        run_name: str | None = None,
        wandb_run_id: str | None = None,
        system_name: str | None = None,
    ) -> None:
        """Open a new stage on the shared timeline: offset its steps past the last
        one already stored and snapshot current cumulative totals as the baseline
        that this stage's ``total/*`` / ``tool/*_count`` values are added onto."""
        with self._lock:
            self._stage_base = (max(self._data) + 1) if self._data else 0
            self._carry = dict(self._last_global)
            # Drop the previous conversation's scheduled-stage previews; the new
            # stage republishes its own the moment its stage list is built.
            self._meta["planned_stages"] = None
            # A fresh stage is starting, so any error surfaced by the previous
            # one no longer applies - clear it so the alert banner disappears.
            self._meta["error"] = None
            self._meta["run_name"] = run_name
            if wandb_run_id is not None:
                self._meta["wandb_run_id"] = wandb_run_id
            if system_name is not None:
                self._meta["system_name"] = system_name
            entry = {
                "run_name": run_name,
                "wandb_run_id": wandb_run_id,
                "base_step": self._stage_base,
            }
            # Replace a trailing stage that never emitted data (its base hasn't been
            # passed) rather than leaving an empty stage marker on the timeline.
            if self._stages and self._stages[-1]["base_step"] == self._stage_base:
                self._stages[-1] = entry
            else:
                self._stages.append(entry)
            self._rev += 1

    def register_planned_stages(
        self, previews: list[dict], stage_name: str | None = None
    ) -> None:
        """Publish the scheduled stages of the currently-running conversation.

        Stored as a self-contained block on the meta - the stage's global
        ``base_step`` (so the UI can tell which executed sections belong to this
        conversation) plus the ordered stage previews. Assigned as a whole new
        dict (never mutated in place) so a concurrent ``_snapshot`` reader that
        captured the previous reference is unaffected.
        """
        with self._lock:
            self._meta["planned_stages"] = {
                "base_step": self._stage_base,
                "stage_name": stage_name,
                "stages": list(previews),
            }
            self._rev += 1

    def report_error(
        self,
        message: str,
        *,
        traceback_text: str | None = None,
        log_file: str | None = None,
    ) -> None:
        """Record that the current run aborted with an unrecovered exception.

        Stored as a self-contained block on the meta (assigned as a whole new
        dict, never mutated in place, so a concurrent ``_snapshot`` reader is
        unaffected). The polling UI turns this into an alert banner and freezes
        its live timer. Cleared automatically when the next stage begins.
        """
        with self._lock:
            self._meta["error"] = {
                "message": message,
                "traceback": traceback_text,
                "log_file": log_file,
                "time": datetime.now().isoformat(timespec="seconds"),
            }
            self._rev += 1

    def _reset(self) -> None:
        """Wipe accumulated data so the next stage starts a fresh pipeline. The
        HTTP server (and its bound port) is left running."""
        with self._lock:
            self._data.clear()
            self._stage_base = 0
            self._carry.clear()
            self._last_global.clear()
            self._stages.clear()
            self._meta["planned_stages"] = None
            self._meta["error"] = None
            self._meta["run_name"] = None
            self._meta["wandb_run_id"] = None
            self._meta["num_threads"] = None
            self._meta["start_time"] = datetime.now().isoformat(timespec="seconds")
            self._rev += 1

    # ------------------------------------------------------------------ emit --

    def emit(self, metrics: dict, step: int) -> None:
        with self._lock:
            gstep = self._stage_base + step
            row = self._data.setdefault(gstep, {})
            for k, v in metrics.items():
                coerced = _duckdb_col_value(v)
                if coerced is None:
                    continue
                if (
                    self._is_cumulative(k)
                    and isinstance(coerced, (int, float))
                    and not isinstance(coerced, bool)
                ):
                    # int default keeps integer counters (tool/*_count) integral.
                    coerced = coerced + self._carry.get(k, 0)
                    self._last_global[k] = coerced
                # The run's serving thread count is a run-level constant; surface it
                # as run metadata rather than leaving it buried in the metric rows.
                if k == "run/num_threads":
                    self._meta["num_threads"] = coerced
                row[k] = coerced
            self._rev += 1

    # -------------------------------------------------------------- internals --

    def _workspace_root(self) -> "Path | None":
        """The generated-code workspace this run operates in (passed in by main)."""
        return self._workspace_dir

    def _snapshot(self, since: "int | None" = None) -> str:
        """Serialize the run state for /api/stats.

        ``since=None`` returns the full snapshot (cached until the next mutation,
        since serializing every step's bodies is the costly path). Otherwise only
        steps at or beyond ``since`` are returned as an incremental delta - tiny,
        so it is always built fresh. Both carry ``latest``/``count`` (the full
        store's max step id and step count) so the client can detect drift.
        """

        def _clean(v):
            if isinstance(v, float) and not math.isfinite(v):
                return None
            return v

        with self._lock:
            if (
                since is None
                and self._cache_full is not None
                and self._cache_rev == self._rev
            ):
                return self._cache_full

            ids = sorted(self._data)
            latest = ids[-1] if ids else None
            count = len(ids)
            meta = dict(self._meta)
            meta["stages"] = [dict(s) for s in self._stages]

            kept = ids if since is None else [k for k in ids if k >= since]
            # Body fields are stripped here (served via /api/step_body) so neither
            # the cached full snapshot nor the per-poll delta carries them.
            data = {
                str(k): {
                    ck: _clean(cv)
                    for ck, cv in self._data[k].items()
                    if ck not in _BODY_FIELDS
                }
                for k in kept
            }
            payload: dict = {
                "meta": meta,
                "steps": kept,
                "data": data,
                "latest": latest,
                "count": count,
            }
            if since is None:
                out = json.dumps(payload)
                self._cache_full = out
                self._cache_rev = self._rev
                return out
            payload["incremental"] = True
            return json.dumps(payload)

    def _body_fields(self, step: str) -> "bytes | None":
        """JSON body for /api/step_body: this step's stripped body fields (None → 404)."""
        try:
            gstep = int(step)
        except (TypeError, ValueError):
            return None
        with self._lock:
            row = self._data.get(gstep)
            if row is None:
                return None
            fields = {k: row[k] for k in _BODY_FIELDS if k in row}
        return json.dumps({"step": str(gstep), "fields": fields}).encode()

    def _start_server(self, host: str, start_port: int) -> int:
        # Retarget the shared server at THIS stage's data, binding the server only the first time.
        # Reading _ACTIVE_SNAPSHOT["fn"] per request (via the closure below) means a later stage's
        # data appears on the same URL with no port change, so the dashboard follows the run.
        global _SHARED_SERVER
        _ACTIVE_SNAPSHOT["fn"] = self._snapshot
        _ACTIVE_SNAPSHOT["workspace"] = self._workspace_root
        _ACTIVE_SNAPSHOT["body"] = self._body_fields
        with _SHARED_SERVER_LOCK:
            if _SHARED_SERVER is not None:
                return _SHARED_SERVER[1]
            port, server = _make_http_server(
                host,
                start_port,
                lambda since=None: _ACTIVE_SNAPSHOT["fn"](since),
                file_list_fn=lambda: _local_files_payload(
                    _ACTIVE_SNAPSHOT["workspace"]()
                ),
                file_read_fn=lambda rel: _local_file_payload(
                    _ACTIVE_SNAPSHOT["workspace"](), rel
                ),
                body_fn=lambda step: _ACTIVE_SNAPSHOT["body"](step),
            )
            t = threading.Thread(target=server.serve_forever, daemon=True)
            t.start()
            _SHARED_SERVER = (server, port)
            return port


def start_live_dashboard(
    *, system_name: str | None = None, workspace_dir: str | Path | None = None
) -> "LiveDashboardDrain":
    """Eagerly bind the shared live-dashboard server without opening a stage.

    Lets a driver start the dashboard (and surface its URL) at construction time,
    before any stage / run_name exists. Idempotent — returns the existing drain if
    one is already running. ``workspace_dir`` (if known) enables the code inspector.
    """
    global _SHARED_DRAIN
    with _SHARED_DRAIN_LOCK:
        if _SHARED_DRAIN is None:
            _SHARED_DRAIN = LiveDashboardDrain(
                system_name=system_name, workspace_dir=workspace_dir
            )
        return _SHARED_DRAIN


def get_or_create_live_drain(
    *,
    run_name: str | None = None,
    wandb_run_id: str | None = None,
    system_name: str | None = None,
    workspace_dir: str | Path | None = None,
) -> "LiveDashboardDrain":
    """Return the process-wide live drain (creating it and its HTTP server if the
    driver did not already start it) and open a new stage on it, so every stage of a
    chained pipeline accumulates onto one continuous timeline.

    ``workspace_dir`` is the generated-code workspace served by the code inspector;
    it is applied only when this call is what first creates the shared drain.
    """
    global _SHARED_DRAIN
    with _SHARED_DRAIN_LOCK:
        if _SHARED_DRAIN is None:
            _SHARED_DRAIN = LiveDashboardDrain(
                system_name=system_name, workspace_dir=workspace_dir
            )
        _SHARED_DRAIN.begin_stage(
            run_name=run_name,
            wandb_run_id=wandb_run_id,
            system_name=system_name,
        )
        return _SHARED_DRAIN


def reset_live_dashboard() -> None:
    """Clear any accumulated live-dashboard data so the next stage starts a fresh
    pipeline. No-op if no drain has been created yet. Keeps the server running."""
    with _SHARED_DRAIN_LOCK:
        if _SHARED_DRAIN is not None:
            _SHARED_DRAIN._reset()


def report_live_dashboard_error(
    message: str,
    *,
    traceback_text: str | None = None,
    log_file: str | None = None,
) -> None:
    """Surface a run-aborting error on the live dashboard, if one is running.

    No-op when no drain has been created yet (nothing is displaying anything to
    warn on). Safe to call from the top-level exception handler of a run.
    """
    drain = _SHARED_DRAIN
    if drain is not None:
        drain.report_error(message, traceback_text=traceback_text, log_file=log_file)


def live_dashboard_url() -> "str | None":
    """URL of the running live dashboard, or None if no stage has started one yet."""
    drain = _SHARED_DRAIN
    return f"http://localhost:{drain.port}" if drain is not None else None
