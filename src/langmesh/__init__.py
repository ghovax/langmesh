"""LangMesh's public library surface."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version
from typing import TYPE_CHECKING, Any

from langmesh.base.configuration import (
    AgentConfiguration,
    BashToolConfiguration,
    Configuration,
    FilesystemConfiguration,
    MCPConfiguration,
    MCPServerConfiguration,
    SandboxConfiguration,
    ToolboxConfiguration,
    ToolsConfiguration,
)
from langmesh.base.configuration.permission_mode import PermissionMode
from langmesh.base.content.attachments import Attachment, AttachmentComposer, ComposedAttachments
from langmesh.base.content.instructions import Instruction
from langmesh.base.content.observations import (
    DirectiveEntry,
    ObservationEntry,
    ObservationSnapshot,
    RegistryCounts,
    RegistryMetadata,
    RegistryTimestamps,
)
from langmesh.base.content.skills import Skill
from langmesh.base.contracts.catalogue import Catalogue
from langmesh.base.contracts.mcp_client import MCPServerManager
from langmesh.base.contracts.ports import (
    AfterTurnHook,
    Approval,
    Approvals,
    Attachments,
    BeforeModelHook,
    BeforeToolsHook,
    CatalogueLike,
    Checkpoints,
    CompactionSummaryState,
    Credentials,
    DurableModelCache,
    FileLeases,
    JobStore,
    MCPServers,
    MemoryCheckpoints,
    MemoryJobStore,
    MemoryTranscript,
    Observation,
    Observer,
    PermissionPolicy,
    PromptComposer,
    PromptLayer,
    SessionAccess,
    SuspensionGate,
    ToolInvocation,
    ToolMiddleware,
    Transcript,
    TurnHook,
    TurnSummary,
)
from langmesh.base.contracts.tools import ToolLike
from langmesh.base.persistence.schedules import (
    ScheduleError,
    is_due,
    next_firing,
)
from langmesh.base.persistence.schedules import (
    validate as validate_schedule,
)
from langmesh.runtime.composition import RuntimeComponents, RuntimeProfile, SessionComponents
from langmesh.runtime.environment import RuntimeEnvironment
from langmesh.runtime.hooks import MaximumToolCalls
from langmesh.runtime.session_control import (
    FeatureState,
    PendingTurn,
    SessionCheckpoint,
    SessionPhase,
    SessionSnapshot,
    SessionState,
)
from langmesh.runtime.tools.execution import ToolExecution, ToolHandler, ToolServices
from langmesh.runtime.turn_events import (
    Checkpoint,
    CompactionDone,
    CompactionStarted,
    DeniedInjection,
    Done,
    Error,
    EventType,
    GoalReviewFinished,
    GoalReviewProgress,
    GoalReviewStarted,
    MCPEvent,
    PermissionReviewing,
    Status,
    Steering,
    Suspended,
    TextChunk,
    Thinking,
    ThinkingDone,
    ToolCall,
    ToolResult,
    TurnEvent,
    TurnEventUnion,
    Usage,
)
from langmesh.session import Session

if TYPE_CHECKING:
    from langmesh.runtime.plugins.locations.executor import SshExecutor
    from langmesh.runtime.runtime import AgentRuntime

try:
    __version__ = version("langmesh")
except PackageNotFoundError:
    __version__ = "0"


def __getattr__(name: str) -> Any:
    """Load optional heavyweight exports only when requested."""
    if name == "AgentRuntime":
        from langmesh.runtime.runtime import AgentRuntime

        return AgentRuntime
    if name == "SshExecutor":
        from langmesh.runtime.plugins.locations.executor import SshExecutor

        return SshExecutor
    raise AttributeError(name)


__all__ = [
    "AfterTurnHook",
    "AgentConfiguration",
    "AgentRuntime",
    "Approval",
    "Approvals",
    "Attachment",
    "AttachmentComposer",
    "Attachments",
    "BashToolConfiguration",
    "BeforeModelHook",
    "BeforeToolsHook",
    "Catalogue",
    "CatalogueLike",
    "Checkpoint",
    "Checkpoints",
    "CompactionDone",
    "CompactionStarted",
    "CompactionSummaryState",
    "ComposedAttachments",
    "Configuration",
    "Credentials",
    "DurableModelCache",
    "DirectiveEntry",
    "DeniedInjection",
    "Done",
    "Error",
    "EventType",
    "FileLeases",
    "FilesystemConfiguration",
    "FeatureState",
    "GoalReviewFinished",
    "GoalReviewProgress",
    "GoalReviewStarted",
    "Instruction",
    "JobStore",
    "MCPConfiguration",
    "MCPServerConfiguration",
    "MCPServerManager",
    "MCPServers",
    "MaximumToolCalls",
    "MCPEvent",
    "MemoryCheckpoints",
    "MemoryJobStore",
    "MemoryTranscript",
    "Observation",
    "ObservationEntry",
    "ObservationSnapshot",
    "Observer",
    "PendingTurn",
    "PermissionMode",
    "PermissionPolicy",
    "PermissionReviewing",
    "PromptComposer",
    "PromptLayer",
    "RegistryCounts",
    "RegistryMetadata",
    "RegistryTimestamps",
    "RuntimeComponents",
    "RuntimeEnvironment",
    "RuntimeProfile",
    "SandboxConfiguration",
    "ScheduleError",
    "Session",
    "SessionAccess",
    "SessionComponents",
    "SessionCheckpoint",
    "SessionPhase",
    "SessionSnapshot",
    "SessionState",
    "Skill",
    "SshExecutor",
    "Status",
    "Steering",
    "Suspended",
    "SuspensionGate",
    "TextChunk",
    "Thinking",
    "ThinkingDone",
    "ToolCall",
    "ToolExecution",
    "ToolHandler",
    "ToolInvocation",
    "ToolLike",
    "ToolMiddleware",
    "ToolResult",
    "ToolServices",
    "ToolboxConfiguration",
    "ToolsConfiguration",
    "Transcript",
    "TurnEvent",
    "TurnEventUnion",
    "TurnHook",
    "TurnSummary",
    "Usage",
    "__version__",
    "is_due",
    "next_firing",
    "validate_schedule",
]
