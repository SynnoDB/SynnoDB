import argparse
import asyncio
import functools
import logging
import os
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

from synnodb.conversations.conv_context import ConvContext
from synnodb.conversations.conversation_engine import Conversation
from synnodb.conversations.filenames import Filenames, get_filenames, get_plan_filename
from synnodb.conversations.prompts_gen import gen_incorrect_output_prompt
from synnodb.conversations.supervision_agent import SupervisionAgent
from synnodb.cpp_runner.prepare_repo.load_snapshot_and_prepare import (
    prepare_repo_and_load_snapshot,
)
from synnodb.cpp_runner.prepare_repo.prepare_features import (
    Parallelism,
    features_metadata_dict,
)
from synnodb.cpp_runner.prepare_repo.prepare_workspace import PrepareWorkspace
from synnodb.ram_check import InsufficientRamError
from synnodb.cpp_runner.prepare_repo.prepare_workspace_olap import OLAPPrepareWorkspace
from synnodb.cpp_runner.prepare_repo.retrieve_framework_version_hash import (
    get_framework_version_artifacts_str,
)
from synnodb.llm.sdk.agents_sdk.openai_sdk import OpenAIAgentsSDKWrapper
from synnodb.observability.live_ui.live_dashboard import (
    get_or_create_live_drain,
    report_live_dashboard_error,
)
from synnodb.observability.logging import notify
from synnodb.observability.logging.logger import setup_logging
from synnodb.observability.logging.run_stats_collector import RunStatsCollector
from synnodb.observability.logging.run_stats_drain import (
    DataDrain,
    DuckDBDrain,
    WandbDrain,
)
from synnodb.observability.logging.weave_cache import configure_weave_cache_dirs
from synnodb.plan import ConversationPlan, SupervisionPolicy
from synnodb.results import RunResult
from synnodb.synth_framework.git_snapshotter import GitSnapshotter
from synnodb.synth_framework.handle_prompt import handle_prompt
from synnodb.synth_framework.runtime_tracker import RuntimeTracker
from synnodb.tools.compile import CompileTool
from synnodb.tools.data_inspect import DataInspectTool
from synnodb.tools.run import RunTool
from synnodb.tools.shell_executor import ShellExecutor
from synnodb.tools.validate.query_validator_class import QueryValidator
from synnodb.tools.workspace_editor import WorkspaceEditor
from synnodb.utils.cli_config import RunConfig, Usecase
from synnodb.utils.confirm_dialog import await_user_confirmation
from synnodb.utils.conv_name_utils import generate_conv_name
from synnodb.utils.core_utils import resolve_target_cores
from synnodb.utils.pkgconfig import check_pkg
from synnodb.utils.snapshot_utils import load_storage_plan_from_snapshot
from synnodb.utils.utils import (
    DBStorage,
    ask_yes_no,
    create_dir_and_set_permissions,
    exclude_workspace_from_enclosing_repo,
    get_disk_db_dir,
)
from synnodb.workloads.query_execution_cache import QueryExecutionCache
from synnodb.workloads.system_factory import System
from synnodb.workloads.system_factory_olap import OLAPSystemFactory
from synnodb.workloads.workload_provider_olap import (
    OLAPWorkloadProvider,
)
from synnodb.workloads.workload_spec import find_sf_dir, get_workload_spec

logger = logging.getLogger(__name__)

# Configuration (SYNNO_DATA_DIR, paths) is resolved lazily via settings so that
# importing this module has no side effects (no assert, no directory creation).
from synnodb import settings


def get_effective_db_storage(usecase: Usecase, db_storage: DBStorage) -> DBStorage:
    return db_storage


