from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from conversations.conversation import AbstractConversation
    from cpp_runner.prepare_repo.load_snapshot_and_prepare import PrepareContext
    from observability.logging.run_stats_collector import RunStatsCollector
    from synth_framework.git_snapshotter import GitSnapshotter
    from synth_framework.runtime_tracker import RuntimeTracker
    from tools.run import RunTool
    from tools.compile import CompileTool
    from tools.validate.query_validator_class import QueryValidator
    from conversations.supervision_agent import SupervisionAgent
    from llm.sdk.agents_sdk.openai_sdk import OpenAIAgentsSDKWrapper
    from utils.utils import DBStorage


@dataclass
class FrameworkContext:
    """Everything main() has assembled, passed to the conversation factory."""

    args: argparse.Namespace
    workload_provider: object
    workspace_path: Path
    db_storage: "DBStorage"
    query_list: list[str]
    run_tool: "RunTool"
    compile_tool: "CompileTool"
    agent_sdk_wrapper: "OpenAIAgentsSDKWrapper"
    snapshotter: "GitSnapshotter"
    run_stats_collector: "RunStatsCollector"
    supervision_agent: "SupervisionAgent | None"
    query_validator: "QueryValidator | None"
    conv_args: dict
    auto_conversation_args: dict
    spec: "ConversationSpec"


@dataclass
class ConversationSpec:
    # Prepare function that brings the workspace into the state this conversation
    # expects. Receives a PrepareContext (assembled by
    # prepare_repo_and_load_snapshot once the start snapshot, if any, has been
    # restored) and returns the prepared-artifacts string. The concrete prepare
    # steps (e.g. prepare_base / prepare_mt) live next to that function, not here.
    prepare: Callable[["PrepareContext"], str]

    # Whether the RunTool should be built with parallelism=True.
    needs_parallelism: bool

    # Whether SupervisionAgent should be constructed with be_relaxed_if_runtime_goal_not_reached=True.
    be_relaxed_supervision: bool

    # Receives a fully-assembled FrameworkContext and returns an AbstractConversation.
    # Concrete conversation class imports live inside this callable, not in main.py.
    factory: Callable[["FrameworkContext"], "AbstractConversation"]
