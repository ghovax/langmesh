"""The linked agent session that independently reviews a goal."""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Awaitable, Callable, Literal, Optional

from a2a.types import Message, Part, Role, Task, TaskState, TaskStatus, TextPart
from a2a.utils import new_task
from pydantic import BaseModel, Field, PrivateAttr, model_validator

from langmesh.base.configuration import PermissionEvaluator, PromptLoader
from langmesh.base.identifiers import new_id
from langmesh.base.serialization import compact
from langmesh.base.tuning import Tunable, active_tuning
from langmesh.protocol.events import ToolStatus
from langmesh.runtime.goal import Goal, NonBlankText
from langmesh.runtime.turn_events import (
    GoalReviewFinished,
    GoalReviewProgress,
    GoalReviewStarted,
    ToolResult,
    TurnEvent,
)
from langmesh.worker.sink import _TurnEventSink


logger = logging.getLogger(__name__)
_DESCRIPTIONS = PromptLoader(Path(__file__).parent / "descriptions")

#: Where a goal stands after one reading of the work, which is not the same as what the session says about it.
GOAL_STANDING = Literal["unmet", "satisfied", "blocked"]
GOAL_CONTRACT = Literal["complete", "needs_revision"]
_REVIEWER_DISABLED_TOOLS = frozenset(
    {
        "ask_user",
        "control_screen",
        "create_session",
        "download_file",
        "message_remote_agent",
        "message_session",
        "set_tasks",
        "update_goal",
        "update_tasks",
    }
)


class GoalReview(BaseModel):
    """One reading of an open goal: where it stands, and what the session is told to do about it."""

    # Evidence precedes the verdict so the decision follows from the review instead of leading it.
    assessment: NonBlankText = Field(
        description=_DESCRIPTIONS.load("goal_review_assessment", {}).strip()
    )
    unmet: list[NonBlankText] = Field(
        default_factory=list,
        description=_DESCRIPTIONS.load("goal_review_unmet", {}).strip(),
    )
    evidence: NonBlankText | None = Field(
        default=None,
        description=_DESCRIPTIONS.load("goal_review_evidence", {}).strip(),
    )
    blocker: NonBlankText | None = Field(
        default=None,
        description=_DESCRIPTIONS.load("goal_review_blocker", {}).strip(),
    )
    goal_contract: GOAL_CONTRACT = Field(
        description=_DESCRIPTIONS.load("goal_review_goal_contract", {}).strip()
    )
    standing: GOAL_STANDING = Field(
        description=_DESCRIPTIONS.load("goal_review_standing", {}).strip()
    )
    message: NonBlankText | None = Field(
        default=None,
        description=_DESCRIPTIONS.load("goal_review_message", {}).strip(),
    )
    _review_id: str = PrivateAttr("")

    @model_validator(mode="after")
    def _carry_what_the_verdict_rests_on(self):
        """A verdict without what establishes it is not a verdict, so the pass is retried rather than believed."""
        if self.standing == "satisfied":
            if self.unmet:
                raise ValueError("A satisfied goal has nothing unmet.")
            if self.goal_contract != "complete":
                raise ValueError("A satisfied goal needs a complete contract.")
            if self.blocker is not None:
                raise ValueError("A satisfied goal has no blocker.")
            if self.message is not None:
                raise ValueError("A satisfied goal opens no continuation message.")
            if self.evidence is None:
                raise ValueError(
                    "A satisfied goal needs the evidence that proves each requirement."
                )
            return self
        if self.evidence is not None:
            raise ValueError("Only a satisfied goal carries completion evidence.")
        if not self.unmet and self.goal_contract == "complete":
            raise ValueError(
                "A goal that is not satisfied has an unmet requirement or needs a stronger contract."
            )
        if self.goal_contract == "needs_revision" and self.standing != "unmet":
            raise ValueError("A goal the session can revise is unmet, not satisfied or blocked.")
        if self.standing == "blocked":
            if self.blocker is None:
                raise ValueError("A blocked goal needs what is in the way and what would clear it.")
            if self.message is not None:
                raise ValueError("A blocked goal opens no continuation message.")
            return self
        if self.blocker is not None:
            raise ValueError("Only a blocked goal carries a blocker.")
        if self.message is None:
            raise ValueError("An unmet goal needs the message that opens its next turn.")
        return self


