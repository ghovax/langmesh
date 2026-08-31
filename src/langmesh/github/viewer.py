"""Read-only transcript data for linked GitHub session pages."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any


def _text_content(value: Any) -> str:
    if isinstance(value, str):
        return value
    if not isinstance(value, Sequence) or isinstance(value, (bytes, bytearray, str)):
        return ""
    parts: list[str] = []
    for block in value:
        if isinstance(block, str):
            parts.append(block)
            continue
        if not isinstance(block, Mapping):
            continue
        text = block.get("text")
        if isinstance(text, str):
            parts.append(text)
        elif isinstance(block.get("type"), str) and isinstance(block.get("reasoning"), str):
            # Reasoning is intentionally omitted from a shared viewer.
            continue
    return "".join(parts)


def _tool_status(value: Any) -> str:
    return "failed" if str(value or "").lower() in {"error", "failed"} else "completed"


def messages_from_checkpoint(
    conversation: Sequence[Mapping[str, Any]],
    *,
    timestamp: str = "",
) -> list[dict[str, Any]]:
    """Convert the checkpoint's private message format to the web viewer's public rows."""
    messages: list[dict[str, Any]] = []
    tool_indexes: dict[str, int] = {}

    for message_index, entry in enumerate(conversation):
        message_type = str(entry.get("type") or "")
        data = entry.get("data")
        if not isinstance(data, Mapping):
            continue

        additional_kwargs = data.get("additional_kwargs")
        if isinstance(additional_kwargs, Mapping) and additional_kwargs.get("reminder"):
            continue

        if message_type in {"human", "HumanMessage", "HumanMessageChunk"}:
            content = _text_content(data.get("content"))
            if content:
                messages.append(
                    {
                        "id": str(data.get("id") or f"viewer-user-{message_index}"),
                        "role": "user",
                        "content": content,
                        "timestamp": timestamp,
                    }
                )
            continue

        if message_type in {"ai", "AIMessage", "AIMessageChunk"}:
            content_blocks: list[dict[str, str]] = []
            raw_content = data.get("content")
            if isinstance(raw_content, Sequence) and not isinstance(
                raw_content, (bytes, bytearray, str)
            ):
                for block_index, block in enumerate(raw_content):
                    if not isinstance(block, Mapping) or block.get("type") != "text":
                        continue
                    content = str(block.get("text") or "")
                    if not content:
                        continue
                    content_blocks.append(
                        {
                            "identifier": str(
                                block.get("id") or f"viewer-text-{message_index}-{block_index}"
                            ),
                            "content": content,
                        }
                    )
            else:
                content = _text_content(raw_content)
                if content:
                    content_blocks.append(
                        {
                            "identifier": f"viewer-text-{message_index}",
                            "content": content,
                        }
                    )
            if content_blocks:
                messages.append(
                    {
                        "id": str(data.get("id") or f"viewer-assistant-{message_index}"),
                        "role": "assistant",
                        "content": "".join(block["content"] for block in content_blocks),
                        "contentBlocks": content_blocks,
                        "timestamp": timestamp,
                    }
                )

            tool_calls = data.get("tool_calls")
            if isinstance(tool_calls, Sequence) and not isinstance(
                tool_calls, (bytes, bytearray, str)
            ):
                for call_index, call in enumerate(tool_calls):
                    if not isinstance(call, Mapping):
                        continue
                    tool_call_id = str(
                        call.get("id") or f"viewer-tool-{message_index}-{call_index}"
                    )
                    arguments = call.get("args")
                    if not isinstance(arguments, Mapping):
                        arguments = {}
                    tool_indexes[tool_call_id] = len(messages)
                    messages.append(
                        {
                            "id": f"viewer-tool-row-{tool_call_id}",
                            "role": "tool_call",
                            "content": str(call.get("name") or "tool"),
                            "timestamp": timestamp,
                            "meta": {
                                "arguments": dict(arguments),
                                "argumentsComplete": True,
                                "toolCallId": tool_call_id,
                                "status": "running",
                            },
                        }
                    )
            continue

        if message_type != "tool":
            continue
        tool_call_id = str(data.get("tool_call_id") or "")
        result = _text_content(data.get("content"))
        status = _tool_status(data.get("status"))
        tool_index = tool_indexes.get(tool_call_id)
        if tool_index is None:
            tool_index = len(messages)
            messages.append(
                {
                    "id": f"viewer-tool-row-{tool_call_id or message_index}",
                    "role": "tool_call",
                    "content": str(data.get("name") or "tool"),
                    "timestamp": timestamp,
                    "meta": {
                        "arguments": {},
                        "argumentsComplete": True,
                        "toolCallId": tool_call_id,
                    },
                }
            )
        row = messages[tool_index]
        metadata = dict(row.get("meta") or {})
        metadata["result"] = result
        metadata["status"] = status
        row["meta"] = metadata

    return messages


__all__ = ["messages_from_checkpoint"]
