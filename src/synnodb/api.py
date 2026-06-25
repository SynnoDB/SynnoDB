"""Importable, typed entry point to the SynnoDB agent pipeline.

    from synnodb import SynnoDB
    db = SynnoDB.in_memory(workload="tpch")
    plan = db.createStoragePlan(queries="1")   # -> StoragePlan; plan.text is the doc
    impl = db.createBaseImpl(storage_plan=plan) # -> BaseImplementation; impl.files

Each call runs one stage to completion (blocking) and returns the stage's domain
artifact (StoragePlan, BaseImplementation, ...) — carrying the produced content
plus the wandb run id used to chain stages.
"""
from __future__ import annotations

import dataclasses
import importlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

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
from synnodb.workloads.workload_provider import Workload
from synnodb.workloads.workload_provider_olap import OLAPWorkload

__all__ = ["SynnoDB", "SynnoConfig", "Stage", "StageParam", "register_stage"]


# ----------------------------- coercion helpers -----------------------------
def _as_workload(v: Workload | str) -> Workload:
    return v if isinstance(v, Workload) else Workload.of(str(v))


def _as_storage(v: DBStorage | str) -> DBStorage:
    return v if isinstance(v, DBStorage) else DBStorage(str(v))


def _as_usecase(v: Usecase | str) -> Usecase:
    return v if isinstance(v, Usecase) else Usecase(str(v))


def _as_arg(v: Any) -> str:
    """A stage artifact collapses to its run id; everything else stringifies."""
    if isinstance(v, StageArtifact):
        if v.run_id is None:
            raise ValueError(
                f"cannot chain from a {type(v).__name__} with no run_id — run the "
                "producing stage with log_to_wandb=True."
            )
        return v.run_id
    return str(v)


# --------------------------------- config -----------------------------------
@dataclass(frozen=True)
class SynnoConfig:
    """Immutable, enum-typed run settings shared across stages."""

    model: str = DEFAULT_MODEL
    workload: Workload = OLAPWorkload.TPCH
    db_storage: DBStorage = DBStorage.IN_MEMORY
    usecase: Usecase = Usecase.OLAP
    queries: str = "1"
    log_to_wandb: bool = True          # required to chain stages
    auto_confirm: bool = True          # --auto_u
    auto_finish: bool = True
    disable_openai_tracing: bool = True
    disable_repo_sync: bool = False
    notify: bool = False
    do_not_cache: bool = False
    workspace: str | None = None       # run output dir; None -> local ./output
    extra_flags: tuple[str, ...] = ()  # escape hatch for any unmodelled CLI flag

    def __post_init__(self) -> None:
        object.__setattr__(self, "workload", _as_workload(self.workload))
        object.__setattr__(self, "db_storage", _as_storage(self.db_storage))
        object.__setattr__(self, "usecase", _as_usecase(self.usecase))

    def to_argv(self) -> list[str]:
        argv = [
            "--model", self.model,
            "--benchmark", self.workload.value,
            "--db_storage", self.db_storage.value,
            "--queries", self.queries,
        ]
        if self.workspace:
            argv += ["--workspace", self.workspace]
        for name, on in (
            ("auto_u", self.auto_confirm),
            ("auto_finish", self.auto_finish),
            ("disable_openai_tracing", self.disable_openai_tracing),
            ("log_to_wandb", self.log_to_wandb),
            ("disable_repo_sync", self.disable_repo_sync),
            ("notify", self.notify),
            ("do_not_cache", self.do_not_cache),
        ):
            if on:
                argv.append(f"--{name}")
        return argv + list(self.extra_flags)


# ---------------------- result builders (workspace -> artifact) -------------
# Each reads the stage's produced artifact out of the run's workspace and wraps
# it with provenance (run_id, workspace, config). Signature is uniform so the
# generic run() can dispatch on the Stage.

ResultBuilder = Callable[["str | None", Path, "SynnoConfig", "dict[str, Any]"], StageArtifact]


def _engine_files(workspace: Path) -> dict[str, str]:
    files: dict[str, str] = {}
    for p in sorted(workspace.glob("*.cpp")) + sorted(workspace.glob("*.hpp")):
        try:
            files[p.name] = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            pass
    return files


def _build_artifact(run_id, workspace, config, inputs) -> StageArtifact:
    return StageArtifact(run_id, workspace, config)


def _build_storage_plan(run_id, workspace, config, inputs) -> StoragePlan:
    path = workspace / "storage_plan.txt"
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    return StoragePlan(run_id, workspace, config, path, text)


def _build_base_impl(run_id, workspace, config, inputs) -> BaseImplementation:
    return BaseImplementation(run_id, workspace, config, _engine_files(workspace))


def _build_optimized(run_id, workspace, config, inputs) -> OptimizedImplementation:
    return OptimizedImplementation(run_id, workspace, config, _engine_files(workspace))


def _build_multithreaded(run_id, workspace, config, inputs) -> MultiThreadedImplementation:
    return MultiThreadedImplementation(run_id, workspace, config, _engine_files(workspace))


