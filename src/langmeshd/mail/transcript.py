"""Recover a turn's visible assistant text from a history payload after the live stream is gone."""

from __future__ import annotations

from typing import Any


_TERMINAL_FAILED = frozenset({"failed", "canceled", "cancelled", "rejected"})
_TERMINAL_OK = frozenset({"completed", "complete", "submitted"})


def _role(message: dict[str, Any]) -> str:
    role = message.get("role")
    if isinstance(role, dict):
        role = role.get("value")
    return str(role or "")


def _message_id(message: dict[str, Any]) -> str:
    return str(message.get("messageId") or message.get("message_id") or "")


def _text_parts(message: dict[str, Any]) -> str:
    chunks: list[str] = []
    for part in message.get("parts") or []:
        if not isinstance(part, dict):
            continue
        if part.get("kind") == "text" and part.get("text"):
            chunks.append(str(part["text"]))
            continue
        data = part.get("data") if part.get("kind") == "data" else None
        if isinstance(data, dict):
            nested = data.get("kind") or data.get("type")
            if nested in {"error", "ErrorEvent"}:
                code = str(data.get("code") or "")
                if code:
                    chunks.append(code)
    return "".join(chunks)


def turn_state(turn: dict[str, Any]) -> str:
    status = turn.get("status")
    if isinstance(status, dict):
        state = status.get("state")
        if isinstance(state, dict):
            state = state.get("value")
        return str(state or "")
    return ""


def turn_failed(turn: dict[str, Any]) -> bool:
    return turn_state(turn) in _TERMINAL_FAILED


def turn_complete(turn: dict[str, Any]) -> bool:
    return turn_state(turn) in _TERMINAL_OK


def assistant_text(turn: dict[str, Any]) -> str:
    """Visible agent prose from one turn, skipping empty and purely-structural messages."""
    chunks: list[str] = []
    for message in turn.get("history") or []:
        if not isinstance(message, dict):
            continue
        if _role(message) not in {"agent", "assistant"}:
            continue
        text = _text_parts(message).strip()
        if text and text not in {"turn_interrupted", "tool_interrupted"}:
            chunks.append(text)
    return "\n\n".join(chunks).strip()


def turn_for_client_message(turns: list[dict[str, Any]], client_message_id: str) -> dict[str, Any] | None:
    if not client_message_id:
        return None
    for turn in turns:
        for message in turn.get("history") or []:
            if not isinstance(message, dict):
                continue
            if _role(message) == "user" and _message_id(message) == client_message_id:
                return turn
    return None


def latest_assistant_text(turns: list[dict[str, Any]]) -> str:
    for turn in reversed(turns):
        text = assistant_text(turn)
        if text:
            return text
    return ""
