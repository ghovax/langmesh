"""Typed access to the installed features, for the daemon's worker.

The runtime core never names a feature. The daemon's worker is the one that composed the
features, so it is the only place allowed to reach them by class. These functions are that
layer's handle: they resolve the installed feature of the relevant class and call it,
mirroring what used to live on the runtime so the harness reads the same way.
"""

from __future__ import annotations

from collections.abc import Sequence


def _resolve_feature(runtime, feature_type):
    """The installed feature of this class, or ``None`` when the profile ships without it."""
    return runtime.features.by_type(feature_type)


def goal(runtime):
    from langmesh.runtime.plugins.goal_review import GoalReviewFeature

    feature = _resolve_feature(runtime, GoalReviewFeature)
    return feature.goal if feature is not None else None


def set_goal_listener(runtime, listener) -> None:
    from langmesh.runtime.plugins.goal_review import GoalReviewFeature

    feature = _resolve_feature(runtime, GoalReviewFeature)
    if feature is not None:
        feature.set_listener(listener)


def write_goal(runtime, value) -> None:
    from langmesh.runtime.plugins.goal_review import GoalReviewFeature

    feature = _resolve_feature(runtime, GoalReviewFeature)
    if feature is not None:
        feature.write(value)


def note_goal_continuation(runtime) -> None:
    from langmesh.runtime.plugins.goal_review import GoalReviewFeature

    feature = _resolve_feature(runtime, GoalReviewFeature)
    if feature is not None:
        feature.note_continuation()


def restore_goal_allowance(runtime) -> None:
    from langmesh.runtime.plugins.goal_review import GoalReviewFeature

    feature = _resolve_feature(runtime, GoalReviewFeature)
    if feature is not None:
        feature.restore_allowance()


def park_goal(runtime) -> None:
    from langmesh.runtime.plugins.goal_review import GoalReviewFeature

    feature = _resolve_feature(runtime, GoalReviewFeature)
    if feature is not None:
        feature.park()


def review_goal(runtime, publish=None):
    from langmesh.runtime.plugins.goal_review import GoalReviewFeature

    feature = _resolve_feature(runtime, GoalReviewFeature)
    return feature.review(publish) if feature is not None else None


def apply_goal_review(runtime, review):
    from langmesh.runtime.plugins.goal_review import GoalReviewFeature

    feature = _resolve_feature(runtime, GoalReviewFeature)
    if feature is None:
        return goal(runtime)
    return feature.apply(review)


def goal_settlement(runtime) -> str:
    from langmesh.base.configuration.configuration import GoalReviewConfiguration
    from langmesh.runtime.plugins.goal_review import GoalReviewFeature

    feature = _resolve_feature(runtime, GoalReviewFeature)
    return feature.settlement if feature is not None else GoalReviewConfiguration.REVIEWER


def apply_agent_goal_mark(runtime):
    from langmesh.runtime.plugins.goal_review import GoalReviewFeature

    feature = _resolve_feature(runtime, GoalReviewFeature)
    if feature is None:
        return goal(runtime)
    return feature.apply_agent_mark()


def should_continue_goal(runtime) -> bool:
    from langmesh.runtime.plugins.continuation import Continuation

    feature = _resolve_feature(runtime, Continuation)
    return bool(feature is not None and feature.should_continue_goal(goal(runtime)))


def should_continue_tasks(runtime) -> bool:
    from langmesh.runtime.plugins.continuation import Continuation

    feature = _resolve_feature(runtime, Continuation)
    return bool(feature is not None and feature.should_continue_tasks(feature.actionable()))


def task_continuation_message(runtime) -> str:
    from langmesh.runtime.plugins.continuation import Continuation

    feature = _resolve_feature(runtime, Continuation)
    if feature is None:
        return ""
    return feature.task_continuation_message(feature.unfinished())


def goal_continuation_message(runtime) -> str:
    from langmesh.runtime.plugins.goal_review import GoalReviewFeature

    feature = _resolve_feature(runtime, GoalReviewFeature)
    if feature is None:
        return ""
    return feature.goal_continuation_message(goal(runtime))


def continuation_messages(runtime, *, segments: Sequence[str] = ()) -> list[str]:
    from langmesh.runtime.plugins.continuation import Continuation

    feature = _resolve_feature(runtime, Continuation)
    if feature is None:
        return [segment.strip() for segment in segments if segment.strip()]
    return feature.continuation_messages(segments=segments)


def unfinished_tasks(runtime) -> list:
    from langmesh.runtime.plugins.continuation import Continuation

    feature = _resolve_feature(runtime, Continuation)
    return feature.unfinished() if feature is not None else []


