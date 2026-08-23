"""Recover whether a history turn is the one this mail started, and whether it failed.

The mail client does not harvest assistant prose. SMTP is `submit_email` in the worker.
"""

from __future__ import annotations

from typing import Any


_TERMINAL_FAILED = frozenset({"failed", "canceled", "cancelled", "rejected"})


def _role(message: dict[str, Any]) -> str:
    role = message.get("role")
    if isinstance(role, dict):
        role = role.get("value")
    return str(role or "")


def _message_id(message: dict[str, Any]) -> str:
    return str(message.get("messageId") or message.get("message_id") or "")


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


def turn_for_client_message(turns: list[dict[str, Any]], client_message_id: str) -> dict[str, Any] | None:
    if not client_message_id:
        return None
    for turn in turns:
        for message in turn.get("history") or []:
            if not isinstance(message, dict):
                continue
            if _role(message) == "user" and _message_id(message) == client_message_id:
                return turn
            for part in message.get("parts") or []:
                if not isinstance(part, dict):
                    continue
                data = part.get("data") if part.get("kind") == "data" else part
                if not isinstance(data, dict):
                    continue
                kind = str(data.get("kind") or data.get("type") or "")
                if kind in {"inbound_message", "InboundMessageEvent"} and str(
                    data.get("message_id") or data.get("messageId") or ""
                ) == client_message_id:
                    return turn
    return None
