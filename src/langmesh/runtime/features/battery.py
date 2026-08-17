"""The battery the library ships: the default features a session runs, composed at the application layer.

Composition is the caller's job, so the core and the seam stay free of any feature name. The
library offers its features as ordinary classes and this ordinary list, bound to the caller's
ports, for a session that wants them.
"""

from __future__ import annotations

from langmesh.runtime.features.plugins.background import BackgroundJobsFeature
from langmesh.runtime.features.plugins.compaction import Compaction
from langmesh.runtime.features.plugins.continuation import Continuation
from langmesh.runtime.features.plugins.goal_review import GoalReviewFeature
from langmesh.runtime.features.plugins.observations import ObservationMemory
from langmesh.runtime.features.plugins.permission_reviewer import PermissionReviewer
from langmesh.runtime.features.plugins.permissions import PermissionReview
from langmesh.runtime.features.plugins.work_habits import WorkHabits


def default_features(components) -> list:
    """The shipped features for a session, with the caller's ports bound."""
    # The automatic permission evaluator is its own plugin; the boundary plugin calls it for a verdict.
    reviewer = PermissionReviewer()
    return [
        GoalReviewFeature(journal=components.goal_review_journal),
        Compaction(
            strategy=components.compaction,
            preparation=components.compaction_preparation,
            summarizer=components.compaction_summarizer,
        ),
        PermissionReview(reviewer=reviewer),
        reviewer,
        Continuation(policy=components.continuations),
        ObservationMemory(),
        BackgroundJobsFeature(store=components.jobs),
        WorkHabits(),
    ]


__all__ = ["default_features"]
