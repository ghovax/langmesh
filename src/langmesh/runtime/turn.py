"""The turn loop: the `stream()` driver, its phases, and how a turn's messages are assembled."""

from __future__ import annotations

import logging
import os
from contextlib import ExitStack, suppress
from datetime import datetime, timezone
from langmesh.base import telemetry as _telemetry
from langmesh.base.identifiers import new_id
from langmesh.runtime.internals import (
    _CONTINUE,
    _detect_workspace,
    _ModelCallOutcome,
    _StepOutcome,
    _PreflightGate,
    _STOP,
    _STREAM_EXHAUSTED,
    _stream_next,
    _ToolPlan,
    _maybe_json,
    conversation_tokens,
    message_tokens,
    settled_arguments,
)
from langmesh.runtime.prompt_environment import probe_local_environment, probe_user_context
from langmesh.runtime.cache_trace import cache_lane
from langmesh.runtime.values import PermissionAnswer, TurnContext
from langmesh.base.instructions import instructions_payload
from langmesh.base.memories import memories_payload
from langmesh.base.message_content import (
    CARRIED_REASONING_KEYS,
    message_content_deltas,
    message_text,
)
from langmesh.base.model_errors import ContextWindowExceeded, over_context_window
from litellm.exceptions import ContextWindowExceededError as ProviderContextWindowExceeded
from langchain_core.utils.json import parse_partial_json
from langmesh.base.skills import enabled_skills, skills_for_agent, skills_payload
from langmesh.base.confinement import Denial
from langmesh.runtime.turn_events import (
    Checkpoint,
    CompactionStarted,
    Done,
    Error,
    RetryRequested,
    Steering,
    Suspended,
    PermissionReviewing,
    SuspensionGate,
    Status,
    TextChunk,
    Thinking,
    ThinkingDone,
    ToolCall,
    TurnEvent,
)
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.messages.ai import add_ai_message_chunks
from pathlib import Path
from typing import Any, AsyncIterator, cast, Optional
import asyncio
import platform
import time
import uuid
from langmesh.base.serialization import compact, lines
from langmesh.base.tuning import Tunable, active_tuning


logger = logging.getLogger(__name__)


def _chunk_advances_model_response(chunk: Any) -> bool:
    message = getattr(chunk, "message", chunk)
    if not isinstance(message, AIMessageChunk):
        return False
    generation_information = getattr(chunk, "generation_info", None) or {}
    response_metadata = getattr(message, "response_metadata", None) or {}
    additional = getattr(message, "additional_kwargs", None) or {}
    if (
        getattr(message, "tool_call_chunks", None)
        or getattr(message, "tool_calls", None)
        or getattr(message, "invalid_tool_calls", None)
        or getattr(message, "usage_metadata", None)
        or any(additional.get(key) for key in CARRIED_REASONING_KEYS)
        or generation_information.get("finish_reason")
        or response_metadata.get("finish_reason")
        or getattr(message, "chunk_position", None) == "last"
    ):
        return True
    return any(block.get("text") or block.get("reasoning") for block in message.content_blocks)


