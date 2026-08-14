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

## Extension ports

::: langmesh.base.ports
    options:
      members: true
      show_root_heading: false

`PromptComposer` receives `PromptLayer` values, while `BeforeModelHook` receives the final provider message list.

## Resources and locations

::: langmesh.base.attachments.AttachmentInput

::: langmesh.base.attachments.PathAttachments

::: langmesh.base.resources.WorkspaceResources
    options:
      members: true

::: langmesh.base.resources.OverlayResources
    options:
      members: true

::: langmesh.base.observations.ObservationRegistry
    options:
      members: true

::: langmesh.runtime.locations.Location

## Standard policies

::: langmesh.runtime.compaction.KeepRecentTurns

::: langmesh.runtime.compaction.ObservationCompactionPreparation

::: langmesh.runtime.compaction.DirectCompactionPreparation

::: langmesh.runtime.continuation.TuningContinuationPolicy

::: langmesh.runtime.hooks.MaximumToolCalls