async def main(args: argparse.Namespace, plan: ConversationPlan) -> str | None:
    # check all dependencies exist
    test_deps()

    #####
    # Assemble Paths
    #####
    # workspace
    workspace_path = settings.get_workspace_dir(getattr(args, "workspace_dir", None))
    workspace_path.mkdir(parents=True, exist_ok=True)
    # The workspace becomes a nested git repo (the snapshotter); make sure it is never
    # accidentally committed/pushed into an enclosing repo (e.g. the SynnoDB checkout).
    exclude_workspace_from_enclosing_repo(workspace_path)

    # cache paths

    cache_dir = settings.get_data_dir() / "cache"
    create_dir_and_set_permissions(cache_dir)

    prepare_workspace_cache_dir = cache_dir / "prepare_workspace"
    query_execution_cache_dir = cache_dir / "query_execution"
    cloc_cache_dir = cache_dir / "cloc_cache"
    compile_cache_dir = cache_dir / "tool" / "compile"
    validate_cache_dir = cache_dir / "tool" / "validate"
    apply_patch_cache_dir = cache_dir / "tool" / "apply_patch"
    shell_cache_dir = cache_dir / "tool" / "shell"
    data_inspect_cache_dir = cache_dir / "tool" / "data_inspect"
    llm_cache_dir = cache_dir / "llm"

    create_dir_and_set_permissions(prepare_workspace_cache_dir)
    create_dir_and_set_permissions(query_execution_cache_dir)
    create_dir_and_set_permissions(cloc_cache_dir)
    create_dir_and_set_permissions(compile_cache_dir)
    create_dir_and_set_permissions(validate_cache_dir)
    create_dir_and_set_permissions(apply_patch_cache_dir)
    create_dir_and_set_permissions(shell_cache_dir)
    create_dir_and_set_permissions(data_inspect_cache_dir)
    create_dir_and_set_permissions(llm_cache_dir)

    # create git snapshot dir
    snapshotter_base = settings.get_snapshotter_dir()
    if snapshotter_base is not None:
        create_dir_and_set_permissions(snapshotter_base)

    # snapshotter cache repo
    snapshotter_cache_repo = (
        None
        if args.disable_repo_sync
        else os.environ.get("GIT_SNAPSHOTTER_SERVER", None)
    )

    # conversations dir
    conversations_dir = settings.get_data_dir() / "conversations"

    usecase = args.usecase

    # parquet dir and workload provider
    if usecase == Usecase.OLAP:
        workload_spec = get_workload_spec(args.benchmark.value)
        # Bring-your-own workloads carry their absolute parquet location; built-ins
        # derive it from the data-dir + benchmark-name convention.
        parquet_dir = workload_spec.parquet_root()
    else:
        raise Exception(f"Unsupported usecase: {usecase}")

    #####
    # Other preparations
    #####

    notify.SEND_NOTIFICATIONS = args.notify

    # storage related setup
    db_storage = get_effective_db_storage(usecase, args.db_storage)
    args.db_storage = db_storage
    assert db_storage in [
        DBStorage.IN_MEMORY,
        DBStorage.SSD,
    ]  # labstore is not yet fully supported

    if usecase == Usecase.OLAP:
        disk_db_dir, bespoke_ssd_storage_dir = get_disk_db_dir(
            db_storage, workspace_path
        )
        logger.info(f"Using database source: {db_storage}. Disk DB dir: {disk_db_dir}")

    if disk_db_dir is not None:
        create_dir_and_set_permissions(disk_db_dir)

    # Requested query subset (e.g. ["1"]). Threaded into the provider so scaffolding
    # and run/validate are confined to exactly these queries; the provider validates
    # them against the workload's full catalog and raises on unknown ids.
    query_list = [q.strip() for q in args.query_list.split(",")]

    # num_instantiations: CLI override (None -> provider default).
    provider_kwargs = {}
    if getattr(args, "num_instantiations", None) is not None:
        provider_kwargs["num_instantiations"] = args.num_instantiations

    if usecase == Usecase.OLAP:
        workload_provider = OLAPWorkloadProvider(
            benchmark=args.benchmark,  # already resolved (enum for builtins, WorkloadId for BYO)
            base_parquet_dir=parquet_dir,
            db_storage=db_storage,
            bespoke_ssd_storage_dir=bespoke_ssd_storage_dir,
            query_ids=query_list,
            **provider_kwargs,
        )
    else:
        raise Exception(f"Unsupported usecase: {usecase}")

    # Materialize any run-time data artifacts the workload derives lazily before the run proceeds -
    # e.g. a DuckDB-sourced workload downscales its fractional fast-check subsets from the frozen
    # snapshot here, on demand, rather than at sync/ingest time. No-op for workloads whose data is
    # already on disk. Runs before the RAM preflight so the subsets it will load are present.
    workload_provider.prepare()

    # Fail fast before the snapshotter / workspace prepare / any LLM call if the
    # host cannot fit the largest dataset the run would load into memory. The
    # provider owns what (if anything) gets loaded; None means nothing to gate.
    ram_check = workload_provider.preflight_ram_check()
    if ram_check is not None:
        if not ram_check.sufficient:
            raise InsufficientRamError(
                f"{ram_check}. An in-memory run would crash OOM mid-generation; "
                f"use db_storage='ssd', a smaller dataset, or a host with more RAM."
            )
        logger.info("RAM preflight passed: %s", ram_check)

    # get files that should be marked as read-only - they will be untracked in git and excluded from snapshot hashed / ... (i.e. unless their version number changes, they will not affect caches)
    readonly_files_not_git_tracked, readonly_files_git_tracked = (
        PrepareWorkspace._get_readonly_files()
    )

    extra_gitignore = [
        "*.o",
        "*.d",
        "/db",
        "/build/",
        "/tmp/",
        "/output/",
        "*.log",
        "*.tmp",
        "*.out",
        "*.bin",
    ]  # allow txt files - they are necessary for planning & co! And enforced to exist after certain stages!
    if not args.keep_csv:
        # in optimize mode, ignore all .csv files (they are generated during validation).
        # in gen_code mode, we want to keep them around in case of major issues with ensuring correctness while generating the base implementation.
        extra_gitignore.append("*.csv")

    snapshotter = GitSnapshotter(
        cache_repo=snapshotter_cache_repo,
        working_dir=workspace_path,
        extra_gitignore=extra_gitignore,
        do_not_snapshot=args.do_not_cache,  # if caching is disabled, also disable snapshotting to avoid confusion about what is actually cached and what is not.
        exclude_files=readonly_files_not_git_tracked,
    )

    is_dirty, git_status_output = snapshotter.is_dirty()
    if is_dirty:
        # ask the use how to proceed
        if await_user_confirmation(
            f"The working directory ({workspace_path}) has uncommitted changes. Git status output:\n{git_status_output}\n\nWe will remove all uncommited changes now. Is this ok?"
        ):
            # delete uncommited changes
            # clean untracked files
            snapshotter.clear_untracked()
            # reset tracked files to last commit
            snapshotter.reset_changes()
        else:
            raise Exception(
                f'Please remove all uncommitted changes in "{workspace_path}". We expect a clean working directory to ensure reproducibility.'
            )

    ##############################
    # Prepare workspace / snapshot
    ##############################

    plan_filename = get_plan_filename(usecase)

    # Plan extraction is decoupled from the workspace prepare below: it only
    # produces the plan *text* (from one of two sources), which prepare then
    # writes into the clean workspace via the storage_plan_text feature.
    if getattr(args, "storage_plan_text", None) is not None:
        # Direct text (W&B-free): no run to look up, no snapshot to restore.
        storage_plan = args.storage_plan_text
        assert args.storage_plan_snapshot is None, (
            "storage_plan_text and storage_plan_snapshot are mutually exclusive"
        )
    elif args.storage_plan_snapshot is not None:
        # load storage plan snapshot and read storage plan form it.
        # afterwards a clean or other snapshot will be loaded
        storage_plan = load_storage_plan_from_snapshot(
            args, snapshotter, workspace_path, plan_filename=plan_filename
        )

        assert args.start_snapshot is None, (
            "loading a storage plan snapshot, but also providing a start snapshot is not supported. Are you really sure? Usually the storage plan will be kept in the snapshots as soons as coding starts, and you don't have to pass them again."
        )
    else:
        storage_plan = None

    filenames_dict = get_filenames(usecase)

    framework_code_content = get_framework_version_artifacts_str()

    if usecase == Usecase.OLAP:
        prepare_ws = OLAPPrepareWorkspace(
            db_storage=db_storage,
            workload_provider=workload_provider,
            workspace_dir=workspace_path,
            git_snapshotter=snapshotter,
            prepare_cache_dir=prepare_workspace_cache_dir,
        )
    else:
        raise Exception(f"Unsupported usecase: {usecase}")

    # setup snapshot / workspace according to mode
    prepare_result = None
    if args.start_snapshot is None and args.continue_run:
        # continue from current ./output state - nothing to set up
        pass
    else:
        if args.start_snapshot is not None:
            assert not args.continue_run

        # plan.prepare is the run's PrepareFeatures; None selects the replay
        # path (checkSfCorrectness), which reuses the features recorded in the
        # restored snapshot's workspace metadata.
        features = plan.prepare
        if features is not None and storage_plan is not None:
            import dataclasses

            features = dataclasses.replace(features, storage_plan_text=storage_plan)

        prepare_result = prepare_repo_and_load_snapshot(
            snapshotter=snapshotter,
            snapshot=args.start_snapshot,
            features=features,
            prepare_workspace_provider=prepare_ws,
            parallelism=args.needs_parallelism,
            do_not_cache=args.do_not_cache,
            conv_name=args.conv_name,
            only_from_cache=args.only_from_cache,
        )
        framework_code_content += prepare_result.artifacts_str

    if plan.prepare is None:
        # Replay stage (checkSfCorrectness): the run's parallelism is the source
        # run's recorded parallelism, read from the restored workspace metadata.
        assert prepare_result is not None, (
            "a replay-prepare stage cannot run with --continue_run"
        )
        args.needs_parallelism = prepare_result.parallelism

    if args.log_to_wandb and prepare_result is not None:
        # Mirror the workspace's prepare record into the W&B config - a
        # convenience for downstream consumers, not a second source of truth.
        import wandb

        wandb.config.update(
            {
                "prepare_features": features_metadata_dict(prepare_result.features),
                "parallelism": prepare_result.parallelism.value,
            },
            allow_val_change=True,
        )

    ###############
    # Misc setup
    ###############

    # measure total time including cache hits - start the timer
    runtime_tracker = RuntimeTracker()

    # Create hooks instance for tracking metrics
    import socket

    data_drains: list[DataDrain] = [
        # Reuses the one process-wide live drain so chained stages (e.g. the
        # SynnoDB notebook pipeline) accumulate onto a single continuous timeline
        # instead of resetting the dashboard every stage.
        get_or_create_live_drain(
            run_name=args.run_name,
            wandb_run_id=getattr(args, "wandb_run_id", None),
            system_name=socket.gethostname(),
            workspace_dir=workspace_path,
        ),
        DuckDBDrain(db_path=settings.duckdb_drain_dir() / f"{args.run_name}.duckdb"),
    ]
    if args.log_to_wandb:
        data_drains.append(WandbDrain())

    collector_args = dict(
        model=args.model,
        git_snapshotter=snapshotter,
        cloc_cache_dir=cloc_cache_dir,
        runtime_tracker=runtime_tracker,
        do_not_cache=args.do_not_cache,
        drains=data_drains,
        # Plan identity, so every metric row is stage-tagged generically.
        stage_name=plan.name,
    )
    run_stats_collector = RunStatsCollector(**collector_args)

    # run tool parallelism setup (must be determined before QueryValidator so the
    # num_threads value is included in the query-cache key)
    #
    # `threads` is the canonical target degree of parallelism (the DuckDB-style
    # config={'threads': N}); None -> 1, 0 -> all usable cores, N -> N. Resolve it against
    # this machine ONCE so the storage planner, the base-impl prompt, the MT round, and the
    # served engine all agree on the same number. The storage/base/optim stages still
    # VALIDATE serially (CORE_IDS=1, byte-identical) but are TOLD this target so they
    # design a layout/implementation that partitions cleanly across it.
    target_threads, target_core_ids = resolve_target_cores(
        getattr(args, "threads", None)
    )
    assert target_threads >= 1 and len(target_core_ids) == target_threads, (
        f"could not resolve any usable core for threads={getattr(args, 'threads', None)}"
    )
    args.target_threads = target_threads

    # Parallelism need (args.needs_parallelism): plan.parallelism, except for
    # replay stages, where it was overridden above from the source run's recorded
    # parallelism. The run tool takes it as a bool paired with the pinned cores;
    # this is the single enum -> bool boundary.
    if args.needs_parallelism is Parallelism.MULTI_THREADED:
        parallelism = True
        core_ids = target_core_ids
    else:
        parallelism = False
        core_ids = None

    # cap result CSVs at this size before snapshotting (used by validator and
    # exposed in the LLM cache key via config_kwargs below for cache stability)
    max_snapshot_csv_size_mb = 5.0

    if usecase == Usecase.OLAP:
        system_factory = OLAPSystemFactory()
    else:
        raise Exception(f"Unsupported usecase: {usecase}")

    query_execution_cache = QueryExecutionCache(
        query_execution_cache_dir=query_execution_cache_dir,
        system_factory=system_factory,
        do_not_cache=args.do_not_cache,
        only_from_cache=args.only_from_cache,
    )

    query_validator: QueryValidator | None = None
    if not args.disable_valtool:
        # Reference oracle systems travel with the workload. DuckDB is always the
        # ground-truth comparator; Umbra is an optional secondary reference a workload
        # can opt into via WorkloadSpec.reference_systems (default None -> DuckDB only).
        reference_systems = workload_spec.reference_systems
        use_umbra = reference_systems is not None and System.UMBRA in reference_systems
        query_validator = QueryValidator(
            validate_cache_dir=validate_cache_dir,
            workspace_path=workspace_path,
            query_execution_cache=query_execution_cache,
            all_query_ids=query_list,
            git_snapshotter=snapshotter,
            runtime_tracker=runtime_tracker,
            do_not_cache=args.do_not_cache,
            only_from_cache=args.only_from_cache,
            max_snapshot_csv_size_mb=max_snapshot_csv_size_mb,
            use_umbra=use_umbra,
        )

    logger.info(f"Workspace root: {workspace_path}")

    ###############
    # Prepare Tools
    ###############

    # truncate configs
    validate_output_truncate = 10000  # 10k chars ~ 2.5k tokens - truncation applied
    compile_output_truncate = 10000  # 10k chars ~ 2.5k tokens - truncation applied
    shell_output_limit = 150000  # 150k chars ~ 37.5k tokens - return only warning if exceeded (no truncation)

    editor = WorkspaceEditor(
        workspace_path,
        run_stats_collector=run_stats_collector,
        readonly_files=readonly_files_not_git_tracked.union(readonly_files_git_tracked),
        snapshotter=snapshotter,
        cache_dir=apply_patch_cache_dir,
        do_not_cache=args.do_not_cache,
        runtime_tracker=runtime_tracker,
        only_from_cache=args.only_from_cache,
        untracked_cpp_runner_content=framework_code_content,
    )
    shell = ShellExecutor(
        workspace_path,
        snapshotter=snapshotter,
        cache_dir=shell_cache_dir,
        do_not_cache=args.do_not_cache,
        run_stats_collector=run_stats_collector,
        runtime_tracker=runtime_tracker,
        shell_output_limit=shell_output_limit,
        readonly_files=readonly_files_not_git_tracked.union(readonly_files_git_tracked),
        only_from_cache=args.only_from_cache,
        untracked_cpp_runner_content=framework_code_content,
    )

    compile_tool = CompileTool(
        cwd=workspace_path,
        compile_cache_dir=compile_cache_dir,
        do_not_cache=args.do_not_cache,
        only_from_cache=args.only_from_cache,
        git_snapshotter=snapshotter,
        run_stats_collector=run_stats_collector,
        runtime_tracker=runtime_tracker,
        output_truncation=compile_output_truncate,
        db_storage=db_storage,
        usecase=usecase,
        untracked_cpp_runner_content=framework_code_content,
    )

    run_tool = RunTool(
        cwd=workspace_path,
        query_validator=query_validator,
        dataset_name=workload_provider.dataset_name,
        base_parquet_dir=parquet_dir,
        workload_provider=workload_provider,
        run_stats_collector=run_stats_collector,
        memory_budget_mb=args.memory_budget_mb,
        include_mem_budget_for_in_mem_in_hashes=args.include_mem_budget_for_in_mem_in_hashes,
        validate_output_truncation=validate_output_truncate,
        compile_output_truncation=compile_output_truncate,
        parallelism=parallelism,
        core_ids=core_ids,
        db_storage=db_storage,
        compiler=compile_tool.compiler,
        # framework code not necessary here: is chained via compile hash
    )

    # A read-only SQL window into the actual benchmark data (DuckDB), so the agent can ground
    # physical-design choices in real distributions/cardinalities rather than schema alone. Only
    # built for OLAP-style providers that expose the benchmark subset. Cached on disk like the
    # other tools so repeated look-ups (and full-run replays) never re-touch the data.
    data_inspect_tool = None
    if hasattr(workload_provider, "spec") and hasattr(workload_provider, "benchmark_sf"):
        data_inspect_tool = DataInspectTool(
            workload_provider=workload_provider,
            cache_dir=data_inspect_cache_dir,
            do_not_cache=args.do_not_cache,
            only_from_cache=args.only_from_cache,
            runtime_tracker=runtime_tracker,
            run_stats_collector=run_stats_collector,
        )

    # #########################
    # # Prepare Model and Agent
    # #########################

    default_agent_name = "Bespoke Assistant"

    # prepare dict to be included in hash. The CSV size cap is kept here as
    # part of the LLM cache key purely for cache stability; truncation itself
    # is now performed by the run tool, not by the LLM helper.
    config_kwargs: dict[str, Any] = {
        "max_snapshot_csv_size_mb": max_snapshot_csv_size_mb,
    }
    if args.start_snapshot is not None:
        # include start snapshot in hash - makes cache specific to this code base
        config_kwargs["start_snapshot"] = args.start_snapshot

    if workload_spec.dataset_version is not None:
        config_kwargs["dataset_version"] = workload_spec.dataset_version

    supervisor_agent_intruction = "You are a supervisor agent that oversees the execution of a task by another agent. Your role is to monitor the progress, provide feedback, and ensure that the task is completed successfully. You will receive updates on the task execution and can intervene if necessary to guide the process towards a successful outcome."

    if args.sdk == "openai":
        agent_sdk_wrapper = OpenAIAgentsSDKWrapper(
            editor=editor,
            shell=shell,
            compile_tool=compile_tool,
            run_tool=run_tool,
            data_inspect_tool=data_inspect_tool,
            args=args,
            cache_path=llm_cache_dir,
            config_kwargs=config_kwargs,
            workspace_path=workspace_path.as_posix(),
            workspace_path_absolute=workspace_path.absolute(),
            default_agent_name=default_agent_name,
            conv_name=args.conv_name_withdatetime,
            supervisor_agent_instruction=supervisor_agent_intruction,
            snapshotter=snapshotter,
            run_stats_collector=run_stats_collector,
            runtime_tracker=runtime_tracker,
        )
    else:
        raise Exception(f"Unsupported SDK: {args.sdk}")

    ##############
    # Supervisor Agent
    ##############
    if plan.supervision != SupervisionPolicy.OFF:
        supervision_agent = SupervisionAgent(
            agent_sdk_wrapper=agent_sdk_wrapper,
            run_stats_collector=run_stats_collector,
            be_relaxed_if_runtime_goal_not_reached=(
                plan.supervision == SupervisionPolicy.RELAXED
            ),
        )
    else:
        supervision_agent = None

    # start time measurement
    runtime_tracker.start()

    builder_path = filenames_dict["builder_path"]
    query_impl_path = filenames_dict["query_impl_path"]

    assert not args.replay, (
        "Replay mode is not supported. Use replay_cache to replay from cache "
        "without user interaction."
    )

    # manually traced conversation - otherwise will produce multiple separate traces (for each Runner.run() invocation)
    async def _conv_run():
        ctx = ConvContext(
            query_ids=query_list,
            filenames=Filenames.for_usecase(usecase),
            workspace_path=workspace_path,
            db_storage=db_storage,
            threads=target_threads,
            model=args.model,
            run_tool=run_tool,
            workload_provider=workload_provider,
            sql_dict=workload_provider.sql_dict,
            workload=args.benchmark,
            bespoke_storage=args.bespoke_storage,
            max_turns=args.max_turns,
            query_validator=query_validator,
            conversation_json_path=conversations_dir
            / f"{args.conv_name_withdatetime}.json",
            agent_sdk_wrapper=agent_sdk_wrapper,
        )
        conv = Conversation(
            plan_stages=plan.stages,
            conv_context=ctx,
            run_tool=run_tool,
            git_snapshotter=snapshotter,
            run_stats_collector=run_stats_collector,
            supervision_agent=supervision_agent,
            gen_incorrect_output_prompt_fn=functools.partial(
                gen_incorrect_output_prompt,
                query_impl_path=query_impl_path,
                builder_path=builder_path,
                persistent_storage=db_storage in [DBStorage.SSD, DBStorage.LABSTORE],
            ),
            agent_sdk_wrapper=agent_sdk_wrapper,
            callback=functools.partial(
                handle_prompt,
                run_tool=run_tool,
                run_stats_collector=run_stats_collector,
                query_validator=query_validator,
                agent_sdk_wrapper=agent_sdk_wrapper,
            ),
            finish_interactive=plan.finish_interactive,
            debug_category=plan.name,
            prompt_pretext=None,
            notify=args.notify,
            auto_finish=args.auto_finish,
            auto_u=args.auto_u,
            replay_cache=args.replay_cache,
            runtime_tracker=runtime_tracker,
        )
        await conv.run()

    # run conversation with sdk tracing
    await agent_sdk_wrapper.run_traced(
        title=f"Bespoke-Agent {args.conv_name} Conversation",
        data={  # log some metadata about this run
            "query": args.conv_name,
            "model": args.model,
        },
        callback=_conv_run,
    )

    # final flush of snapshot refs to the cache repo. Snapshot() throttles
    # pushes internally; this guarantees the tail of the conversation reaches
    # the remote.
    snapshotter.maybe_push_snapshots(force=True)

    # Publish the finished engine so the drop-in router auto-discovers and routes to it.
    _publish_generated_engine(
        workspace_path,
        workload_provider,
        query_list,
        parquet_dir,
        workload_spec,
        getattr(args, "wandb_run_id", None),
        run_tool=run_tool,
        threads=getattr(args, "target_threads", None),
    )

    logger.debug(
        f"Model cache total saved: ${agent_sdk_wrapper.get_total_saved_by_llm_cache():0.6f}"
    )
    logger.debug(
        f"Total runtime: {runtime_tracker.retrieve_total_time():.2f} seconds (including cache hits, excluding time spent waiting for user input)"
    )
    logger.debug(
        f"Cost: ${run_stats_collector.total_stats['cost_usd']:.6f} (real cost: ${run_stats_collector.total_stats['real_cost_usd']:.6f}, including cache hits)"
    )

    if args.log_to_wandb:
        # Log final summary to wandb
        import wandb

        wandb.log(
            {
                "final/total_cost_usd": run_stats_collector.total_stats["cost_usd"],
                "final/total_real_cost_usd": run_stats_collector.total_stats[
                    "real_cost_usd"
                ],
                "final/total_turns": run_stats_collector.last_turn,
                "final/total_tokens": run_stats_collector.total_stats["output_tokens"]
                + run_stats_collector.total_stats["input_tokens"]
                + run_stats_collector.total_stats["reasoning_tokens"],
                "final/num_prompts": run_stats_collector.prompt_idx + 1,
            }
        )

    # The final git snapshot of the produced code — the W&B-free token a later
    # stage can restore directly from this (same) local workspace repo.
    return snapshotter.current_hash


