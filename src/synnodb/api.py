"""Importable, typed entry point to the SynnoDB agent pipeline.

    from synnodb import SynnoDB
    db = SynnoDB.in_memory(workload="tpch")
    plan = db.createStoragePlan(queries="1")        # -> StoragePlan; plan.text is the doc
    impl = db.createBaseImpl(storage_plan=plan.text) # -> BaseImplementation; impl.files

``run_synthesis`` is the single entry point for executing a conversation: it
takes a complete :class:`~synnodb.plan.ConversationPlan` (predefined or
user-assembled) plus the chain token (``start``) and runs it to completion.
The named stage methods (``createStoragePlan`` ... ``checkSfCorrectness``) are
thin wrappers that resolve their chain inputs, construct their parameterized
built-in plan, and call ``run_synthesis`` - one entry point, one execution
pipeline.
"""

from __future__ import annotations

import dataclasses
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from synnodb import settings
from synnodb.ram_check import (  # noqa: F401  (re-exported public API)
    IN_MEMORY_RAM_FACTOR,
    InsufficientRamError,
    RamCheck,
)

# These enums resolve without SYNNO_DATA_DIR (the module-level assert was made
# lazy), so importing synnodb stays config-free. The heavy pipeline modules
# (synnodb.main, the builders) are imported lazily inside run_synthesis().
from synnodb.results import (
    BaseImplementation,
    CorrectnessReport,
    MultiThreadedImplementation,
    OptimizedImplementation,
    StageArtifact,
    StoragePlan,
)
from synnodb.utils.cli_config import DEFAULT_MODEL, RunConfig, Usecase
from synnodb.utils.utils import DBStorage
from synnodb.workloads.workload_provider import Workload, WorkloadId
from synnodb.workloads.workload_provider_olap import OLAPWorkload

if TYPE_CHECKING:
    from synnodb.plan import ConversationPlan

__all__ = [
    "SynnoDB",
    "SynnoConfig",
    "RamCheck",
    "InsufficientRamError",
    "IN_MEMORY_RAM_FACTOR",
]


# ----------------------------- coercion helpers -----------------------------
def _as_workload(v: Workload | str) -> Workload | WorkloadId:
    if isinstance(v, (Workload, WorkloadId)):
        return v
    # built-in name -> enum; any registered (bring-your-own) name -> WorkloadId
    from synnodb.workloads.workload_spec import resolve_workload

    return resolve_workload(str(v))


def _as_storage(v: DBStorage | str) -> DBStorage:
    return v if isinstance(v, DBStorage) else DBStorage(str(v))


def _as_usecase(v: Usecase | str) -> Usecase:
    return v if isinstance(v, Usecase) else Usecase(str(v))


def _as_run_id(v: Any) -> str:
    """A stage artifact collapses to its run id; everything else stringifies."""
    if isinstance(v, StageArtifact):
        if v.run_id is None:
            raise ValueError(
                f"cannot chain from a {type(v).__name__} with no run_id — either "
                "chain W&B-free via its snapshot hash, or run the producing stage "
                "with W&B logging enabled (set wandb_entity/wandb_project)."
            )
        return v.run_id
    return str(v)


def _resolve_chain(
    stage: str, source: Any, source_wandb_id: Any
) -> tuple[str | None, str | None]:
    """Resolve the chaining tokens for a stage that consumes a previous stage's
    output. Returns ``(snapshot_hash, wandb_run_id)`` with exactly one set:
      - ``source`` -> its ``.snapshot_hash`` (or a raw hash str): W&B-free path.
      - ``source_wandb_id`` -> its ``.run_id`` (or a raw id str): W&B path.
    """
    snap = source.snapshot_hash if isinstance(source, StageArtifact) else source
    wid = _as_run_id(source_wandb_id) if source_wandb_id is not None else None
    if (snap is None) == (wid is None):
        raise ValueError(
            f"{stage} requires exactly one of `source` (an artifact or git snapshot "
            "hash, W&B-free) or `source_wandb_id` (a W&B run id) — got "
            + ("both" if snap is not None else "neither")
            + "."
        )
    return snap, wid