class _ReviewsGoal:
    """Review an open goal in an isolated session with its own visible transcript."""

    async def _tool_submit_goal_review(
        self,
        tool_name: str,
        tool_arguments: dict,
        tool_call_identifier: str,
        decision: Any,
        policy: Any,
        resolved_location: Any,
    ):
        if not self._accepts_goal_review:
            result = {
                "code": "goal_review_unavailable",
                "status": ToolStatus.ERROR.value,
                "message": "This tool is available only to the internal goal reviewer.",
            }
        else:
            review = GoalReview.model_validate(tool_arguments)
            goal = self.goal
            blocked_available = goal is not None and goal.continuations >= active_tuning().amount(
                Tunable.goal_blocked_turns
            )
            if review.standing == "blocked" and not blocked_available:
                result = {
                    "code": "goal_review_blocked_too_soon",
                    "status": ToolStatus.ERROR.value,
                    "message": "Blocking is not available yet; submit unmet with the next review message.",
                }
            else:
                self._submitted_goal_review = review
                self._abort_event.set()
                result = {
                    "code": "goal_review_submitted",
                    "status": ToolStatus.OK.value,
                    "message": "The independent goal review was recorded.",
                }
        yield ToolResult(id=tool_call_identifier, name=tool_name, result=result)

    def _goal_reviewer(self, scratch_directory: str):
        from langmesh.runtime.runtime import AgentRuntime

        reviewer_configuration = self._agent_configuration.model_copy(
            update={"permission_mode": "automatic"}
        )
        reviewer_global_configuration = self._global_configuration.model_copy(
            update={
                "toolbox": self._global_configuration.toolbox.model_copy(update={"enabled": False})
            }
        )
        reviewer_permissions = PermissionEvaluator(
            reviewer_configuration.model_copy(
                update={
                    "tools": reviewer_configuration.tools.model_copy(
                        update={
                            "bash": reviewer_configuration.tools.bash.model_copy(
                                update={"background_allowed": False}
                            ),
                            "disabled": sorted(
                                set(reviewer_configuration.tools.disabled)
                                | _REVIEWER_DISABLED_TOOLS
                            ),
                        }
                    )
                }
            )
        )
        granted_sandbox = self._granted_profile()
        reviewer_sandbox = granted_sandbox.narrowed(
            writable=(scratch_directory,),
            network=granted_sandbox.network,
            workspace=self._working_directory,
        )
        reviewer_sandbox = replace(
            reviewer_sandbox,
            environment={
                **reviewer_sandbox.environment,
                "TMPDIR": scratch_directory,
                "XDG_CACHE_HOME": scratch_directory,
            },
        )
        toolbox = self._tool_context.toolbox
        if toolbox is not None:
            reviewer_sandbox = replace(
                reviewer_sandbox,
                filesystem=replace(
                    reviewer_sandbox.filesystem,
                    readable=tuple(
                        dict.fromkeys(reviewer_sandbox.filesystem.readable + (str(toolbox.root),))
                    ),
                ),
            )
        reviewer_tools = tuple(
            tool for tool in self._tools if tool.name not in _REVIEWER_DISABLED_TOOLS
        )
        reviewer = AgentRuntime(
            agent_configuration=reviewer_configuration,
            global_configuration=reviewer_global_configuration,
            session_id=self._session_id,
            conversation=list(self._conversation),
            working_directory=self._working_directory,
            project_directory=self._project_directory,
            permission_mode="automatic",
            locations=None,
            parent_session=self._parent_session,
            sandbox=reviewer_sandbox,
            session_access=None,
            mcp_manager=self._tool_context.mcp_manager,
            model=self._model,
            catalogue=self._catalogue,
            tools=tuple(self._extra_tools.values()),
            supplied_tool_gate=self._supplied_tool_gate,
            permissions=reviewer_permissions,
            toolset=reviewer_tools,
            accepts_goal_review=True,
        )
        reviewer._locations = dict(self._locations)
        reviewer._locations_by_name = dict(self._locations_by_name)
        reviewer._tool_context = replace(reviewer._tool_context, toolbox=toolbox)
        reviewer.restore_session(self.session_snapshot())
        reviewer._cached_system_prompt = self._build_static_system_prompt()
        reviewer._turn_reader = self._turn_reader
        reviewer._attached_files = list(self._attached_files)
        return reviewer

    async def _run_goal_review_turn(
        self,
        reviewer,
        instruction: str,
        sink: _TurnEventSink,
        review_id: str,
        publish: Callable[[TurnEvent], Awaitable[None]] | None,
    ) -> bool:
        async def run() -> None:
            async for event in reviewer.stream(
                instruction, as_system_note=True, opens_exchange=True
            ):
                if publish is not None:
                    await publish(GoalReviewProgress(review_id=review_id, event=event))
                await sink.handle(event)

        review_turn = asyncio.create_task(run())
        abort_wait = asyncio.create_task(self._abort_event.wait())
        try:
            finished, _ = await asyncio.wait(
                {review_turn, abort_wait}, return_when=asyncio.FIRST_COMPLETED
            )
            if abort_wait in finished and self._abort_event.is_set():
                reviewer.abort()
                await review_turn
                return False
            await review_turn
            return True
        finally:
            abort_wait.cancel()
            with suppress(asyncio.CancelledError):
                await abort_wait
            if not review_turn.done():
                reviewer.abort()
                review_turn.cancel()
                with suppress(asyncio.CancelledError):
                    await review_turn

    async def _goal_review_transcript(
        self, review_id: str, assignment: str, goal: Goal
    ) -> tuple[Task, _TurnEventSink]:
        created_at = datetime.now(timezone.utc).isoformat()
        message = Message(
            role=Role.user,
            parts=[Part(root=TextPart(text=assignment))],
            message_id=new_id("message"),
            context_id=review_id,
        )
        task = new_task(message)
        task.status = TaskStatus(state=TaskState.working, timestamp=created_at)
        if self._turn_store is not None:
            await self._turn_store.create_goal_review(
                review_id, self._session_id, goal.text, created_at
            )
            await self._turn_store.save(task)

        async def emit(part: Part, *, publish_stream_event: bool = True) -> None:
            task.history = [
                *(task.history or []),
                Message(
                    role=Role.agent,
                    parts=[part],
                    message_id=new_id("message"),
                    task_id=task.id,
                    context_id=review_id,
                ),
            ]
            if self._turn_store is not None and publish_stream_event:
                await self._turn_store.save_goal_review(self._session_id, review_id, task, part)
            elif self._turn_store is not None:
                await self._turn_store.save(task)

        def emit_delta(channel: str, block_id: str, text: str) -> None:
            # Review transcripts share the same bus, so their model deltas take the same direct lane.
            from langmesh.daemon import state

            state.event_bus.publish_delta(review_id, channel, block_id, text)

        async def save_conversation() -> None:
            return None

        async def suspend(_interactions, _plans) -> bool:
            return False

        sink = _TurnEventSink(
            emit=emit,
            emit_delta=emit_delta,
            save_conversation=save_conversation,
            suspend=suspend,
            telemetry_span=None,
            model_identifier=lambda: self.model_identifier,
        )
        return task, sink

    async def _finish_goal_review_transcript(
        self,
        task: Task,
        review_id: str,
        status: TaskState,
        standing: str | None,
    ) -> None:
        completed_at = datetime.now(timezone.utc).isoformat()
        task.status = TaskStatus(state=status, timestamp=completed_at)
        if self._turn_store is not None:
            await self._turn_store.save(task)
            await self._turn_store.finish_goal_review(
                self._session_id,
                review_id,
                status.value,
                standing,
                completed_at,
            )

    @staticmethod
    def _require_review_submission(reviewer) -> None:
        """Constrain a reviewer that already investigated to its one accepted verdict tool."""
        review_tool = next(tool for tool in reviewer._tools if tool.name == "submit_goal_review")
        reviewer._tools = [review_tool]
        reviewer._tool_schemas = {review_tool.name: review_tool.args_schema}
        reviewer._bound_model = reviewer._model.bind_tools(
            [review_tool], tool_choice="submit_goal_review"
        )

    async def review_goal(
        self, publish: Callable[[TurnEvent], Awaitable[None]] | None = None
    ) -> Optional[GoalReview]:
        """Run the linked reviewer until it submits a verdict or the parent turn is cancelled."""
        goal = self.goal
        if goal is None or not goal.is_open:
            return None
        goal = self.goal
        if goal is None or not goal.is_open:
            return None
        instructions = self._prompt_loader.load(
            "goal_review",
            {
                "goal_contract": compact(
                    {
                        "goal": goal.text,
                        "purpose": goal.purpose,
                        "minimum_conditions": goal.requirements,
                    }
                ),
                "previous_review_message": goal.review_message,
                "blocked_turns": active_tuning().amount(Tunable.goal_blocked_turns),
                "blocked_available": goal.continuations
                >= active_tuning().amount(Tunable.goal_blocked_turns),
            },
        )
        review_id = new_id("review")
        assignment = self._prompt_loader.load(
            "goal_review_assignment",
            {
                "goal_contract": compact(
                    {
                        "goal": goal.text,
                        "purpose": goal.purpose,
                        "minimum_conditions": goal.requirements,
                    }
                ),
            },
        )
        task, sink = await self._goal_review_transcript(review_id, assignment, goal)
        transcript_finished = False
        if publish is not None:
            await publish(
                GoalReviewStarted(
                    review_id=review_id,
                    goal=goal.text,
                    purpose=goal.purpose,
                    minimum_conditions=tuple(goal.requirements),
                )
            )

        async def finish_transcript(status: TaskState, review: GoalReview | None = None) -> None:
            nonlocal transcript_finished
            if transcript_finished:
                return
            transcript_finished = True
            await sink.flush()
            await self._finish_goal_review_transcript(
                task, review_id, status, review.standing if review is not None else None
            )
            if publish is not None:
                await publish(
                    GoalReviewFinished(
                        review_id=review_id,
                        status=status.value,
                        standing=review.standing if review is not None else None,
                        assessment=review.assessment if review is not None else None,
                        unmet=tuple(review.unmet) if review is not None else (),
                        evidence=review.evidence if review is not None else None,
                        blocker=review.blocker if review is not None else None,
                        contract_status=review.goal_contract if review is not None else None,
                        message=review.message if review is not None else None,
                    )
                )

        with TemporaryDirectory(prefix="langmesh-goal-review-") as scratch_directory:
            reviewer = self._goal_reviewer(scratch_directory)
            try:
                instruction = instructions
                while not self._abort_event.is_set():
                    if not await self._run_goal_review_turn(
                        reviewer, instruction, sink, review_id, publish
                    ):
                        await finish_transcript(TaskState.canceled)
                        return None
                    if reviewer._submitted_goal_review is not None:
                        review = reviewer._submitted_goal_review
                        review._review_id = review_id
                        await finish_transcript(TaskState.completed, review)
                        return review
                    logger.warning(
                        "the goal reviewer stopped without submitting its verdict; continuing it"
                    )
                    self._require_review_submission(reviewer)
                    instruction = self._prompt_loader.load("goal_review_missing", {})
            except asyncio.CancelledError:
                await finish_transcript(TaskState.canceled)
                raise
            except Exception:
                await finish_transcript(TaskState.failed)
                raise
            finally:
                reviewer.abort()
                reviewer.background_jobs.cancel_all()
                if not transcript_finished:
                    await finish_transcript(TaskState.canceled)
        return None

    def apply_goal_review(self, review: Optional[GoalReview]) -> Optional[Goal]:
        """Write the verdict onto the goal and answer with it, so the caller reads one value rather than two."""
        goal = self.goal
        if goal is None or not goal.is_open:
            return goal
        if review is None:
            logger.warning("the goal review did not land; carrying the goal on unchanged")
            self.write_goal(goal.updated(review_message=None, review_id=None))
            return self.goal
        if review.standing == "satisfied":
            self.write_goal(
                goal.updated(
                    status=Goal.SATISFIED,
                    blocker=None,
                    evidence=review.evidence,
                    review_message=None,
                    review_id=None,
                )
            )
            return self.goal
        if review.standing == "blocked":
            self.write_goal(
                goal.updated(
                    status=Goal.BLOCKED,
                    blocker=review.blocker,
                    evidence=None,
                    review_message=None,
                    review_id=None,
                )
            )
            return self.goal
        self.write_goal(
            goal.updated(
                blocker=None,
                evidence=None,
                review_message=review.message,
                review_id=review._review_id,
            )
        )
        return self.goal
