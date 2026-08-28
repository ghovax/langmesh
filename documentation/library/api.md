# API reference

## Session and state

::: langmesh.Session options: members: true

::: langmesh.runtime.session_control.SessionState

::: langmesh.runtime.session_control.PendingTurn

::: langmesh.runtime.session_control.SessionPhase

## Composition

::: langmesh.runtime.composition.RuntimeProfile

::: langmesh.runtime.composition.RuntimeComponents

::: langmesh.runtime.composition.SessionComponents

::: langmesh.runtime.runtime.AgentRuntime options: members: true

## Tool grants

::: langmesh.base.contracts.tools

`Session(..., tools=[...])` binds ordinary LangChain tools into the initial stable provider schema; `Session.grant_tool(...)` adds or replaces one later and intentionally changes the next request's schema. See [Granting a tool to a session](composition.md#granting-a-tool-to-a-session).

## Extension ports

::: langmesh.base.contracts.ports options: members: true show_root_heading: false

`PromptComposer` receives `PromptLayer` values only when the static prompt is constructed. The public hook surface cannot rewrite the final provider message list.

## The plugin seam

::: langmesh.runtime.features.seam.Feature

::: langmesh.runtime.features.seam.Features

::: langmesh.runtime.features.context.PluginContext

::: langmesh.runtime.features.bus.PluginBus

## Resources and locations

::: langmesh.base.content.attachments.ComposedAttachments

::: langmesh.base.content.attachments.AttachmentComposer

::: langmesh.base.contracts.ports.ArtifactReference

::: langmesh.base.contracts.ports.ArtifactWriter

::: langmesh.base.contracts.ports.Artifacts

::: langmesh.base.contracts.ports.MemoryArtifacts

::: langmesh.base.persistence.checkpoints.SQLiteCheckpoints options: members: true

## Compaction, continuation, and hooks

::: langmesh.runtime.plugins.compaction.ObservationCompactionPreparation

::: langmesh.runtime.plugins.compaction.DirectCompactionPreparation

::: langmesh.runtime.plugins.continuation.policy.DefaultContinuationPolicy

::: langmesh.runtime.plugins.compaction.Compaction

::: langmesh.runtime.plugins.goal_review.GoalReviewFeature

::: langmesh.runtime.hooks.MaximumToolCalls