# --------------------------------- config -----------------------------------
@dataclass(frozen=True)
class SynnoConfig:
    """Immutable, enum-typed run settings shared across stages."""

    model: str = DEFAULT_MODEL
    workload: Workload | WorkloadId = OLAPWorkload.TPCH
    db_storage: DBStorage = DBStorage.IN_MEMORY
    usecase: Usecase = Usecase.OLAP
    queries: str = "1"
    # wandb is opt-in and has no separate on/off flag: it is enabled iff a
    # ``wandb_entity`` or ``wandb_project`` is set. With neither set nothing
    # wandb-related runs — no login, init, or logging.
    wandb_entity: str | None = None  # None -> the user's own default W&B entity
    wandb_project: str | None = None  # None -> the default "SynnoDB" project
    auto_confirm: bool = True  # --auto_u
    auto_finish: bool = True
    disable_openai_tracing: bool = True
    disable_repo_sync: bool = False
    notify: bool = False
    do_not_cache: bool = False
    verbose: bool = False  # stream DEBUG logs to the console (logfile is always DEBUG)
    workspace: str | None = None  # run output dir; None -> local ./output
    # DuckDB-style engine config options, with defaults. `threads` is the target degree
    # of parallelism the generated engine is designed, validated, and served at (the
    # DuckDB config={'threads': N}); None -> 1 (single-threaded, the default), 0 -> all
    # usable cores of the host, N -> N threads. `max_turns` is the default per-stage LLM
    # turn budget; None -> each conversation's own default.
    threads: int | None = None
    max_turns: int | None = None
    # Merged into every LiteLLM request's extra_body (host-independent escape hatch,
    # e.g. OpenRouter provider routing: {"provider": {"sort": "throughput", ...}}).
    # None -> falls back to the $MODEL_EXTRA_BODY env var.
    model_extra_body: dict[str, Any] | None = None
    # escape hatch for any unmodelled RunConfig field: applied (dataclasses.replace)
    # onto the RunConfig run_synthesis produces, just before execution.
    extra_config: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "workload", _as_workload(self.workload))
        object.__setattr__(self, "db_storage", _as_storage(self.db_storage))
        object.__setattr__(self, "usecase", _as_usecase(self.usecase))

    @property
    def wandb_enabled(self) -> bool:
        """wandb is on iff an entity or project is set."""
        return self.wandb_entity is not None or self.wandb_project is not None


# --------------------------- chain-input resolution --------------------------
def _base_run_config(cfg: SynnoConfig) -> dict[str, Any]:
    """Map the typed ``SynnoConfig`` onto the RunConfig kwargs every run shares.
    Settings the API does not model take ``RunConfig``'s own field defaults."""
    return dict(
        model=cfg.model,
        benchmark=cfg.workload,
        db_storage=cfg.db_storage,
        usecase=cfg.usecase,
        queries_str=cfg.queries,
        notify=cfg.notify,
        disable_openai_tracing=cfg.disable_openai_tracing,
        auto_u=cfg.auto_confirm,
        auto_finish=cfg.auto_finish,
        log_to_wandb=cfg.wandb_enabled,
        wandb_entity=cfg.wandb_entity,
        wandb_project=cfg.wandb_project,
        disable_repo_sync=cfg.disable_repo_sync,
        do_not_cache=cfg.do_not_cache,
        workspace_dir=cfg.workspace,
        verbose=cfg.verbose,
        threads=cfg.threads,
        max_turns=cfg.max_turns,
        model_extra_body=cfg.model_extra_body,
    )


