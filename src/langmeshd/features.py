"""The daemon's plugin assembly: which plugins a hosted session runs and the ports they need.

Composition is the caller's choice, never a library default. This is the daemon's bundle,
built explicitly with the ports it owns. A session runs exactly these plugins and nothing
the library guessed.
"""

from __future__ import annotations

from typing import Any

from langmeshd.daemon.persistence.observation_registry import SQLiteObservationStore
from langmesh.runtime.plugins.compaction import ObservationCompactionPreparation
from langmesh.runtime.plugins.background import BackgroundJobsFeature
from langmesh.runtime.plugins.bash import Bash
from langmesh.runtime.plugins.compaction import Compaction
from langmesh.runtime.plugins.computer_use import ComputerUse
from langmesh.runtime.plugins.continuation import Continuation
from langmesh.runtime.plugins.goal_review import GoalReviewFeature
from langmesh.runtime.plugins.interaction import Interaction
from langmesh.runtime.plugins.locations import Locations
from langmesh.runtime.plugins.observations import ObservationMemory
from langmesh.runtime.plugins.permission_reviewer import PermissionReviewer
from langmesh.runtime.plugins.permissions import PermissionReview
from langmesh.runtime.plugins.titling import TitleAssignment
from langmesh.runtime.plugins.web import Web
from langmesh.runtime.plugins.work_habits import WorkHabits
from langmesh.runtime.tools.arguments import with_shared_fields
from langmeshd.daemon.machine_environment import _shell_command_usage
from langmeshd.commons.configuration_locations import observation_database
from langmeshd.daemon.workflow_catalogue import FilesystemWorkflowCatalogue
from langmeshd.daemon.browser import browser_endpoint, save_browser_download
from langmeshd.daemon.scratch import FilesystemScratchSpaces
from langmeshd.commons.paths import runtime_directory


_WORKFLOWS = FilesystemWorkflowCatalogue()
_SCRATCH_SPACES = FilesystemScratchSpaces(runtime_directory() / "scratch")


def _compaction_preparation(global_configuration: Any, runtime_directory: str) -> Any:
    """The compaction preparation the daemon owns, from the observation store."""
    return ObservationCompactionPreparation(
        SQLiteObservationStore(observation_database(runtime_directory))
    )


def _session_locations(session_id: str) -> list[dict[str, Any]] | None:
    """The workspace's locations for a session, resolved by the daemon's own services."""
    from langmeshd.commons.services.locations import _resolve_session_locations

    return attach_location_executors(_resolve_session_locations(session_id))


def attach_location_executors(
    locations: list[dict[str, Any]] | None,
) -> list[dict[str, Any]] | None:
    """Attach daemon-owned executors to serialized workspace locations."""
    from langmesh.runtime.plugins.locations.resolver import LocationAddress, executor_for
    from langmeshd.commons.paths import ssh_control_directory

    for location in locations or []:
        address = LocationAddress(
            kind=str(location.get("kind") or "local"),
            base_directory=str(location.get("base_directory") or ""),
            host_alias=str(location.get("host_alias") or ""),
        )
        location["executor"] = executor_for(address, control_directory=ssh_control_directory())
    return locations


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
    locations = _session_locations(session_id)
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
        WorkHabits(_shell_command_usage()),
        TitleAssignment(),
        # The locations plugin is opt-in: it is composed only when the workspace has locations.
        *([Locations()] if locations else []),
        Bash(),
        Web(),
        Interaction(),
        ComputerUse(),
    ]
    # The goal plugin hears every goal change through the host's listener: that is how the
    # interface learns of one. The plugin owns the goal; the host owns the listener.
    for feature in features:
        if isinstance(feature, GoalReviewFeature):
            feature.set_listener(goal_listener)
    services: dict[str, Any] = {
        "goal_review_journal": goal_review_journal,
        "compaction_preparation": preparation,
        "workflows": _WORKFLOWS,
        "scratch_spaces": _SCRATCH_SPACES,
        "browser_endpoint": browser_endpoint,
        "browser_download": save_browser_download,
    }
    if locations:
        services["locations"] = locations
    return {
        "features": features,
        "services": services,
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
        Bash(),
        Web(),
        Interaction(),
        ComputerUse(),
    ]
    tools: dict[str, Any] = {}
    for feature in features:
        for tool in feature.contribute_tools() if hasattr(feature, "contribute_tools") else []:
            name = getattr(tool, "name", "")
            if name:
                tools[name] = with_shared_fields(tool)
    return tools
