import logging
from pathlib import Path
from typing import Optional

from synnodb.cpp_runner.compiler.compiler_factory_olap import OLAPCompilerFactory
from synnodb.observability.logging.run_stats_collector import RunStatsCollector
from synnodb.synth_framework.git_snapshotter import GitSnapshotter
from synnodb.synth_framework.runtime_tracker import RuntimeTracker
from synnodb.utils.cli_config import Usecase
from synnodb.utils.utils import DBStorage

logger = logging.getLogger(__name__)


class CompileTool:
    """Compiles the database"""

    def __init__(
        self,
        cwd: Path,
        run_stats_collector: RunStatsCollector,
        db_storage: DBStorage,
        untracked_cpp_runner_content: str,
        usecase: Usecase = Usecase.OLAP,
        compile_cache_dir: Optional[Path] = None,
        do_not_cache: bool = False,
        only_from_cache: bool = False,
        git_snapshotter: Optional[GitSnapshotter] = None,
        runtime_tracker: Optional[RuntimeTracker] = None,
        output_truncation: Optional[
            int
        ] = None,  # restrict to 10000 chars ~ 2.5 Thousand tokens
    ) -> None:
        self.cwd = cwd
        if usecase == Usecase.OLAP:
            factory = OLAPCompilerFactory(db_storage=db_storage)
        self.compiler = factory.make_compiler(
            cwd,
            compile_cache_dir=compile_cache_dir,
            git_snapshotter=git_snapshotter,
            runtime_tracker=runtime_tracker,
            do_not_cache=do_not_cache,
            only_from_cache=only_from_cache,
            untracked_cpp_runner_content=untracked_cpp_runner_content,
        )
        self.git_snapshotter = git_snapshotter
        self.run_stats_collector = run_stats_collector
        self.output_truncation = output_truncation

    def __call__(self, optimize: bool) -> str:
        logger.info("compile call")

        self.compiler.set_compile_options(optimize=True)

        err = self.compiler.build()
        served_from_cache = getattr(
            self.compiler, "last_build_served_from_cache", False
        )
        if err is None:
            output = "**Compilation successfull**"
        else:
            output = err

        # Truncate the output if it exceeds the truncation limit
        truncated = False
        if self.output_truncation is not None:
            if len(output) > self.output_truncation:
                output = output[: self.output_truncation]
                output += "\n...[truncated]..."
                truncated = True

        # report stats
        self.run_stats_collector.log_metrics_callback(
            {
                "type": "compile",
                "compile/error": True if err is not None else False,
                "compile/truncated": truncated,
                "compile/cached": served_from_cache,
            },
            log_and_increment=True,
        )
        # store summary of activity (for supervision agent)
        self.run_stats_collector.add_to_activity_summary(
            f"Compile Tool called: {'success' if err is None else 'error'}"
        )

        return output
