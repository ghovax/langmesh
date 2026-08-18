"""Executing a tool call: the shared preamble, batch draining, and the background runner.

Each tool's execution lives in `runtime/tools/handlers.py` as a `Tool` unit's handler; this module
owns the one preamble every call passes through (permission, validation, location, policy) and
the sequential batch runner. The runtime dispatches over its own tool set, so nothing here knows
a tool name.
"""

from __future__ import annotations

import logging
import time

from contextlib import suppress
from datetime import datetime, timezone
from langmesh.base.primitives import telemetry as _telemetry
from langmesh.base.confinement import parse_access_request
from langmesh.runtime.internals import (
    _cap_model_result_payload,
    _coerce_mcp_arguments,
    _coerce_structured_arguments,
    _maybe_json,
    _model_result_status,
    _ResolvedToolDecision,
    _tool_timing_metadata,
    _utc_timestamp,
)
from langmesh.runtime.tools import context as tool_context
from langmesh.runtime.values import ToolStatus
from langmesh.base.primitives.tuning import current_context_window
from langmesh.runtime.turn_events import (
    DeniedInjection,
    Error,
    ToolCall,
    ToolResult,
    TurnEvent,
)
from langchain_core.messages import ToolMessage
from pydantic import ValidationError
from typing import Any, AsyncIterator, cast
import asyncio
from langmesh.base.primitives.serialization import compact
from langmesh.runtime.tools.execution import (
    bind_tool_decision,
    bind_tool_services,
    unbind_tool_decision,
    unbind_tool_services,
)

logger = logging.getLogger(__name__)


def _with_schema_defaults(schema: Any, arguments: dict) -> dict:
    """The arguments with every omitted key filled from the schema. Only omitted: an explicit ``None`` stands."""
    fields = getattr(schema, "model_fields", None)
    if not fields:
        return arguments
    filled = dict(arguments)
    for name, field in fields.items():
        if name in filled:
            continue
        default = getattr(field, "default", None)
        if default is not None and default is not Ellipsis and repr(default) != "PydanticUndefined":
            filled[name] = default
    return filled


