"""The public plugin seam: the hooks a feature implements and the dispatch the core calls.

The core is independent of every feature: it runs a plain model turn and, at a fixed set of
points, asks the installed features to participate. A feature implements the hooks it needs
and is never named by the core. Who composes features, in what order, and with which ports is
the caller's concern — not the core's and not this seam's.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, AsyncIterator, Sequence

from langmesh.base.configuration import PromptLoader
from langmesh.runtime import plugins as _plugins_package
from langmesh.runtime.features.context import PluginContext
from langmesh.runtime.features.host import PluginHost
from langmesh.runtime.turn_events import TurnEventUnion

plugins_package_root = Path(_plugins_package.__file__).resolve().parent


class Feature:
    """A pluggable sub-behavior. Implement any hook below; hooks you omit are no-ops.

    The turn loop calls each hook on every installed feature at the point in the turn where it
    can act. Hooks receive exactly what that point is about — the context, the request, the
    batch of calls — never the runtime itself.

    The runtime installs an instance by calling ``attach`` with the public `PluginContext` and,
    for the library's own features, the internal `PluginHost`. A caller's feature need not
    override ``attach``: it is constructed with whatever ports it declares and uses only the
    hooks and the context.
    """

    def attach(self, context: PluginContext, host: "PluginHost | None" = None) -> None:
        """Installed by the runtime; library features keep the internal view here."""

    def compose_context(self, context: dict) -> None:
        """Contribute to the turn's model-facing context, merging into ``context`` in place."""

    def contribute_tools(self) -> list:
        """The tools this feature provides to the session's roster, empty when it provides none."""
        return []

    def contribute_schema_fields(self, tool_name: str) -> dict:
        """Extra argument fields to add to a tool's schema, by tool name.

        A plugin that extends another tool's contract (the locations plugin adding a
        ``location`` selector to bash) answers with its fields here, so the explanation of an
        extra parameter lives with the plugin that contributes it.
        """
        return {}

    def invoke(self, name: str, *args, **kwargs):
        """Answer a named capability the core asks for by string, or ``None`` to pass.

        The core never names a plugin or a port: it asks for a capability by name and the
        first installed feature that answers it does. This is how a plugin exposes its
        services without the core knowing which plugin owns them.
        """
        return None

    def compose_prompt(self, variables: dict[str, str]) -> None:
        """Contribute named prompt sections, merging into ``variables`` in place."""

    async def assign_title(self, first_message: str) -> str | None:
        """A suggested title from this feature's own naming call, or ``None`` to leave it unnamed."""
        return None

    def prepare_request(self, messages: list) -> list:
        """Adjust the exact request about to leave; return the list to send."""
        return messages

    def should_maintain(self, request_tokens: int) -> bool:
        """Whether the loop should hold for this feature before admitting new input."""
        return False

    def maintenance_active(self) -> bool:
        """Whether this feature is currently holding the loop."""
        return False

    def begin_maintenance(self, *, reason: str, resume_after: bool) -> None:
        """Start holding the loop, preparing the durable handoff the fold needs."""

    async def advance_maintenance(self) -> AsyncIterator[TurnEventUnion]:
        """Advance the hold one step (record a handoff, announce a phase), yielding its events."""
        if False:
            yield None

    async def run_maintenance(self, *, reason: str) -> AsyncIterator[TurnEventUnion]:
        """Complete the hold and reclaim context, yielding the fold's events."""
        if False:
            yield None

    def valid_during_maintenance(self, call: dict) -> bool:
        """Whether a tool call may run while this feature holds the loop."""
        return True

    def maintenance_ready(self) -> bool:
        """Whether this feature's held handoff has completed and the fold may run."""
        return False

    def maintenance_reason(self) -> str:
        """The reason this feature is holding the loop, for the fold it runs."""
        return "manual"

    async def maintenance_describe(self) -> dict:
        """The durable handoff's verification to adopt before the fold runs."""
        return {}

    def maintenance_violation_message(self) -> str:
        """The refusal a model is given for calling outside the held loop's protocol."""
        return ""

    async def fail_maintenance(self, message: str) -> AsyncIterator[TurnEventUnion]:
        """The hold could not complete; fail it as a durable blocker, yielding its events."""
        if False:
            yield None

    def record_maintenance_handoff(self) -> None:
        """The model declined to act during the hold; the feature records its handoff."""

    def maintenance_tool_schemas(self) -> dict:
        """The tool schemas a held loop accepts, keyed by name, merged for validation."""
        return {}

    async def plan_tool_calls(self, tool_calls: list[dict]) -> tuple[dict, list] | None:
        """Plan one batch of calls: return ``(plans, gates)`` or ``None`` to not gate this batch.

        ``plans`` maps a tool-call id to its plan; ``gates`` are the interactions the calls
        raised. The concrete types are the gating plugin's own, so this seam leaves them open.
        """
        return None

    def resolve_gates(self, plans: dict, answers: dict) -> dict:
        """Turn the plans plus the answers into one verdict per call."""
        return {}

    async def review_automatic_gate(self, gate) -> Any | None:
        """The verdict a feature that gates decides for one automatic gate, or ``None`` to pass."""
        return None

    def drain(self) -> list[TurnEventUnion]:
        """The events this feature has finished producing since the last drain."""
        return []

    def blocks_input(self) -> str | None:
        """Why new input must be refused (a failed fold, an unrepaired registry), or ``None``."""
        return None

    def snapshot(self) -> dict | None:
        """This feature's durable state to persist beside the checkpoint, or ``None``."""
        return None

    def restore(self, snapshot: dict) -> None:
        """Rehydrate from the durable checkpoint, reading the keys this feature wrote."""