def _parse_queries(cfg: SynnoConfig) -> list[str]:
    from synnodb.utils.gen_common import parse_query_ids

    query_ids = parse_query_ids(cfg.queries, benchmark=cfg.workload)
    assert query_ids is not None, f"Failed to parse query ids from {cfg.queries!r}"
    return query_ids


def _memory_budget(cfg: SynnoConfig) -> int | None:
    """Pick a RAM budget only for persistent storage (in-memory uses all RAM)."""
    if cfg.db_storage in (DBStorage.LABSTORE, DBStorage.SSD):
        return 50 * 1024
    return None


def validate_snapshot(
    snapshot_config, benchmark, queries_str, query_ids, db_storage, model
):
    """Validate that a previous run's logged config matches this run's settings."""
    from synnodb.utils.confirm_dialog import await_user_confirmation
    from synnodb.utils.gen_common import parse_query_ids

    snapshot_benchmark: str = snapshot_config["benchmark"]
    snapshot_queries_str = snapshot_config["queries_str"]
    snapshot_model = snapshot_config["model"]
    snapshot_db_storage = snapshot_config["db_storage"]

    # .value works for both built-in enum members and a WorkloadId (bring-your-own)
    assert snapshot_benchmark.upper() == benchmark.value.upper(), (
        f"Expected benchmark {benchmark.value.upper()} in storage plan run, got {snapshot_benchmark}"
    )
    if queries_str is not None:
        assert snapshot_queries_str == queries_str, (
            f"Expected queries str {queries_str} in storage plan run, got {snapshot_queries_str}"
        )
    assert query_ids == parse_query_ids(snapshot_queries_str, benchmark=benchmark), (
        f"Expected query ids {query_ids} in storage plan run, got {parse_query_ids(snapshot_queries_str, benchmark=benchmark)}"
    )

    if db_storage is not None:
        assert snapshot_db_storage.lower() == db_storage.value.lower(), (
            f"Expected db_storage {db_storage.value.lower()} in storage plan run, got {snapshot_db_storage.lower()}"
        )

    if model is not None and snapshot_model != model:
        response = await_user_confirmation(
            f"Model in storage plan run is {snapshot_model}, but current model is {model}. Do you want to continue?"
        )
        if not response:
            print("Aborting run.")
            import sys

            sys.exit(0)


def resolve_source_snapshot(
    cfg: SynnoConfig,
    *,
    snapshot: str | None,
    wandb_id: str | None,
    source_kind: str,
) -> str:
    """Resolve the git snapshot hash of a previous stage's output. Provide exactly
    one of ``snapshot`` (the git hash directly, W&B-free) or ``wandb_id`` (a W&B run
    id resolved to its logged snapshot and validated against this run's config).

    This is the one generic chain-input resolver every built-in wrapper uses."""
    if (snapshot is None) == (wandb_id is None):
        raise ValueError(
            f"Provide exactly one of a snapshot (git snapshot hash, W&B-free) "
            f"or a W&B run id to load the {source_kind} snapshot — got "
            + ("both" if snapshot is not None else "neither")
            + "."
        )
    if snapshot is not None:
        return snapshot

    from synnodb.observability.logging.wandb_api_helper import (
        wandb_retrieve_metrics_for_run,
    )

    statistics, config, _ = wandb_retrieve_metrics_for_run(
        cfg.workload,
        wandb_id,
        entity=cfg.wandb_entity,
        project=cfg.wandb_project,
        fetch_latest_runtimes=False,
    )
    validate_snapshot(
        config,
        cfg.workload,
        cfg.queries,
        _parse_queries(cfg),
        db_storage=cfg.db_storage,
        model=cfg.model,
    )
    commit_hash = statistics["code/snapshot_hash"]
    assert commit_hash != "N/A", (
        f"Could not retrieve a valid commit hash from wandb for run {wandb_id} in "
        f"benchmark {cfg.workload}. Got {commit_hash}."
    )
    return commit_hash


