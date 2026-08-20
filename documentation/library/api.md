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

`Session(..., tools=[...])` accepts bare LangChain tools or `ToolGrant` values; `Session.grant_tool(...)` adds one at any later moment. Both are append-only. See [Granting a tool to a session](composition.md#granting-a-tool-to-a-session).

## Extension ports

::: langmesh.base.contracts.ports
    options:
      members: true
      show_root_heading: false

`PromptComposer` receives `PromptLayer` values, while `BeforeModelHook` receives the final provider message list.

## The plugin seam

::: langmesh.runtime.features.seam.Feature

::: langmesh.runtime.features.seam.Features

::: langmesh.runtime.features.context.PluginContext

::: langmesh.runtime.features.bus.PluginBus

## Resources and locations

::: langmesh.base.content.attachments.ComposedAttachments

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

## Compaction, continuation, and hooks

::: langmesh.runtime.plugins.compaction.KeepRecentTurns

::: langmesh.runtime.plugins.compaction.ObservationCompactionPreparation

::: langmesh.runtime.plugins.compaction.DirectCompactionPreparation

::: langmesh.runtime.plugins.continuation.policy.DefaultContinuationPolicy

::: langmesh.runtime.plugins.compaction.Compaction

::: langmesh.runtime.plugins.goal_review.GoalReviewFeature

::: langmesh.runtime.hooks.MaximumToolCalls