def _resolve_subset_dir(base_parquet_dir, scale_factor):
    """The subset directory for a scale factor, tolerant of int/float formatting (``fraction1``
    vs ``fraction1.0``, ``sf1`` vs ``sf1.0``); falls back to the first ``fraction*``/``sf*``
    present. None if there is none."""
    resolved = find_sf_dir(base_parquet_dir, scale_factor)
    if resolved is not None:
        return resolved
    found = sorted(
        d
        for d in Path(base_parquet_dir).glob("*")
        if d.is_dir() and (d.name.startswith("fraction") or d.name.startswith("sf"))
    )
    return found[0] if found else None


def _loader_is_shm_capable(workspace_path) -> bool:
    """Whether the compiled loader can ingest from /dev/shm. The shm branch
    (``shm_ingest_enabled()``) is generated only for the in-memory plane (see
    ``prepare_workspace_olap._gen_table_reads``); probing the emitted loader advertises shm
    exactly when the binary supports it, with no separate storage-mode flag to keep in sync."""
    reader = workspace_path / "parquet_reader.cpp"
    try:
        return reader.exists() and "shm_ingest_enabled" in reader.read_text()
    except OSError:
        return False


def _derive_expected_tables(sf_dir, tables, serve_from):
    """The manifest's ``expected_tables`` (column name + DuckDB type) for the engine's tables,
    read the *same way* the serving compatibility gate reads the live schema
    (``information_schema.columns`` + ``check_compatibility``), so an engine never rejects its own
    build. Source of the schema follows ``serve_from``: each table's parquet for a parquet
    workload, or the ``subset.duckdb`` for a DuckDB-native one. Returns ``None`` if any table's
    schema is unavailable, so the caller publishes without the shm plane rather than shipping a
    half-declared schema (the shm gate refuses an engine whose ``expected_tables`` does not cover
    every table its loader reads)."""
    import duckdb

    from synnodb.router.registry import ColumnSpec
    from synnodb.utils.utils import ServeFrom
    from synnodb.workloads.workload_spec import SUBSET_DUCKDB_FILENAME

    def _cols(con, table) -> tuple:
        rows = con.execute(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE lower(table_name) = ? ORDER BY ordinal_position",
            [table.lower()],
        ).fetchall()
        return tuple(ColumnSpec(n, str(dt)) for n, dt in rows)

    if serve_from == ServeFrom.DUCKDB:
        subset_db = Path(sf_dir) / SUBSET_DUCKDB_FILENAME
        if not subset_db.exists():
            return None
        con = duckdb.connect(str(subset_db), read_only=True)
        try:
            out = {}
            for t in tables:
                cols = _cols(con, t)
                if not cols:
                    return None
                out[t] = cols
            return out
        finally:
            con.close()

    con = duckdb.connect()
    try:
        out = {}
        for t in tables:
            pq = Path(sf_dir) / f"{t}.parquet"
            if not pq.exists():
                return None
            ident = '"' + t.replace('"', '""') + '"'
            lit = "'" + str(pq).replace("'", "''") + "'"
            con.execute(f"CREATE TABLE {ident} AS SELECT * FROM read_parquet({lit})")
            out[t] = _cols(con, t)
        return out
    finally:
        con.close()