# --------------------------------- facade -----------------------------------
class SynnoDB:
    """Programmatic pipeline driver. Each call runs one conversation to completion."""

    def __init__(
        self,
        config: SynnoConfig | None = None,
        /,
        *,
        data_dir: str | Path | None = None,
        engines_dir: str | Path | None = None,
        workspace: str | Path | None = None,
        env_file: str | None = None,
        cleanup_workspace: bool = False,
        **overrides: Any,
    ):
        # The three project folders, resolved here so the whole pipeline agrees.
        # Each falls back to .env then a default, with an explicit argument winning:
        #   data_dir    -> SYNNO_DATA_DIR    (the one root everything else derives from)
        #   engines_dir -> SYNNO_ENGINES_DIR (default: <data_dir>/engines)
        #   workspace   -> SYNNO_WORKSPACE   (default: ./output; must be relative)
        # data_dir/engines_dir become environment for the lazily-imported pipeline
        # modules; workspace rides on the config and is read via get_workspace_dir.
        settings.configure(
            data_dir=data_dir, engines_dir=engines_dir, env_file=env_file
        )
        if workspace is not None:
            overrides = {**overrides, "workspace": os.fspath(workspace)}
        base = config or SynnoConfig()
        self.config = dataclasses.replace(base, **overrides) if overrides else base
        # Start the live dashboard now, at driver construction, and show its URL so
        # the user can open it before the first stage runs. A fresh SynnoDB(...)
        # begins a clean pipeline (prior accumulated timeline is reset); every stage
        # chained on this driver then streams onto this one continuous dashboard.
        self._start_live_dashboard()

        # Fail fast on a missing API key: .env is loaded by now, so check up front
        # rather than several stages later when the SDK session is first built.
        from synnodb.utils.model_setup import validate_model_credentials

        validate_model_credentials(self.config.model)
        # Ephemeral runs: delete the workspace directory when this process exits
        # (normal finish, uncaught exception, or SIGINT/SIGTERM). Avoids the
        # accumulation of per-run engine workspaces. SIGKILL cannot be intercepted.
        self._cleanup_installed = False
        self._cleanup_workspace = cleanup_workspace
        if cleanup_workspace:
            self._install_workspace_cleanup()

    def _install_workspace_cleanup(self) -> None:
        import atexit
        import shutil
        import signal

        if self._cleanup_installed:
            return
        self._cleanup_installed = True
        target = Path(settings.get_workspace_dir(self.config.workspace)).resolve()
        state = {"done": False}

        def _clean(*_: Any) -> None:
            if state["done"]:
                return
            state["done"] = True
            shutil.rmtree(target, ignore_errors=True)

        atexit.register(_clean)  # normal exit + uncaught exception
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                previous = signal.getsignal(sig)

                def _handler(signum: int, frame: Any, _prev: Any = previous) -> None:
                    _clean()
                    if callable(_prev) and _prev not in (
                        signal.SIG_DFL,
                        signal.SIG_IGN,
                    ):
                        _prev(signum, frame)
                    else:
                        raise SystemExit(128 + signum)

                signal.signal(sig, _handler)
            except (ValueError, OSError):
                pass  # signals only settable from the main thread; atexit still covers exit

    def cleanup(self) -> None:
        """Delete this run's workspace now (idempotent)."""
        import shutil

        shutil.rmtree(
            Path(settings.get_workspace_dir(self.config.workspace)).resolve(),
            ignore_errors=True,
        )

    def _start_live_dashboard(self) -> None:
        """Start the shared live dashboard and print its URL so the user can open it
        right away. Best-effort: a dashboard failure must never block constructing or
        using the driver."""
        try:
            import socket

            from synnodb.observability.live_ui.live_dashboard import (
                live_dashboard_url,
                reset_live_dashboard,
                start_live_dashboard,
            )

            reset_live_dashboard()  # fresh, clean timeline for this pipeline
            workspace_dir = Path(
                settings.get_workspace_dir(self.config.workspace)
            ).resolve()
            start_live_dashboard(
                system_name=socket.gethostname(), workspace_dir=workspace_dir
            )
            url = live_dashboard_url()
            if url:
                print(f"\033[1;32m📊 SynnoDB live dashboard: {url}\033[0m")
        except Exception as exc:  # dashboard is non-essential — never fail the driver
            import logging

            logging.getLogger(__name__).warning(
                "Live dashboard could not be started: %s", exc
            )

    @property
    def dashboard_url(self) -> str | None:
        """URL of the live dashboard for this pipeline, or None if it isn't running.
        Started at construction; all stages chained on this driver stream to it."""
        import sys

        _ld = sys.modules.get("synnodb.observability.live_ui.live_dashboard")
        return _ld.live_dashboard_url() if _ld is not None else None

    def __enter__(self) -> "SynnoDB":
        return self

    def __exit__(self, *_exc: Any) -> bool:
        # Workspace deletion is opt-in: only an ephemeral run (cleanup_workspace=True)
        # is torn down on block exit. The default keeps generated artifacts, so
        # `with SynnoDB.in_memory() as db:` never erases the workspace (e.g. ./output).
        if self._cleanup_workspace:
            self.cleanup()
        return False

    # ---- alternative constructors --------------------------------------
    @classmethod
    def from_config(cls, config: SynnoConfig, **kw: Any) -> "SynnoDB":
        return cls(config, **kw)

    @classmethod
    def from_env(cls, env_file: str | None = None, **overrides: Any) -> "SynnoDB":
        return cls(env_file=env_file, **overrides)

    @classmethod
    def in_memory(cls, **overrides: Any) -> "SynnoDB":
        return cls(db_storage=DBStorage.IN_MEMORY, **overrides)

    @classmethod
    def on_ssd(cls, **overrides: Any) -> "SynnoDB":
        return cls(db_storage=DBStorage.SSD, **overrides)

    @classmethod
    def for_tpch(cls, **overrides: Any) -> "SynnoDB":
        return cls(workload=OLAPWorkload.TPCH, **overrides)

    @classmethod
    def for_ceb(cls, **overrides: Any) -> "SynnoDB":
        return cls(workload=OLAPWorkload.CEB, **overrides)

    def with_(self, **overrides: Any) -> "SynnoDB":
        """A new driver with a derived config (immutable; e.g. per-call SF/storage)."""
        return SynnoDB(dataclasses.replace(self.config, **overrides))

    def check_ram_for_sf(self, sf: float) -> RamCheck:
        """Whether the host has enough available RAM to serve this driver's workload
        at scale factor ``sf`` fully in memory."""
        from synnodb.workloads.workload_spec import find_sf_dir, get_workload_spec

        spec = get_workload_spec(self.config.workload.value)
        root = spec.parquet_root()
        sf_dir = find_sf_dir(root, sf)
        if sf_dir is None:
            raise FileNotFoundError(
                f"No sf{sf:g} dataset under {root} for workload {spec.name!r} - "
                "generate the dataset before checking RAM."
            )
        return RamCheck.measure(sf, sf_dir, spec.tables)

    # ---- the single entry point ------------------------------------------
    def run_synthesis(
        self,
        plan: "ConversationPlan",
        *,
        start: StageArtifact | str | None = None,
        storage_plan_snapshot: str | None = None,
        verbose: bool | None = None,
    ) -> StageArtifact:
        """Execute a :class:`~synnodb.plan.ConversationPlan` to completion.

        The plan is the single, self-contained description of the run - no
        kwargs assembly, no field overriding. ``start`` is the per-invocation
        chain token: a stage artifact (its ``.snapshot_hash`` is used), a raw
        git snapshot hash, or None for a fresh workspace.

        ``storage_plan_snapshot`` is the second chain token used only by
        ``createBaseImpl``'s W&B path: the storage-plan text is recovered from
        that snapshot inside the run and injected into the workspace.
        ``verbose`` streams DEBUG logs to the console for this call (a logging
        toggle, not a run-semantic override).

        Returns the plan's typed artifact, stamped with the workspace's prepare
        record (``prepare_features`` / ``parallelism``) so the run chains into
        ``checkSfCorrectness`` or further custom runs with no extra ceremony.
        """
        cfg = self.config
        if isinstance(start, StageArtifact):
            if start.snapshot_hash is None:
                raise ValueError(
                    f"cannot chain from a {type(start).__name__} without a "
                    "snapshot_hash (was the producing run snapshotted?)"
                )
            start_snapshot: str | None = start.snapshot_hash
        else:
            start_snapshot = start

        run_config = RunConfig(
            **_base_run_config(cfg),
            query_list=",".join(map(str, _parse_queries(cfg))),
            bespoke_storage=True,
            start_snapshot=start_snapshot,
            storage_plan_snapshot=storage_plan_snapshot,
            run_tool_offer_trace_option=plan.offer_trace_option,
            memory_budget_mb=_memory_budget(cfg),
        )
        # Per-call verbose overrides the driver default (cfg.verbose, already baked
        # into run_config); either streams DEBUG logs to the console (the logfile is
        # always DEBUG regardless).
        if verbose is not None:
            run_config = dataclasses.replace(run_config, verbose=verbose)
        if cfg.extra_config:
            run_config = dataclasses.replace(run_config, **cfg.extra_config)

        from synnodb.main import run_conv_wrapper  # heavy import, lazy

        result = run_conv_wrapper(run_config=run_config, plan=plan)
        workspace = settings.get_workspace_dir(cfg.workspace)
        artifact = plan.result(result.run_id, result.snapshot_hash, workspace, cfg)
        # Mirror the workspace's prepare record onto the artifact (the workspace
        # metadata file is the source of truth; this is a convenience copy so
        # chained/custom runs can inspect what they start from).
        from synnodb.cpp_runner.prepare_repo.prepare_features import (
            read_prepare_metadata,
        )

        features, parallelism = read_prepare_metadata(workspace)
        return dataclasses.replace(
            artifact, prepare_features=features, parallelism=parallelism
        )

    # ---- ergonomic named methods (thin wrappers over run_synthesis) -------
    def createStoragePlan(self, *, verbose: bool | None = None) -> StoragePlan:
        from synnodb.builtin_plans import storage_plan_plan

        return self.run_synthesis(storage_plan_plan(), verbose=verbose)  # type: ignore[return-value]

    def createBaseImpl(
        self,
        storage_plan: Any = None,
        *,
        storage_plan_wandb_id: Any = None,
        verbose: bool | None = None,
    ) -> BaseImplementation:
        """Build a base implementation from a storage plan.

        Provide exactly one of:
          - ``storage_plan``: the plan *content* itself (a ``str``, or a
            ``StoragePlan`` artifact whose ``.text`` is used). This path is
            W&B-free — the text is injected straight into the workspace.
          - ``storage_plan_wandb_id``: the W&B run id of a logged
            ``createStoragePlan`` run (a ``str``, or a ``StoragePlan`` artifact
            whose ``.run_id`` is used). The plan is recovered from W&B.
        """
        from synnodb.builtin_plans import base_impl_plan

        text = (
            storage_plan.text if isinstance(storage_plan, StoragePlan) else storage_plan
        )
        wandb_id = (
            _as_run_id(storage_plan_wandb_id)
            if storage_plan_wandb_id is not None
            else None
        )
        if (text is None) == (wandb_id is None):
            raise ValueError(
                "createBaseImpl requires exactly one of `storage_plan` (the plan "
                "content) or `storage_plan_wandb_id` (a W&B run id) — got "
                + ("both" if text is not None else "neither")
                + "."
            )

        storage_plan_snapshot = None
        if wandb_id is not None:
            # W&B path: resolve the storage-plan run to its snapshot; the plan
            # text is recovered from that snapshot inside the run.
            storage_plan_snapshot = resolve_source_snapshot(
                self.config,
                snapshot=None,
                wandb_id=wandb_id,
                source_kind="storage plan",
            )

        return self.run_synthesis(  # type: ignore[return-value]
            base_impl_plan(storage_plan_text=text),
            storage_plan_snapshot=storage_plan_snapshot,
            verbose=verbose,
        )

    def runOptimLoop(
        self,
        base_impl: Any = None,
        *,
        base_impl_wandb_id: Any = None,
        plan_source: str = "umbra",
        verbose: bool | None = None,
    ) -> OptimizedImplementation:
        """Optimize a base implementation. Provide exactly one of:
        - ``base_impl``: a ``BaseImplementation`` artifact (or raw git snapshot
          hash) — W&B-free; the snapshot is restored from the local repo.
        - ``base_impl_wandb_id``: the W&B run id of the base-impl run (or an
          artifact whose ``.run_id`` is used)."""
        from synnodb.builtin_plans import optim_plan

        # validate model format (provider/model) for non gpt-/anthropic ids
        model = self.config.model
        if not (model.startswith("anthropic/") or model.startswith("gpt-")):
            assert "/" in model, (
                f"Model name {model} is not in the expected format <provider>/<model_name>"
            )

        snap, wid = _resolve_chain("runOptimLoop", base_impl, base_impl_wandb_id)
        commit_hash = resolve_source_snapshot(
            self.config,
            snapshot=snap,
            wandb_id=wid,
            source_kind="base implementation",
        )
        return self.run_synthesis(  # type: ignore[return-value]
            optim_plan(plan_source=plan_source), start=commit_hash, verbose=verbose
        )

    def addMultiThreading(
        self,
        optimized: Any = None,
        *,
        optimized_wandb_id: Any = None,
        verbose: bool | None = None,
    ) -> MultiThreadedImplementation:
        """Add multi-threading to an optimized implementation. Provide exactly one
        of ``optimized`` (an ``OptimizedImplementation`` artifact / raw snapshot
        hash, W&B-free) or ``optimized_wandb_id`` (the optim run's W&B id)."""
        from synnodb.builtin_plans import mt_plan

        snap, wid = _resolve_chain("addMultiThreading", optimized, optimized_wandb_id)
        commit_hash = resolve_source_snapshot(
            self.config,
            snapshot=snap,
            wandb_id=wid,
            source_kind="optimized implementation",
        )
        return self.run_synthesis(  # type: ignore[return-value]
            mt_plan(), start=commit_hash, verbose=verbose
        )

    def checkSfCorrectness(
        self,
        source: Any = None,
        *,
        target_sf: float,
        source_wandb_id: Any = None,
        verbose: bool | None = None,
    ) -> CorrectnessReport:
        """Validate an engine at a larger scale factor. Provide exactly one of
        ``source`` (any engine artifact / raw snapshot hash, W&B-free) or
        ``source_wandb_id`` (the producing run's W&B id).

        The source run's prepare is replayed from the prepare record committed
        into the source snapshot itself (.synnodb_prepare.json), so no extra
        provenance needs to be passed."""
        from synnodb.builtin_plans import check_sf_plan

        snap, wid = _resolve_chain("checkSfCorrectness", source, source_wandb_id)
        commit_hash = resolve_source_snapshot(
            self.config,
            snapshot=snap,
            wandb_id=wid,
            source_kind="implementation to validate",
        )
        # whole numbers format nicer in prompts (100.0 -> 100)
        if float(target_sf).is_integer():
            target_sf = int(target_sf)
        return self.run_synthesis(  # type: ignore[return-value]
            check_sf_plan(target_sf), start=commit_hash, verbose=verbose
        )
