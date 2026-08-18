"""The goal-review plugin: the goal's own state, its durable review, and the linked reviewer session.

The goal belongs to this plugin, never to the runtime core the other plugins attach to: the
runtime only delegates `goal`, `write`, the review, and the allowance bookkeeping here. The
reviewer is an isolated session with its own visible transcript and only its verdict tool; its
prompts and the verdict schema's descriptions live beside this module so they are configurable.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Awaitable, Callable, Optional

from pydantic import ValidationError

from langmesh.base.configuration import PermissionEvaluator, PromptLoader
from langmesh.base.contracts.ports import GoalReviewContext, GoalReviewOutcome
from langmesh.base.primitives.identifiers import new_id
from langmesh.base.primitives.serialization import compact
from langmesh.base.contracts.tools import ToolGrant
from langmesh.runtime.internals import race_interrupt
from langmesh.runtime.cache_trace import cache_lane
from langmesh.runtime.goal import Goal
from langmesh.runtime.turn_events import (
    Done,
    GoalReviewFinished,
    GoalReviewProgress,
    GoalReviewStarted,
    TurnEvent,
)
from langmesh.runtime.features import Feature, PluginContext, PluginHost
from langmesh.runtime.features.plugins.continuation import Continuation
from langmesh.runtime.features.plugins.goal_review.models import GoalReview
from langmesh.runtime.features.plugins.goal_review.tools import (
    submit_goal_review,
    update_goal,
)
from langmesh.runtime.composition import RuntimeComponents, RuntimeProfile
from langmesh.runtime.runtime import AgentRuntime

logger = logging.getLogger(__name__)

#: A goal's schema descriptions and its instructions, both configurable beside this plugin.
_DESCRIPTIONS = PromptLoader(Path(__file__).parent / "prompts")

#: Where a goal stands after one reading of the work, which is not the same as what the session says about it.
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


class GoalReviewFeature(Feature):
    """The session's goal and the isolated review that decides where it stands."""


    def __init__(self, *, journal: Any = None) -> None:
        self._journal = journal
        self._goal: Optional[Goal] = None
        self._listener: Optional[Callable[[Optional[Goal]], None]] = None
        self._submitted: Optional[GoalReview] = None

    def attach(self, context: PluginContext, host: PluginHost) -> None:
        self._context = context
        self._host = host
        self._prompts = context.prompts("goal_review")

    @property
    def goal(self) -> Optional[Goal]:
        """The session's goal, or ``None`` when it has none."""
        return self._goal

    @property
    def submitted(self) -> Optional[GoalReview]:
        """The verdict a reviewer session has handed over, read once the review turn ends."""
        return self._submitted

    def set_listener(self, listener: Optional[Callable[[Optional[Goal]], None]]) -> None:
        """Install the callback that hears every goal change, which is how the interface learns of one."""
        self._listener = listener

    def invoke(self, name: str, *args, **kwargs):
        """Answer the goal capabilities the core and tools ask for by name."""
        if name == "goal_current":
            return self._goal
        if name == "goal_write":
            (goal,) = args
            self.write(goal)
            return True
        if name == "submit_goal_review":
            (review,) = args
            self.submit(review)
            return True
        return None

    def compose_context(self, context: dict) -> None:
        """The goal as the model sees it, never the bookkeeping around it."""
        context["goal"] = self.goal.for_model() if self.goal is not None else {}

    def contribute_tools(self) -> list:
        """The goal and review tools this plugin owns."""
        return [update_goal, submit_goal_review]

    def snapshot(self) -> dict | None:
        return {"goal": self.goal.model_dump() if self.goal is not None else None}

    def restore(self, snapshot: dict) -> None:
        stored = snapshot.get("goal")
        goal = None
        if isinstance(stored, dict) and str(stored.get("text", "")).strip():
            try:
                goal = Goal.model_validate(stored)
            except ValidationError:
                logger.warning("discarding a stored goal that no longer validates")
        self._goal = goal

    def write(self, goal: Optional[Goal]) -> None:
        """Set, replace or drop the goal, and announce it. The single writer, so no path changes it silently."""
        self._goal = goal
        self._host.bookkeeping.note_state_changed()
        if self._listener is not None:
            self._listener(goal)

    def note_continuation(self) -> None:
        """Count one review-opened turn and consume the message that opened it."""
        if self._goal is None:
            return
        self.write(
            self._goal.updated(
                continuations=self._goal.continuations + 1,
                review_message=None,
                review_id=None,
            )
        )

    def restore_allowance(self) -> None:
        """A person spoke, so the allowance restarts and a parked goal resumes. A settled one keeps its answer."""
        goal = self._goal
        if goal is None or goal.status not in (Goal.ACTIVE, Goal.PARKED):
            return
        if goal.continuations == 0 and goal.status == Goal.ACTIVE and not goal.review_message:
            return
        self.write(
            goal.updated(
                continuations=0,
                status=Goal.ACTIVE,
                review_message=None,
                review_id=None,
            )
        )

    def park(self) -> None:
        """Stop working the goal until a person speaks. The goal is kept: it is still what the session is for."""
        if self._goal is None or not self._goal.is_open:
            return
        self.write(self._goal.updated(status=Goal.PARKED, review_message=None, review_id=None))

    def submit(self, review: Optional[GoalReview]) -> None:
        """The reviewer's verdict tool lands here, so the review loop reads one submission slot."""
        self._submitted = review

    def _goal_reviewer(self, scratch_directory: str):
        reviewer_configuration = self._context.agent_configuration.model_copy(
            update={"permission_mode": "automatic"}
        )
        reviewer_global_configuration = self._context.global_configuration.model_copy(
            update={
                "toolbox": self._context.global_configuration.toolbox.model_copy(update={"enabled": False})
            }
        )
        reviewer_permissions = PermissionEvaluator(
            reviewer_configuration.model_copy(
                update={
                    "tools": reviewer_configuration.tools.model_copy(
                        update={
                            "bash": reviewer_configuration.tools.bash.model_copy(
                                update={"background_allowed": False}
                            )
                        }
                    )
                }
            )
        )
        granted_sandbox = self._host.boundary.granted_profile()
        reviewer_sandbox = granted_sandbox.narrowed(
            writable=(scratch_directory,),
            network=granted_sandbox.network,
            workspace=self._context.working_directory,
        )
        reviewer_sandbox = replace(
            reviewer_sandbox,
            environment={
                **reviewer_sandbox.environment,
                "TMPDIR": scratch_directory,
                "XDG_CACHE_HOME": scratch_directory,
            },
        )
        toolbox = self._host.tools.tool_context.toolbox
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
        reviewer = AgentRuntime(
            RuntimeProfile(
                agent=reviewer_configuration,
                configuration=reviewer_global_configuration,
                session_id=self._context.session_id,
                working_directory=self._context.working_directory,
                project_directory=self._context.project_directory,
                permission_mode="automatic",
                parent_session=self._context.parent_session,
                sandbox=reviewer_sandbox,
            ),
            RuntimeComponents(
                model=self._host.conversation.model,
                catalogue=self._context.catalogue,
                sessions=None,
                mcp_servers=self._host.tools.tool_context.mcp_server_manager,
                # The verdict tool is injected here and only here: the main session never carries it.
                tools=[ToolGrant(submit_goal_review)],
                supplied_tool_gate=self._host.tools.supplied_tool_gate,
                permissions=reviewer_permissions,
                # What the parent's model sees, minus the tools a reviewer must never use.
                toolset=tuple(
                    tool
                    for tool in self._host.tools.model_tools
                    if tool.name not in _REVIEWER_DISABLED_TOOLS
                ),
                related_turns=self._host.tools.turn_reader,
                features=[
                    feature_class()
                    for feature_class in self._host.turn.feature_classes(Continuation)
                ],
            ),
            conversation=list(self._host.conversation.messages),
        )
        reviewer._locations = dict(self._host.boundary.locations)
        reviewer._locations_by_name = dict(self._host.boundary.locations_by_name)
        reviewer._tool_context = replace(reviewer._tool_context, toolbox=toolbox)
        reviewer.restore_session(self._host.bookkeeping.session_snapshot())
        reviewer._cached_system_prompt = self._host.turn.build_static_system_prompt()
        reviewer._attached_files = dict(self._host.boundary.attached_files)
        return reviewer

    async def _run_goal_review_turn(
        self,
        reviewer,
        instruction: str,
        review_id: str,
        publish: Callable[[TurnEvent], Awaitable[None]] | None,
    ) -> bool:
        async def run() -> None:
            with cache_lane(f"goal-review/{review_id}"):
                async for event in reviewer.stream(
                    instruction, as_system_note=False, opens_exchange=True
                ):
                    if isinstance(event, Done):
                        reviewer._last_review_text = event.text
                    if publish is not None:
                        await publish(GoalReviewProgress(review_id=review_id, event=event))
                    if self._journal is not None:
                        await self._journal.append(review_id, event)

        review_turn = asyncio.create_task(run())
        try:
            if await race_interrupt(review_turn, self._host.turn.abort_event):
                reviewer.abort()
                await review_turn
                return False
            return True
        finally:
            if not review_turn.done():
                reviewer.abort()
                review_turn.cancel()
                with suppress(asyncio.CancelledError):
                    await review_turn

    async def _open_goal_review_journal(
        self, review_id: str, assignment: str, goal: Goal
    ) -> None:
        if self._journal is None:
            return
        await self._journal.open(
            GoalReviewContext(
                review_id=review_id,
                session_id=self._context.session_id,
                goal=goal.text,
                assignment=assignment,
                created_at=datetime.now(timezone.utc),
            )
        )

    async def _close_goal_review_journal(
        self, review_id: str, status: str, standing: str | None
    ) -> None:
        if self._journal is None:
            return
        await self._journal.close(
            GoalReviewOutcome(
                review_id=review_id,
                session_id=self._context.session_id,
                status=status,
                standing=standing,
                completed_at=datetime.now(timezone.utc),
            )
        )

    @staticmethod
    def _require_review_submission(reviewer) -> None:
        """Constrain a reviewer that already investigated to its one accepted verdict tool, including what the model is bound to."""
        review_tool = next(tool for tool in reviewer.constrained_tool_named("submit_goal_review"))
        reviewer.constrain_toolset([review_tool])

    async def review(
        self, publish: Callable[[TurnEvent], Awaitable[None]] | None = None
    ) -> Optional[GoalReview]:
        """Run the linked reviewer until it submits a verdict or the parent turn is cancelled."""
        goal = self.goal
        if goal is None or not goal.is_open:
            return None
        instructions = self._prompts.load(
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
            },
        )
        review_id = new_id("review")
        assignment = self._prompts.load(
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
        await self._open_goal_review_journal(review_id, assignment, goal)
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

        async def finish_transcript(status: str, review: GoalReview | None = None) -> None:
            nonlocal transcript_finished
            if transcript_finished:
                return
            transcript_finished = True
            await self._close_goal_review_journal(
                review_id, status, review.standing if review is not None else None
            )
            if publish is not None:
                await publish(
                    GoalReviewFinished(
                        review_id=review_id,
                        status=status,
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
                from langmesh.runtime.verdict import drive_verdict_session

                maximum_attempts = self._context.global_configuration.goal_review.maximum_attempts

                async def _run_turn(instruction: str) -> bool:
                    ran = await self._run_goal_review_turn(
                        reviewer, instruction, review_id, publish
                    )
                    if not ran:
                        await finish_transcript("canceled")
                    return ran

                def _submitted():
                    feature = reviewer._features.by_type(GoalReview)
                    return feature.submitted if feature is not None else None

                async def _on_success(review):
                    review._review_id = review_id
                    await finish_transcript("completed", review)

                def _on_empty(attempt: int, maximum: int) -> None:
                    logger.warning(
                        "the goal reviewer stopped without submitting its verdict (attempt %d/%d); continuing it",
                        attempt,
                        maximum,
                    )

                async def _on_exhausted():
                    logger.error(
                        "goal reviewer did not submit after %d attempts; last text: %r",
                        maximum_attempts,
                        getattr(reviewer, "_last_review_text", ""),
                    )
                    await finish_transcript("failed")

                review = await drive_verdict_session(
                    attempts=maximum_attempts,
                    reason=f"goal review {review_id}",
                    run_turn=_run_turn,
                    submitted=_submitted,
                    require_submission=lambda: self._require_review_submission(reviewer),
                    missing_instruction=lambda: self._prompts.load("goal_review_missing", {}),
                    aborted=lambda: self._host.turn.abort_event.is_set(),
                    initial_instruction=instructions,
                    on_success=_on_success,
                    on_empty=_on_empty,
                    on_exhausted=_on_exhausted,
                )
                if review is not None:
                    return review
            except asyncio.CancelledError:
                await finish_transcript("canceled")
                raise
            except Exception:
                await finish_transcript("failed")
                raise
            finally:
                reviewer.abort()
                runner = reviewer.features.invoke("background")
                if runner is not None:
                    runner.cancel_all()
                if not transcript_finished:
                    await finish_transcript("canceled")
        return None

    def apply(self, review: Optional[GoalReview]) -> Optional[Goal]:
        """Write the verdict onto the goal and answer with it, so the caller reads one value rather than two."""
        goal = self.goal
        if goal is None or not goal.is_open:
            return goal
        if review is None:
            logger.warning("the goal review did not land; carrying the goal on unchanged")
            self.write(goal.updated(review_message=None, review_id=None))
            return self.goal
        if review.standing == "satisfied":
            self.write(
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
            self.write(
                goal.updated(
                    status=Goal.BLOCKED,
                    blocker=review.blocker,
                    evidence=None,
                    review_message=None,
                    review_id=None,
                )
            )
            return self.goal
        self.write(
            goal.updated(
                blocker=None,
                evidence=None,
                review_message=review.message,
                review_id=review._review_id,
            )
        )
        return self.goal


__all__ = ["GoalReview", "GoalReviewFeature"]
