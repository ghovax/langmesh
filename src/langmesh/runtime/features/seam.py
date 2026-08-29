"""The public plugin seam: the hooks a feature implements and the dispatch the core calls.

The core is independent of every feature: it runs a plain model turn and, at a fixed set of
points, asks the installed features to participate. A feature implements the hooks it needs
and is never named by the core. Who composes features, in what order, and with which ports is
the caller's concern — not the core's and not this seam's.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, AsyncIterator, Callable, Sequence, TypeVar, cast
from weakref import ReferenceType, ref

from langmesh.base.content.prompts import PackagePromptLoader, PromptTemplates
from langmesh.runtime.features.context import PluginContext
from langmesh.runtime.features.host import PluginHost
from langmesh.runtime.session_control import FeatureState
from langmesh.runtime.turn_events import TurnEventUnion

_Capability = TypeVar("_Capability")
_FeatureType = TypeVar("_FeatureType", bound="Feature")

plugins_package_root = Path(__file__).resolve().parents[1] / "plugins"


async def _empty_events() -> AsyncIterator[TurnEventUnion]:
    for event in ():
        yield event


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

    __slots__ = ("__weakref__",)

    @property
    def state_name(self) -> str:
        """The stable namespace used for this plugin's durable state."""
        return f"{type(self).__module__}.{type(self).__qualname__}"

    def attach(self, context: PluginContext, host: "PluginHost | None" = None) -> None:
        """Installed by the runtime; library features keep the internal view here."""

    def compose_context(self, context: dict) -> None:
        """Contribute to the turn's model-facing context, merging into ``context`` in place."""

    def contribute_tools(self) -> list:
        """The tools this feature provides to the session's roster, empty when it provides none."""
        return []

    def contribute_tool_handlers(self) -> dict[str, Any]:
        """Event-rich handlers for contributed tools that cannot use the generic invocation path."""
        return {}

    def required_capabilities(self) -> tuple[type, ...]:
        """Structural capabilities that must be installed beside this feature."""
        return ()

    def contribute_schema_fields(self, tool_name: str) -> dict:
        """Extra argument fields to add to a tool's schema, by tool name.

        A plugin that extends another tool's contract (the locations plugin adding a
        ``location`` selector to bash) answers with its fields here, so the explanation of an
        extra parameter lives with the plugin that contributes it.
        """
        return {}

    def compose_prompt(self, variables: dict[str, str]) -> None:
        """Contribute named prompt sections, merging into ``variables`` in place."""

    def prompt_revision(self) -> str:
        """Identify the prompt templates this feature owns, or its stable absence of them."""
        revision = getattr(getattr(self, "_prompts", None), "revision", None)
        return str(revision()) if callable(revision) else ""

    async def assign_title(self, first_message: str) -> str | None:
        """A suggested title from this feature's own naming call, or ``None`` to leave it unnamed."""
        return None

    def prepare_request(self) -> None:
        """Append any request-boundary state through the feature's conversation capability."""

    def should_maintain(self, request_tokens: int) -> bool:
        """Whether the loop should hold for this feature before admitting new input."""
        return False

    def maintenance_active(self) -> bool:
        """Whether this feature is currently holding the loop."""
        return False

    def begin_maintenance(
        self, *, reason: str, resume_after: bool, context_tokens: int = 0
    ) -> None:
        """Start holding the loop, preparing the durable handoff the fold needs."""

    def advance_maintenance(self) -> AsyncIterator[TurnEventUnion]:
        """Advance the hold one step (record a handoff, announce a phase), yielding its events."""
        return _empty_events()

    def run_maintenance(self, *, reason: str) -> AsyncIterator[TurnEventUnion]:
        """Complete the hold and reclaim context, yielding the fold's events."""
        return _empty_events()

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

    def fail_maintenance(self, message: str) -> AsyncIterator[TurnEventUnion]:
        """The hold could not complete; fail it as a durable blocker, yielding its events."""
        return _empty_events()

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

    def incomplete_reminder(self) -> str | None:
        """A reminder when the model stopped without finishing this feature's work, or ``None``."""
        return None

    def should_complete_turn(self) -> bool:
        """Whether this feature considers the turn done even without further model output."""
        return False

    def blocks_input(self) -> str | None:
        """Why new input must be refused (a failed fold, an unrepaired registry), or ``None``."""
        return None

    def snapshot(self) -> object | None:
        """This feature's durable state to persist beside the checkpoint, or ``None``."""
        return None

    def restore(self, state: object) -> None:
        """Rehydrate the durable value previously returned by :meth:`snapshot`."""

    def acknowledge_checkpoint(self) -> None:
        """Acknowledge side records only after their matching conversation snapshot commits."""

    def terminate_tool_call(self, tool_call_id: str) -> bool:
        """Tear down live work this feature owns for ``tool_call_id``.

        Every installed feature is asked, not first-wins, so each plugin can close its own
        side effects (a process group, a screen-control child, a background search). Return
        ``True`` when this feature handled that live call.
        """
        return False

    def interrupt(self, *, restarting: bool) -> None:
        """Stop live work owned by this feature at a turn or process boundary."""

    def bind_detached_tool(self, job_id: str, tool_call_id: str) -> bool:
        """Associate a detached operation with its originating tool call."""
        return False

    def resolve_execution(self, tool_name: str, arguments: dict) -> Any:
        """Resolve an optional execution target, or return ``None`` for the local runtime."""
        return None

    async def reconsider_gate(self, gate: Any) -> dict[str, Any] | None:
        """Reconsider one suspended interaction after live policy changes."""
        return None

    def bind_tool_context(self) -> Callable[[], None] | None:
        """Bind feature-owned context for one tool invocation and return its cleanup."""
        return None


