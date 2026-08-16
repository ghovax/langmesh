"""The feature seam: replaceable sub-behaviors, shipped as plugins with their own templates.

Each sub-behavior the harness once implemented on the runtime — goal review, folding, permission
review, autonomous continuation, the observational-memory ledger, background jobs — is one plugin:
its own module, its own state, its own prompt templates, and an explicit `FeatureServices` bundle
instead of the runtime itself. The runtime builds the plugins it was composed with and delegates;
a plugin is omitted by a `False` slot, supplied by the caller through a class, or shipped by the
library when a slot is `None`. The inventory lives here as classes, assembled by slot name the way
tool units are assembled from the registry.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from langmesh.base.configuration import PromptLoader


#: The one place a runtime names which sub-behaviors a session runs and of what kind.
_FEATURE_SLOTS = (
    "goal_review",
    "compaction",
    "permissions",
    "continuation",
    "observations",
    "background",
)


@dataclass(frozen=True)
class RuntimeFeatures:
    """Replaceable sub-behaviors: ``None`` ships the built-in, ``False`` omits it, a class runs instead."""

    goal_review: Any = None
    compaction: Any = None
    permissions: Any = None
    continuation: Any = None
    observations: Any = None
    background: Any = None

    def named(self, name: str) -> Any:
        return getattr(self, name)


class Features:
    """The sub-behaviors one runtime runs, each frozen to its own state."""

    def __init__(self, goal_review: Any = None, compaction: Any = None, permissions: Any = None,
                 continuation: Any = None, observations: Any = None, background: Any = None) -> None:
        self.goal_review = goal_review
        self.compaction = compaction
        self.permissions = permissions
        self.continuation = continuation
        self.observations = observations
        self.background = background

    def named(self, name: str) -> Any:
        return getattr(self, name)

    def present(self, name: str) -> bool:
        return getattr(self, name) is not None


@dataclass
class FeatureServices:
    """The runtime capabilities a feature may use, bundled so a feature never reaches into the runtime."""

    session_id: str
    parent_session: str
    working_directory: str
    project_directory: str
    agent_configuration: Any
    global_configuration: Any
    model: Any
    conversation: list
    catalogued_prompts: Callable[[str], PromptLoader]
    catalogue: Any
    tool_context: Any
    model_tools: list
    tool_schemas: dict
    supplied_tool_names: set
    supplied_tool_gate: str
    sandbox: Any
    locations: dict
    locations_by_name: dict
    context_window: int = 0
    latest_context_tokens: int = 0
    access_grants: list = field(default_factory=list)
    attached_files: dict = field(default_factory=dict)
    task_manager: Any = None
    goal: Any = None
    write_goal: Any = None
    turn_reader: Any = None
    background: Any = None
    job_store: Any = None
    reminder_message: Any = None
    compaction: Any = None
    compaction_preparation: Any = None
    compaction_summarizer: Any = None
    continuation_policy: Any = None
    goal_review_journal: Any = None
    set_latest_context_tokens: Any = None
    refresh_cached_prompt: Any = None
    abort_event: Any = None
    mark_dirty: Any = None
    record_event: Any = None
    discard_pending_steering: Any = None
    session_snapshot: Any = None
    restore_session: Any = None
    build_static_system_prompt: Any = None
    build_turn_messages: Any = None
    refuse_if_over_window: Any = None
    compaction_waiting: Any = None
    resolve_location: Any = None
    call_policy: Any = None
    granted_profile: Any = None


class _CataloguePromptLoader:
    """The shared core templates a feature falls back to when a name is not its own."""

    def __init__(self, catalogue: Any) -> None:
        self._catalogue = catalogue

    def load(self, template_name: str, variables: dict) -> str:
        return self._catalogue.prompt(template_name, dict(variables))


def feature_prompts(name: str, catalogue: Any) -> PromptLoader:
    """A plugin's own templates, behind the catalogue's overrides and in front of the shared set."""
    directory = Path(__file__).resolve().parent / name / "prompts"
    overrides = getattr(catalogue, "prompt_override", None)
    return PromptLoader(
        directory,
        overrides=overrides if overrides is not None else None,
        fallback=_CataloguePromptLoader(catalogue),
    )


#: The class each shipped plugin publishes for its slot, resolved on first build of a runtime.
_FEATURE_CLASSES = {
    "goal_review": "GoalReviewFeature",
    "compaction": "Compaction",
    "permissions": "PermissionReview",
    "continuation": "Continuation",
    "observations": "ObservationMemory",
    "background": "BackgroundJobsFeature",
}


def _builtin(name: str) -> type | None:
    """The library's shipped implementation for a slot, or ``None`` while that behavior still lives on the runtime."""
    try:
        module = __import__(f"langmesh.runtime.features.{name}", fromlist=[name])
    except ImportError:
        return None
    return getattr(module, _FEATURE_CLASSES[name], None)


def build_features(choice: RuntimeFeatures | None, services: FeatureServices) -> Features:
    """Assemble the runtime's sub-behaviors: a ``False`` slot is omitted, a class replaces the
    built-in, and a slot with no shipped implementation yet stays empty as the runtime migrates."""
    selected = choice if choice is not None else RuntimeFeatures()
    instances: dict[str, Any] = {}
    for name in _FEATURE_SLOTS:
        slot = selected.named(name)
        if slot is False:
            instances[name] = None
            continue
        feature_class = slot if slot is not None else _builtin(name)
        instances[name] = feature_class(services) if feature_class is not None else None
    return Features(**instances)


__all__ = [
    "FeatureServices",
    "Features",
    "RuntimeFeatures",
    "build_features",
    "feature_prompts",
]