import argparse
import asyncio
import functools
import logging
import os
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any, Dict

import wandb
from dotenv import load_dotenv

from conversations.base_impl_conversation import BaseImplConversation
from conversations.check_sf_correctness_conv import CheckSFCorrectnessConv
from conversations.filenames import get_filenames
from conversations.gen_storage_plan_conversation import GenStoragePlanConversation
from conversations.in_mem_1_optim_conv import InMem1OptimizationConversation
from conversations.in_mem_2_mt_conv import InMem2MTConversation
from conversations.optimization_conversation import OptimConvArgs
from conversations.prompts_gen import gen_incorrect_output_prompt
from conversations.scripted_conversation import ScriptedConversation
from conversations.ssd_1_st_opt_conv import SSD1STOptimConv
from conversations.ssd_2_mt_conv import SSD2MTOptConv
from conversations.supervision_agent import SupervisionAgent
from cpp_runner.prepare_repo.load_snapshot_and_prepare import (
    prepare_repo_and_load_snapshot,
)
from cpp_runner.prepare_repo.prepare_workspace import PrepareWorkspace
from cpp_runner.prepare_repo.prepare_workspace_olap import OLAPPrepareWorkspace
from cpp_runner.prepare_repo.retrieve_framework_version_hash import (
    get_framework_version_artifacts_str,
)
from llm.sdk.agents_sdk.openai_sdk import OpenAIAgentsSDKWrapper
from observability.live_ui.live_dashboard import LiveDashboardDrain
from observability.logging import notify
from observability.logging.logger import setup_logging
from observability.logging.run_stats_collector import RunStatsCollector
from observability.logging.run_stats_drain import DataDrain, DuckDBDrain, WandbDrain
from observability.logging.weave_cache import configure_weave_cache_dirs
from synth_framework.git_snapshotter import GitSnapshotter
from synth_framework.handle_prompt import handle_prompt
from synth_framework.runtime_tracker import RuntimeTracker
from tools.compile import CompileTool
from tools.run import RunTool
from tools.shell_executor import ShellExecutor
from tools.validate.query_validator_class import QueryValidator
from tools.workspace_editor import WorkspaceEditor
from utils.cli_config import RunConfig, add_common_args
from utils.confirm_dialog import await_user_confirmation
from utils.conv_name_utils import ConvMode, generate_conv_name
from utils.core_utils import get_cores_for_current_machine
from utils.get_sample_q_args import get_sample_query_args
from utils.hugepages import get_num_numa_nodes, set_hugepages
from utils.pkgconfig import check_pkg
from utils.sf_list_gen import gen_sf
from utils.snapshot_utils import load_storage_plan_from_snapshot
from utils.utils import (
    DBStorage,
    ask_yes_no,
    create_dir_and_set_permissions,
    get_disk_db_dir,
)
from workloads.dataset.dataset_tables_dict import get_benchmark_schema, get_dataset_name
from workloads.dataset.query_gen_factory import get_query_gen
from workloads.workload_provider_olap import OLAPWorkload, OLAPWorkloadProvider

logger = logging.getLogger(__name__)


