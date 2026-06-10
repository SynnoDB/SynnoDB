import logging
from pathlib import Path
from typing import Optional

from observability.logging.run_stats_collector import RunStatsCollector
from synth_framework.git_snapshotter import GitSnapshotter
from synth_framework.runtime_tracker import RuntimeTracker
from utils.utils import DBStorage

from ..cpp_runner.compiler_utils import make_compiler

logger = logging.getLogger(__name__)


class CompileTool:
    """Compiles the database"""

    def __init__(
        self,
        cwd: Path,
        run_stats_collector: RunStatsCollector,
        db_storage: DBStorage,
        untracked_cpp_runner_content: str,
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
        self.compiler = make_compiler(
            cwd,
            db_storage=db_storage,
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

        cxx_flags = []
        if optimize:
            cxx_flags.extend(["-O3", "-flto"])
        self.compiler.set_extra_cxxflags(
            cxx_flags
        )  # if this methodolyg is changed, keep in mind to update the cache hash calculation

        err = self.compiler.build()
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
            },
            log_and_increment=True,
        )
        # store summary of activity (for supervision agent)
        self.run_stats_collector.add_to_activity_summary(
            f"Compile Tool called: {'success' if err is None else 'error'}"
        )

        return output
