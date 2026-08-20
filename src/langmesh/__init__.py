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
from langmesh.base.confinement.file_leases import FileLeaseManager
from langmesh.base.content.attachments import AttachmentInput, PathAttachments
from langmesh.base.content.instructions import Instruction
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
    WorkspaceManager,
)
from langmesh.base.contracts.tools import ToolGrant, ToolLike, as_tool_grants
from langmesh.base.persistence.observations import ObservationRegistry
from langmesh.base.persistence.resources import (
    LocalResourceChanges,
    MaterializedResources,
    OverlayResources,
    ResourceChange,
    ResourceChangeSource,
    ResourceWatchUnsupported,
    WorkspaceResources,
    WorkspaceResourcesLike,
)
from langmesh.base.persistence.schedules import (
    ScheduleError,
    is_due,
    next_firing,
)
from langmesh.base.persistence.schedules import (
    validate as validate_schedule,
)
from langmesh.base.persistence.worktrees import (
    SessionWorktree,
    SessionWorktreeManager,
    WorktreeStrategy,
)
from langmesh.runtime.composition import RuntimeComponents, RuntimeProfile, SessionComponents
from langmesh.runtime.hooks import MaximumToolCalls
from langmesh.runtime.session_control import PendingTurn, SessionPhase, SessionState
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
    Mcp,
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
    "AttachmentInput",
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
    "Configuration",
    "Credentials",
    "DeniedInjection",
    "Done",
    "Error",
    "EventType",
    "FileLeaseManager",
    "FileLeases",
    "FilesystemConfiguration",
    "GoalReviewFinished",
    "GoalReviewProgress",
    "GoalReviewStarted",
    "Instruction",
    "JobStore",
    "LocalResourceChanges",
    "MCPConfiguration",
    "MCPServerConfiguration",
    "MCPServerManager",
    "MCPServers",
    "MaterializedResources",
    "MaximumToolCalls",
    "Mcp",
    "MemoryCheckpoints",
    "MemoryJobStore",
    "MemoryTranscript",
    "Observation",
    "ObservationRegistry",
    "Observer",
    "OverlayResources",
    "PathAttachments",
    "PendingTurn",
    "PermissionMode",
    "PermissionPolicy",
    "PermissionReviewing",
    "PromptComposer",
    "PromptLayer",
    "ResourceChange",
    "ResourceChangeSource",
    "ResourceWatchUnsupported",
    "RuntimeComponents",
    "RuntimeProfile",
    "SandboxConfiguration",
    "ScheduleError",
    "Session",
    "SessionAccess",
    "SessionComponents",
    "SessionPhase",
    "SessionState",
    "SessionWorktree",
    "SessionWorktreeManager",
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
    "ToolGrant",
    "ToolInvocation",
    "ToolLike",
    "ToolMiddleware",
    "ToolResult",
    "ToolboxConfiguration",
    "ToolsConfiguration",
    "Transcript",
    "TurnEvent",
    "TurnEventUnion",
    "TurnHook",
    "TurnSummary",
    "Usage",
    "WorkspaceManager",
    "WorkspaceResources",
    "WorkspaceResourcesLike",
    "WorktreeStrategy",
    "__version__",
    "as_tool_grants",
    "is_due",
    "next_firing",
    "validate_schedule",
]