class Features:
    """The installed features of one runtime, and the dispatch the core calls at each point.

    The container knows nothing about any particular feature: it holds whatever was installed,
    in order, and calls the hooks each implements.
    """

    def __init__(self, instances: Sequence[Feature] = ()) -> None:
        self._instances = list(instances)

    @property
    def instances(self) -> list[Feature]:
        return self._instances

    def by_type(self, feature_type: type) -> Feature | None:
        """The installed feature of a given class, for a composer that wants its instance back."""
        return next(
            (feature for feature in self._instances if isinstance(feature, feature_type)), None
        )

    def invoke(self, name: str, *args, **kwargs):
        """The first installed feature that answers a named capability, in composer order.

        The core asks for capabilities by string name; the plugin that answers owns the
        capability. Nothing answers (``None``) when no installed feature handles it.
        """
        for feature in self._instances:
            result = feature.invoke(name, *args, **kwargs)
            if result is not None:
                return result
        return None

    # The context and the request.

    def compose_context(self, context: dict) -> None:
        for feature in self._instances:
            feature.compose_context(context)

    def contributed_tools(self) -> list:
        """Every tool the installed features provide, in feature order."""
        tools: list = []
        for feature in self._instances:
            tools.extend(feature.contribute_tools())
        return tools

    def contributed_schema_fields(self, tool_name: str) -> dict:
        """Every extra argument field the installed features add to one tool, merged in feature order."""
        fields: dict = {}
        for feature in self._instances:
            fields.update(feature.contribute_schema_fields(tool_name))
        return fields

    def compose_prompt(self, variables: dict[str, str]) -> None:
        for feature in self._instances:
            feature.compose_prompt(variables)

    def prepare_request(self, messages: list) -> list:
        for feature in self._instances:
            messages = feature.prepare_request(messages)
        return messages

    async def assign_title(self, first_message: str) -> str | None:
        """The first feature that names the session, or ``None`` when none does."""
        for feature in self._instances:
            title = await feature.assign_title(first_message)
            if title:
                return title
        return None

    # Conversation maintenance: the loop holds while a feature reclaims context.

    def maintainers(self, request_tokens: int) -> list[Feature]:
        return [feature for feature in self._instances if feature.should_maintain(request_tokens)]

    def active_maintenance(self) -> list[Feature]:
        return [feature for feature in self._instances if feature.maintenance_active()]

    def valid_during_maintenance(self, call: dict) -> bool:
        active = self.active_maintenance()
        if not active:
            return True
        return all(feature.valid_during_maintenance(call) for feature in active)

    def maintenance_ready(self) -> list[Feature]:
        return [feature for feature in self.active_maintenance() if feature.maintenance_ready()]

    def maintenance_reason(self) -> str:
        ready = self.maintenance_ready()
        return ready[0].maintenance_reason() if ready else "manual"

    async def maintenance_describe(self) -> dict:
        active = self.active_maintenance()
        if not active:
            return {}
        return await active[0].maintenance_describe()

    def maintenance_violation_message(self) -> str:
        for feature in self.active_maintenance():
            message = feature.maintenance_violation_message()
            if message:
                return message
        return ""

    async def fail_maintenance(self, message: str) -> AsyncIterator[Any]:
        for feature in self.active_maintenance():
            async for event in feature.fail_maintenance(message):
                yield event

    def maintenance_tool_schemas(self) -> dict:
        schemas: dict[str, Any] = {}
        for feature in self.active_maintenance():
            schemas.update(feature.maintenance_tool_schemas())
        return schemas

    def record_maintenance_handoff(self) -> None:
        for feature in self.active_maintenance():
            feature.record_maintenance_handoff()

    async def advance_maintenance(self) -> AsyncIterator[Any]:
        for feature in self.active_maintenance():
            async for event in feature.advance_maintenance():
                yield event

    async def run_maintenance(self, *, reason: str) -> AsyncIterator[Any]:
        for feature in self.active_maintenance():
            async for event in feature.run_maintenance(reason=reason):
                yield event

    # Tool gating.

    async def plan_tool_calls(self, tool_calls: list[dict]) -> Any | None:
        for feature in self._instances:
            planned = await feature.plan_tool_calls(tool_calls)
            if planned is not None:
                return planned
        return None

    def resolve_gates(self, plans: Any, answers: dict) -> dict:
        for feature in self._instances:
            resolved = feature.resolve_gates(plans, answers)
            if resolved:
                return resolved
        return {}

    async def review_automatic_gates(self, gates: Sequence[Any]) -> dict:
        reviewed: dict[str, Any] = {}
        for gate in gates:
            for feature in self._instances:
                answer = await feature.review_automatic_gate(gate)
                if answer is not None:
                    reviewed[gate.request_id] = answer
                    break
        return reviewed

    # Finished turn events and durable state.

    def drain(self) -> list[Any]:
        events: list[Any] = []
        for feature in self._instances:
            events.extend(feature.drain())
        return events

    def snapshot(self) -> dict:
        merged: dict[str, Any] = {}
        for feature in self._instances:
            own = feature.snapshot()
            if own:
                merged.update(own)
        return merged

    def restore(self, snapshot: dict) -> None:
        for feature in self._instances:
            feature.restore(snapshot)

    def blocked_reason(self) -> str | None:
        """Why new input must be refused, per the first feature that blocks it."""
        for feature in self._instances:
            reason = feature.blocks_input()
            if reason:
                return reason
        return None