class _DispatchesTools:
    """Everything the runtime does with a tool call: resolve it, gate it, run it, report it."""

    async def _run_one_tool(
        self,
        tool_call_data: dict,
        turn_tool_calls_log: list[dict],
        turn_tool_results_log: list[dict],
        outcomes: dict[str, dict],
        decision: _ResolvedToolDecision,
    ) -> AsyncIterator[TurnEvent]:
        """Run one call and record its outcome. Self-contained, so it can run concurrently with other tools."""
        tool_name = tool_call_data["name"]
        tool_arguments = tool_call_data["args"]
        tool_call_identifier = tool_call_data["id"]
        started_at = datetime.now(timezone.utc)
        started_monotonic = time.monotonic()

        yield ToolCall(
            name=tool_name,
            arguments=tool_arguments,
            id=tool_call_identifier,
        )
        turn_tool_calls_log.append(
            {
                "name": tool_name,
                "arguments": tool_arguments,
                "tool_call_id": tool_call_identifier,
                "started_at": _utc_timestamp(started_at),
            }
        )

        result_content: str = ""
        background_job_id: str | None = None
        denied_commands: list[str] = []
        model_guidance: list[str] = []
        tool_failed = False

        tool_span = _telemetry.start_span("tool.execute", {"tool.name": tool_name})
        try:
            async for event in self._execute_tool(
                tool_name, tool_arguments, tool_call_identifier, decision
            ):
                yield event
                if isinstance(event, ToolResult):
                    if event.model_guidance:
                        model_guidance.append(event.model_guidance)
                    result_str = event.result
                    if isinstance(result_str, dict) and event.status == ToolStatus.RUNNING.value:
                        raw_job_id = result_str.get("job_id")
                        background_job_id = raw_job_id if isinstance(raw_job_id, str) else None
                        result_content = compact(
                            {
                                "code": "background_job_scheduled",
                                "job_id": background_job_id,
                            }
                        )
                        turn_tool_results_log.append(
                            {"name": tool_name, "result": compact(result_str)}
                        )
                    else:
                        if isinstance(result_str, dict):
                            # A result reporting an error status marks the call failed, for the model and the UI alike.
                            if event.status == ToolStatus.ERROR.value:
                                tool_failed = True
                            # Minified for the model, non-ASCII verbatim. The UI gets the dict on a separate path.
                            result_str = compact(result_str)
                        result_content = _cap_model_result_payload(str(result_str))
                        turn_tool_results_log.append({"name": tool_name, "result": result_content})
                elif isinstance(event, Error):
                    tool_failed = True
                    if event.model_guidance:
                        model_guidance.append(event.model_guidance)
                        result_data = {
                            "code": event.code or "permission_denied",
                            "status": ToolStatus.ERROR.value,
                        }
                        result_data.update(event.extra)
                        result_content = compact(result_data)
                    else:
                        result_content = event.message
                    turn_tool_results_log.append({"name": tool_name, "result": result_content})
                elif isinstance(event, DeniedInjection):
                    denied_commands.append(event.command)
        except asyncio.CancelledError:
            result_content = "Tool call aborted by the user; if any, read their newest request first."
            yield Error(
                id=tool_call_identifier,
                message=result_content,
                tool=tool_name,
            )
            turn_tool_results_log.append({"name": tool_name, "result": result_content})
        except Exception as exception:
            result_content = f"{exception}"
            yield Error(
                id=tool_call_identifier,
                message=result_content,
                tool=tool_name,
            )
            turn_tool_results_log.append({"name": tool_name, "result": result_content})
        finally:
            _telemetry.end_span(tool_span)

        completed_at = datetime.now(timezone.utc)
        duration_milliseconds = int((time.monotonic() - started_monotonic) * 1000)
        timing_metadata = _tool_timing_metadata(
            tool_name=tool_name,
            tool_call_identifier=tool_call_identifier,
            started_at=started_at,
            completed_at=completed_at,
            duration_milliseconds=duration_milliseconds,
            background_job_id=background_job_id,
        )

        outcomes[tool_call_identifier] = {
            "content": result_content,
            "ok": not tool_failed,
            "background_job_id": background_job_id,
            "denied_commands": denied_commands,
            "model_guidance": model_guidance,
            "metadata": timing_metadata,
        }

    async def _drain_tool_batch(
        self,
        tool_calls: list[dict],
        turn_tool_calls_log: list[dict],
        turn_tool_results_log: list[dict],
        outcomes: dict[str, dict],
        decisions: dict[str, _ResolvedToolDecision],
    ) -> AsyncIterator[TurnEvent]:
        # One call at a time, in order; a Stop or per-call cancel reaches the running task directly.
        for tool_call_data in tool_calls:
            if self._abort_event.is_set() or self._stop_requested:
                break
            tool_call_identifier = tool_call_data["id"]
            pipe: asyncio.Queue[TurnEvent | None] = asyncio.Queue()

            async def run_one() -> None:
                try:
                    async for event in self._run_one_tool(
                        tool_call_data,
                        turn_tool_calls_log,
                        turn_tool_results_log,
                        outcomes,
                        decisions.get(tool_call_identifier)
                        or _ResolvedToolDecision(tool_call_id=tool_call_identifier),
                    ):
                        await pipe.put(event)
                finally:
                    await pipe.put(None)

            task = asyncio.create_task(run_one())
            self._active_tool_tasks[tool_call_identifier] = task
            try:
                while True:
                    event = await pipe.get()
                    if event is None:
                        break
                    yield event
            finally:
                self._active_tool_tasks.pop(tool_call_identifier, None)
                if not task.done():
                    task.cancel()
                    with suppress(BaseException):
                        await task

    def _validate_tool_call(
        self,
        tool_name: str,
        arguments: dict,
    ) -> tuple[str, str] | None:
        tool_schemas = (
            {**self._tool_schemas, **self._features.maintenance_tool_schemas()}
            if self._features.active_maintenance()
            else self._tool_schemas
        )
        if not isinstance(arguments, dict):
            return (
                "invalid_tool_arguments",
                self._prompt_loader.load(
                    "tool_arguments_not_object",
                    {"tool_name": tool_name},
                ),
            )
        if tool_name not in tool_schemas:
            return (
                "unknown_tool",
                self._prompt_loader.load(
                    "unknown_tool",
                    {
                        "tool_name": tool_name,
                        "available_tools": compact(sorted(tool_schemas)),
                    },
                ),
            )
        schema = tool_schemas.get(tool_name)
        if schema is not None:
            fields = set(getattr(schema, "model_fields", {}).keys())
            unknown_arguments = sorted(set(arguments) - fields)
            if unknown_arguments:
                return (
                    "invalid_tool_arguments",
                    self._prompt_loader.load(
                        "unsupported_tool_arguments",
                        {
                            "tool_name": tool_name,
                            "rejected_arguments": compact(unknown_arguments),
                            "accepted_arguments": compact(sorted(fields)),
                        },
                    ),
                )
            # Models often emit an MCP server call's `arguments` as a JSON string; coerce before validating it.
            if tool_name == "call_mcp_server_tool" and isinstance(arguments.get("arguments"), str):
                arguments = {
                    **arguments,
                    "arguments": _coerce_mcp_arguments(arguments.get("arguments")),
                }
            try:
                schema_validator = getattr(schema, "model_validate", None)
                if schema_validator is not None:
                    schema_validator(arguments)
            except ValidationError as exception:
                return ("invalid_tool_arguments", str(exception))
            # The validated model is kept — see `_with_schema_defaults` — so a documented default actually applies. Validated once here, so a malformed request fails as a tool error rather than becoming a grant.
        if "access_request" in arguments:
            _, complaint = parse_access_request(arguments.get("access_request"))
            if complaint:
                return ("invalid_access_request", complaint)
        if tool_name == "call_mcp_server_tool":
            if not arguments.get("server"):
                return ("invalid_mcp_server", "server is required.")
            if not arguments.get("tool_name"):
                return ("invalid_mcp_tool", "tool_name is required.")
        return None

    def _append_tool_results(self, response, outcomes: dict[str, dict]) -> None:
        """A ToolMessage per tool_call, contiguous, since providers require every result in the block that follows."""
        guidance_notes: list[tuple[str, str]] = []
        for tool_call_data in cast(list[dict], response.tool_calls):
            tool_call_identifier = tool_call_data["id"]
            outcome = outcomes.get(tool_call_identifier, {})
            content = outcome.get("content", "")
            if not content:
                content = "(interrupted)" if self._abort_event.is_set() else ""
            result_status, result_code = _model_result_status(
                content,
                ok=outcome.get("ok", True),
                backgrounded=bool(outcome.get("background_job_id")),
            )
            metadata = outcome.get("metadata") or _tool_timing_metadata(
                tool_name=tool_call_data.get("name", ""),
                tool_call_identifier=tool_call_identifier,
                started_at=datetime.now(timezone.utc),
                completed_at=datetime.now(timezone.utc),
                duration_milliseconds=0,
            )
            self._conversation.append(
                ToolMessage(
                    content=content,
                    tool_call_id=tool_call_identifier,
                    name=str(tool_call_data.get("name") or ""),
                    status="error" if result_status == ToolStatus.ERROR.value else "success",
                    artifact={
                        "metadata": metadata,
                        "status": result_status,
                        "code": result_code,
                        "result": _maybe_json(content),
                    },
                )
            )
            guidance_notes.extend(
                (tool_call_identifier, note) for note in outcome.get("model_guidance", []) if note
            )
            background_job_id = outcome.get("background_job_id")
            if background_job_id:
                self._features.invoke("bind_background_tool", background_job_id, tool_call_identifier)
            denied_commands = outcome.get("denied_commands", [])
            if denied_commands:
                guidance_notes.append(
                    (
                        tool_call_identifier,
                        self._prompt_loader.load(
                            "command_denied",
                            {"commands": compact(denied_commands)},
                        ),
                    )
                )
        for tool_call_identifier, guidance in guidance_notes:
            self._conversation.append(
                self._reminder_message(
                    guidance,
                    marks={"tool_guidance": True, "tool_call_id": tool_call_identifier},
                )
            )

        # Malformed calls are corrected by a reminder: a ToolMessage would be orphaned and rejected.
        for invalid in response.invalid_tool_calls:
            self._conversation.append(
                self._reminder_message(
                    self._invalid_tool_call_content(cast(dict, invalid)),
                )
            )

    async def _execute_tool(
        self,
        tool_name: str,
        tool_arguments: dict,
        tool_call_identifier: str,
        decision: _ResolvedToolDecision,
    ) -> AsyncIterator[TurnEvent]:
        """Execute one call, yielding events. Permission is already resolved, so this path never prompts."""
        # A call that already ran is replayed: whatever side effects it had, it had once.
        if decision.completed is not None:
            yield ToolResult(
                id=tool_call_identifier,
                name=tool_name,
                result=decision.completed.get("result"),
                model_guidance=str(decision.completed.get("model_guidance") or ""),
            )
            return

        # A refused call never runs: surface the recorded refusal and stop.
        if decision.denial is not None:
            error_kwargs: dict[str, Any] = {
                "id": tool_call_identifier,
                "tool": tool_name,
                "message": decision.denial.get("message", ""),
                "model_guidance": decision.denial.get("message", ""),
                "extra": {"decision": decision.denial.get("decision", {})},
            }
            error_kwargs["code"] = decision.denial.get("code") or "permission_denied"
            yield Error(**error_kwargs)
            if decision.denial.get("denied_injection"):
                yield DeniedInjection(
                    id=tool_call_identifier,
                    command=decision.denial.get("raw_command", ""),
                )
            return

        # This agent's live window, so every window-scaled cap is sized for the model actually running.
        current_context_window.set(self._context_window)

        # The session state tools read at call time, bound per call so two open turns cannot see each other's.
        tool_context.bind(
            self._tool_context.with_grants(self._access_grants).with_attachments(
                self._attached_files
            )
        )

        # Coerce JSON-string arguments up front, so validation and dispatch see the real container.
        schema = (
            self._features.maintenance_tool_schemas().get(tool_name)
            if self._features.active_maintenance() and tool_name in self._features.maintenance_tool_schemas()
            else self._tool_schemas.get(tool_name)
        )
        if schema is not None:
            tool_arguments = _coerce_structured_arguments(schema, tool_arguments)
            # And fill the schema's defaults, so a documented default is the one that applies.
            tool_arguments = _with_schema_defaults(schema, tool_arguments)


        validation_error = self._validate_tool_call(
            tool_name,
            tool_arguments,
        )
        if validation_error:
            error_code, error_message = validation_error
            yield Error(
                id=tool_call_identifier, code=error_code, message=error_message, tool=tool_name
            )
            return

        # Resolve the call's execution target: a feature answers with an opaque call site, or `None` for local.
        call_site = None
        try:
            call_site = self._features.invoke("resolve_execution", tool_name, tool_arguments)
        except ValueError as exception:
            yield Error(
                id=tool_call_identifier,
                code="invalid_location",
                message=str(exception),
                tool=tool_name,
            )
            return
        policy = self._call_policy(None)

        # Dispatch is data-driven over the session's own tool units: a built-in or a caller's tool is the same `Tool`, so there is no name table and a caller's implementation of the same name simply replaces the built-in's.
        unit = self._tool_units.get(tool_name)
        if unit is None:
            yield Error(
                id=tool_call_identifier,
                message=f"Unknown tool '{tool_name}'",
                tool=tool_name,
            )
            return
        # A handler that invokes a schema tool directly (`.ainvoke`) resolves the same services and the resolved decision.
        services_token = bind_tool_services(self._services)
        decision_token = bind_tool_decision(decision)
        try:
            async for event in unit.handler(
                self._services,
                tool_name,
                tool_arguments,
                tool_call_identifier,
                decision,
                policy,
                call_site,
            ):
                yield event
        finally:
            unbind_tool_decision(decision_token)
            unbind_tool_services(services_token)

