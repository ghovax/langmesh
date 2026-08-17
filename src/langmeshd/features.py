"""The daemon's plugin assembly: which plugins a hosted session runs and the ports they need.

Composition is the caller's choice, never a library default. This is the daemon's bundle,
built explicitly with the ports it owns. A session runs exactly these plugins and nothing
the library guessed.
"""

from __future__ import annotations

from typing import Any

from langmesh.base.persistence.observation_store import SQLiteObservationStore
from langmesh.runtime.compaction import ObservationCompactionPreparation
from langmesh.runtime.features.plugins.background import BackgroundJobsFeature
from langmesh.runtime.features.plugins.compaction import Compaction
from langmesh.runtime.features.plugins.computer_use import ComputerUse
from langmesh.runtime.features.plugins.continuation import Continuation
from langmesh.runtime.features.plugins.goal_review import GoalReviewFeature
from langmesh.runtime.features.plugins.observations import ObservationMemory
from langmesh.runtime.features.plugins.permission_reviewer import PermissionReviewer
from langmesh.runtime.features.plugins.permissions import PermissionReview
from langmesh.runtime.features.plugins.titling import TitleAssignment
from langmesh.runtime.features.plugins.work_habits import WorkHabits


def _compaction_preparation(global_configuration: Any, runtime_directory: str) -> Any:
    """The compaction preparation the daemon owns, from the observation store."""
    return ObservationCompactionPreparation(
        SQLiteObservationStore(
            global_configuration.observation_database_for(runtime_directory)
        )
    )


def compose_plugins(
    *,
    session_id: str,
    runtime_directory: str,
    configuration: Any,
    catalogue: Any,
    job_store: Any,
    goal_listener: Any,
    goal_review_journal: Any,
    global_configuration: Any,
) -> dict[str, Any]:
    """The plugins a hosted session runs and the ports they need, as one bundle."""
    reviewer = PermissionReviewer()
    preparation = _compaction_preparation(global_configuration, runtime_directory)
    features = [
        GoalReviewFeature(journal=goal_review_journal),
        Compaction(
            strategy=None,
            preparation=preparation,
            summarizer=None,
        ),
        PermissionReview(reviewer=reviewer),
        reviewer,
        Continuation(policy=None),
        ObservationMemory(),
        BackgroundJobsFeature(store=job_store),
        WorkHabits(),
        TitleAssignment(),
        ComputerUse(),
    ]
    # The goal plugin hears every goal change through the host's listener: that is how the
    # interface learns of one. The plugin owns the goal; the host owns the listener.
    for feature in features:
        if isinstance(feature, GoalReviewFeature):
            feature.set_listener(goal_listener)
    return {
        "features": features,
        "services": {
            "goal_review_journal": goal_review_journal,
            "compaction_preparation": preparation,
        },
    }


def contributed_tools() -> dict[str, Any]:
    """Every tool the composed plugins contribute, keyed by name."""
    reviewer = PermissionReviewer()
    features = [
        GoalReviewFeature(journal=None),
        Compaction(strategy=None, preparation=None, summarizer=None),
        PermissionReview(reviewer=reviewer),
        reviewer,
        Continuation(policy=None),
        ObservationMemory(),
        BackgroundJobsFeature(store=None),
        WorkHabits(),
        TitleAssignment(),
        ComputerUse(),
    ]
    tools: dict[str, Any] = {}
    for feature in features:
        for tool in (feature.contribute_tools() if hasattr(feature, "contribute_tools") else []):
            name = getattr(tool, "name", "")
            if name:
                tools[name] = tool
    return tools