def has_actionable_tasks(runtime) -> bool:
    from langmesh.runtime.plugins.continuation import Continuation

    feature = _resolve_feature(runtime, Continuation)
    return bool(feature is not None and feature.actionable())


def compaction_failure(runtime):
    from langmesh.runtime.plugins.compaction import Compaction

    feature = _resolve_feature(runtime, Compaction)
    return feature.failure if feature is not None else None


def retry_compaction(runtime):
    from langmesh.runtime.plugins.compaction import Compaction

    feature = _resolve_feature(runtime, Compaction)
    return feature.retry() if feature is not None else None


def begin_compaction_preparation(runtime) -> bool:
    from langmesh.runtime.plugins.compaction import Compaction

    feature = _resolve_feature(runtime, Compaction)
    return bool(feature is not None and feature.begin_explicit())


def resumes_after_compaction(runtime) -> bool:
    from langmesh.runtime.plugins.compaction import Compaction

    feature = _resolve_feature(runtime, Compaction)
    return bool(feature is not None and feature.control.resume_after)


def pending_compaction_reason(runtime) -> str:
    from langmesh.runtime.plugins.compaction import Compaction

    feature = _resolve_feature(runtime, Compaction)
    return feature.control.reason if feature is not None else "manual"


def awaiting_compaction_recording(runtime) -> bool:
    from langmesh.runtime.plugins.compaction import Compaction

    feature = _resolve_feature(runtime, Compaction)
    return bool(feature is not None and feature.control.waiting)


async def compact(runtime, reason: str = "manual"):
    from langmesh.runtime.plugins.compaction import Compaction

    feature = _resolve_feature(runtime, Compaction)
    if feature is None:
        return
    async for event in feature.compact(reason):
        yield event


def reconsider_gate(runtime, gate):
    from langmesh.runtime.plugins.permissions import PermissionReview

    feature = _resolve_feature(runtime, PermissionReview)
    if feature is None:
        return {}
    return feature.reconsider_gate(gate)


def note_observation_registry(runtime, metadata: dict, error: str | None = None) -> None:
    from langmesh.runtime.plugins.observations import ObservationMemory

    feature = _resolve_feature(runtime, ObservationMemory)
    if feature is not None:
        feature.note(metadata, error)


def background_jobs(runtime):
    from langmesh.runtime.plugins.background import BackgroundJobs

    feature = _resolve_feature(runtime, BackgroundJobs)
    return feature.runner if feature is not None else None


def has_pending_jobs(runtime) -> bool:
    runner = background_jobs(runtime)
    return bool(runner is not None and runner.has_pending())


def has_completed_undelivered_jobs(runtime) -> bool:
    runner = background_jobs(runtime)
    return bool(runner is not None and runner.has_completed_undelivered())


async def wait_for_jobs(runtime) -> None:
    runner = background_jobs(runtime)
    if runner is not None:
        await runner.wait_for_completion()


def inject_stored_background_result(runtime, **kwargs) -> None:
    from langmesh.runtime.plugins.background import BackgroundJobs

    feature = _resolve_feature(runtime, BackgroundJobs)
    if feature is not None:
        feature.inject_stored_result(**kwargs)


def background_snapshots(runtime) -> list:
    runner = background_jobs(runtime)
    return runner.active_snapshots() if runner is not None else []


def send_tool_to_background(runtime, tool_call_identifier: str) -> bool:
    runner = background_jobs(runtime)
    return bool(runner is not None and runner.request_background(tool_call_identifier))


__all__ = [
    "apply_agent_goal_mark",
    "apply_goal_review",
    "awaiting_compaction_recording",
    "background_jobs",
    "background_snapshots",
    "begin_compaction_preparation",
    "compact",
    "compaction_failure",
    "continuation_messages",
    "goal",
    "goal_continuation_message",
    "goal_settlement",
    "has_actionable_tasks",
    "has_completed_undelivered_jobs",
    "has_pending_jobs",
    "inject_stored_background_result",
    "note_goal_continuation",
    "note_observation_registry",
    "park_goal",
    "pending_compaction_reason",
    "reconsider_gate",
    "resumes_after_compaction",
    "restore_goal_allowance",
    "retry_compaction",
    "review_goal",
    "send_tool_to_background",
    "set_goal_listener",
    "should_continue_goal",
    "should_continue_tasks",
    "task_continuation_message",
    "unfinished_tasks",
    "wait_for_jobs",
    "write_goal",
]