def _build_correctness(run_id, workspace, config, inputs) -> CorrectnessReport:
    return CorrectnessReport(run_id, workspace, config, float(inputs["target_sf"]))


# --------------------------------- stages -----------------------------------
@dataclass(frozen=True)
class StageParam:
    kw: str                  # python kwarg, e.g. "storage_plan"
    flag: str                # CLI flag, e.g. "--storage_plan_run_id"
    required: bool = True


@dataclass(frozen=True)
class Stage:
    name: str                            # "createStoragePlan"
    module: str                          # "synnodb.run_gen_storage_plan"
    usecases: frozenset[Usecase]
    result: ResultBuilder = _build_artifact   # builds the typed domain object
    params: tuple[StageParam, ...] = ()
    flags: tuple[str, ...] = ()          # always-appended, e.g. ("--bespoke_storage",)


_REGISTRY: dict[str, Stage] = {}


def register_stage(stage: Stage) -> None:
    if stage.name in _REGISTRY:
        raise ValueError(f"stage {stage.name!r} already registered")
    _REGISTRY[stage.name] = stage


def _register_olap_stages() -> None:
    olap = frozenset({Usecase.OLAP})
    bespoke = ("--bespoke_storage",)
    register_stage(Stage(
        "createStoragePlan", "synnodb.run_gen_storage_plan", olap,
        result=_build_storage_plan,
    ))
    register_stage(Stage(
        "createBaseImpl", "synnodb.run_gen_base_impl", olap,
        result=_build_base_impl,
        params=(StageParam("storage_plan", "--storage_plan_run_id"),),
        flags=bespoke,
    ))
    register_stage(Stage(
        "runOptimLoop", "synnodb.run_optim_loop", olap,
        result=_build_optimized,
        params=(StageParam("base_impl", "--base_impl_run_id"),),
        flags=bespoke,
    ))
    register_stage(Stage(
        "addMultiThreading", "synnodb.run_add_multi_threading", olap,
        result=_build_multithreaded,
        params=(StageParam("optimized", "--optim_run_id"),),
        flags=bespoke,
    ))
    register_stage(Stage(
        "checkSfCorrectness", "synnodb.run_check_sf_correctness", olap,
        result=_build_correctness,
        params=(StageParam("source", "--source_run_id"),
                StageParam("target_sf", "--target_sf")),
        flags=bespoke,
    ))


_register_olap_stages()


# --------------------------------- facade -----------------------------------
class SynnoDB:
    """Programmatic pipeline driver. Each call runs one stage to completion."""

    def __init__(
        self,
        config: SynnoConfig | None = None,
        /,
        *,
        data_dir: str | None = None,
        env_file: str | None = None,
        **overrides: Any,
    ):
        # The stage modules (imported lazily in run()) still need SYNNO_DATA_DIR;
        # configure it now so the first stage call doesn't fail at import.
        settings.configure(data_dir=data_dir, env_file=env_file)
        base = config or SynnoConfig()
        self.config = dataclasses.replace(base, **overrides) if overrides else base

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
        st = stage if isinstance(stage, Stage) else _REGISTRY[stage]
        cfg = self.config
        if cfg.usecase not in st.usecases:
            allowed = sorted(u.value for u in st.usecases)
            raise ValueError(
                f"stage {st.name!r} serves usecases {allowed}, not {cfg.usecase.value!r}"
            )
        argv = cfg.to_argv() + list(st.flags)
        for p in st.params:
            if inputs.get(p.kw) is not None:
                argv += [p.flag, _as_arg(inputs[p.kw])]
            elif p.required:
                raise TypeError(f"stage {st.name!r} requires {p.kw!r}")
        module = importlib.import_module(st.module)         # heavy import, lazy
        run_id = module.main(module.build_parser().parse_args(argv))
        workspace = settings.get_workspace_dir(cfg.workspace)
        return st.result(run_id, workspace, cfg, inputs)

    # ---- ergonomic named methods (OLAP) --------------------------------
    def createStoragePlan(self, **inputs: Any) -> StoragePlan:
        return self.run("createStoragePlan", **inputs)  # type: ignore[return-value]

    def createBaseImpl(self, storage_plan: Any, **inputs: Any) -> BaseImplementation:
        return self.run("createBaseImpl", storage_plan=storage_plan, **inputs)  # type: ignore[return-value]

    def runOptimLoop(self, base_impl: Any, **inputs: Any) -> OptimizedImplementation:
        return self.run("runOptimLoop", base_impl=base_impl, **inputs)  # type: ignore[return-value]

    def addMultiThreading(self, optimized: Any, **inputs: Any) -> MultiThreadedImplementation:
        return self.run("addMultiThreading", optimized=optimized, **inputs)  # type: ignore[return-value]

    def checkSfCorrectness(self, source: Any, target_sf: float, **inputs: Any) -> CorrectnessReport:
        return self.run(  # type: ignore[return-value]
            "checkSfCorrectness", source=source, target_sf=target_sf, **inputs
        )
