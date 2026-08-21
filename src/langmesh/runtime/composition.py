"""Configured values that compose the harness without reaching into a product layer."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Sequence

from langchain_core.tools import BaseTool

from langmesh.base.configuration import AgentConfiguration, Configuration
from langmesh.base.contracts.ports import (
    Approvals,
    Attachments,
    CatalogueLike,
    Checkpoints,
    CredentialStore,
    FileLeases,
    JobStore,
    MCPServers,
    Observer,
    PermissionPolicy,
    PromptComposer,
    SessionAccess,
    Transcript,
    describe_unmet,
)
from langmesh.runtime.environment import RuntimeEnvironment


@dataclass(frozen=True)
class RuntimeProfile:
    """Immutable facts that define one runtime and its confinement boundary."""

    agent: AgentConfiguration
    configuration: Configuration
    session_id: str
    working_directory: str
    project_directory: str = ""
    permission_mode: str = ""
    sandbox: Any = None
    parent_session: str = ""

    def __post_init__(self) -> None:
        if not self.session_id.strip():
            raise ValueError("session_id must not be empty")
        if not self.working_directory or not Path(self.working_directory).is_absolute():
            raise ValueError("working_directory must be an absolute path")


@dataclass(frozen=True)
class RuntimeComponents:
    """Replaceable capabilities used by a runtime, with product policy supplied from above."""

    model: Any = None
    catalogue: Any = None
    jobs: Any = None
    observer: Any = None
    approvals: Any = None
    transcript: Any = None
    sessions: Any = None
    mcp_servers: Any = None
    file_leases: Any = None
    permissions: Any = None
    prompt_composer: Any = None
    prompt_revision: str = ""
    toolbox: Any = None
    tools: Sequence[BaseTool] = field(default_factory=tuple)
    toolset: Sequence[BaseTool] | None = None
    tool_gate: str = "ask"
    hooks: Sequence[Any] = field(default_factory=tuple)
    middleware: Sequence[Any] = field(default_factory=tuple)
    synchronize_resources: Callable[[], Awaitable[None]] | None = None
    related_turns: Callable[[str], Awaitable[Any]] | None = None
    features: Sequence[Any] | None = None
    # The host's opaque plugin bundle: whatever the composing host supplies for its plugins
    # (journals, preparations, listeners). The core never inspects it; it is carried through
    # so plugins that reach it via services can. None means the host composed no bundle.
    services: Any = None
    # The machine snapshot and user context, probed by the host and passed in. None means the
    # library supplies a minimal platform-only snapshot and no personal context.
    machine_snapshot: dict[str, Any] | None = None
    user_context: dict[str, Any] | None = None
    environment: RuntimeEnvironment | None = None

    def __post_init__(self) -> None:
        if self.tool_gate not in {"ask", "none"}:
            raise ValueError("tool_gate must be 'ask' or 'none'")
        for name in ("tools", "hooks", "middleware"):
            object.__setattr__(self, name, tuple(getattr(self, name)))
        if self.toolset is not None:
            object.__setattr__(self, "toolset", tuple(self.toolset))
        if self.features is not None:
            object.__setattr__(self, "features", tuple(self.features))

        ports = {
            "approvals": Approvals,
            "catalogue": CatalogueLike,
            "file_leases": FileLeases,
            "jobs": JobStore,
            "mcp_servers": MCPServers,
            "observer": Observer,
            "permissions": PermissionPolicy,
            "prompt_composer": PromptComposer,
            "sessions": SessionAccess,
            "transcript": Transcript,
        }
        for name, port in ports.items():
            candidate = getattr(self, name)
            if candidate is None:
                continue
            unmet = describe_unmet(port, candidate)
            if unmet:
                raise TypeError(f"{name}: {unmet}")


@dataclass(frozen=True)
class SessionComponents(RuntimeComponents):
    """Runtime capabilities plus the ownership seams of an embedded session."""

    checkpoints: Any = None
    attachments: Any = None
    credential_store: Any = None
    tracer_provider: Any = None

    def __post_init__(self) -> None:
        super().__post_init__()
        for name, port in {
            "attachments": Attachments,
            "checkpoints": Checkpoints,
            "credential_store": CredentialStore,
        }.items():
            candidate = getattr(self, name)
            if candidate is None:
                continue
            unmet = describe_unmet(port, candidate)
            if unmet:
                raise TypeError(f"{name}: {unmet}")

    def for_runtime(self, **updates: Any) -> RuntimeComponents:
        """Project session ownership out, leaving exactly what an ``AgentRuntime`` consumes."""
        values = {name: getattr(self, name) for name in RuntimeComponents.__dataclass_fields__}
        values.update(updates)
        return RuntimeComponents(**values)


__all__ = ["RuntimeComponents", "RuntimeProfile", "SessionComponents"]
