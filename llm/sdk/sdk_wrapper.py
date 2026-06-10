from abc import abstractmethod
from pathlib import Path
from typing import Any, Callable

from observability.logging.run_stats_collector import RunStatsCollector
from synth_framework.git_snapshotter import GitSnapshotter
from synth_framework.runtime_tracker import RuntimeTracker
from tools.compile import CompileTool
from tools.run import RunTool
from tools.shell_executor import ShellExecutor
from tools.workspace_editor import WorkspaceEditor


class SDKWrapper:
    def __init__(
        self,
        sdk: str,
        editor: WorkspaceEditor,
        shell: ShellExecutor,
        compile_tool: CompileTool,
        run_tool: RunTool,
        args,
        cache_path: Path,
        config_kwargs: dict[str, Any],
        workspace_path: str,
        workspace_path_absolute: Path,
        default_agent_name: str,
        conv_name: str,
        supervisor_agent_instruction: str,
        snapshotter: GitSnapshotter | None = None,
        run_stats_collector: RunStatsCollector | None = None,
        runtime_tracker: RuntimeTracker | None = None,
    ):
        self._sdk = sdk
        self.editor = editor
        self.shell = shell
        self.compile_tool = compile_tool
        self.run_tool = run_tool
        self.args = args
        self.cache_path = cache_path
        self.config_kwargs = config_kwargs
        self.workspace_path = workspace_path
        self.workspace_path_absolute = workspace_path_absolute
        self.default_agent_name = default_agent_name
        self.conv_name = conv_name
        self.supervisor_agent_instruction = supervisor_agent_instruction
        self.snapshotter = snapshotter
        self.run_stats_collector = run_stats_collector
        self.runtime_tracker = runtime_tracker

        assert not Path(self.workspace_path).is_absolute(), (
            "workspace_path must be a relative path - otherwise caches across different machines/users would not be portable at all"
        )
        assert self.workspace_path_absolute.is_absolute(), (
            "workspace_path_absolute must be an absolute path - it is used for security checks to ensure that the agent does not access files outside of the working directory, so it needs to be an absolute path to do proper checks"
        )

        # has to exist
        assert workspace_path_absolute.exists(), (
            f"workspace_path_absolute {workspace_path_absolute} does not exist - it needs to exist for security checks to work properly"
        )

    def __getattr__(self, item):
        return getattr(self._sdk, item)

    @abstractmethod
    async def run_traced(
        self, title: str, data: dict, callback: Callable, add_tools: bool = True
    ):
        pass

    @abstractmethod
    def get_total_saved_by_llm_cache(self) -> float:
        pass

    @abstractmethod
    async def clear_supervisor_session(self):
        pass

    @abstractmethod
    async def run_supervisor_agent(self, prompt: str, max_turns: int) -> str:
        pass

    @abstractmethod
    async def run_agent(
        self,
        prompt: str,
        max_turns: int,
        run_stats_collector: RunStatsCollector,
        short_desc: str | None = None,
    ) -> str:
        pass

    @abstractmethod
    async def run_compaction(self):
        pass

    @abstractmethod
    async def get_conversation_turns(self) -> int:
        pass

    @abstractmethod
    async def switch_to_conversation_branch(self, branch_name: str):
        pass

    @abstractmethod
    async def create_conversation_branch_from_turn(
        self, branch_name: str, turn_nr: int
    ) -> str:
        pass

    @abstractmethod
    def last_llm_call_was_cached(self) -> bool:
        pass
