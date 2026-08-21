"""The observational-memory plugin: watcher metadata, its failure, and the feedback the model owes."""

from __future__ import annotations

from langmesh.base.primitives.serialization import compact
from langmesh.runtime.features import Feature, PluginContext, PluginHost
from langmesh.runtime.features.events import MemoryHandoffFailed, MemoryHandoffVerified


class ObservationMemory(Feature):
    """The registry's reported picture and what the model must be told when it is broken.

    A watcher reports its metadata and any schema failure. The picture is given to the model and
    to the memory panel; a changed failure is queued as request-local feedback so the next model
    opening hears about it exactly once, without it becoming user history.
    """

    def __init__(self) -> None:
        self._metadata: dict = {}
        self._error: str | None = None
        self._pending_feedback: str | None = None

    def attach(self, context: PluginContext, host: PluginHost) -> None:
        self._context = context
        self._host = host
        self._prompts = context.prompts("observations")
        context.bus.subscribe(MemoryHandoffVerified, self._on_handoff_verified)
        context.bus.subscribe(MemoryHandoffFailed, self._on_handoff_failed)

    def _on_handoff_verified(self, event) -> None:
        self.adopt(event.metadata)

    def _on_handoff_failed(self, event) -> None:
        self.note({}, event.error)

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
            # The memory panel receives this revision immediately, while the model's static prefix adopts it only at an explicit prompt refresh such as successful compacting.
            self._metadata = dict(metadata)
        self._error = normalized_error
        if error_changed:
            self._pending_feedback = normalized_error

    def take_feedback(self) -> str | None:
        """The one schema-failure note owed to the next model opening, if any."""
        message = self._pending_feedback
        self._pending_feedback = None
        return message

    def _has_shell(self) -> bool:
        """Whether the model is bound to a shell tool it could repair the registry with."""
        tools = getattr(getattr(self._host, "tools", None), "model_tools", None) or []
        return any(getattr(tool, "name", None) == "bash" for tool in tools)

    def compose_prompt(self, variables: dict[str, str]) -> None:
        """Place the current metadata in the cached prompt until an explicit refresh adopts a newer revision."""
        if self._metadata:
            variables["observational_memory"] = self._prompts.load(
                "observational_memory", {"metadata": compact(self._metadata)}
            ).strip()

    def prepare_request(self) -> None:
        """Append a changed registry failure durably at the request boundary where the model first sees it."""
        if self._host.turn.maintenance_active():
            return
        feedback = self.take_feedback()
        if not feedback:
            return
        note = self._prompts.load(
            "observation_registry_error"
            if self._has_shell()
            else "observation_registry_error_unrepairable",
            {"error": feedback},
        )
        reminder = self._host.turn.reminder_message(note.strip())
        self._host.conversation.messages.append(reminder)
        self._host.bookkeeping.note_state_changed()
