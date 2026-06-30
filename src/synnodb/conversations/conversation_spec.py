from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from synnodb.api import Stage
    from synnodb.observability.logging.run_stats_collector import RunStatsCollector
    from synnodb.synth_framework.git_snapshotter import GitSnapshotter
    from synnodb.tools.run import RunTool
    from synnodb.tools.compile import CompileTool
    from synnodb.tools.validate.query_validator_class import QueryValidator
    from synnodb.conversations.supervision_agent import SupervisionAgent
    from synnodb.llm.sdk.agents_sdk.openai_sdk import OpenAIAgentsSDKWrapper
    from synnodb.utils.utils import DBStorage


@dataclass
class FrameworkContext:
    """Everything main() has assembled, passed to a stage's conversation factory."""

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
    spec: "Stage"