def _publish_generated_engine(
    workspace_path,
    workload_provider,
    query_list,
    base_parquet_dir,
    workload_spec,
    run_id,
    run_tool,
    threads=None,
):
    """Publish the engine produced by this run for the drop-in router to auto-discover.

    Best-effort: only base/optimized runs leave a ``db`` binary, an engines directory must be
    configured, and the parquet the engine serves must exist. Any failure is logged and
    swallowed so it never fails a generation run.

    The publish is gated on a cache-bypassed live re-validation of the queries being published:
    *run_tool* re-compiles and re-runs them with the validation cache disabled, producing a
    :class:`ValidationReceipt` that publish requires. If that final validation does not pass, the
    engine is *not* published - this is what stops a since-broken build (e.g. one that OOM-failed
    to load) from shipping on the strength of an earlier cached success.

    A parquet workload is published as shm-capable (the zero-copy ``/dev/shm`` hot-load plane)
    with the ``expected_tables`` the plane requires and the disk-backed parquet snapshot as the
    fallback plane; because generation ran over parquet, the receipt covers only the parquet plane
    and the shm plane is downgraded to parquet-only at publish time. A DuckDB-native workload
    (``serve_from=DUCKDB``) instead validates over shm during generation, so it publishes as a
    pure shm engine (no parquet plane) whose receipt covers the shm plane; if its loader is not
    shm-capable there is no parquet fallback, so it is not published at all.
    """
    try:
        if not (workspace_path / "db").exists():
            return  # not an engine-producing run (e.g. storage plan)
        from synnodb.duckdb_compat.discovery import resolve_engines_dir
        from synnodb.workloads.engine_publish import publish_from_provider
        from synnodb.workloads.validation_receipt import PASS

        if resolve_engines_dir(None) is None:
            logger.info(
                "publish: no engines dir (SYNNO_ENGINES_DIR / SYNNO_DATA_DIR); skipping"
            )
            return
        from synnodb.utils.utils import ServeFrom
        from synnodb.workloads.workload_spec import SUBSET_DUCKDB_FILENAME

        serve_from = getattr(workload_spec, "serve_from", ServeFrom.PARQUET)
        native = serve_from == ServeFrom.DUCKDB
        sf = workload_spec.benchmark_sf
        sf_dir = _resolve_subset_dir(base_parquet_dir, sf)
        if sf_dir is None:
            logger.info(
                "publish: no subset under %s; skipping engine publish",
                base_parquet_dir,
            )
            return

        # Final cache-bypassed live validation. Its receipt is the publish precondition; a failing
        # verdict means the current build is broken, so we refuse to publish rather than ship it.
        receipt = run_tool.validate_for_publish(query_list)
        if receipt.verdict != PASS:
            logger.warning(
                "publish: final live validation did not pass (verdict=%s); NOT publishing the "
                "engine in %s",
                receipt.verdict,
                workspace_path,
            )
            return

        shm_capable = False
        expected_tables = None
        if _loader_is_shm_capable(workspace_path):
            expected_tables = _derive_expected_tables(
                sf_dir, list(workload_provider.dataset_tables), serve_from
            )
            shm_capable = expected_tables is not None
            if not shm_capable:
                logger.info(
                    "publish: loader is shm-capable but the subset schema under %s is "
                    "incomplete; withholding the shm plane",
                    sf_dir,
                )

        if native:
            # A DuckDB-native engine serves only over shm (it ingests the connection's own live
            # tables); it has no parquet plane. Without a shm-capable loader there is nothing safe
            # to serve, so skip rather than ship a non-servable engine. source_db records the
            # benchmark subset the engine was built from.
            if not shm_capable:
                logger.info(
                    "publish: DuckDB-native engine is not shm-capable (no parquet fallback "
                    "exists); skipping publish for %s",
                    workspace_path,
                )
                return
            parquet_dir = None
            source_db = str(Path(sf_dir) / SUBSET_DUCKDB_FILENAME)
        else:
            parquet_dir = sf_dir
            source_db = None

        dest = publish_from_provider(
            workspace_path,
            workload_provider,
            query_list,
            receipt=receipt,
            parquet_dir=parquet_dir,
            scale_factor=sf,
            source_run_id=run_id,
            shm_capable=shm_capable,
            expected_tables=expected_tables,
            source_db=source_db,
            threads=threads,
        )
        if dest is not None:
            logger.info(
                "published bespoke engine for auto-discovery -> %s (shm_capable=%s)",
                dest,
                shm_capable,
            )
    except Exception:
        logger.warning(
            "publish: could not publish the generated engine (continuing)",
            exc_info=True,
        )