class Features:
    """The installed features of one runtime, and the dispatch the core calls at each point.

    The container knows nothing about any particular feature: it holds whatever was installed,
    in order, and calls the hooks each implements.
    """

    def __init__(self, instances: Sequence[Feature] = ()) -> None:
        self._instances = tuple(instances)

    @property
    def instances(self) -> tuple[Feature, ...]:
        """The fixed feature roster in installation order."""
        return self._instances

    def by_type(self, feature_type: type[_FeatureType]) -> _FeatureType | None:
        """The installed feature of a given class, for a composer that wants its instance back."""
        return next(
            (feature for feature in self._instances if isinstance(feature, feature_type)), None
        )

    def capability(self, contract: type[_Capability]) -> _Capability | None:
        """Return the first installed feature that structurally implements ``contract``."""
        return next(
            (
                cast(_Capability, feature)
                for feature in self._instances
                if isinstance(feature, contract)
            ),
            None,
        )

    def require(self, contract: type[_Capability]) -> _Capability:
        """Return an installed capability or fail at the boundary that requires it."""
        capability = self.capability(contract)
        if capability is None:
            raise RuntimeError(f"The {contract.__name__} feature capability is not installed.")
        return capability

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

    def contributed_tool_handlers(self) -> dict[str, Any]:
        """Merge event-rich contributed handlers in feature order, with later features replacing earlier ones."""
        handlers: dict[str, Any] = {}
        for feature in self._instances:
            handlers.update(feature.contribute_tool_handlers())
        return handlers

    def contributed_schema_fields(self, tool_name: str) -> dict:
        """Every extra argument field the installed features add to one tool, merged in feature order."""
        fields: dict = {}
        for feature in self._instances:
            fields.update(feature.contribute_schema_fields(tool_name))
        return fields

    def compose_prompt(self, variables: dict[str, str]) -> None:
        for feature in self._instances:
            feature.compose_prompt(variables)

    def prompt_revision(self) -> list[dict[str, str]]:
        """Return the ordered identities of installed features and their prompt material."""
        return [
            {"feature": feature.state_name, "prompts": feature.prompt_revision()}
            for feature in self._instances
        ]

    def prepare_request(self) -> None:
        for feature in self._instances:
            feature.prepare_request()

    def acknowledge_checkpoint(self) -> None:
        for feature in self._instances:
            feature.acknowledge_checkpoint()

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

    def incomplete_reminder(self) -> str | None:
        """The first feature that still needs the model to finish, or ``None``."""
        for feature in self._instances:
            reminder = feature.incomplete_reminder()
            if reminder:
                return reminder
        return None

    def should_complete_turn(self) -> bool:
        """Whether any feature considers the turn finished without further model output."""
        return any(feature.should_complete_turn() for feature in self._instances)

    def snapshot(self) -> tuple[FeatureState, ...]:
        states: list[FeatureState] = []
        for feature in self._instances:
            own = feature.snapshot()
            if own is not None:
                states.append(FeatureState(feature.state_name, own))
        return tuple(states)

    def restore(self, states: Sequence[FeatureState]) -> None:
        by_name = {state.name: state.value for state in states}
        for feature in self._instances:
            if feature.state_name in by_name:
                feature.restore(by_name[feature.state_name])

    def blocked_reason(self) -> str | None:
        """Why new input must be refused, per the first feature that blocks it."""
        for feature in self._instances:
            reason = feature.blocks_input()
            if reason:
                return reason
        return None

    def terminate_tool_call(self, tool_call_id: str) -> bool:
        """Ask every feature to tear down live work for this call. Every plugin is notified."""
        handled = False
        for feature in self._instances:
            if feature.terminate_tool_call(tool_call_id):
                handled = True
        return handled

    def interrupt(self, *, restarting: bool) -> None:
        for feature in self._instances:
            feature.interrupt(restarting=restarting)

    def bind_detached_tool(self, job_id: str, tool_call_id: str) -> bool:
        return any(feature.bind_detached_tool(job_id, tool_call_id) for feature in self._instances)

    def resolve_execution(self, tool_name: str, arguments: dict) -> Any:
        for feature in self._instances:
            resolved = feature.resolve_execution(tool_name, arguments)
            if resolved is not None:
                return resolved
        return None

    async def reconsider_gate(self, gate: Any) -> dict[str, Any] | None:
        for feature in self._instances:
            verdict = await feature.reconsider_gate(gate)
            if verdict is not None:
                return verdict
        return None

    def bind_tool_contexts(self) -> list[Callable[[], None]]:
        cleanups: list[Callable[[], None]] = []
        for feature in self._instances:
            cleanup = feature.bind_tool_context()
            if cleanup is not None:
                cleanups.append(cleanup)
        return cleanups


