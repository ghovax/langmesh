"""The background-jobs plugin: the per-session runner and how a completed job reaches the model.

Background work is one pluggable concern: the runner, the in-flight grouping the turn context
shows, and the cache-stable result messages a completion lands in the conversation. Its prompts
live beside this module so they are configurable, and the durable store is the caller's port.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from langmesh.runtime.background import (
    BackgroundJobs,
    background_completion_event,
    background_include_result,
)
from langmesh.runtime.internals import (
    _cap_model_result_payload,
    _maybe_json,
    _model_result_status,
    _tool_timing_metadata,
)
from langmesh.runtime.turn_events import ToolResult, TurnEvent
from langmesh.runtime.features import Feature, PluginContext, PluginHost


class BackgroundJobsFeature(Feature):
    """One background-job runner and the delivery of its finished work to the model."""

    def __init__(self, *, store: Any = None) -> None:
        self._store = store

    def attach(self, context: PluginContext, host: PluginHost) -> None:
        self._context = context
        self._host = host
        self._prompts = context.prompts("background")
        self._runner = BackgroundJobs(
            session_id=context.session_id,
            agent_name=context.agent_configuration.identifier,
            store=self._store,
        )

    @property
    def runner(self) -> BackgroundJobs:
        """The runner the executor's resume pump and the tools read."""
        return self._runner

    def bind_tool_call(self, job_id: str, tool_call_id: str) -> None:
        """Associate a detached job with the tool call that started it."""
        self._runner.bind_tool_call(job_id, tool_call_id)

    def compose_context(self, context: dict) -> None:
        """The in-flight jobs the turn context groups by kind."""
        context["background"] = {
            "running": self._runner.active_by_context_key(),
            "active_count": self._runner.active_count(),
        }

    def drain(self) -> list[TurnEvent]:
        """Every finished job as the turn events the loop yields, delivered and removed."""
        events: list[TurnEvent] = []
        for completion in self._runner.drain_completed():
            capped_result = _cap_model_result_payload(
                completion.result,
                code=f"{completion.kind}_result_truncated",
            )
            duration_milliseconds = int(
                (completion.completed_at - completion.started_at).total_seconds() * 1000
            )
            background_metadata = _tool_timing_metadata(
                tool_name=completion.kind,
                tool_call_identifier=completion.tool_call_identifier,
                started_at=completion.started_at,
                completed_at=completion.completed_at,
                duration_milliseconds=duration_milliseconds,
                background_job_id=completion.identifier,
            )
            # Append-only: the placeholder stays and the result lands as a new user-role reminder, keeping the prefix.
            background_status, background_code = _model_result_status(
                capped_result,
                ok=True,
                backgrounded=False,
            )
            self._append_result_messages(
                capped_result,
                background_metadata,
                background_status,
                background_code,
            )
            events.append(
                ToolResult(
                    id=completion.tool_call_identifier,
                    name=completion.kind,
                    result=_maybe_json(capped_result),
                    status=background_status,
                    job_id=completion.identifier,
                )
            )
            completion_event_data: dict[str, Any] = {"job_id": completion.identifier}
            if background_include_result(completion.kind):
                completion_event_data["result"] = capped_result
            self._host.bookkeeping.record_event(
                background_completion_event(completion.kind), completion_event_data
            )
        return events

    def inject_stored_result(
        self, *, kind: str, identifier: str, tool_call_identifier: str, result: str
    ) -> None:
        """Append a restored background result, so a rebuilt runtime replays it exactly like a live completion."""
        capped_result = _cap_model_result_payload(result, code=f"{kind}_result_truncated")
        metadata = _tool_timing_metadata(
            tool_name=kind,
            tool_call_identifier=tool_call_identifier,
            started_at=datetime.now(timezone.utc),
            completed_at=datetime.now(timezone.utc),
            duration_milliseconds=0,
            background_job_id=identifier,
        )
        status, code = _model_result_status(capped_result, ok=True, backgrounded=False)
        self._append_result_messages(capped_result, metadata, status, code)

    def _append_result_messages(
        self,
        content: str,
        metadata: dict[str, Any],
        status: str,
        code: str | None,
    ) -> None:
        """Append background data first and any actionable guidance second."""
        self._host.conversation.messages.append(
            self._host.turn.reminder_message(
                self._result_message(content, metadata, status, code),
                marks={"background_result": metadata, "status": status, "code": code},
            )
        )
        result_data = _maybe_json(content)
        result_code = str(result_data.get("code") or "") if isinstance(result_data, dict) else ""
        if result_code.endswith("_interrupted"):
            self._host.conversation.messages.append(
                self._host.turn.reminder_message(
                    self._prompts.load(
                        "background_interrupted",
                        {"kind": str(metadata.get("tool_name") or "tool")},
                    ),
                    marks={
                        "background_guidance": True,
                        "background_result": metadata,
                    },
                )
            )

    def _result_message(
        self,
        content: str,
        metadata: dict[str, Any],
        status: str,
        code: str | None,
    ) -> str:
        """Render a background completion while keeping its machine metadata on the message envelope."""
        return self._prompts.load(
            "background_result",
            {
                "tool_name": str(metadata.get("tool_name") or "background tool"),
                "job_id": str(metadata.get("background_job_id") or "unknown"),
                "status": status,
                "code": code or "none",
                "content": content,
            },
        )
