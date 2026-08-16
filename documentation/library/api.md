# API reference

## Session and state

::: langmesh.Session
    options:
      members: true

::: langmesh.runtime.session_control.SessionState

::: langmesh.runtime.session_control.PendingTurn

::: langmesh.runtime.session_control.SessionPhase

## Composition

::: langmesh.runtime.composition.RuntimeProfile

::: langmesh.runtime.composition.RuntimeComponents

::: langmesh.runtime.composition.SessionComponents

::: langmesh.runtime.runtime.AgentRuntime
    options:
      members: true

## Tool grants

::: langmesh.base.contracts.tools

`Session(..., tools=[...])` accepts bare LangChain tools or `ToolGrant` values; `Session.grant_tool(...)` adds one at any later moment. Both are append-only. See [Granting a tool to a session](customization.md#granting-a-tool-to-a-session).

## Extension ports

::: langmesh.base.contracts.ports
    options:
      members: true
      show_root_heading: false

`PromptComposer` receives `PromptLayer` values, while `BeforeModelHook` receives the final provider message list.

## Resources and locations

::: langmesh.base.content.attachments.AttachmentInput

::: langmesh.base.content.attachments.PathAttachments

::: langmesh.base.persistence.resources.WorkspaceResources
    options:
      members: true

::: langmesh.base.persistence.resources.OverlayResources
    options:
      members: true

::: langmesh.base.persistence.observations.ObservationRegistry
    options:
      members: true

::: langmesh.runtime.locations.Location

## Standard policies

::: langmesh.runtime.compaction.KeepRecentTurns

::: langmesh.runtime.compaction.ObservationCompactionPreparation

::: langmesh.runtime.compaction.DirectCompactionPreparation

::: langmesh.runtime.continuation.TuningContinuationPolicy

::: langmesh.runtime.hooks.MaximumToolCalls