def feature_prompts(name: str, catalogue: Any) -> PromptTemplates:
    """A plugin's own templates, behind the catalogue's overrides and in front of the shared set."""
    # Anchored on the plugins package itself, so a plugin's templates follow the plugin, not this module.
    directory = Path(plugins_package_root) / name / "prompts"
    overrides = getattr(catalogue, "prompt_override", None)
    return PackagePromptLoader(
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

    def revision(self) -> str:
        return self._catalogue.prompt_revision()


class FeatureAttachmentRegistry:
    """Track which live session has an attached feature without modifying the feature."""

    def __init__(self) -> None:
        self._sessions: dict[int, tuple[ReferenceType[Feature], str]] = {}

    def session_for(self, feature: Feature) -> str | None:
        """Return the live session associated with a feature, if it is still registered."""
        entry = self._sessions.get(id(feature))
        if entry is None:
            return None
        reference, session_id = entry
        if reference() is feature:
            return session_id
        self._sessions.pop(id(feature), None)
        return None

    def register(self, feature: Feature, session_id: str) -> None:
        """Associate a feature with a session until the feature is garbage-collected."""
        identity = id(feature)

        def release(reference: ReferenceType[Feature]) -> None:
            entry = self._sessions.get(identity)
            if entry is not None and entry[0] is reference:
                self._sessions.pop(identity, None)

        self._sessions[identity] = (ref(feature, release), session_id)


_ATTACHMENTS = FeatureAttachmentRegistry()


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
    installed = tuple(instances or ())
    features = Features(installed)
    for feature in installed:
        for contract in feature.required_capabilities():
            if features.capability(contract) is None:
                raise ValueError(
                    f"{type(feature).__name__} requires the {contract.__name__} capability."
                )
        attached_session = _ATTACHMENTS.session_for(feature)
        if attached_session is not None and attached_session != context.session_id:
            raise ValueError(
                f"{type(feature).__name__} is already attached to session {attached_session!r}; feature instances cannot be shared between sessions."
            )
    for feature in installed:
        feature.attach(context, host)
        _ATTACHMENTS.register(feature, context.session_id)
    return features


__all__ = [
    "Feature",
    "FeatureAttachmentRegistry",
    "Features",
    "build_features",
    "feature_prompts",
]
