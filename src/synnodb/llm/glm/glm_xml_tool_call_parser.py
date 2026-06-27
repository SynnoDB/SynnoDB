import json
import re
from dataclasses import dataclass


@dataclass
class XmlToolCall:
    name: str
    arguments: dict


def parse_xml_tool_calls(text: str) -> list[XmlToolCall]:
    """Extract GLM-style XML tool calls from a reasoning text block.

    GLM-5.1 sometimes writes tool calls in its native format inside the
    reasoning/thinking block instead of emitting proper JSON function calls:
        <tool_call>toolname<arg_key>k</arg_key><arg_value>v</arg_value></tool_call>
    """
    results = []
    for block in re.finditer(r"<tool_call>(.*?)</tool_call>", text, re.DOTALL):
        inner = block.group(1)
        name_match = re.match(r"^([^<]+)", inner)
        if not name_match:
            continue
        name = name_match.group(1).strip()
        keys = re.findall(r"<arg_key>(.*?)</arg_key>", inner, re.DOTALL)
        vals = re.findall(r"<arg_value>(.*?)</arg_value>", inner, re.DOTALL)
        if len(keys) != len(vals):
            continue
        arguments = {k.strip(): v for k, v in zip(keys, vals)}
        results.append(XmlToolCall(name=name, arguments=arguments))
    return results


_MAX_ARG_BYTES = 8000


def format_for_reprompt(calls: list[XmlToolCall]) -> str:
    lines = []
    for call in calls:
        lines.append(f"Tool: `{call.name}`")
        args_json = json.dumps(call.arguments)
        if len(args_json) > _MAX_ARG_BYTES:
            args_json = args_json[:_MAX_ARG_BYTES] + "... [truncated]"
        lines.append(f"Arguments (JSON): `{args_json}`")
    return "\n".join(lines)