def feature_prompts(name: str, catalogue: Any) -> PromptLoader:
    """A plugin's own templates, behind the catalogue's overrides and in front of the shared set."""
    # Anchored on the plugins package itself, so a plugin's templates follow the plugin, not this module.
    directory = Path(plugins_package_root) / name / "prompts"
    overrides = getattr(catalogue, "prompt_override", None)
    return PromptLoader(
        directory,
        overrides=overrides if overrides is not None else None,
        fallback=_CataloguePromptLoader(catalogue),
    )


class _CataloguePromptLoader:
    """The shared core templates a feature falls back to when a name is not its own."""

    def __init__(self, catalogue: Any) -> None:
        self._catalogue = catalogue

    def load(self, template_name: str, variables: dict) -> str:
        return self._catalogue.prompt(template_name, dict(variables))


def build_features(
    instances: Sequence[Feature] | None,
    context: PluginContext,
    host: PluginHost,
) -> Features:
    """Install the given feature instances, in order.

    The instances are composed by the application layer from the library's ordinary feature
    classes; installing only hands each its context and, for library features, the internal
    host. Nothing here names a feature or inspects how it was built.
    """
    installed = list(instances or ())
    for feature in installed:
        feature.attach(context, host)
    return Features(installed)


__all__ = [
    "Feature",
    "Features",
    "build_features",
    "feature_prompts",
]
