import argparse
import asyncio
import functools
import logging
import os
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any


from synnodb.conversations.conversation_spec import ConversationSpec, FrameworkContext
from synnodb.conversations.filenames import get_filenames, get_plan_filename
from synnodb.conversations.prompts_gen import gen_incorrect_output_prompt
from synnodb.conversations.supervision_agent import SupervisionAgent
from synnodb.cpp_runner.prepare_repo.load_snapshot_and_prepare import (
    prepare_repo_and_load_snapshot,
)
from synnodb.cpp_runner.prepare_repo.prepare_workspace import PrepareWorkspace
from synnodb.cpp_runner.prepare_repo.prepare_workspace_olap import OLAPPrepareWorkspace
from synnodb.cpp_runner.prepare_repo.retrieve_framework_version_hash import (
    get_framework_version_artifacts_str,
)
from synnodb.llm.sdk.agents_sdk.openai_sdk import OpenAIAgentsSDKWrapper
from synnodb.observability.live_ui.live_dashboard import LiveDashboardDrain
from synnodb.observability.logging import notify
from synnodb.observability.logging.logger import setup_logging
from synnodb.observability.logging.run_stats_collector import RunStatsCollector
from synnodb.observability.logging.run_stats_drain import DataDrain, DuckDBDrain, WandbDrain
from synnodb.observability.logging.weave_cache import configure_weave_cache_dirs
from synnodb.synth_framework.git_snapshotter import GitSnapshotter
from synnodb.synth_framework.handle_prompt import handle_prompt
from synnodb.synth_framework.runtime_tracker import RuntimeTracker
from synnodb.tools.compile import CompileTool
from synnodb.tools.run import RunTool
from synnodb.tools.shell_executor import ShellExecutor
from synnodb.tools.validate.query_validator_class import QueryValidator
from synnodb.tools.workspace_editor import WorkspaceEditor
from synnodb.utils.cli_config import RunConfig, Usecase, add_common_args
from synnodb.utils.confirm_dialog import await_user_confirmation
from synnodb.utils.conv_name_utils import generate_conv_name
from synnodb.utils.core_utils import get_cores_for_current_machine
from synnodb.utils.hugepages import get_num_numa_nodes, set_hugepages
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
from synnodb.workloads.system_factory_olap import OLAPSystemFactory
from synnodb.workloads.workload_provider_olap import (
    OLAPWorkload,
    OLAPWorkloadProvider,
)
from synnodb.workloads.workload_spec import get_workload_spec

logger = logging.getLogger(__name__)

# Configuration (SYNNO_DATA_DIR, paths) is resolved lazily via settings so that
# importing this module has no side effects (no assert, no directory creation).
from synnodb import settings


def get_effective_db_storage(usecase: Usecase, db_storage: DBStorage) -> DBStorage:
    return db_storage