async def main(args: argparse.Namespace) -> None:
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

    workspace_path = Path("./output")
    workspace_path.mkdir(exist_ok=True)

    cache_path = Path(args.artifacts_dir) / "cache"
    create_dir_and_set_permissions(cache_path)
    cache_repo = (
        None
        if args.disable_repo_sync
        else os.environ.get("GIT_SNAPSHOTTER_SERVER", None)
    )

    conversations_dir = Path(args.artifacts_dir) / "conversations"

    notify.SEND_NOTIFICATIONS = args.notify

    workload_provider = OLAPWorkloadProvider(benchmark=OLAPWorkload(args.benchmark))

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
        cache_repo=cache_repo,
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

    db_storage = args.db_storage
    assert db_storage in [
        DBStorage.IN_MEMORY,
        DBStorage.SSD,
    ]  # labstore is not yet fully supported

    disk_db_dir, bespoke_db_dir = get_disk_db_dir(db_storage, workspace_path)

    logger.info(f"Using database source: {db_storage}. Disk DB dir: {disk_db_dir}")

    if disk_db_dir is not None:
        create_dir_and_set_permissions(disk_db_dir)

    ##############################
    # Prepare workspace / snapshot
    ##############################

    # prepare query gen
    gen_query_fn = get_query_gen(args.benchmark)

    query_list = [q.strip() for q in args.query_list.split(",")]

    if args.storage_plan_snapshot is not None:
        # load storage plan snapshot and read storage plan form it.
        # afterwards a clean or other snapshot will be loaded
        storage_plan = load_storage_plan_from_snapshot(
            args, snapshotter, workspace_path
        )

        assert args.start_snapshot is None, (
            "loading a storage plan snapshot, but also providing a start snapshot is not supported. Are you really sure? Usually the storage plan will be kept in the snapshots as soons as coding starts, and you don't have to pass them again."
        )
    else:
        storage_plan = None

    filenames_dict = get_filenames()

    framework_code_content = get_framework_version_artifacts_str()

    # setup snapshot / workspace according to mode
    if args.start_snapshot is None and args.continue_run:
        # continue from current ./output state - nothing to set up
        pass
    else:
        if args.start_snapshot is not None:
            assert not args.continue_run

        if args.conv_mode == ConvMode.STORAGE_PLAN:
            prepare_mode = "storage_plan"
        elif args.conv_mode == ConvMode.OPTIM:
            prepare_mode = "optim"
        elif args.conv_mode == ConvMode.MAKE_MT:
            prepare_mode = "mt"
        elif args.conv_mode == ConvMode.CHECK_SF:
            prepare_mode = args.prepare_mode
        else:
            prepare_mode = "base"

        usecase_prepare_args = dict()
        if storage_plan is not None:
            usecase_prepare_args["storage_plan"] = storage_plan

        framework_code_content += prepare_repo_and_load_snapshot(
            snapshotter=snapshotter,
            snapshot=args.start_snapshot,
            prepare=prepare_mode,
            usecase_prepare_args=usecase_prepare_args,
            do_not_cache=args.do_not_cache,
            conv_name=args.conv_name,
            only_from_cache=args.only_from_cache,
            prepare_workspace_provider=OLAPPrepareWorkspace(
                db_storage=db_storage,
                workload_provider=workload_provider,
                workspace_dir=workspace_path,
                git_snapshotter=snapshotter,
                prepare_cache_dir=cache_path / "prepare_cache"
                if cache_path is not None
                else None,
            ),
        )

    ###############
    # Misc setup
    ###############

    parquet_path = args.artifacts_dir + f"/{get_dataset_name(args.benchmark)}_parquet/"

    # measure total time including cache hits - start the timer
    runtime_tracker = RuntimeTracker()

    # Create hooks instance for tracking metrics
    log_path = Path(args.artifacts_dir) / "logs"
    create_dir_and_set_permissions(log_path)
    import socket

    data_drains: list[DataDrain] = [
        LiveDashboardDrain(
            run_name=args.run_name,
            wandb_run_id=getattr(args, "wandb_run_id", None),
            system_name=socket.gethostname(),
        ),
        DuckDBDrain(db_path=log_path / f"{args.run_name}.duckdb"),
    ]
    if not args.disable_wandb:
        data_drains.append(WandbDrain())

    collector_args = dict(
        model=args.model,
        git_snapshotter=snapshotter,
        cloc_cache_dir=cache_path / "cloc_cache",
        runtime_tracker=runtime_tracker,
        do_not_cache=args.do_not_cache,
        drains=data_drains,
    )
    run_stats_collector = RunStatsCollector(**collector_args)

    # assemble default sf values for the selected benchmark
    verify_sf_list, max_scale_factor = gen_sf(
        args.benchmark,
        benchmark_sf=args.max_scale_factor,
        multi_threaded_mode=args.conv_mode == ConvMode.MAKE_MT
        or (args.conv_mode == ConvMode.CHECK_SF and prepare_mode == "mt"),
    )

    if args.conv_mode == ConvMode.MAKE_MT:
        # get pre_sf
        _, pre_sf = gen_sf(
            args.benchmark,
            benchmark_sf=args.max_scale_factor,
            multi_threaded_mode=False,  # pre-sf is always generated with single-threaded settings
        )

    compile_cache_dir = cache_path / "compile"

    # run tool parallelism setup (must be determined before QueryValidator so the
    # num_threads value is included in the query-cache key)
    if args.conv_mode == ConvMode.MAKE_MT or (
        args.conv_mode == ConvMode.CHECK_SF and prepare_mode == "mt"
    ):  # make multi-threaded
        # configuration for the run tool.
        parallelism = True
        num_threads, core_ids = get_cores_for_current_machine(
            leave_core_0_out=True,
            allow_hyperthreading=True,
            ncores_to_use=args.max_num_threads,  # measure perf improvement with 20 cores
        )
    else:
        parallelism = False
        num_threads, core_ids = 1, None

    # cap result CSVs at this size before snapshotting (used by validator and
    # exposed in the LLM cache key via config_kwargs below for cache stability)
    max_snapshot_csv_size_mb = 5.0

    validator_sf_list = verify_sf_list + [max_scale_factor]
    if args.conv_mode == ConvMode.CHECK_SF and args.target_sf is not None:
        # the check-sf conversation validates correctness at target_sf, so the
        # validator must precompute reference results for it as well
        if args.target_sf not in validator_sf_list:
            validator_sf_list = validator_sf_list + [args.target_sf]
    if args.conv_mode == ConvMode.MAKE_MT and pre_sf not in validator_sf_list:
        # the MT-introduction stage measures the pre-MT reference runtime at pre_sf,
        # so the validator must precompute reference results for it as well
        validator_sf_list = validator_sf_list + [pre_sf]

    query_validator: QueryValidator | None = None
    if not args.disable_valtool:
        query_validator = QueryValidator(
            benchmark=args.benchmark,
            gen_query_fn=gen_query_fn,
            sf_list=validator_sf_list,
            parquet_path=parquet_path,
            wandb_pin_worker=True,
            all_query_ids=query_list,
            num_random_query_instantiations=10,
            query_cache_dir=cache_path / "query_cache",
            validate_cache_dir=cache_path / "validate",
            workspace_path=workspace_path,
            git_snapshotter=snapshotter,
            runtime_tracker=runtime_tracker,
            do_not_cache=args.do_not_cache,
            run_umbra_as_well=True,
            only_from_cache=args.only_from_cache,
            num_threads=num_threads,
            core_ids=core_ids,
            max_snapshot_csv_size_mb=max_snapshot_csv_size_mb,
            db_storage=db_storage,
            disk_db_dir=disk_db_dir,
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
        cache_dir=cache_path / "apply_patch",
        do_not_cache=args.do_not_cache,
        runtime_tracker=runtime_tracker,
        only_from_cache=args.only_from_cache,
        untracked_cpp_runner_content=framework_code_content,
    )
    shell = ShellExecutor(
        workspace_path,
        snapshotter=snapshotter,
        cache_dir=cache_path / "shell",
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
        untracked_cpp_runner_content=framework_code_content,
    )

    dataset_name = get_dataset_name(args.benchmark)
    run_tool = RunTool(
        cwd=workspace_path,
        query_validator=query_validator,  # do not cache and co passed via query validator
        run_stats_collector=run_stats_collector,
        dataset_name=dataset_name,
        base_parquet_dir=os.path.join(args.base_parquet_dir, f"{dataset_name}_parquet"),
        bespoke_storage_dir=bespoke_db_dir,
        memory_budget_mb=args.memory_budget_mb,
        include_mem_budget_for_in_mem_in_hashes=args.include_mem_budget_for_in_mem_in_hashes,
        validate_output_truncation=validate_output_truncate,
        compile_output_truncation=compile_output_truncate,
        only_from_cache=args.only_from_cache,
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
    config_kwargs: Dict[str, Any] = {
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
            cache_path=cache_path,
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
            be_relaxed_if_runtime_goal_not_reached=args.conv_mode
            in [ConvMode.OPTIM, ConvMode.MAKE_MT],
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
                max_scale_factor=max_scale_factor,
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
            benchmark_sf=max_scale_factor,
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
        if args.conv_mode == ConvMode.STORAGE_PLAN:
            conv = GenStoragePlanConversation(
                benchmark=args.benchmark,
                schema=get_benchmark_schema(args.benchmark),
                workspace_path=workspace_path,
                db_storage=db_storage,
                **auto_conversation_args,
                **conv_args,
            )
        elif args.conv_mode == ConvMode.SCRIPTED:
            # all prompts are pre-defined and are listed in the json file (hence "scripted") - user can still give input.
            conv = ScriptedConversation(**conv_args)
        elif args.conv_mode == ConvMode.BASE:
            # get sample query args for later use in the conversation (e.g. for better prompt formatting)
            sample_query_args_dict: Dict[str, str] = get_sample_query_args(args)

            # load the base impl conversation
            conv = BaseImplConversation(
                verify_sf_list=verify_sf_list,
                max_scale_factor=max_scale_factor,
                artifacts_dir=Path(args.artifacts_dir),
                benchmark=args.benchmark,
                read_storage_plan=args.bespoke_storage,
                sample_query_args_dict=sample_query_args_dict,
                workspace_path=workspace_path,
                use_master_prompt=args.use_autonomy_master_prompt,
                sql_dict=workload_provider.sql_dict,
                db_storage=db_storage,
                **auto_conversation_args,
                **conv_args,
            )
        elif args.conv_mode == ConvMode.CHECK_SF:
            assert args.target_sf is not None, (
                "target_sf must be provided for check-sf correctness conversation"
            )
            conv = CheckSFCorrectnessConv(
                query_ids=query_list,
                target_sf=args.target_sf,
                verify_sf_list=verify_sf_list,
                bespoke_storage=args.bespoke_storage,
                db_storage=db_storage,
                **auto_conversation_args,
                **conv_args,
            )
        elif args.conv_mode in [ConvMode.OPTIM, ConvMode.MAKE_MT]:
            assert query_validator is not None, (
                "query_validator must be provided for optim conversation"
            )

            optim_conv_args = OptimConvArgs(
                query_ids=query_list,
                bespoke_storage=args.bespoke_storage,
                verify_sf_list=verify_sf_list,
                query_validator=query_validator,
                plan_source=args.optimize_sample_plan_source,
                cleanup_plans=True,
                model=args.model,
                db_storage=db_storage,
            )

            if args.conv_mode == ConvMode.OPTIM:
                # optim single threaded
                if db_storage == DBStorage.IN_MEMORY:
                    conv = InMem1OptimizationConversation(
                        optim_conv_args=optim_conv_args,
                        **auto_conversation_args,
                        **conv_args,
                    )
                elif db_storage == DBStorage.SSD:
                    conv = SSD1STOptimConv(
                        optim_conv_args=optim_conv_args,
                        **auto_conversation_args,
                        **conv_args,
                    )
                else:
                    raise Exception(
                        f"Unsupported db source for optimization conversation: {db_storage}"
                    )
            elif args.conv_mode == ConvMode.MAKE_MT:
                # second optimization round: adds multi-threading on top of the single-threaded result
                assert query_validator is not None, (
                    "query_validator must be provided for make_mt conversation"
                )

                if db_storage == DBStorage.IN_MEMORY:
                    conv = InMem2MTConversation(
                        benchmark=args.benchmark,
                        optim_conv_args=optim_conv_args,
                        **auto_conversation_args,
                        **conv_args,
                    )
                elif db_storage == DBStorage.SSD:
                    conv = SSD2MTOptConv(
                        optim_conv_args=optim_conv_args,
                        benchmark_sf_pre=pre_sf,
                        **auto_conversation_args,
                        **conv_args,
                    )
                else:
                    raise Exception(
                        f"Unsupported db source for make_mt conversation: {db_storage}"
                    )
            else:
                raise ValueError(f"Unknown conversation mode: {args.conv_mode}")

        else:
            raise ValueError(f"Unknown conversation mode: {args.conv_mode}")

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

    logger.debug(
        f"Model cache total saved: ${agent_sdk_wrapper.get_total_saved_by_llm_cache():0.6f}"
    )
    logger.debug(
        f"Total runtime: {runtime_tracker.retrieve_total_time():.2f} seconds (including cache hits, excluding time spent waiting for user input)"
    )
    logger.debug(
        f"Cost: ${run_stats_collector.total_stats['cost_usd']:.6f} (real cost: ${run_stats_collector.total_stats['real_cost_usd']:.6f}, including cache hits)"
    )

    if not args.disable_wandb:
        # Log final summary to wandb
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


def _setup() -> None:
    if not check_pkg("arrow", "parquet"):
        raise Exception("arrow and parquet are not available. See README.")

    for node in range(get_num_numa_nodes()):
        set_hugepages(node=node, page_kb=2048, count=0)


def run_conv_wrapper(
    args: argparse.Namespace | None, run_config: RunConfig | None
) -> None:
    # assemble args from run_config if main.py is started from run scripts
    if args is None and run_config is None:
        raise Exception("Either args or run_config must be provided.")
    elif args is None:
        # convert run_config to args for conv wrapper
        args = argparse.Namespace(**vars(run_config))
    else:
        pass

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

    # load environment variables
    load_dotenv()
    if args.sdk == "openai":
        if args.disable_openai_tracing:
            # disable agents sdk tracing
            from agents.tracing import set_tracing_disabled

            set_tracing_disabled(True)
    else:
        raise Exception(f"Unsupported SDK: {args.sdk}")

    if not args.disable_wandb:
        # add weave (wandb) tracing in addition to openai tracing
        configure_weave_cache_dirs()
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
    log_path = Path(args.artifacts_dir) / "logs"
    create_dir_and_set_permissions(log_path)
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
    setup_logging(logging.DEBUG, log_path / log_filename)

    if args.notify:
        logger.info(
            "This run will send notifications about errors to the configured Zulip channel."
        )

    try:
        asyncio.run(main(args))

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
        include_disable_wandb=True,
        include_query_list=True,
        include_continue_run=True,
        include_artifacts_dir=True,
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
        include_base_parquet_dir=True,
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
        run_conv_wrapper(args, run_config=None)
    else:
        raise Exception(f"Unknown {args.command}")
