"""A session's verbs: send a message, answer a gate, cancel a turn, reset its runtime."""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, cast

from a2a.types import DataPart, Part, TextPart

from langmesh.protocol.metadata import Metadata


logger = logging.getLogger(__name__)


def _message_parts(params: dict) -> list[Part]:
    """Build the message's parts from prose or an explicit list, so a caller can send plain text."""
    explicit = params.get("parts")
    if isinstance(explicit, list) and explicit:
        parts: list[Part] = []
        for entry in explicit:
            if not isinstance(entry, dict):
                continue
            if entry.get("kind") == "text":
                parts.append(Part(root=TextPart(text=str(entry.get("text", "")))))
            else:
                # The part's `data` rather than the part, so the payload does not arrive one level too deep.
                payload = entry.get("data") if isinstance(entry.get("data"), dict) else entry
                parts.append(Part(root=DataPart(data=cast(dict[str, Any], payload))))
        if parts:
            return parts
    return [Part(root=TextPart(text=str(params.get("text", ""))))]


async def _send(session, params: dict) -> dict:
    """Serialize intake, steering plain text in place and queueing structured messages intact."""
    async with session.serialized_send():
        compaction_error = await session.compaction_failure()
        if compaction_error:
            return {
                "accepted": False,
                "compaction_required": True,
            }
        # A session parked on a decision takes no new turn, since starting one would discard the parked turn silently.
        parked = await session.pending_decision()
        if parked and not session.is_running:
            return {"accepted": False, "awaiting_input": True, "waiting_on": parked}
        message_id = str((params.get("metadata") or {}).get("messageId") or "")
        if message_id:
            existing = await session.user_message_turn(message_id)
            if existing is not None:
                # A client that died after we accepted this id must not start a second copy of the same mail.
                return {
                    "accepted": True,
                    "injected": bool(session.is_running),
                    "turn_id": existing["turn_id"],
                    "duplicate": True,
                    "state": existing["state"],
                }
        explicit_parts = params.get("parts")
        has_structured_parts = isinstance(explicit_parts, list) and any(
            isinstance(entry, dict) and entry.get("kind") != "text" for entry in explicit_parts
        )
        if session.is_running and not has_structured_parts and not params.get("serialize"):
            text = "".join(
                str(entry.get("text", ""))
                for entry in (params.get("parts") or [])
                if isinstance(entry, dict) and entry.get("kind") == "text"
            ) or str(params.get("text", ""))
            # The sender's own id for this message, so a client can recognise its own copy when the session echoes it.
            inject_id = str((params.get("metadata") or {}).get("messageId") or "")
            # Who sent it, carried into the running turn, so a peer's report is not shown as the user's own words.
            peer_sender = str((params.get("metadata") or {}).get(Metadata.PEER_SENDER) or "")
            if await session.inject(text, inject_id, peer_sender):
                return {"accepted": True, "injected": True, "turn_id": session.live_turn_id()}
            compaction_error = await session.compaction_failure()
            if compaction_error:
                return {
                    "accepted": False,
                    "compaction_required": True,
                }
        # The context lock serializes this with any active turn. Structured parts take this path deliberately because reducing an attachment-bearing message to steerable text would lose data.
        turn_id = await session.start_turn(
            _message_parts(params), dict(params.get("metadata") or {})
        )
        return {"accepted": True, "injected": False, "turn_id": turn_id}


async def _respond(session, params: dict) -> dict:
    """Answer a pending permission or question, unblocking the parked turn."""
    request_id = str(params.get("request_id") or "")
    if not request_id:
        raise ValueError("request_id is required")
    data: dict[str, Any] = {"request_id": request_id}
    if params.get("declined"):
        data["declined"] = True
        if params.get("reason"):
            data["reason"] = str(params["reason"])
    elif params.get("answers") is not None:
        data["answers"] = params.get("answers")
    else:
        data["decision"] = str(params.get("decision") or "deny")
        if params.get("reason"):
            data["reason"] = str(params["reason"])
    resolved = await session.resolve_pending_input(data)
    return {"resolved": resolved}


async def _cancel(session, params: dict) -> dict:
    tool_call_id = str(params.get("tool_call_id") or "")
    if tool_call_id:
        # The facade method rather than the context-keyed one, since a worker is one session and its id is implicit.
        return {"cancelled": session.abort_tool_call(tool_call_id)}
    return {"cancelled": await session.abort()}


async def _status(session, _params: dict) -> dict:
    return session.status_payload()


async def _abort_input(session, _params: dict) -> dict:
    return {"aborted": await session.abort_pending_input()}


async def _clear_goal(session, _params: dict) -> dict:
    """The person called the goal off, which is what stops the session opening further turns for itself."""
    return {"cleared": await session.clear_goal(session.session_id)}


async def _resume_goal(session, _params: dict) -> dict:
    """The person restarted a parked goal, so the session opens turns for it again."""
    return {"resumed": await session.resume_goal(session.session_id)}


async def _compact(session, _params: dict) -> dict:
    return await session.compact()


async def _retry(session, _params: dict) -> dict:
    return await session.retry_turn(session.session_id)


async def _list_jobs(session, _params: dict) -> dict:
    return {"jobs": session.background_jobs()}


async def _detach_job(session, params: dict) -> dict:
    return {"backgrounded": session.background_tool_call(str(params.get("tool_call_id") or ""))}


async def _set_locations(session, params: dict) -> dict:
    """The workspace's locations were edited, and the whole re-resolved set is sent because an edit can repoint one."""
    entries = params.get("locations")
    return {"locations": session.set_locations(entries if isinstance(entries, list) else None)}


async def _set_permission_mode(session, params: dict) -> dict:
    """The person changed this session's approval policy while it runs, making it true for the turn in flight."""
    return {
        "permission_mode": await session.set_permission_mode(
            str(params.get("permission_mode") or "")
        )
    }


async def _reset(session, _params: dict) -> dict:
    """Settings changed under a live session, so drop the cached runtime and rebuild on the next turn."""
    session.reset_runtimes()
    return {"ok": True}


async def _observation_registry(session, params: dict) -> dict:
    """Refresh progressive-disclosure metadata and queue schema feedback when present."""
    metadata = params.get("metadata")
    session.note_observation_registry(
        metadata if isinstance(metadata, dict) else {},
        str(params["error"]) if params.get("error") else None,
    )
    return {"ok": True}


# Every verb a session answers, in one table, as the control plane and the intake do.
METHODS: dict[str, Callable[[Any, dict], Awaitable[dict]]] = {
    "message/send": _send,
    "input/respond": _respond,
    "input/abort": _abort_input,
    "tasks/cancel": _cancel,
    "session/status": _status,
    "session/goal-clear": _clear_goal,
    "session/goal-resume": _resume_goal,
    "session/compact": _compact,
    "session/retry": _retry,
    "session/locations": _set_locations,
    "session/permission-mode": _set_permission_mode,
    "session/reset": _reset,
    "session/observation-registry": _observation_registry,
    "jobs/list": _list_jobs,
    "jobs/detach": _detach_job,
}
