import logging
import os


from synnodb.conversations.prompts_gen import (
    parse_supervision_output,
    supervision_agent_prompt,
)
from synnodb.conversations.stage_items import (
    BENCHMARK_MARKER,
    COMPACTION_MARKER,
    VALIDATE_OFF,
    VALIDATE_ON,
    VALIDATE_OUTPUT_STDOUT_OFF,
    VALIDATE_OUTPUT_STDOUT_ON,
    MarkerItem,
    StageItem,
)
from synnodb.llm.sdk.sdk_wrapper import SDKWrapper
from synnodb.observability.logging.run_stats_collector import RunStatsCollector

logger = logging.getLogger(__name__)

SUPERVISION_STAGE_VISIBILITY_MARKER = "<<SUPERVISION_STAGE_VISIBILITY_MARKER>>"  # break here the past/future visibility for the supervision agent. Is placed in the stages list of the conversation


class SupervisionAgent:
    def __init__(
        self,
        run_stats_collector: RunStatsCollector,
        agent_sdk_wrapper: SDKWrapper,
        be_relaxed_if_runtime_goal_not_reached: bool = False,
        generate_dev_hints: bool | None = None,
    ):

        # # session
        # self.agent = Agent(
        #     name=SUPERVISOR_AGENT_NAME,
        #     model=model,
        #     instructions="You are a supervisor agent that oversees the execution of a task by another agent. Your role is to monitor the progress, provide feedback, and ensure that the task is completed successfully. You will receive updates on the task execution and can intervene if necessary to guide the process towards a successful outcome.",
        # )

        # # assemble session
        # session_id = f"{conv_name}-supervision"
        # self.session = AdvancedSQLiteSession(
        #     session_id=session_id,
        #     create_tables=True,
        # )
        self.run_stats_collector = run_stats_collector
        self.last_stage_nr = -1
        self.be_relaxed_if_runtime_goal_not_reached = (
            be_relaxed_if_runtime_goal_not_reached
        )
        self.agent_sdk_wrapper = agent_sdk_wrapper
        self.generate_dev_hints = (
            generate_dev_hints
            if generate_dev_hints is not None
            else os.environ.get("GENERATE_DEV_HINTS") == "1"
        )

    def register_workload_info(self, stages: list[StageItem | str]):
        # Lower typed marker items to their legacy strings: the scoping and
        # skip-set logic below operates on the marker strings.
        stages = [s.marker if isinstance(s, MarkerItem) else s for s in stages]
        self.stages = stages
        self.stage_descriptions = []
        for i, stage in enumerate(stages):
            if isinstance(stage, StageItem):
                # every non-marker item (prompt stages and composites alike)
                # describes itself via its descriptor
                stage_str = stage.descriptor
            else:
                stage_str = stage
            self.stage_descriptions.append(stage_str)

    def reset_activity_monitoring(self):
        # reset the activity summary at the beginning of each turn
        self.run_stats_collector.activity_summary = []

    def _assemble_stage_overview(self, current_stage_nr: int) -> str:
        assert current_stage_nr < len(self.stage_descriptions), (
            f"Stage nr is larger than available descriptions: {current_stage_nr} vs. {len(self.stage_descriptions)}"
        )

        # assemble the string
        str_list = self.stage_descriptions[:]
        str_list[current_stage_nr] = f"{str_list[current_stage_nr]} <-- current stage"

        # filter the stages
        _, str_list = scope_stages_for_supervisor(
            self.stages, str_list, current_stage_nr
        )

        # prefix str_list with pos
        str_list = [f"{i + 1}: {desc}" for i, desc in enumerate(str_list)]

        # assemble output
        return "\n".join(str_list)

    async def get_supervision(
        self,
        prompt: str,
        llm_output: str,
        current_stage_nr: int,
    ) -> str | None:

        # check if we should clear our history since new stage started
        assert current_stage_nr >= self.last_stage_nr, (
            f"Current: {current_stage_nr}, last: {self.last_stage_nr}"
        )
        if current_stage_nr != self.last_stage_nr:
            # update last stage nr
            self.last_stage_nr = current_stage_nr

            # clean up session
            logger.debug(
                "Registered start of a new stage. Clearup past interactions with supervision agent."
            )
            await self.agent_sdk_wrapper.clear_supervisor_session()

        # assemble stage overview
        if len(self.stage_descriptions) == 0:
            logger.debug(
                "No stage overview known to supervision-agent. Giving feedback now without being aware of past/future steps."
            )
            stage_overview_str = ""
        else:
            stage_overview_str = self._assemble_stage_overview(current_stage_nr)

        # ask the supervision agent to review the LLM output and provide feedback or guidance
        supervision_prompt = supervision_agent_prompt(
            user_prompt=prompt,
            activity_summary=self.run_stats_collector.activity_summary,
            llm_output=llm_output,
            stage_overview=stage_overview_str,
            be_relaxed_if_runtime_goal_not_reached=self.be_relaxed_if_runtime_goal_not_reached,
            generate_dev_hints=self.generate_dev_hints,
        )

        # run the supervision agent
        output = await self.agent_sdk_wrapper.run_supervisor_agent(
            supervision_prompt, max_turns=10
        )

        # Approval is signalled by the last non-empty line being exactly the
        # success keyword (as the prompt instructs). Checking only the last line
        # prevents rejection responses that mention "success" in passing (e.g.
        # "Run Tool called: success") from being misread as approvals. The
        # <run_summary>/<dev_hints> meta blocks are stripped out of feedback_text
        # before it's echoed back to the supervised agent.
        result = parse_supervision_output(output)
        if result.approved:
            return None
        else:
            return result.feedback_text


def scope_stages_for_supervisor(
    stage_list: list[str | StageItem],
    stage_descr_list: list[str],
    stage_pos: int,
) -> tuple[list[str | StageItem], list[str]]:

    # search marker before stage_pos
    start_pos = 0
    for i in reversed(range(0, stage_pos)):
        if stage_list[i] == SUPERVISION_STAGE_VISIBILITY_MARKER:
            start_pos = i
            break

    # search marker after stage_pos
    end_pos = len(stage_list)
    for i in range(stage_pos + 1, len(stage_list)):
        if stage_list[i] == SUPERVISION_STAGE_VISIBILITY_MARKER:
            end_pos = i
            break

    assert start_pos != end_pos
    assert start_pos < end_pos
    assert start_pos >= 0
    assert start_pos < len(stage_list)
    assert end_pos <= len(stage_list)

    sliced_stages = stage_list[start_pos:end_pos]
    sliced_descrs = stage_descr_list[start_pos:end_pos]

    # filter out stages that should not be shown to the agent
    excluded = {
        COMPACTION_MARKER,
        VALIDATE_OFF,
        VALIDATE_ON,
        VALIDATE_OUTPUT_STDOUT_OFF,
        VALIDATE_OUTPUT_STDOUT_ON,
        BENCHMARK_MARKER,
    }

    filter_bitmap = [not isinstance(s, str) or s not in excluded for s in sliced_stages]

    # apply filter bitmap
    filtered_stages = [s for i, s in enumerate(sliced_stages) if filter_bitmap[i]]
    filtered_descrs = [d for i, d in enumerate(sliced_descrs) if filter_bitmap[i]]

    return list(filtered_stages), list(filtered_descrs)