class _RunsTurns:
    """The turn itself: what the model is told, what comes back, and when it is over."""

    def _locations_summary(self) -> list[dict]:
        """The locations as the model sees them: the URI to pass, and enough to choose the right one."""
        return [
            {
                "location": resolved.uri,
                "name": resolved.name,
                "kind": resolved.kind,
                "base_directory": resolved.base_directory,
                "writable": resolved.is_remote or self.writes_anywhere,
            }
            for resolved in self._locations.values()
        ]

    def _build_static_system_prompt(self) -> str:
        """Build the session prompt once, then rebuild it only at an explicit refresh boundary."""
        if self._cached_system_prompt is None:
            all_skills = enabled_skills(list(self._catalogue.skills()))
            agent_skills = skills_for_agent(all_skills, self._agent_configuration.skills)
            memories = list(self._catalogue.memories())
            worktree_root, is_git_repo = _detect_workspace(self._working_directory)
            context = TurnContext(
                now=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
                pwd=self._working_directory or str(Path.cwd()),
                goal=self._goal.for_model() if self._goal is not None else {},
                tasks=self._task_manager.to_dict_list(),
                background={
                    "running": self._background.active_by_context_key(),
                    "active_count": self._background.active_count(),
                    "recent_events": self._execution_history[-20:],
                },
                screen=self._screen_context(),
                locations=self._locations_summary(),
                confinement=self._confinement_summary(),
            ).model_dump(exclude_defaults=True)
            context.update(
                {
                    "session": self._session_id,
                    **({"parent_session": self._parent_session} if self._parent_session else {}),
                    "working_directory": self._working_directory,
                    "project_directory": self._project_directory,
                    "worktree_root": worktree_root,
                    "is_git_repo": is_git_repo,
                    "session_worktree_strategy": self._global_configuration.workspace.strategy,
                    "platform": platform.system(),
                    "today_date": datetime.now().strftime("%Y-%m-%d"),
                    "machine": _maybe_json(probe_local_environment(self._child_path())),
                }
            )
            if self._user_context_enabled():
                user_context = _maybe_json(
                    probe_user_context(self._global_configuration.user_context.refresh_hours)
                )
                if isinstance(user_context, dict) and user_context:
                    context["user_context"] = user_context
            context_json = compact(context)
            # Conditional, since it asserts "you are running as a session", which a library runtime is not.
            parent_report = (
                self._prompt_loader.load("parent_report", {"parent": self._parent_session})
                if self._parent_session
                else ""
            )
            agent_context = (
                self._prompt_loader.load("agent_context", {"parent_report": parent_report})
                if "message_session" in {tool.name for tool in self._tools}
                else ""
            )
            # The user-context section is its own template, so it is simply absent when the setting is off.
            user_environment = ""
            if self._user_context_enabled():
                user_environment = self._prompt_loader.load("user_context", {})
            # The screen tools are opt-in, so their guidance enters the prompt only when they do.
            computer_control_guidance = ""
            if self._global_configuration.computer_control.enabled:
                computer_control_guidance = self._prompt_loader.load(
                    "computer_control_guidance", {}
                )
            # Guidance for tools this session lacks is guidance to call something that is not there.
            toolbox = (
                self._prompt_loader.load("toolbox", {})
                if self._tool_context.toolbox is not None
                else ""
            )
            available = {tool.name for tool in self._tools}
            peer_sessions = (
                self._prompt_loader.load("peer_sessions", {})
                if "create_session" in available or "message_session" in available
                else ""
            )
            mcp_servers = (
                self._prompt_loader.load("mcp_servers", {})
                if "call_mcp_server_tool" in available
                else ""
            )
            # Whole documents, each laid out by its own template: metadata as JSON, the document as itself.
            instruction_files = "".join(
                self._prompt_loader.load(
                    "instruction_file",
                    {
                        # Whatever the payload carries and nothing invented: `scope` is absent for a non-file document.
                        "metadata": compact(
                            {key: entry[key] for key in ("source", "scope") if key in entry}
                        ),
                        "content": entry["content"].strip(),
                    },
                )
                for entry in instructions_payload(self._catalogue.instructions())
            ).strip()
            instructions = (
                self._prompt_loader.load("instructions", {"files": instruction_files})
                if instruction_files
                else ""
            )
            # One statement of how to think, rendered into this prompt and the reviewer's, so they cannot drift.
            thinking_language = self._prompt_loader.load("thinking_language", {}).strip()
            variables = {
                "agent_prompt": self._system_prompt,
                "thinking_language": thinking_language,
                "context": context_json,
                "user_environment": user_environment,
                "instructions": instructions,
                "skills": lines(skills_payload(agent_skills)),
                "memories": lines(memories_payload(memories)),
                "observational_memory": self._prompt_loader.load(
                    "observational_memory",
                    {"metadata": compact(self._observation_registry_metadata)},
                ).strip(),
                "agent_context": agent_context,
                "computer_control_guidance": computer_control_guidance,
                "toolbox": toolbox,
                "peer_sessions": peer_sessions,
                "mcp_servers": mcp_servers,
            }
            if self._prompt_composer is None:
                prompt = self._prompt_loader.load("system_prompt", variables)
            else:
                from langmesh.base.ports import PromptLayer

                prompt = self._prompt_composer.compose(
                    tuple(PromptLayer(name, content) for name, content in variables.items())
                )
                if not isinstance(prompt, str):
                    raise TypeError("prompt_composer.compose() must return a string")
            self._cached_system_prompt = prompt.strip()
        return self._cached_system_prompt

    def _user_context_enabled(self) -> bool:
        user_context = getattr(self._global_configuration, "user_context", None)
        return user_context is not None and bool(user_context.enabled)

    def _child_path(self) -> list[str]:
        """The `PATH` a tool child is actually given, split into entries."""
        from langmesh.base.confinement import child_environment

        environment = child_environment(self._sandbox, workspace=self._working_directory or "")
        environment.update(self._tool_context.child_environment(environment))
        return [entry for entry in environment.get("PATH", "").split(os.pathsep) if entry]

    def _confinement_summary(self) -> dict:
        """The boundary the operating system will enforce: the configured profile, plus the grants so far, apart."""
        profile = getattr(self._tool_context, "sandbox", None)
        if profile is None or self._global_configuration.sandbox.enforce == "off":
            return {}
        summary = profile.describe(workspace=self._working_directory or "")
        if self._access_grants:
            summary["granted"] = [grant.as_dict() for grant in self._access_grants]
        return summary

    def _screen_context(self) -> dict:
        """Every place a screen script can be pointed at, and what may be called in each."""
        if not self._global_configuration.computer_control.enabled:
            return {}
        try:
            from langmesh.computer import targets as target_registry
            from langmesh.computer.surface import message_loader

            # Skipped rather than waited on: enumerating costs ~1.8s the first time a process asks.
            if not target_registry.warm():
                return {"reading": message_loader("computer")("screen_warming")}

            from langmesh.computer import workflows

            # Every primitive is listed: what a script may call is decided per call, from what it asks to do.
            block = target_registry.context_block()
            # Saved workflows, read off the files without importing them, since this runs every turn.
            saved = workflows.available(self._project_directory or self._working_directory or "")
            if saved:
                block["workflows"] = saved
            return block
        except Exception:  # noqa: BLE001 — context is an aid, never the thing that fails a turn
            logger.debug("could not enumerate screen targets for the turn context", exc_info=True)
            return {}

    def _record_turn(
        self, user_message: str, tool_calls: list, tool_results: list, final_response: str
    ):
        self._record_message("human", user_message)
        for tool_call_entry in tool_calls:
            self._record_message(
                "tool", compact(tool_call_entry.get("args", {})), tool_call_entry.get("name", "")
            )
        for tool_result_entry in tool_results:
            self._record_message(
                "tool", str(tool_result_entry.get("result", "")), tool_result_entry.get("name", "")
            )
        if final_response:
            self._record_event(
                "assistant_response_completed",
                {
                    "content_characters": len(final_response),
                    "tool_call_count": len(tool_calls),
                    "tool_result_count": len(tool_results),
                },
            )
        self._record_message("ai", final_response)

    async def _record_transcript_turn(
        self,
        request: str,
        response: str,
        outcome: str,
        tool_calls: list,
        started_at: datetime,
        error: str = "",
    ) -> None:
        """Hand one completed turn to the caller's transcript: one entry per turn, not per message."""
        from langmesh.base.ports import TurnSummary

        usage = self._token_usage
        summary = TurnSummary(
            session_id=self._session_id,
            turn_id=new_id("turn"),
            started_at=started_at,
            ended_at=datetime.now(timezone.utc),
            request=request,
            response=response,
            outcome=outcome,
            tools_called=tuple(entry.get("name", "") for entry in tool_calls),
            input_tokens=int(usage.get("input_tokens", 0)),
            output_tokens=int(usage.get("output_tokens", 0)),
            error=error,
        )
        try:
            if self._transcript is not None:
                await self._transcript.record(summary)
        except Exception:  # noqa: BLE001 — a record that cannot be written must not lose the turn
            logger.warning("the transcript raised while recording a turn", exc_info=True)
        await self._hooks.after_turn(summary)

    async def _drain_steering_messages(self) -> list[TurnEvent]:
        events: list[TurnEvent] = []
        pending = self._pending_steering
        if not pending:
            return events
        # Drain the whole FIFO in order; a sender learns it was accepted the moment it is drained.
        self._pending_steering = []
        for message, message_id, peer_sender, accepted in pending:
            self._conversation.append(HumanMessage(content=message))
            events.append(Steering(text=message, message_id=message_id, peer_sender=peer_sender))
            if not accepted.done():
                accepted.set_result(True)
        return events

    async def _answer_gates(self, gates: list[SuspensionGate]) -> dict[str, Any]:
        """Ask the caller's `Approvals` about each gate. Without one, every gate suspends, as it always has."""
        if self._approvals is None or not gates:
            return {}
        answers: dict[str, Any] = {}
        for gate in gates:
            try:
                verdict = await self._approvals.decide(gate)
            except Exception:  # noqa: BLE001 — a failing approver escalates, it does not allow
                logger.warning(
                    "the approver raised on %s; escalating the gate", gate.kind, exc_info=True
                )
                continue
            if verdict is None:
                continue
            if gate.kind == "question":
                answers[gate.request_id] = (
                    verdict.answers
                    if verdict.allow
                    else {
                        "__declined__": True,
                        "__reason__": verdict.reason,
                        "__actor__": "configured approver",
                    }
                )
            else:
                answers[gate.request_id] = PermissionAnswer(
                    allow=verdict.allow,
                    reason=verdict.reason,
                    actor="approver",
                ).model_dump()
        return answers

    def _reminder_message(
        self,
        content: str,
        image_blocks: list[dict] | None = None,
        marks: dict[str, Any] | None = None,
    ) -> HumanMessage:
        """Append harness guidance once; provider adapters preserve its instruction role without moving it."""
        text = self._prompt_loader.load("reminder", {"content": content.strip()}).strip()
        tags = {"reminder": True, **(marks or {})}
        if image_blocks:
            return HumanMessage(
                content=[{"type": "text", "text": text}, *image_blocks], additional_kwargs=tags
            )
        return HumanMessage(content=text, additional_kwargs=tags)

    def _invalid_tool_call_content(self, invalid: dict) -> str:
        """The message for a malformed tool call, from a template, so the wording stays out of the code."""
        return self._prompt_loader.load(
            "invalid_tool_call",
            {
                "name": invalid.get("name") or "unknown",
                "error": invalid.get("error") or "arguments could not be parsed",
            },
        )

    def _repair_dangling_tool_calls(self, *, protect_tail_batch: bool = False) -> None:
        """Make every assistant tool-call batch adjacent to the tool messages answering it, so history is valid for the provider."""
        if not self._conversation:
            return
        protected: set[str] = set()
        if protect_tail_batch and isinstance(self._conversation[-1], AIMessage):
            protected = {
                call["id"]
                for call in getattr(self._conversation[-1], "tool_calls", None) or []
                if call.get("id")
            }
        by_identifier = {
            message.tool_call_id: message
            for message in self._conversation
            if isinstance(message, ToolMessage) and message.tool_call_id
        }
        repaired: list = []
        consumed: set[str] = set()
        changed = False
        for message in self._conversation:
            if isinstance(message, ToolMessage) and message.tool_call_id in consumed:
                changed = True  # already re-emitted beside its assistant message
                continue
            repaired.append(message)
            if isinstance(message, AIMessage):
                for call in getattr(message, "tool_calls", None) or []:
                    identifier = call.get("id")
                    if not identifier or identifier in protected:
                        continue
                    if identifier not in by_identifier:
                        by_identifier[identifier] = ToolMessage(
                            content="Tool call aborted by the user; if any, read their newest request first.",
                            tool_call_id=identifier,
                        )
                        changed = True
                    if identifier not in consumed:
                        repaired.append(by_identifier[identifier])
                        consumed.add(identifier)
        if len(repaired) != len(self._conversation) or changed:
            self._conversation[:] = repaired
            self._mark_session_dirty()

    def abandon_suspension(self) -> None:
        """Close a parked tool batch without executing it, leaving valid append-only conversation state."""
        self._repair_dangling_tool_calls()

    async def resume_stream(
        self, plans: dict[str, dict], answers: dict[str, Any]
    ) -> AsyncIterator[TurnEvent]:
        """Resume a suspended turn: run its pending batch with the recorded answers, then continue normally."""
        async for event in self.stream("", resume_plans=plans, resume_answers=answers):
            yield event

    async def continue_stream(self) -> AsyncIterator[TurnEvent]:
        """Continue an already-recorded user turn after a failed compaction was retried successfully."""
        async for event in self.stream("", continue_existing=True):
            yield event

    async def prepare_compaction_stream(self) -> AsyncIterator[TurnEvent]:
        """Run the private recording segment and compaction, without inventing a user turn afterward."""
        async for event in self.stream("", continue_existing=True, stop_after_compaction=True):
            yield event

    async def stream(
        self,
        user_message: str | list,
        as_system_note: bool = False,
        # A note that nonetheless begins a unit of work, which is what the record is written per.
        opens_exchange: bool = False,
        resume_plans: Optional[dict[str, dict]] = None,
        resume_answers: Optional[dict[str, Any]] = None,
        continue_existing: bool = False,
        stop_after_compaction: bool = False,
    ) -> AsyncIterator[TurnEvent]:
        from langmesh.base.errors import CompactionBlockedError

        if self._compaction_control.failure and not continue_existing:
            raise CompactionBlockedError(
                f"Context compaction failed: {self._compaction_control.failure} Retry compaction before sending more work."
            )
        self._abort_event.clear()
        # A turn's own bookkeeping: the no-op nudge happens once, and the start time feeds the transcript.
        response_nudged: list[bool] = [False]
        # The person's words are held until the request is assembled, so every part sent with them reads first.
        pending_user_message = None
        # A turn runs until the model is done or the user interrupts: an unfinished goal outlives this loop.
        turn_tool_calls_log: list[dict] = []
        turn_tool_results_log: list[dict] = []

        # Every request leaves from valid history: a resume protects its pending batch, every other path closes unanswered tool calls.
        self._repair_dangling_tool_calls(protect_tail_batch=resume_plans is not None)

        if continue_existing:
            recorded_user_message = ""
        elif resume_plans is not None:
            # Resume: the checkpoint is already at the tail, so run its batch and fall into the loop.
            recorded_user_message = ""
            response = self._conversation[-1] if self._conversation else None
            if response is None or not getattr(response, "tool_calls", None):
                yield Done(text="", stop_reason="completed")
                return
            resolved = self._resolve_tool_decisions(
                {
                    tool_call_id: _ToolPlan.from_dict(plan)
                    for tool_call_id, plan in resume_plans.items()
                },
                resume_answers or {},
            )
            resume_outcomes: dict[str, dict] = {}
            async for event in self._drain_tool_batch(
                cast(list[dict], response.tool_calls),
                turn_tool_calls_log,
                turn_tool_results_log,
                resume_outcomes,
                resolved,
            ):
                yield event
            self._append_tool_results(response, resume_outcomes)
            if self._resource_sync is not None:
                await self._resource_sync()
            yield Checkpoint()
        else:
            # Usually prose, but an attachment turn carries a content list so a vision model sees the pixels.
            turn_message = (
                self._reminder_message(
                    user_message, marks={"opens_exchange": True} if opens_exchange else None
                )
                if as_system_note and isinstance(user_message, str)
                else HumanMessage(content=user_message)
            )
            # The event-log recorder only wants prose from LangChain's standard blocks.
            recorded_user_message = message_text(turn_message)
            # Held until compaction has had a chance to reclaim the existing conversation.
            pending_user_message = turn_message
        turn_started_at = datetime.now(timezone.utc)

        while True:
            if self._abort_event.is_set() or self._stop_requested:
                # A stop supersedes steering: drop it and tell its sender no.
                self.discard_pending_steering()
                yield Done(text="", stop_reason="cancelled")
                return

            background_events = self._background_result_events()
            if background_events:
                for background_event in background_events:
                    yield background_event
                continue

            # The threshold is a preparation boundary, not a hard cut. The reserved window
            # gives the agent room for one private recording batch before the compaction happens.
            should_compact = self._should_compact(pending_user_message)
            if should_compact and self._compaction_control.idle:
                self._begin_compaction_preparation(reason="auto", resume_after=True)
            if (
                self._compaction_control.waiting
                and self._compaction_control.preparation_token is None
            ):
                self._compaction_control.preparation_token = (
                    await self._compaction_preparation.baseline()
                )
                self._mark_session_dirty()
            if (
                self._compaction_control.waiting
                and self._compaction_control.preparation_token is not None
            ):
                if await self._compaction_preparation.completed(
                    self._compaction_control.preparation_token
                ):
                    # The write may have committed just before a process stopped or a checkpoint
                    # was persisted. Its revision is the durable acknowledgement; do not ask the
                    # model to repeat a side effect merely because the in-memory state was lost.
                    self._record_compaction_preparation()
            if self._compaction_control.waiting and not self._compaction_control.started:
                # The indicator opens when the recording handoff begins, not when the compaction finally
                # runs: preparation is the long phase, and a session restart must not drop it.
                self._compaction_control.started = True
                self._mark_session_dirty()
                yield CompactionStarted(
                    reason=self._compaction_control.reason,
                    messages_before=len(self._without_compaction_preparation(self._conversation)),
                    tokens_before=conversation_tokens(self._conversation),
                )
            if self._compaction_control.recorded:
                compaction_reason = self._compaction_control.reason
                try:
                    self._observation_registry_metadata = (
                        await self._compaction_preparation.describe()
                    )
                except Exception as error:  # noqa: BLE001 — the compaction verification below remains authoritative
                    self.note_observation_registry({}, str(error) or type(error).__name__)
                async for compaction_event in self.compact(reason=compaction_reason):
                    yield compaction_event
                if self.compaction_failure:
                    # The send was already accepted. Keep its user message in the durable
                    # conversation, but do not continue into a model call until retry succeeds.
                    if pending_user_message is not None:
                        self._conversation.append(pending_user_message)
                        pending_user_message = None
                    return
                if stop_after_compaction:
                    return
                continue

            # The person's words join the request exactly once, after any needed compaction.
            if pending_user_message is not None and not self._compaction_control.waiting:
                self._conversation.append(pending_user_message)
                pending_user_message = None

            # Steering accepted while a new message was waiting belongs after that message. During
            # preparation it remains queued until the private segment has compacted away.
            if not self._compaction_control.waiting:
                for steering_event in await self._drain_steering_messages():
                    yield steering_event

                registry_error = self._take_observation_registry_feedback()
            else:
                registry_error = ""

            messages = self._build_turn_messages()
            if registry_error:
                # Feedback is request-local: it reaches the next model opening but never becomes
                # user history or a stale reminder after another process repairs the registry.
                messages.append(
                    self._reminder_message(
                        self._prompt_loader.load(
                            "observation_registry_error", {"error": registry_error}
                        )
                    )
                )

            # The model call: yields the stream and hands back the assembled response, or a terminal condition.
            call = _ModelCallOutcome()
            try:
                async for event in self._stream_model_call(messages, call):
                    yield event
            except (ContextWindowExceeded, ProviderContextWindowExceeded) as error:
                overflow = (
                    error
                    if isinstance(error, ContextWindowExceeded)
                    else ContextWindowExceeded(
                        "The provider rejected the assembled request as larger than its context window.",
                        model=self.model_identifier,
                        context_window=self._context_window,
                    )
                )
                # A refusal is itself a measurement, so the indicator stops reporting the last successful call's reading.
                window = overflow.context_window or self._context_window
                if window > 0:
                    self._context_window = window
                    self._latest_context_tokens = max(
                        self._latest_context_tokens, overflow.tokens or window
                    )
                if not self._compaction_control.recorded:
                    self._compaction_control.reason = "overflow"
                    self._compaction_control.resume_after = True
                    for compaction_event in self._fail_compaction_preparation(
                        "The provider exhausted the context window before compaction preparation could run."
                    ):
                        yield compaction_event
                    return
                async for compaction_event in self.compact(reason="overflow"):
                    yield compaction_event
                if self.compaction_failure:
                    return
                # The same accepted turn retries against the newly compacted conversation;
                # asking the user to resend would duplicate it in frontend and backend state.
                continue
            if call.cancelled:
                # Steering queued behind a cancelled read is superseded by the Stop: answer its
                # sender now rather than leaving the accepted future pending forever.
                self.discard_pending_steering()
                # The user's message was already appended before the provider call. Close the
                # exchange explicitly so the next request cannot inherit a dangling instruction
                # and finish the work that Stop just canceled. Keep whatever partial response the
                # provider already saw, or the prefix cache would be invalidated by the stop.
                self._conversation.append(
                    call.response
                    if call.response is not None
                    else AIMessage(content="", additional_kwargs={"langmesh_cancelled": True})
                )
                self._record_turn(
                    recorded_user_message, turn_tool_calls_log, turn_tool_results_log, ""
                )
                await self._record_transcript_turn(
                    recorded_user_message,
                    "",
                    "canceled",
                    turn_tool_calls_log,
                    turn_started_at,
                )
                return
            if call.aborted_for_steering:
                for steering_event in await self._drain_steering_messages():
                    yield steering_event
                continue
            response = call.response

            usage_event = self._accumulate_usage(response)
            if usage_event is not None:
                yield usage_event

            # LangChain serializes invalid tool calls as `tool_calls`, so each must still get a tool message.
            for invalid in response.invalid_tool_calls:
                if not invalid.get("id"):
                    invalid["id"] = f"call_invalid_{uuid.uuid4().hex[:24]}"

            # Nothing to call: retry a malformed batch, answer agents, or finish the turn.
            if not response.tool_calls:
                step = _StepOutcome()
                async for event in self._finalize_no_tool_calls(
                    response,
                    recorded_user_message,
                    turn_tool_calls_log,
                    turn_tool_results_log,
                    step,
                    turn_started_at,
                    response_nudged,
                ):
                    yield event
                if self.compaction_failure:
                    if pending_user_message is not None:
                        self._conversation.append(pending_user_message)
                        pending_user_message = None
                    return
                if step.directive == _STOP:
                    return
                continue

            # Calls to make: run the batch behind its checkpoint, then honour a Stop that landed during it.
            preparing_compaction = self._compaction_control.waiting
            def _valid_preparation_call(call: dict) -> bool:
                if call.get("name") == "bash":
                    return not str((call.get("args") or {}).get("location") or "").strip() and not bool(
                        (call.get("args") or {}).get("background")
                    )
                # Read-only skill loading is part of the handoff protocol; it cannot mutate anything.
                return call.get("name") == "load_skill"

            preparation_call_is_valid = bool(response.tool_calls) and all(
                _valid_preparation_call(call) for call in response.tool_calls
            )
            if preparing_compaction and not preparation_call_is_valid:
                self._conversation.append(response)
                refusal = self._prompt_loader.load("compaction_preparation_violation", {})
                for call_data in response.tool_calls:
                    identifier = str(call_data.get("id") or "")
                    yield Error(
                        id=identifier,
                        tool=str(call_data.get("name") or ""),
                        code="compaction_preparation_violation",
                        message=refusal,
                    )
                    self._conversation.append(ToolMessage(content=refusal, tool_call_id=identifier))
                for event in self._fail_compaction_preparation(refusal):
                    yield event
                if pending_user_message is not None:
                    self._conversation.append(pending_user_message)
                return
            step = _StepOutcome()
            async for event in self._run_tool_batch(
                response,
                recorded_user_message,
                turn_tool_calls_log,
                turn_tool_results_log,
                step,
            ):
                yield event
            if self._compaction_control.waiting:
                token = self._compaction_control.preparation_token
                if token is None:
                    raise RuntimeError("compaction preparation has no durable baseline")
                if await self._compaction_preparation.completed(token):
                    self._record_compaction_preparation()
            if self.compaction_failure:
                if pending_user_message is not None:
                    self._conversation.append(pending_user_message)
                    pending_user_message = None
                return
            if self._compaction_control.recorded:
                # The successful recording call is the terminal action of this model segment.
                # The next loop iteration compacts and resumes the already-accepted work.
                continue
            if preparing_compaction:
                # Inspection, repair, and recording may need several foreground Bash batches.
                # The segment ends only when a valid revision advances or the model stops.
                continue
            if step.directive == _STOP:
                return
            if step.directive == _CONTINUE:
                continue

            for steering_event in await self._drain_steering_messages():
                yield steering_event

    def _build_turn_messages(self) -> list:
        """This iteration's messages: the static prompt and the whole conversation."""
        system_message = SystemMessage(content=self._build_static_system_prompt())
        conversation = self._conversation
        if self._compaction_control.waiting:
            # Normally the output reserve leaves the complete conversation enough room for
            # its recording handoff. A restored or provider-rejected oversized session is the
            # exceptional case: give the handoff the largest recent view that can actually run,
            # while retaining the untouched full conversation until the compaction commits.
            preparation_budget = self._usable_context() - message_tokens(system_message)
            if conversation_tokens([system_message, *conversation]) > self._usable_context():
                handoff = conversation[-1:]
                older_budget = preparation_budget - sum(
                    message_tokens(message) for message in handoff
                )
                conversation = [
                    *self._tail_within_budget(conversation[:-1], older_budget),
                    *handoff,
                ]
        return [system_message, *conversation]

    def _refuse_if_over_window(self, messages: list) -> None:
        """Refuse a request that cannot fit before sending it, with numbers, since the harness knows the window."""
        window = self._context_window
        if self._context_window_estimated:
            return  # an estimate may schedule a safe compaction, but it must never impersonate a provider limit
        tokens = conversation_tokens(messages)
        if not over_context_window(tokens, window):
            return
        # Recorded, so the indicator agrees with the refusal rather than the last call that succeeded.
        self._latest_context_tokens = tokens
        raise ContextWindowExceeded(
            "The assembled request is larger than this model's context window.",
            model=self.model_identifier,
            context_window=window,
            tokens=tokens,
        )

    async def _stream_model_call(
        self, messages: list, outcome: _ModelCallOutcome
    ) -> AsyncIterator[TurnEvent]:
        """One streamed model call, writing the assembled response or a terminal condition into ``outcome``."""
        response_chunks: list[AIMessageChunk] = []
        # Calls already announced from the stream, so the completed message does not announce them twice.
        announced_tool_calls: set[str] = set()
        # What each call looks like so far: its id by stream index, its name, its raw argument text, and what was shown.
        streaming_call_ids: dict[Any, str] = {}
        streaming_call_names: dict[str, str] = {}
        streaming_call_args: dict[str, str] = {}
        streaming_call_shown: dict[str, dict] = {}
        # When each call last redrew, so a fast provider cannot redraw a row faster than a screen can show it.
        streaming_call_drawn_at: dict[str, float] = {}
        aborted_for_steering = False
        # A generation span, started rather than made current so it is safe to hold across yields.
        generation_span = _telemetry.start_span(
            "gen_ai.generation", {"gen_ai.request.model": self.model_identifier}
        )
        # A hook may read the conversation about to leave the process, and may change it.
        if not self._hooks.empty:
            messages = await self._hooks.before_model(messages)
        self._refuse_if_over_window(messages)
        # This is the truthful phase boundary: hooks and local validation have completed, and
        # the next awaited operation starts the provider stream. The client opens its Thinking
        # row from this status, so no optimistic or timer-driven model activity is fabricated.
        thinking_started_at = time.monotonic()
        thinking_done_emitted = False
        yield Status(code="awaiting_model")
        # The compaction checkpoint is a protocol capability: even a profile that omits ordinary shell
        # access must be able to maintain its workspace-owned registry inside the same sandbox.
        bound_model = self._bound_model
        model_stream = None
        abort_waiter = None
        cache_scope = ExitStack()
        try:
            if self._compaction_control.waiting:
                cache_scope.enter_context(cache_lane("compaction"))
            model_stream = bound_model.astream(messages)
            abort_waiter = asyncio.ensure_future(self._abort_event.wait())
            silence_limit = active_tuning().duration(Tunable.model_silence_give_up)
            progress_deadline = asyncio.get_running_loop().time() + silence_limit
            pending_chunk = None
            while True:
                if self._abort_event.is_set() or self._stop_requested:
                    # A real stop: drop the pending read and stop consuming the stream.
                    if pending_chunk is not None:
                        pending_chunk.cancel()
                        with suppress(BaseException):
                            await pending_chunk
                        pending_chunk = None
                    # Keep the chunks the provider already saw, so the next request's prefix stays cache-stable.
                    if response_chunks:
                        outcome.response = add_ai_message_chunks(
                            response_chunks[0], *response_chunks[1:]
                        )
                        outcome.response.additional_kwargs["langmesh_cancelled"] = True
                    yield Done(text="", stop_reason="cancelled")
                    outcome.cancelled = True
                    return
                if self._has_queued_steering():
                    # Steering waits at a semantic boundary; never mid-tool-call, or the call would be orphaned.
                    if any(
                        getattr(chunk, "tool_call_chunks", None)
                        or getattr(chunk, "tool_calls", None)
                        or getattr(chunk, "invalid_tool_calls", None)
                        for chunk in response_chunks
                    ):
                        pass
                    else:
                        # Only a text prefix has been shown, so the steering can replace it here.
                        aborted_for_steering = True
                        break

                if pending_chunk is None:
                    pending_chunk = asyncio.ensure_future(_stream_next(model_stream))
                completed, _ = await asyncio.wait(
                    {pending_chunk, abort_waiter},
                    timeout=max(0.0, progress_deadline - asyncio.get_running_loop().time()),
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if not completed:
                    pending_chunk.cancel()
                    with suppress(BaseException):
                        await pending_chunk
                    pending_chunk = None
                    raise TimeoutError(
                        f"The model stream made no meaningful progress for {silence_limit:g} seconds."
                    )
                if pending_chunk not in completed:
                    continue  # the top of the loop decides what this interrupt means
                chunk = pending_chunk.result()
                pending_chunk = None
                if chunk is _STREAM_EXHAUSTED:
                    break
                if not _chunk_advances_model_response(chunk):
                    continue
                progress_deadline = asyncio.get_running_loop().time() + silence_limit
                response_chunks.append(chunk)
                # A call is announced the moment the model names it, and again as each argument finishes:
                # writing a large one is seconds, and the turn would otherwise show nothing for all of them.
                for named in getattr(chunk, "tool_call_chunks", None) or []:
                    identifier = (named or {}).get("id") or streaming_call_ids.get(
                        (named or {}).get("index")
                    )
                    name = (named or {}).get("name") or streaming_call_names.get(identifier, "")
                    if not identifier:
                        continue
                    streaming_call_ids[(named or {}).get("index")] = identifier
                    if name:
                        streaming_call_names[identifier] = name
                    if name and identifier not in announced_tool_calls:
                        announced_tool_calls.add(identifier)
                        if not thinking_done_emitted:
                            thinking_done_emitted = True
                            yield ThinkingDone(
                                duration_milliseconds=int(
                                    (time.monotonic() - thinking_started_at) * 1000
                                )
                            )
                        yield ToolCall(
                            name=name,
                            arguments=None,
                            id=identifier,
                            arguments_complete=False,
                        )
                    fragment = (named or {}).get("args") or ""
                    if not fragment or identifier not in announced_tool_calls:
                        continue
                    streaming_call_args[identifier] = (
                        streaming_call_args.get(identifier, "") + fragment
                    )
                    settled = parse_partial_json(streaming_call_args[identifier]) or {}
                    if isinstance(settled, dict):
                        # Only what has finished arriving, so no argument is ever drawn while it is still growing.
                        settled = settled_arguments(settled, streaming_call_args[identifier])
                    if (
                        isinstance(settled, dict)
                        and settled
                        and settled != streaming_call_shown.get(identifier)
                    ):
                        # Redrawn on the same clock the text uses, since both end as one row on one screen.
                        drawn = time.monotonic()
                        if drawn - streaming_call_drawn_at.get(identifier, 0.0) < 0.016667:
                            continue
                        streaming_call_drawn_at[identifier] = drawn
                        streaming_call_shown[identifier] = settled
                        yield ToolCall(
                            name=streaming_call_names.get(identifier, ""),
                            arguments=settled,
                            id=identifier,
                            arguments_complete=False,
                        )
                for content_delta in message_content_deltas(chunk):
                    if content_delta.kind == "text":
                        if not thinking_done_emitted:
                            thinking_done_emitted = True
                            yield ThinkingDone(
                                duration_milliseconds=int(
                                    (time.monotonic() - thinking_started_at) * 1000
                                ),
                            )
                        yield TextChunk(
                            text=content_delta.text,
                            block_id=content_delta.block_identifier,
                        )
                    else:
                        yield Thinking(
                            text=content_delta.text,
                            block_id=content_delta.block_identifier,
                        )
        finally:
            cache_scope.close()
            _telemetry.end_span(generation_span)
            if abort_waiter is not None:
                abort_waiter.cancel()
            # Close the HTTP stream, so an aborted turn never leaks a provider connection.
            with suppress(BaseException):
                stream_closer = getattr(model_stream, "aclose", None) if model_stream else None
                if stream_closer is not None:
                    await stream_closer()
        # A tool-only turn produces no answer text, so close the phase here.
        if not thinking_done_emitted:
            yield ThinkingDone(
                duration_milliseconds=int((time.monotonic() - thinking_started_at) * 1000),
            )
        if aborted_for_steering:
            if response_chunks:
                # These chunks were already visible in the chat. Persist that exact assistant
                # prefix before the steering message, or replay would reorder what the user saw.
                self._conversation.append(
                    add_ai_message_chunks(response_chunks[0], *response_chunks[1:])
                )
            outcome.aborted_for_steering = True
            return
        outcome.response = (
            add_ai_message_chunks(response_chunks[0], *response_chunks[1:])
            if response_chunks
            else AIMessageChunk(content="")
        )

    async def _finalize_no_tool_calls(
        self,
        response: AIMessageChunk,
        recorded_user_message: str,
        turn_tool_calls_log: list[dict],
        turn_tool_results_log: list[dict],
        step: _StepOutcome,
        started_at: datetime,
        nudged: list[bool],
    ) -> AsyncIterator[TurnEvent]:
        """Handle a response with no tool calls: retry a malformed batch, deliver agent messages, or finish."""
        if response.invalid_tool_calls:
            # Only malformed calls: a ToolMessage would be orphaned, so they are corrected by a reminder.
            if response.content:
                self._conversation.append(response)
            for invalid in response.invalid_tool_calls:
                self._conversation.append(
                    self._reminder_message(
                        self._invalid_tool_call_content(cast(dict, invalid)),
                    )
                )
            step.directive = _CONTINUE
            return

        if self._compaction_control.waiting:
            self._conversation.append(response)
            # Best-effort handoff: the summary is the durable memory, so an unadvanced registry must not block the fold.
            self._record_compaction_preparation()
            step.directive = _CONTINUE
            return

        # No tool calls, so the turn ends: the resume pump wakes the agent when the next result lands.
        final_text = message_text(response)
        self._conversation.append(response)
        # A response with no prose (thinking only) is a no-op: prompt the model once to actually
        # answer, so the exchange cannot end silently. A second no-op ends the turn for real.
        if not final_text and not nudged[0]:
            nudged[0] = True
            self._conversation.append(
                self._reminder_message(self._prompt_loader.load("response_required", {}))
            )
            step.directive = _CONTINUE
            return
        steering_events = await self._drain_steering_messages()
        if steering_events:
            for steering_event in steering_events:
                yield steering_event
            step.directive = _CONTINUE
            return
        # An unfinished goal does not hold the turn open: the goal is durable, and a later turn picks it up.
        self._record_turn(
            recorded_user_message,
            turn_tool_calls_log,
            turn_tool_results_log,
            final_text,
        )
        await self._record_transcript_turn(
            recorded_user_message,
            final_text,
            "completed",
            turn_tool_calls_log,
            started_at,
        )
        yield Done(text=final_text, stop_reason="completed")
        step.directive = _STOP

    async def _rerun_answered(
        self,
        tool_calls,
        turn_tool_calls_log,
        turn_tool_results_log,
        outcomes,
        decisions,
    ):
        """Run the calls a retry answer settled, once the rest of the batch has finished."""
        pending = [
            call
            for call in tool_calls
            if (decision := decisions.get(call["id"])) is not None and decision.completed is None
        ]
        if not pending:
            return
        async for event in self._drain_tool_batch(
            pending,
            turn_tool_calls_log,
            turn_tool_results_log,
            outcomes,
            decisions,
        ):
            yield event

    async def _run_tool_batch(
        self,
        response: AIMessageChunk,
        recorded_user_message: str,
        turn_tool_calls_log: list[dict],
        turn_tool_results_log: list[dict],
        step: _StepOutcome,
    ) -> AsyncIterator[TurnEvent]:
        """Run the tool batch behind a durable checkpoint, with every permission resolved before any tool runs."""
        self._conversation.append(response)
        tool_calls = cast(list[dict], response.tool_calls)
        outcomes: dict[str, dict] = {}
        if not self._abort_event.is_set() and not self._stop_requested:
            plans, pending = await self._preflight_permissions(tool_calls)
            gates = [SuspensionGate(**gate.to_dict()) for gate in pending]
            # Automatic-mode gates are announced first, then weighed by the reviewer, so the call
            # is visible while the decision is pending instead of appearing only once it is made.
            auto_gates = [gate for gate in pending if gate.automatic_review]
            reviewed: dict[str, str] = {}
            if auto_gates:
                yield PermissionReviewing(
                    interactions=[SuspensionGate(**gate.to_dict()) for gate in auto_gates]
                )
                for gate in auto_gates:
                    reviewed[gate.request_id] = await self._review_auto_gate(gate)
                gates = [gate for gate in gates if not gate.automatic_review]
            interactive_answers = await self._answer_gates(gates)
            answered = {**reviewed, **interactive_answers}
            if gates and len(interactive_answers) < len(gates):
                # One suspend event per turn, and a durable pause, so a session waiting on a person survives a restart.
                yield Suspended(
                    interactions=[gate for gate in gates if gate.request_id not in answered],
                    plans={tool_call_id: plan.to_dict() for tool_call_id, plan in plans.items()},
                )
                step.directive = _STOP
                return
            else:
                decisions = self._resolve_tool_decisions(plans, answered)
            # After the barrier: a hook sees only what the rules approved, so it can drop calls but never add one.
            if not self._hooks.empty:
                tool_calls = await self._hooks.before_tools(tool_calls)
            retries: list[_PreflightGate] = []
            async for event in self._drain_tool_batch(
                tool_calls,
                turn_tool_calls_log,
                turn_tool_results_log,
                outcomes,
                decisions,
            ):
                if isinstance(event, RetryRequested):
                    # Never yielded onward: a client draws its prompt from the suspension below.
                    gate = self.retry_gate(
                        tool_call_id=event.id,
                        command=event.command,
                        denial=Denial(kind=event.denial_kind, evidence=event.denial_evidence),
                        explanation=event.explanation,
                    )
                    gate.refused_result = event.result
                    gate.tool_name = "bash"
                    gate.arguments = {"command": event.command, "explanation": event.explanation}
                    retries.append(gate)
                    continue
                yield event
            if retries:
                # Some of the batch ran: completed calls carry their result so the resumed batch replays rather than re-runs.
                for tool_call_id, plan in plans.items():
                    plan.gates = []
                    plan.refusal = None
                    plan.completed = (
                        {"result": outcomes[tool_call_id].get("content")}
                        if tool_call_id in outcomes
                        else None
                    )
                for gate in retries:
                    plan = plans[gate.tool_call_id]
                    plan.completed = None
                    plan.gates = [gate]
                gates = [SuspensionGate(**gate.to_dict()) for gate in retries]
                answered = await self._answer_gates(gates)
                if len(answered) < len(gates):
                    yield Suspended(
                        interactions=[gate for gate in gates if gate.request_id not in answered],
                        plans={
                            tool_call_id: plan.to_dict() for tool_call_id, plan in plans.items()
                        },
                    )
                    step.directive = _STOP
                    return
                async for event in self._rerun_answered(
                    tool_calls,
                    turn_tool_calls_log,
                    turn_tool_results_log,
                    outcomes,
                    self._resolve_tool_decisions(plans, answered),
                ):
                    yield event
        self._append_tool_results(response, outcomes)
        if self._resource_sync is not None:
            await self._resource_sync()
        yield Checkpoint()

        if self._abort_event.is_set() or self._stop_requested:
            self._record_turn(recorded_user_message, turn_tool_calls_log, turn_tool_results_log, "")
            yield Done(text="", stop_reason="cancelled")
            step.directive = _STOP
