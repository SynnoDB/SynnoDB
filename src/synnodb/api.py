"""Importable, typed entry point to the SynnoDB agent pipeline.

    from synnodb import SynnoDB
    db = SynnoDB.in_memory(workload="tpch")
    plan = db.createStoragePlan(queries="1")        # -> StoragePlan; plan.text is the doc
    impl = db.createBaseImpl(storage_plan=plan.text) # -> BaseImplementation; impl.files

Each call runs one stage to completion (blocking) and returns the stage's domain
artifact (StoragePlan, BaseImplementation, ...) — carrying the produced content
plus the wandb run id used to chain stages.
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from synnodb import settings

# These enums resolve without SYNNO_DATA_DIR (the module-level assert was made
# lazy), so importing synnodb stays config-free. The heavy stage modules
# (synnodb.main, synnodb.run_*) are imported lazily inside run().
from synnodb.results import (
    BaseImplementation,
    CorrectnessReport,
    MultiThreadedImplementation,
    OptimizedImplementation,
    StageArtifact,
    StoragePlan,
)
from synnodb.utils.cli_config import DEFAULT_MODEL, Usecase
from synnodb.utils.utils import DBStorage
from synnodb.workloads.workload_provider import Workload, WorkloadId
from synnodb.workloads.workload_provider_olap import OLAPWorkload

if TYPE_CHECKING:
    from synnodb.conversations.conversation_spec import FrameworkContext
    from synnodb.cpp_runner.prepare_repo.load_snapshot_and_prepare import PrepareContext
    from synnodb.conversations.conversation import AbstractConversation
    from synnodb.utils.cli_config import RunConfig

__all__ = ["SynnoDB", "SynnoConfig", "Stage", "register_stage"]


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


def _as_arg(v: Any) -> str:
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


def _resolve_chain(stage: str, source: Any, source_wandb_id: Any) -> tuple[str | None, str | None]:
    """Resolve the chaining tokens for a stage that consumes a previous stage's
    output. Returns ``(snapshot_hash, wandb_run_id)`` with exactly one set:
      - ``source`` -> its ``.snapshot_hash`` (or a raw hash str): W&B-free path.
      - ``source_wandb_id`` -> its ``.run_id`` (or a raw id str): W&B path.
    """
    snap = source.snapshot_hash if isinstance(source, StageArtifact) else source
    wid = _as_arg(source_wandb_id) if source_wandb_id is not None else None
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
    wandb_entity: str | None = None    # None -> the user's own default W&B entity
    wandb_project: str | None = None   # None -> the default "SynnoDB" project
    auto_confirm: bool = True          # --auto_u
    auto_finish: bool = True
    disable_openai_tracing: bool = True
    disable_repo_sync: bool = False
    notify: bool = False
    do_not_cache: bool = False
    workspace: str | None = None       # run output dir; None -> local ./output
    # escape hatch for any unmodelled RunConfig field: applied (dataclasses.replace)
    # onto the RunConfig a stage's build_config produces, just before execution.
    extra_config: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "workload", _as_workload(self.workload))
        object.__setattr__(self, "db_storage", _as_storage(self.db_storage))
        object.__setattr__(self, "usecase", _as_usecase(self.usecase))

    @property
    def wandb_enabled(self) -> bool:
        """wandb is on iff an entity or project is set."""
        return self.wandb_entity is not None or self.wandb_project is not None


# ---------------------- result builders (workspace -> artifact) -------------
# Each reads the stage's produced artifact out of the run's workspace and wraps
# it with provenance (run_id, snapshot_hash, workspace, config). Signature is
# uniform so the generic run() can dispatch on the Stage.

ResultBuilder = Callable[
    ["str | None", "str | None", Path, "SynnoConfig", "dict[str, Any]"], StageArtifact
]


def _engine_files(workspace: Path) -> dict[str, str]:
    files: dict[str, str] = {}
    for p in sorted(workspace.glob("*.cpp")) + sorted(workspace.glob("*.hpp")):
        try:
            files[p.name] = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            pass
    return files


def _build_artifact(run_id, snapshot_hash, workspace, config, inputs) -> StageArtifact:
    return StageArtifact(run_id, workspace, config, snapshot_hash=snapshot_hash)


def _build_storage_plan(run_id, snapshot_hash, workspace, config, inputs) -> StoragePlan:
    path = workspace / "storage_plan.txt"
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    return StoragePlan(run_id, workspace, config, path, text, snapshot_hash=snapshot_hash)


def _build_base_impl(run_id, snapshot_hash, workspace, config, inputs) -> BaseImplementation:
    return BaseImplementation(run_id, workspace, config, _engine_files(workspace), snapshot_hash=snapshot_hash)


def _build_optimized(run_id, snapshot_hash, workspace, config, inputs) -> OptimizedImplementation:
    return OptimizedImplementation(run_id, workspace, config, _engine_files(workspace), snapshot_hash=snapshot_hash)


def _build_multithreaded(run_id, snapshot_hash, workspace, config, inputs) -> MultiThreadedImplementation:
    return MultiThreadedImplementation(run_id, workspace, config, _engine_files(workspace), snapshot_hash=snapshot_hash)


def _build_correctness(run_id, snapshot_hash, workspace, config, inputs) -> CorrectnessReport:
    return CorrectnessReport(run_id, workspace, config, float(inputs["target_sf"]), snapshot_hash=snapshot_hash)


# --------------------------------- stages -----------------------------------
# One descriptor per pipeline stage, end to end. ``build_config`` assembles the
# RunConfig directly from the typed SynnoConfig + per-call inputs (no argv); the
# remaining fields are the execution-backend contract main.py consumes (prepare /
# parallelism / factory) plus the result builder. The concrete instances live in
# synnodb/stages.py, imported lazily by run() so plain ``import synnodb`` stays light.
@dataclass(frozen=True)
class Stage:
    name: str                                  # "createStoragePlan" — the run-type identity
    usecases: frozenset[Usecase]
    build_config: "Callable[[SynnoConfig, dict[str, Any]], RunConfig]"
    prepare: "Callable[[PrepareContext], str]"
    needs_parallelism: bool
    be_relaxed_supervision: bool
    factory: "Callable[[FrameworkContext], AbstractConversation]"
    result: ResultBuilder = _build_artifact    # builds the typed domain object


_REGISTRY: dict[str, Stage] = {}


def register_stage(stage: Stage) -> None:
    if stage.name in _REGISTRY:
        raise ValueError(f"stage {stage.name!r} already registered")
    _REGISTRY[stage.name] = stage


_stages_loaded = False


def _load_stages() -> None:
    """Import the stage catalog once, populating ``_REGISTRY`` as a side effect."""
    global _stages_loaded
    if not _stages_loaded:
        import synnodb.stages  # noqa: F401  (registers stages on import)

        _stages_loaded = True


def all_stages() -> tuple[Stage, ...]:
    """Every registered stage (loads the catalog on first call)."""
    _load_stages()
    return tuple(_REGISTRY.values())


def get_stage(name: str) -> Stage:
    """Look up a registered stage by name (loads the catalog on first call)."""
    _load_stages()
    if name not in _REGISTRY:
        raise ValueError(f"Unknown stage {name!r}. Known stages: {sorted(_REGISTRY)}")
    return _REGISTRY[name]


# Source artifact type -> the stage name CHECK_SF must replay its prepare from,
# for the W&B-free path (the W&B path reads it from the source run's config).
_SOURCE_STAGE_NAME = {
    "BaseImplementation": "createBaseImpl",
    "OptimizedImplementation": "runOptimLoop",
    "MultiThreadedImplementation": "addMultiThreading",
}


# --------------------------------- facade -----------------------------------
class SynnoDB:
    """Programmatic pipeline driver. Each call runs one stage to completion."""

    def __init__(
        self,
        config: SynnoConfig | None = None,
        /,
        *,
        data_dir: str | Path | None = None,
        env_file: str | None = None,
        cleanup_workspace: bool = False,
        **overrides: Any,
    ):
        # The stage modules (imported lazily in run()) still need SYNNO_DATA_DIR;
        # configure it now so the first stage call doesn't fail at import.
        settings.configure(data_dir=data_dir, env_file=env_file)
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
                    if callable(_prev) and _prev not in (signal.SIG_DFL, signal.SIG_IGN):
                        _prev(signum, frame)
                    else:
                        raise SystemExit(128 + signum)

                signal.signal(sig, _handler)
            except (ValueError, OSError):
                pass  # signals only settable from the main thread; atexit still covers exit

    def cleanup(self) -> None:
        """Delete this run's workspace now (idempotent)."""
        import shutil

        shutil.rmtree(Path(settings.get_workspace_dir(self.config.workspace)).resolve(), ignore_errors=True)

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
            workspace_dir = Path(settings.get_workspace_dir(self.config.workspace)).resolve()
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

    # ---- generic engine -------------------------------------------------
    def run(self, stage: str | Stage, /, **inputs: Any) -> StageArtifact:
        _load_stages()
        st = stage if isinstance(stage, Stage) else _REGISTRY[stage]
        cfg = self.config
        if cfg.usecase not in st.usecases:
            allowed = sorted(u.value for u in st.usecases)
            raise ValueError(
                f"stage {st.name!r} serves usecases {allowed}, not {cfg.usecase.value!r}"
            )
        # Assemble the RunConfig straight from the typed config + inputs — no argv,
        # no argparse round-trip. extra_config is the escape hatch for any field the
        # typed SynnoConfig does not model.
        run_config = st.build_config(cfg, inputs)
        if cfg.extra_config:
            run_config = dataclasses.replace(run_config, **cfg.extra_config)

        from synnodb.main import run_conv_wrapper  # heavy import, lazy

        result = run_conv_wrapper(args=None, run_config=run_config, spec=st)
        workspace = settings.get_workspace_dir(cfg.workspace)
        return st.result(result.run_id, result.snapshot_hash, workspace, cfg, inputs)

    # ---- ergonomic named methods (OLAP) --------------------------------
    def createStoragePlan(self, **inputs: Any) -> StoragePlan:
        return self.run("createStoragePlan", **inputs)  # type: ignore[return-value]

    def createBaseImpl(
        self,
        storage_plan: Any = None,
        *,
        storage_plan_wandb_id: Any = None,
        **inputs: Any,
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
        text = storage_plan.text if isinstance(storage_plan, StoragePlan) else storage_plan
        wandb_id = _as_arg(storage_plan_wandb_id) if storage_plan_wandb_id is not None else None
        if (text is None) == (wandb_id is None):
            raise ValueError(
                "createBaseImpl requires exactly one of `storage_plan` (the plan "
                "content) or `storage_plan_wandb_id` (a W&B run id) — got "
                + ("both" if text is not None else "neither")
                + "."
            )
        return self.run(  # type: ignore[return-value]
            "createBaseImpl",
            storage_plan_text=text,
            storage_plan_wandb_id=wandb_id,
            **inputs,
        )

    def runOptimLoop(
        self, base_impl: Any = None, *, base_impl_wandb_id: Any = None, **inputs: Any
    ) -> OptimizedImplementation:
        """Optimize a base implementation. Provide exactly one of:
          - ``base_impl``: a ``BaseImplementation`` artifact (or raw git snapshot
            hash) — W&B-free; the snapshot is restored from the local repo.
          - ``base_impl_wandb_id``: the W&B run id of the base-impl run (or an
            artifact whose ``.run_id`` is used)."""
        snap, wid = _resolve_chain("runOptimLoop", base_impl, base_impl_wandb_id)
        return self.run(  # type: ignore[return-value]
            "runOptimLoop", base_impl_snapshot=snap, base_impl=wid, **inputs
        )

    def addMultiThreading(
        self, optimized: Any = None, *, optimized_wandb_id: Any = None, **inputs: Any
    ) -> MultiThreadedImplementation:
        """Add multi-threading to an optimized implementation. Provide exactly one
        of ``optimized`` (an ``OptimizedImplementation`` artifact / raw snapshot
        hash, W&B-free) or ``optimized_wandb_id`` (the optim run's W&B id)."""
        snap, wid = _resolve_chain("addMultiThreading", optimized, optimized_wandb_id)
        return self.run(  # type: ignore[return-value]
            "addMultiThreading", optim_snapshot=snap, optimized=wid, **inputs
        )

    def checkSfCorrectness(
        self,
        source: Any = None,
        *,
        target_sf: float,
        source_wandb_id: Any = None,
        source_stage: str | None = None,
        **inputs: Any,
    ) -> CorrectnessReport:
        """Validate an engine at a larger scale factor. Provide exactly one of
        ``source`` (any engine artifact / raw snapshot hash, W&B-free) or
        ``source_wandb_id`` (the producing run's W&B id).

        On the W&B-free path the source run's stage is needed to replay its
        prepare steps: it is inferred from the ``source`` artifact's type, or
        pass ``source_stage`` explicitly (e.g. 'createBaseImpl', 'runOptimLoop',
        'addMultiThreading') when ``source`` is a raw snapshot hash."""
        snap, wid = _resolve_chain("checkSfCorrectness", source, source_wandb_id)
        if snap is not None and source_stage is None and isinstance(source, StageArtifact):
            source_stage = _SOURCE_STAGE_NAME.get(type(source).__name__)
        return self.run(  # type: ignore[return-value]
            "checkSfCorrectness",
            source_snapshot=snap,
            source=wid,
            source_stage=source_stage,
            target_sf=target_sf,
            **inputs,
        )
