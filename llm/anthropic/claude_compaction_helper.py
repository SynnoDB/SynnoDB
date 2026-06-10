import json
import logging
import os
from typing import Any, Dict, List

from agents import TResponseInputItem

logger = logging.getLogger(__name__)


class ClaudeCompactionHelper:
    def __init__(
        self,
        claude_compaction_model: str,
        api_base: str | None = None,
    ):
        self.claude_compaction_model = claude_compaction_model
        self._is_anthropic = claude_compaction_model.startswith(
            "anthropic/"
        ) or claude_compaction_model.startswith("claude-")
        self._api_base = api_base

        if self._is_anthropic:
            from anthropic import AsyncAnthropic

            anthropic_api_key = os.getenv("ANTHROPIC_API_KEY")
            if not anthropic_api_key:
                raise ValueError(
                    "ANTHROPIC_API_KEY must be set in environment for Claude compaction"
                )
            self.claude_client = AsyncAnthropic(api_key=anthropic_api_key)
        else:
            self.claude_client = None

    async def compact_with_claude(
        self, session_items: List[TResponseInputItem]
    ) -> List[Dict[str, Any]]:
        # extract claude model name
        model = self.claude_compaction_model
        model = self._normalize_anthropic_model_name(model)

        messages = self._convert_session_items_to_anthropic_messages(session_items)

        compact_messages = messages + [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "You have written a partial transcript for the task above. "
                            "Please write a summary of the transcript. The purpose of this "
                            "summary is to provide continuity so you can continue to make "
                            "progress towards solving the task in a future context, where "
                            "the raw history above may not be accessible and will be replaced "
                            "with this summary. Write down anything that would be helpful, "
                            "including the state, next steps, learnings, tool usage, file edits, "
                            "compile/run results, errors encountered, and unresolved issues. "
                            "Preserve exact technical details when they matter. "
                            "You must wrap your summary in a <summary></summary> block."
                        ),
                    }
                ],
            }
        ]

        logger.info(f"Running compaction with model: {model}")

        if self._is_anthropic:
            response = await self.claude_client.messages.create(
                model=model,
                max_tokens=16000,
                messages=compact_messages,  # type: ignore
            )
            summary_parts = []
            for block in response.content:
                text = getattr(block, "text", None)
                if text:
                    summary_parts.append(text)
            summary_text = "\n".join(summary_parts).strip()
        else:
            import litellm

            # Convert messages to simple OpenAI format for litellm
            oai_messages = [
                {
                    "role": m["role"],
                    "content": m["content"][0]["text"]
                    if isinstance(m["content"], list)
                    else m["content"],
                }
                for m in compact_messages
            ]
            response = await litellm.acompletion(
                model=model,
                messages=oai_messages,
                max_tokens=16000,
                **({"base_url": self._api_base} if self._api_base else {}),
            )
            summary_text = response.choices[0].message.content.strip()

        output_items = [
            {
                "role": "user",
                "content": (
                    "Here is a summary of our prior conversation:\n\n"
                    f"{summary_text}\n\n"
                    "Let's continue."
                ),
            }
        ]
        print(f"Claude compaction summary:\n{summary_text}")
        return output_items

    def _normalize_anthropic_model_name(self, model: str) -> str:
        if model.startswith("anthropic/"):
            model = model[len("anthropic/") :]
        return model

    def _convert_session_items_to_anthropic_messages(
        self,
        session_items: List[Any],
    ) -> List[Dict[str, Any]]:
        messages: List[Dict[str, Any]] = []

        for item in session_items:
            if not isinstance(item, dict):
                item = item.model_dump(exclude_unset=True, warnings=False)
            messages.extend(self._item_to_anthropic_messages(item))

        return self._merge_adjacent_anthropic_messages(messages)

    def _merge_adjacent_anthropic_messages(
        self, messages: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """Merge consecutive messages with the same role to reduce payload size."""
        if not messages:
            return messages

        # Initialize merged list with the first message
        merged: List[Dict[str, Any]] = [messages[0]]

        # Iterate through remaining messages and merge if roles match
        for msg in messages[1:]:
            prev = merged[-1]
            # If current message has same role as previous, extend content
            if msg["role"] == prev["role"]:
                prev["content"].extend(msg["content"])
            # Otherwise, append as a new message
            else:
                merged.append(msg)

        return merged

    def _item_to_anthropic_messages(self, item: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Convert a single OpenAI Responses API session item into one or more Anthropic messages.

        The OpenAI Responses API uses a flat list of items (messages, tool calls, tool results)
        whereas Anthropic expects a list of {role, content} dicts. This method maps each item
        type to its closest Anthropic equivalent, representing tool calls/results as plain text
        since Anthropic's native tool_use blocks are not needed here — we only want Claude to
        read and summarize the history, not to re-execute tools.

        Returns a list because some items may produce zero messages (e.g. empty content).
        """
        out: list[dict[str, Any]] = []

        item_type = item.get("type")
        role = item.get("role")

        # Case 1: normal role-based message (ResponseOutputMessage / user input)
        # role takes priority — items with a role field are standard chat turns.
        if role in {"user", "assistant"}:
            content = item.get("content")
            text_parts: list[str] = []

            if isinstance(content, str):
                if content.strip():
                    text_parts.append(content.strip())

            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict):
                        extracted = self._extract_text_from_openai_content_block(block)
                        if extracted:
                            text_parts.append(extracted)

            if text_parts:
                out.append(
                    {
                        "role": role,
                        "content": [{"type": "text", "text": "\n\n".join(text_parts)}],
                    }
                )
            return out

        # Case 2: tool/function call (ResponseFunctionToolCall)
        # Rendered as an assistant text turn so Claude can read what was called.
        if item_type in {"function_call", "tool_call"}:
            name = item.get("name", "<unknown_tool>")
            arguments = item.get("arguments", "")
            tool_text = (
                f"[Tool call] {name}\nArguments:\n{self._safe_pretty_json(arguments)}"
            )
            out.append(
                {
                    "role": "assistant",
                    "content": [{"type": "text", "text": tool_text}],
                }
            )
            return out

        # Case 3: tool/function output (ResponseFunctionToolCallOutput)
        # Rendered as a user turn — mirrors the pattern used by the OpenAI Responses API
        # where tool results come back "from the environment" (user side).
        # NOTE: large outputs (e.g. file contents) are included verbatim here, which can
        # consume significant context. If this becomes a problem, truncate `output` before
        # passing to _safe_pretty_json.
        if item_type in {"function_call_output", "tool_result"}:
            output = item.get("output", "")
            call_id = item.get("call_id")
            prefix = "[Tool result]"
            if call_id:
                prefix += f" call_id={call_id}"
            result_text = f"{prefix}\n{self._safe_pretty_json(output)}"
            out.append(
                {
                    "role": "user",
                    "content": [{"type": "text", "text": result_text}],
                }
            )
            return out

        # Fallback: unknown item type — preserve as readable user-role text so the
        # summary can still reference it. Strips noisy/large fields that don't add meaning.
        fallback = {
            k: v for k, v in item.items() if k not in {"provider_data", "status", "id"}
        }
        out.append(
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": "[Unrecognized session item]\n"
                        + self._safe_pretty_json(fallback),
                    }
                ],
            }
        )
        return out

    def _extract_text_from_openai_content_block(
        self, block: dict[str, Any]
    ) -> str | None:
        """Extract a text representation from a single OpenAI content block.

        Handles the block types that appear in ResponseOutputMessage.content:
          - text / input_text / output_text: plain text, returned as-is.
          - refusal: model refusal, preserved with a prefix label.
          - summary: server-side compaction summary blocks.
          - everything else: serialized as JSON with a warning prefix.

        WARNING: This method never returns None — the fallback always produces a string.
        That means image_url / image / audio blocks will be serialized as potentially
        large JSON strings. Callers should be aware this can inflate context size
        significantly if binary content is present in the session.
        """
        block_type = block.get("type")

        if block_type in {"text", "input_text", "output_text"}:
            text = block.get("text")
            if isinstance(text, str) and text.strip():
                return text.strip()

        if block_type == "refusal":
            # OpenAI refusal blocks store the refusal text in the "refusal" key, not "text"
            text = block.get("refusal")
            if isinstance(text, str) and text.strip():
                return f"[Refusal]\n{text.strip()}"

        if block_type == "summary":
            # Produced by OpenAI's server-side compaction; treat like plain text
            text = block.get("text")
            if isinstance(text, str) and text.strip():
                return f"[Summary block]\n{text.strip()}"

        # Fallback for unknown structured blocks (e.g. image_url, tool_use, audio).
        # Strips annotation/logprob noise but otherwise dumps the whole block.
        cleaned = {
            k: v for k, v in block.items() if k not in {"annotations", "logprobs"}
        }
        return f"[Unsupported content block]\n{self._safe_pretty_json(cleaned)}"

    def _safe_pretty_json(self, value: Any) -> str:
        """Serialize value to pretty-printed JSON, or return its string representation on failure.

        If value is a JSON string, it is parsed and re-formatted for readability.
        """
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
                return json.dumps(parsed, indent=2, ensure_ascii=False, sort_keys=True)
            except Exception:
                return value
        try:
            return json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True)
        except Exception:
            return str(value)