async def main(args: argparse.Namespace, spec: ConversationSpec) -> None:
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
    llm_cache_dir = cache_dir / "llm"

    create_dir_and_set_permissions(prepare_workspace_cache_dir)
    create_dir_and_set_permissions(query_execution_cache_dir)
    create_dir_and_set_permissions(cloc_cache_dir)
    create_dir_and_set_permissions(compile_cache_dir)
    create_dir_and_set_permissions(validate_cache_dir)
    create_dir_and_set_permissions(apply_patch_cache_dir)
    create_dir_and_set_permissions(shell_cache_dir)
    create_dir_and_set_permissions(llm_cache_dir)

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
        dataset_name = workload_spec.dataset_name
        # Bring-your-own workloads carry their absolute parquet location; built-ins
        # derive it from the data-dir + benchmark-name convention.
        if workload_spec.base_parquet_dir is not None:
            parquet_dir = Path(workload_spec.base_parquet_dir)
        else:
            parquet_dir = (
                settings.get_data_dir()
                / "workloads"
                / args.benchmark.value
                / f"{dataset_name}_parquet"
            )
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

    # add versioning for the table dataset (dataset got regenerated, scale-up/down code was changed, query args input syntax was changed, etc. - in these cases we want to make sure that old cache entries are not used for the new dataset version)
    dataset_version = None
    if args.benchmark == "ceb":
        dataset_version = "3"

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

    if args.storage_plan_snapshot is not None:
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

    # Dynamic prepare mode of the source run, only set for CHECK_SF (where the
    # prepare steps and parallelism are replayed from the source run via
    # args.prepare_mode). None for every other conv mode.
    source_run_prepare_mode = getattr(args, "prepare_mode", None)

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
    if args.start_snapshot is None and args.continue_run:
        # continue from current ./output state - nothing to set up
        pass
    else:
        if args.start_snapshot is not None:
            assert not args.continue_run

        usecase_prepare_args = dict()
        if storage_plan is not None:
            usecase_prepare_args["storage_plan"] = storage_plan

        framework_code_content += prepare_repo_and_load_snapshot(
            snapshotter=snapshotter,
            snapshot=args.start_snapshot,
            prepare_fn=spec.prepare,
            source_run_prepare_mode=source_run_prepare_mode,
            usecase_prepare_args=usecase_prepare_args,
            do_not_cache=args.do_not_cache,
            conv_name=args.conv_name,
            only_from_cache=args.only_from_cache,
            prepare_workspace_provider=prepare_ws,
        )

    ###############
    # Misc setup
    ###############

    # measure total time including cache hits - start the timer
    runtime_tracker = RuntimeTracker()

    # Create hooks instance for tracking metrics
    import socket

    data_drains: list[DataDrain] = [
        LiveDashboardDrain(
            run_name=args.run_name,
            wandb_run_id=getattr(args, "wandb_run_id", None),
            system_name=socket.gethostname(),
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
    )
    run_stats_collector = RunStatsCollector(**collector_args)

    # run tool parallelism setup (must be determined before QueryValidator so the
    # num_threads value is included in the query-cache key)
    # For CHECK_SF the need for parallelism is derived from the dynamic prepare
    # mode replayed from the source run; every other mode uses spec.needs_parallelism.
    if source_run_prepare_mode is not None:
        effective_needs_parallelism = source_run_prepare_mode == "mt"
    else:
        effective_needs_parallelism = spec.needs_parallelism
    if effective_needs_parallelism:
        parallelism = True
        _, core_ids = get_cores_for_current_machine(
            leave_core_0_out=True,
            allow_hyperthreading=True,
            ncores_to_use=args.max_num_threads,  # measure perf improvement with 20 cores
        )
    else:
        parallelism = False
        _, core_ids = 1, None

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
            use_umbra=False,
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

    if dataset_version is not None:
        config_kwargs["dataset_version"] = dataset_version

    supervisor_agent_intruction = "You are a supervisor agent that oversees the execution of a task by another agent. Your role is to monitor the progress, provide feedback, and ensure that the task is completed successfully. You will receive updates on the task execution and can intervene if necessary to guide the process towards a successful outcome."

    if args.sdk == "openai":
        agent_sdk_wrapper = OpenAIAgentsSDKWrapper(
            editor=editor,
            shell=shell,
            compile_tool=compile_tool,
            run_tool=run_tool,
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
    if args.use_supervision_agent:
        supervision_agent = SupervisionAgent(
            agent_sdk_wrapper=agent_sdk_wrapper,
            run_stats_collector=run_stats_collector,
            be_relaxed_if_runtime_goal_not_reached=spec.be_relaxed_supervision,
        )
    else:
        supervision_agent = None

    # start time measurement
    runtime_tracker.start()

    builder_path = filenames_dict["builder_path"]
    query_impl_path = filenames_dict["query_impl_path"]

    # manually traced conversation - otherwise will produce multiple separate traces (for each Runner.run() invocation)
    async def _conv_run():
        conv_args = dict(
            conversation_json_path=conversations_dir
            / f"{args.conv_name_withdatetime}.json",
            callback=functools.partial(
                handle_prompt,
                run_tool=run_tool,
                run_stats_collector=run_stats_collector,
                query_validator=query_validator,
                agent_sdk_wrapper=agent_sdk_wrapper,
            ),
            auto_finish=args.auto_finish,
            replay_cache=args.replay_cache,
            auto_u=args.auto_u,
            replay=args.replay,
            notify=args.notify,
            runtime_tracker=runtime_tracker,
            agent_sdk_wrapper=agent_sdk_wrapper,
            all_query_ids=query_list,
        )
        auto_conversation_args = dict(
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
        )
        ctx = FrameworkContext(
            args=args,
            workload_provider=workload_provider,
            workspace_path=workspace_path,
            db_storage=db_storage,
            query_list=query_list,
            run_tool=run_tool,
            compile_tool=compile_tool,
            agent_sdk_wrapper=agent_sdk_wrapper,
            snapshotter=snapshotter,
            run_stats_collector=run_stats_collector,
            supervision_agent=supervision_agent,
            query_validator=query_validator,
            conv_args=conv_args,
            auto_conversation_args=auto_conversation_args,
            spec=spec,
        )
        conv = spec.factory(ctx)
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
        workspace_path, workload_provider, query_list, parquet_dir, workload_spec,
        getattr(args, "wandb_run_id", None),
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


def _resolve_sf_dir(base_parquet_dir, scale_factor):
    """The ``sf<N>`` data directory for a scale factor, tolerant of int/float formatting
    (``sf1`` vs ``sf1.0``); falls back to the first ``sf*`` present. None if there is none."""
    base = Path(base_parquet_dir)
    candidates = []
    try:
        if float(scale_factor).is_integer():
            candidates.append(f"sf{int(scale_factor)}")
    except (TypeError, ValueError):
        pass
    candidates.append(f"sf{scale_factor}")
    for name in candidates:
        if (base / name).exists():
            return base / name
    found = sorted(base.glob("sf*"))
    return found[0] if found else None


def _publish_generated_engine(
    workspace_path, workload_provider, query_list, base_parquet_dir, workload_spec, run_id
):
    """Publish the engine produced by this run for the drop-in router to auto-discover.

    Best-effort: only base/optimized runs leave a ``db`` binary, an engines directory must be
    configured, and the parquet the engine serves must exist. Any failure is logged and
    swallowed so it never fails a generation run.
    """
    try:
        if not (workspace_path / "db").exists():
            return  # not an engine-producing run (e.g. storage plan)
        from synnodb.duckdb_compat.discovery import resolve_engines_dir
        from synnodb.workloads.engine_publish import publish_from_provider

        if resolve_engines_dir(None) is None:
            logger.info("publish: no engines dir (SYNNO_ENGINES_DIR / SYNNO_DATA_DIR); skipping")
            return
        sf = workload_spec.benchmark_sf
        sf_dir = _resolve_sf_dir(base_parquet_dir, sf)
        if sf_dir is None:
            logger.info("publish: no parquet under %s; skipping engine publish", base_parquet_dir)
            return
        dest = publish_from_provider(
            workspace_path, workload_provider, query_list,
            parquet_dir=sf_dir, scale_factor=sf, source_run_id=run_id,
        )
        if dest is not None:
            logger.info("published bespoke engine for auto-discovery -> %s", dest)
    except Exception:
        logger.warning("publish: could not publish the generated engine (continuing)", exc_info=True)


def _setup() -> None:
    if not check_pkg("arrow", "parquet"):
        raise Exception("arrow and parquet are not available. See README.")

    for node in range(get_num_numa_nodes()):
        set_hugepages(node=node, page_kb=2048, count=0)


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
    args: argparse.Namespace | None,
    run_config: RunConfig | None,
    spec: ConversationSpec,
) -> str | None:
    # assemble args from run_config if main.py is started from run scripts
    if args is None and run_config is None:
        raise Exception("Either args or run_config must be provided.")
    elif args is None:
        # convert run_config to args for conv wrapper
        args = argparse.Namespace(**vars(run_config))
    else:
        pass

    args.db_storage = get_effective_db_storage(args.usecase, args.db_storage)
    args.log_to_wandb = getattr(args, "log_to_wandb", False)

    _setup()
    if args.continue_run:
        ask_yes_no(
            "Are you really sure you want to continue the current snapshot? Does not start from fresh and continues from current state of output folder. This is DANGEROUS as it might include unwanted files already present in the output folder!"
        )

    # assemble conv name
    conv_name, conv_name_withdatetime = generate_conv_name(
        conv_type=args.conv_mode,
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
        import wandb
        import weave

        entity = os.getenv("WANDB_ENTITY", "learneddb")
        project = os.getenv("WANDB_PROJECT", "bespoke-olap-internal")

        weave.init(
            f"{entity}/{project}",
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
    else:
        _wandb_run = None

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
    setup_logging(logging.DEBUG, settings.log_dir() / log_filename)

    if args.notify:
        logger.info(
            "This run will send notifications about errors to the configured Zulip channel."
        )

    try:
        _run_coroutine(main(args, spec))

        if args.notify:
            # notify about successful completion
            # get exception and stacktrace info

            notify.send_notification(
                f"Conversation completed successfully (*{run_name}*))"
            )
    except Exception as e:
        if args.notify:
            # send notification about the error (e.g. via email or slack - not implemented here, just a placeholder)
            logger.error(f"An error occurred: {e}. Sending notification...")

            # get exception and stacktrace info
            import traceback

            notify_msg = f"Error in conversation (*{run_name}*):\n```quote\n{str(e)}\n```\n\nStacktrace:\n```shell\n{traceback.format_exc()}\n```"

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

    # The wandb run id (None unless --log_to_wandb) is how downstream stages
    # chain off this run; return it so programmatic callers can pass it along.
    return args.wandb_run_id


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


# Run manually e.g. with:
# python main.py manual --model gpt-5.4 --conv_name debugq1-22v1 --start_snapshot eb0d178ebc7be2cacec11ea474823daecf7eb013 --benchmark tpch --bespoke_storage --query_list 1,2 --do_not_cache

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    manual = subparsers.add_parser(
        "manual",
        help="Run a conversation using explicit mode/query args.",
    )
    add_common_args(
        manual,
        include_model=True,
        include_benchmark=True,
        include_replay=True,
        include_disable_openai_tracing=True,
        include_log_to_wandb=True,
        include_query_list=True,
        include_continue_run=True,
        include_no_preload=True,
        include_notify=True,
        include_start_snapshot=True,
        include_disable_repo_sync=True,
        include_replay_cache=True,
        include_auto_u=True,
        include_auto_finish=True,
        include_keep_csv=True,
        include_disable_valtool=True,
        include_conv_mode=True,
        include_run_tool_offer_trace_option=True,
        include_bespoke_storage=True,
        include_only_from_llm_cache=True,
        include_only_from_cache=True,
        include_do_not_cache=True,
        include_tool_search_tool=True,
        include_use_autonomy_master_prompt=True,
        include_sdk=True,
        include_optimize_sample_plan_source=True,
        include_use_supervision_agent=True,
        include_memory_budget_mb=True,
        include_include_mem_budget_for_in_mem_in_hashes=True,
        include_db_storage=True,
    )
    args = parser.parse_args()
    args.write_query_and_args_files = True

    if args.command == "manual":
        # the manual debug entry point is the only consumer that resolves a spec
        # by name; the run_*.py scripts pass their own spec directly.
        from synnodb.conversations.manual_specs import get_spec

        spec = get_spec(args.conv_mode)
        run_conv_wrapper(args, run_config=None, spec=spec)
    else:
        raise Exception(f"Unknown {args.command}")
