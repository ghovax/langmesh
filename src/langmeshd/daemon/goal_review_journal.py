"""Product adapter that records core goal-review events as the host's A2A transcript."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from a2a.types import Message, Part, Role, Task, TaskState, TaskStatus, TextPart
from a2a.utils import new_task

from langmesh.base.primitives.identifiers import new_id
from langmesh.base.contracts.ports import GoalReviewContext, GoalReviewOutcome
from langmeshd.worker.host import HostServices, NullHostServices
from langmeshd.worker.sink import _TurnEventSink


@dataclass
class _OpenReview:
    task: Task
    sink: _TurnEventSink


class HostGoalReviewJournal:
    """Translate the library's typed review stream into the product's durable and live lanes."""

    def __init__(
        self, turn_store: Any, model_identifier: Callable[[], str], host: Any = None
    ) -> None:
        self._turn_store = turn_store
        self._model_identifier = model_identifier
        self._reviews: dict[str, _OpenReview] = {}
        self._host: HostServices = host if host is not None else NullHostServices()

    async def open(self, context: GoalReviewContext) -> None:
        message = Message(
            role=Role.user,
            parts=[Part(root=TextPart(text=context.assignment))],
            message_id=new_id("message"),
            context_id=context.review_id,
        )
        task = new_task(message)
        task.status = TaskStatus(
            state=TaskState.working,
            timestamp=context.created_at.isoformat(),
        )
        await self._turn_store.create_goal_review(
            context.review_id,
            context.session_id,
            context.goal,
            context.created_at.isoformat(),
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
                    context_id=context.review_id,
                ),
            ]
            if publish_stream_event:
                await self._turn_store.save_goal_review(
                    context.session_id, context.review_id, task, part
                )
            else:
                await self._turn_store.save(task)

        def emit_delta(channel: str, block_id: str, text: str) -> None:
            self._host.publish_delta(context.review_id, channel, block_id, text)

        async def no_op() -> None:
            return None

        async def reject_suspension(_interactions, _plans) -> bool:
            return False

        self._reviews[context.review_id] = _OpenReview(
            task=task,
            sink=_TurnEventSink(
                emit=emit,
                emit_delta=emit_delta,
                save_conversation=no_op,
                suspend=reject_suspension,
                telemetry_span=None,
                model_identifier=self._model_identifier,
            ),
        )

    async def append(self, review_id: str, event: Any) -> None:
        review = self._reviews.get(review_id)
        if review is None:
            raise RuntimeError(f"Goal review {review_id!r} has no open journal.")
        await review.sink.handle(event)

    async def close(self, outcome: GoalReviewOutcome) -> None:
        review = self._reviews.pop(outcome.review_id, None)
        if review is None:
            return
        await review.sink.flush()
        try:
            state = TaskState(outcome.status)
        except ValueError:
            state = TaskState.failed
        review.task.status = TaskStatus(
            state=state,
            timestamp=outcome.completed_at.isoformat(),
        )
        await self._turn_store.save(review.task)
        await self._turn_store.finish_goal_review(
            outcome.session_id,
            outcome.review_id,
            outcome.status,
            outcome.standing,
            outcome.completed_at.isoformat(),
        )


__all__ = ["HostGoalReviewJournal"]