def _setup() -> None:
    if not check_pkg("arrow", "parquet"):
        raise Exception("arrow and parquet are not available. See README.")


def _run_coroutine(coro):
    """Drive a top-level coroutine to completion, whether or not an event loop is already
    running. Plain ``asyncio.run`` raises "cannot be called from a running event loop" when
    invoked from inside one - which is exactly the case when the API is driven from a Jupyter
    notebook (the documented in-process usage). In that case we run it on a fresh loop in a
    worker thread; otherwise we use ``asyncio.run`` directly."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)  # no loop running: the normal CLI path
    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


def run_conv_wrapper(
    run_config: RunConfig,
    plan: ConversationPlan,
) -> RunResult:
    # The pipeline operates on an argparse.Namespace; the RunConfig the API
    # builds is expanded into one here.
    args = argparse.Namespace(**vars(run_config))

    args.db_storage = get_effective_db_storage(args.usecase, args.db_storage)
    args.log_to_wandb = getattr(args, "log_to_wandb", False)
    # The plan identity drives run naming and is logged to W&B.
    args.stage_name = plan.name

    # Whether this run executes queries multi-threaded (a Parallelism enum).
    # Logged to W&B (as the enum's string value) as a stable, stage-name-independent
    # property so downstream consumers (e.g. benchmark replay) can tell
    # multi-threaded runs apart without matching on the user-defined stage
    # name. For a replay stage (checkSfCorrectness) main() overrides this after
    # restoring the source snapshot, from the parallelism recorded in the
    # workspace prepare metadata.
    args.needs_parallelism = plan.parallelism

    # Pre-initialized so the except/finally handlers below stay valid even when a
    # setup step (dependency checks, SDK selection, W&B init) crashes before these
    # are assigned - the live dashboard, already started by SynnoDB.__init__, still
    # gets its crash banner instead of freezing at its no-data state.
    _wandb_run = None
    run_name: str | None = None
    log_filename: str | None = None
    snapshot_hash: str | None = None
    try:
        _setup()
        if args.continue_run:
            ask_yes_no(
                "Are you really sure you want to continue the current snapshot? Does not start from fresh and continues from current state of output folder. This is DANGEROUS as it might include unwanted files already present in the output folder!"
            )

        # assemble conv name
        conv_name, conv_name_withdatetime = generate_conv_name(
            stage_name=plan.name,
            benchmark=args.benchmark,
            queries_str=args.queries_str,
            model=args.model,
            bespoke_storage=args.bespoke_storage,
            db_storage=args.db_storage,
        )
        # add conv name to args for later use (e.g. in the agent or conversation)
        args.conv_name = conv_name
        args.conv_name_withdatetime = conv_name_withdatetime

        if args.sdk == "openai":
            if args.disable_openai_tracing:
                # disable agents sdk tracing
                from agents.tracing import set_tracing_disabled

                set_tracing_disabled(True)
        else:
            raise Exception(f"Unsupported SDK: {args.sdk}")

        if args.log_to_wandb:
            # add weave (wandb) tracing in addition to openai tracing
            configure_weave_cache_dirs()
            import weave

            import wandb
            from synnodb.settings import get_wandb_entity_project

            entity, project = get_wandb_entity_project(
                getattr(args, "wandb_entity", None),
                getattr(args, "wandb_project", None),
            )

            # With no entity, pass a bare project name so weave/wandb log to the
            # caller's own default entity rather than a hardcoded one.
            weave.init(
                f"{entity}/{project}" if entity else project,
                # weave_log_level="info",
                settings={"log_level": "INFO", "print_call_link": False},
            )

            db_storage_translate_dict = {
                DBStorage.IN_MEMORY: "in-memory",
                DBStorage.SSD: "ssd",
                DBStorage.LABSTORE: "labstore",
            }

            # log statistics to wandb
            tags = [
                db_storage_translate_dict.get(args.db_storage),
                args.benchmark,
            ]

            _wandb_run = wandb.init(
                config=vars(args),
                entity=entity,
                project=project,
                name=args.conv_name,
                tags=tags,
                # dir=f"/tmp/{os.environ['USER']}/wandb",
            )

        # create log dir and setup logging
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

        # assemble log filename
        if _wandb_run is not None:
            run_name = f"{timestamp}_{_wandb_run.id}_{args.conv_name}"
        else:
            run_name = f"{timestamp}_{args.conv_name}"
        args.run_name = run_name  # add run name to args for later use (e.g. in the agent or conversation)

        if _wandb_run is not None:
            _wandb_run.config.update({"log_run_name": run_name})
            args.wandb_run_id = _wandb_run.id
        else:
            args.wandb_run_id = None

        log_filename = f"{run_name}.log"
        # Full DEBUG always goes to the logfile; the console stays at INFO unless the
        # caller opted into verbose output (e.g. db.createStoragePlan(verbose=True)).
        verbose = getattr(args, "verbose", False)
        setup_logging(
            logging.DEBUG,
            settings.log_dir() / log_filename,
            console_level=logging.DEBUG if verbose else logging.INFO,
        )

        if args.notify:
            logger.info(
                "This run will send notifications about errors to the configured Zulip channel."
            )

        snapshot_hash = _run_coroutine(main(args, plan))

        if args.notify:
            # notify about successful completion
            # get exception and stacktrace info

            notify.send_notification(
                f"Conversation completed successfully (*{run_name}*))"
            )
    except BaseException as e:
        import traceback

        stacktrace = traceback.format_exc()

        # str(KeyboardInterrupt()) is empty, so fall back to the exception type
        # name to give the banner something meaningful to display.
        error_message = str(e) or type(e).__name__

        # Surface the failure on the live dashboard so a watcher sees the run
        # aborted (banner + frozen timer) instead of a timer that keeps ticking
        # forever on a dead run. Best-effort: never let reporting mask the
        # original error.
        try:
            report_live_dashboard_error(
                error_message,
                traceback_text=stacktrace,
                log_file=str(settings.log_dir() / log_filename)
                if log_filename
                else None,
            )
        except Exception:
            logger.warning("could not report error to live dashboard", exc_info=True)

        if args.notify:
            # send notification about the error (e.g. via email or slack - not implemented here, just a placeholder)
            logger.error(f"An error occurred: {error_message}. Sending notification...")

            notify_msg = f"Error in conversation (*{run_name}*):\n```quote\n{error_message}\n```\n\nStacktrace:\n```shell\n{stacktrace}\n```"

            notify.send_notification(notify_msg, check_tmux=False)

        raise e
    finally:
        # Finish the wandb run so a subsequent stage executed in the SAME process (the documented
        # in-process chain createStoragePlan -> createBaseImpl) starts its own fresh run instead of
        # re-using this still-active one and failing on the locked "log_run_name" config key.
        # args.wandb_run_id was captured above, so the returned id is unaffected.
        if _wandb_run is not None:
            try:
                _wandb_run.finish()
            except Exception:
                logger.warning("could not finish wandb run cleanly", exc_info=True)

    # Downstream stages chain off this run either via the wandb run id (None
    # unless --log_to_wandb) or, W&B-free, via the final git snapshot hash.
    return RunResult(run_id=args.wandb_run_id, snapshot_hash=snapshot_hash)


def test_deps():
    # check that pyarrow exists before any operations that might need it
    if not check_pkg("arrow", "parquet"):
        raise Exception("arrow and parquet are not available. See README.")

    # check that cloc is available (used by run_stats_collector to count LOC)
    if shutil.which("cloc") is None:
        raise Exception("cloc is not installed. See README.")

    # check that pyarrow library exists
    try:
        import importlib.util

        if importlib.util.find_spec("pyarrow") is None:
            raise ImportError
    except ImportError:
        raise Exception("pyarrow Python package is not installed. Run: uv sync")
