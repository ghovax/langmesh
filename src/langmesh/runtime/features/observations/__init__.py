"""The observational-memory ledger: watcher metadata, its failure, and the feedback the model owes."""

from __future__ import annotations


from langmesh.base.serialization import compact
from langmesh.runtime.features.base import FeatureServices, feature_prompts


class ObservationMemory:
    """The registry's reported picture and what the model must be told when it is broken.

    A watcher reports its metadata and any schema failure. The picture is given to the model and
    to the memory panel; a changed failure is queued as request-local feedback so the next model
    opening hears about it exactly once, without it becoming user history.
    """

    def __init__(self, services: FeatureServices) -> None:
        self._services = services
        self._prompts = feature_prompts("observations", services.catalogue)
        self._metadata: dict = {}
        self._error: str | None = None
        self._pending_feedback: str | None = None

    @property
    def metadata(self) -> dict:
        """The registry's metadata, as the memory panel reads it."""
        return dict(self._metadata)

    def adopt(self, metadata: dict) -> None:
        """Replace the picture from compaction verification, which is authoritative on success."""
        self._metadata = dict(metadata)

    def note(self, metadata: dict, error: str | None = None) -> None:
        """Adopt watcher metadata and queue a changed schema failure for the next model opening."""
        normalized_error = error.strip() if error else None
        metadata_changed = metadata != self._metadata
        error_changed = normalized_error != self._error
        if not metadata_changed and not error_changed:
            return
        if metadata_changed:
            # The memory panel receives this revision immediately, while the model's static
            # prefix adopts it only at an explicit prompt refresh such as successful compacting.
            self._metadata = dict(metadata)
        self._error = normalized_error
        if error_changed:
            self._pending_feedback = normalized_error

    def take_feedback(self) -> str | None:
        """The one schema-failure note owed to the next model opening, if any."""
        message = self._pending_feedback
        self._pending_feedback = None
        return message

    def system_variable(self) -> str:
        """The prompt section carrying the registry's metadata to the model."""
        return self._prompts.load(
            "observational_memory", {"metadata": compact(self._metadata)}
        ).strip()

    def error_request_message(self, error: str) -> str:
        """The request-local note that tells the model its memory registry needs repair."""
        return self._prompts.load("observation_registry_error", {"error": error})