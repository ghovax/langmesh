"""The goal-review plugin: the goal's own state, its durable review, and the linked reviewer session.

The goal belongs to this plugin, never to the runtime core the other plugins attach to: the
runtime only delegates `goal`, `write`, the review, and the allowance bookkeeping here. The
reviewer is an isolated session with its own visible transcript, an explicit investigative
tool allowlist, and its verdict tool; its prompts and schema descriptions live beside this module.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Literal, Optional

from pydantic import ValidationError

from langmesh.base.configuration import PermissionEvaluator
from langmesh.base.configuration.configuration import GoalReviewConfiguration
from langmesh.base.content.prompts import PackagePromptLoader
from langmesh.base.contracts.ports import GoalReviewContext, GoalReviewOutcome
from langmesh.base.primitives.identifiers import new_id
from langmesh.base.primitives.serialization import compact
from langmesh.runtime.internals import await_interruptible
from langmesh.runtime.cache_trace import cache_lane
from langmesh.runtime.plugins.goal_review.goal import Goal
from langmesh.runtime.turn_events import (
    Done,
    GoalReviewFinished,
    GoalReviewProgress,
    GoalReviewStarted,
    TurnEvent,
)
from langmesh.runtime.features import Feature, PluginContext, PluginHost
from langmesh.runtime.features import BackgroundCapability
from langmesh.runtime.plugins.continuation import Continuation
from langmesh.runtime.plugins.goal_review.models import GoalReview
from langmesh.runtime.plugins.goal_review.tools import (
    described_update_goal,
    submit_goal_review,
)
from langmesh.runtime.composition import RuntimeComponents, RuntimeProfile
from langmesh.runtime.runtime import AgentRuntime

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class GoalReviewState:
    """The goal currently owned by the goal-review plugin."""

    goal: Goal | None = None

    @classmethod
    def from_data(cls, value: object) -> "GoalReviewState":
        """Validate a goal-review state from its storage representation."""
        if isinstance(value, cls):
            return value
        if not isinstance(value, Mapping):
            return cls()
        stored = value.get("goal")
        if isinstance(stored, Goal):
            return cls(stored.model_copy(deep=True))
        if isinstance(stored, Mapping) and str(stored.get("text", "")).strip():
            try:
                return cls(Goal.model_validate(stored))
            except ValidationError:
                logger.warning("discarding a stored goal that no longer validates")
        return cls()


#: A goal's schema descriptions and its instructions, both configurable beside this plugin.
_DESCRIPTIONS = PackagePromptLoader(Path(__file__).parent / "prompts")

#: The investigative tools a reviewer may inherit from its parent session.
_REVIEWER_TOOLS = frozenset(
    {
        "bash",
        "fetch_url",
        "list_mcp_resources",
        "list_mcp_tools",
        "list_sessions",
        "load_skill",
        "read_mcp_resource",
        "read_session",
        "read_turn",
        "search_web",
        "stop_tool_call",
        "read_paths",
    }
)


class GoalReviewFeature(Feature):
    """The session's goal and, when configured, the isolated review that decides where it stands."""

    def __init__(self, *, journal: Any = None) -> None:
        self._journal = journal
        self._goal: Optional[Goal] = None
        self._listener: Optional[Callable[[Optional[Goal]], None]] = None
        self._submitted: Optional[GoalReview] = None
        self._live_reviewer: Any = None

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

    def compose_context(self, context: dict) -> None:
        """The goal as the model sees it, never the bookkeeping around it."""
        context["goal"] = self.goal.for_model() if self.goal is not None else {}

    @property
    def settlement(self) -> str:
        """Who settles a `satisfied` or `blocked` mark for this session."""
        context = getattr(self, "_context", None)
        if context is None:
            return GoalReviewConfiguration.REVIEWER
        return context.global_configuration.goal_review.settlement

    def contribute_tools(self) -> list:
        """The goal and review tools this plugin owns."""
        return [described_update_goal(settlement=self.settlement), submit_goal_review]

    def apply_agent_mark(self) -> Optional[Goal]:
        """Take the working agent's claimed status as the settlement, with no secondary review."""
        goal = self.goal
        if goal is None:
            return None
        claimed = goal.pending_review
        if claimed not in (Goal.SATISFIED, Goal.BLOCKED):
            return goal
        self.write(
            goal.updated(
                status=claimed,
                pending_review=None,
                review_message=None,
                review_id=None,
                blocker=None if claimed == Goal.SATISFIED else goal.blocker,
                evidence=None if claimed == Goal.BLOCKED else goal.evidence,
            )
        )
        return self.goal

    def terminate_tool_call(self, tool_call_id: str) -> bool:
        """Stop a tool still running inside the live reviewer session, if any."""
        reviewer = self._live_reviewer
        if reviewer is None:
            return False
        return bool(reviewer.abort_tool(tool_call_id))

    def snapshot(self) -> GoalReviewState:
        return GoalReviewState(self.goal.model_copy(deep=True) if self.goal is not None else None)

    def restore(self, state: object) -> None:
        restored = GoalReviewState.from_data(state)
        self._goal = restored.goal.model_copy(deep=True) if restored.goal is not None else None

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

    def goal_continuation_message(self, goal: Optional[Goal]) -> str:
        """The goal's own continuation reminder: work is still pending, so the session should keep going."""
        return self._prompts.load(
            "goal_continuation_note", {"goal": goal.text if goal is not None else ""}
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

    def _goal_reviewer(self):
        reviewer_configuration = self._context.agent_configuration.model_copy(
            update={"permission_mode": "automatic"}
        )
        reviewer_global_configuration = self._context.global_configuration.model_copy(
            update={
                "toolbox": self._context.global_configuration.toolbox.model_copy(
                    update={"enabled": False}
                )
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
            writable=(),
            network=granted_sandbox.network,
            workspace=self._context.working_directory,
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
                application_tools=[submit_goal_review],
                tool_gate=self._host.tools.tool_gate,
                permissions=reviewer_permissions,
                # A reviewer inherits only the investigative tools this plugin names explicitly.
                available_tools=tuple(
                    tool for tool in self._host.tools.model_tools if tool.name in _REVIEWER_TOOLS
                ),
                related_turns=self._host.tools.turn_reader,
                features=[
                    feature_class()
                    for feature_class in self._host.turn.feature_classes(Continuation)
                ],
            ),
            conversation=list(self._host.conversation.messages),
        )
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
            if await await_interruptible(review_turn, self._host.turn.abort_event, reviewer.abort):
                return False
            return True
        finally:
            if not review_turn.done():
                reviewer.abort()
                review_turn.cancel()
                with suppress(asyncio.CancelledError):
                    await review_turn

    async def _open_goal_review_journal(self, review_id: str, assignment: str, goal: Goal) -> None:
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
        reviewer.retain_tools([review_tool])

    async def review(
        self, publish: Callable[[TurnEvent], Awaitable[None]] | None = None
    ) -> Optional[GoalReview]:
        """Run the linked reviewer until it submits a verdict or the parent turn is cancelled."""
        goal = self.goal
        if goal is None or (not goal.is_open and not goal.pending_review):
            return None
        # A marked status is a claim the secondary review confirms or overrides; it is told
        # what the session asserted so it audits that rather than reviewing in a vacuum.
        claimed_status = goal.pending_review if goal.pending_review else ""
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
                "claimed_status": claimed_status,
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

        async def close_transcript(
            status: Literal["completed", "canceled", "failed"], review: GoalReview | None = None
        ) -> None:
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

        reviewer = self._goal_reviewer()
        self._live_reviewer = reviewer
        try:
            from langmesh.runtime.verdict import drive_verdict_session

            async def _run_turn(instruction: str) -> bool:
                ran = await self._run_goal_review_turn(reviewer, instruction, review_id, publish)
                if not ran:
                    await close_transcript("canceled")
                return ran

            def _submitted():
                feature = reviewer._features.by_type(GoalReview)
                return feature.submitted if feature is not None else None

            async def _on_success(review):
                review._review_id = review_id
                await close_transcript("completed", review)

            def _on_empty(attempt: int) -> None:
                logger.warning(
                    "the goal reviewer stopped without submitting its verdict (attempt %d); continuing it",
                    attempt,
                )

            # No cap: the reviewer is asked again until it submits correctly, which is the model's
            # own job — a hard limit on how often it may get it wrong only sets a price on honesty.
            review = await drive_verdict_session(
                run_turn=_run_turn,
                submitted=_submitted,
                require_submission=lambda: self._require_review_submission(reviewer),
                missing_instruction=lambda: self._prompts.load("goal_review_missing", {}),
                aborted=lambda: self._host.turn.abort_event.is_set(),
                initial_instruction=instructions,
                on_success=_on_success,
                on_empty=_on_empty,
            )
            if review is not None:
                return review
        except asyncio.CancelledError:
            await close_transcript("canceled")
            raise
        except Exception:
            await close_transcript("failed")
            raise
        finally:
            self._live_reviewer = None
            reviewer.abort()
            background = reviewer.features.capability(BackgroundCapability)
            runner = background.runner if background is not None else None
            if runner is not None:
                runner.cancel_all()
            if not transcript_finished:
                await close_transcript("canceled")
        return None

    def apply(self, review: Optional[GoalReview]) -> Optional[Goal]:
        """Write the verdict onto the goal and answer with it, so the caller reads one value rather than two."""
        goal = self.goal
        if goal is None or (not goal.is_open and not goal.pending_review):
            return goal
        if review is None:
            logger.warning("the goal review did not land; the claimed status is not confirmed")
            # An unverified claim is no verdict: return the goal to work so it is not silently settled.
            self.write(
                goal.updated(
                    status=Goal.ACTIVE,
                    review_message=None,
                    review_id=None,
                    pending_review=None,
                )
            )
            return self.goal
        if review.standing == "satisfied":
            self.write(
                goal.updated(
                    status=Goal.SATISFIED,
                    blocker=None,
                    evidence=review.evidence,
                    review_message=None,
                    review_id=None,
                    pending_review=None,
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
                    pending_review=None,
                )
            )
            return self.goal
        # unmet: the reviewer overrides any status the session claimed, returning the goal to work.
        self.write(
            goal.updated(
                status=Goal.ACTIVE,
                blocker=None,
                evidence=None,
                review_message=review.message,
                review_id=review._review_id,
                pending_review=None,
            )
        )
        return self.goal


__all__ = ["GoalReview", "GoalReviewFeature"]
