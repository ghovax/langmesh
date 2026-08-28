# Lifecycle and control

`Session.state` is the control authority. Its `phase` is one of `idle`, `running`,
`suspended`, `compacting`, or `retrying`; invalid combinations such as running and
suspended are unrepresentable. Only the core's own controls live in `SessionState` —
plugin-owned state (goals, tasks, compaction, background jobs) is reached through the
feature seam.

## Suspension and resume

A gated batch emits `Suspended` and becomes a durable `PendingTurn`. Inspect the gates,
record one `Approval` per request, then resume the same batch.

```python
from langmesh import Approval, Suspended


async for event in session.stream("Run the release checks."):
    if isinstance(event, Suspended):
        for gate in event.interactions:
            decision = await review_in_ui(gate)
            await session.respond(
                gate.request_id,
                Approval(
                    allow=decision.allowed,
                    reason=decision.reason,
                    answers=decision.answers,
                ),
            )

if session.state.pending and session.state.pending.ready:
    async for event in session.resume():
        ...  # Continue the resumed turn.
```

`PendingTurn` stores the serialized execution plans and completed decisions. A restored
`Session` with the same id and checkpoint store resumes without rerunning calls that
finished before suspension.

Call `cancel_pending()` to close the dangling tool calls without executing them. New
user work is rejected until a pending batch is resumed or cancelled.

## Programmatic approval

Use `Approvals` when policy can decide before suspension. Return `None` to leave a gate
interactive.

```python
class ReadOnlyApprover:
    async def decide(self, gate):
        if gate.kind == "permission" and not getattr(gate.escape, "writes", ()):
            return Approval(True, "This application pre-approves reads.")
        return None


components = SessionComponents(approvals=ReadOnlyApprover())
```

An approver exception escalates the gate; it never grants authority. A `SuspensionGate`
carries `request_id`, `kind` (`permission` or `question`), `tool_name`, `command`,
`explanation`, and `escape`, so a client writes the decision surface in its own
language.

## Interrupts and steering

```python
session.interrupt()                 # Cancel the active model read and foreground work.
session.interrupt_tool(call_id)     # Cancel one active tool call.
await session.steer("Check the migration too.")
```

`steer()` appends the message at the next safe provider boundary and returns whether the
runtime accepted it. It does not rewrite the active request or prior conversation. A
`Steering` event reports the accepted message, with a `message_id` the sender supplied.
Background and detached work has its own lifecycle and is not cancelled by
`interrupt()`; it belongs to the background-jobs feature.

## Live policy

```python
await session.set_permission_mode("automatic")
```

`set_permission_mode()` checkpoints the new mode before the runtime adopts it, changes
the next tool decision, and reconsiders unanswered suspended gates, returning the
updated state. The modes are `ask`, `automatic`, and `allow`; see
[Permission modes](../user/configuration.md#permission-modes). Changing mode preserves
the existing conversation prefix and survives an immediate restart.

## Failure and retry

Provider and tool failures checkpoint the conversation tail. `retry()` continues that
tail without appending the user's message again.

```python
try:
    await session.ask("Complete the migration.")
except Exception:
    async for event in session.retry():
        ...  # Continue the saved conversation tail.
```

`Session.retryable_turn` says whether the last failed turn can continue. Compaction is
driven by the compaction feature, not by `Session`; a blocked compaction must succeed
before new user work is accepted.

## Session close

`aclose()` stops live in-process work as a restart boundary, waits for the active turn
to leave its lock, restores an existing checkpoint first when necessary, writes the
final typed checkpoint, and only then releases the runtime. If the save fails, the
runtime remains available and the error is returned to the caller. Supplied MCP servers,
checkpoint connections, artifact stores, credentials, and tracers remain caller-owned
and are never closed implicitly.

## Events and driving patterns

`Session.stream()`, `resume()`, and `retry()` yield a closed `TurnEventUnion`. Dispatch
on the variant class; the `EventType` enum is available for generic transports.

| Event                                                             | Meaning                                            | Typical action                                       |
| ----------------------------------------------------------------- | -------------------------------------------------- | ---------------------------------------------------- |
| `Status`                                                          | A session status name                              | Update a status chip                                 |
| `TextChunk`                                                       | Assistant text delta                               | Paint immediately                                    |
| `Thinking` / `ThinkingDone`                                       | Reasoning delta and boundary                       | Update a collapsible reasoning region                |
| `ToolCall`                                                        | Partial or complete tool request                   | Create or update one tool card by id                 |
| `ToolResult`                                                      | Tool completion                                    | Close the matching card                              |
| `MCPEvent`                                                        | An MCP server event (connect, progress, log)       | Update the MCP surface                               |
| `Suspended`                                                       | Durable permission or question batch               | Collect decisions, call `respond()`, then `resume()` |
| `PermissionReviewing`                                             | Automatic-mode gates the reviewer is weighing      | Show the call before the verdict                     |
| `Steering`                                                        | Mid-turn user message accepted                     | Reconcile optimistic UI by message id                |
| `Usage`                                                           | Latest request and cumulative token and cache data | Update usage telemetry                               |
| `Checkpoint`                                                      | Tool batch became durable                          | Commit a product high-water mark if needed           |
| `CompactionStarted` / `CompactionDone`                            | Context compaction lifecycle                       | Show compaction state and reclaimed size             |
| `GoalReviewStarted` / `GoalReviewProgress` / `GoalReviewFinished` | Independent goal review                            | Render review status separately from assistant prose |
| `Error`                                                           | Structured turn or tool failure                    | Render its code and parameters                       |
| `DeniedInjection`                                                 | A steered-in message was refused                   | Match the failed injection                           |
| `Done`                                                            | One model turn completed                           | Read final text and stop this stream                 |

`Done` ends a turn, not the session. Autonomous goal or task continuation can produce
several `Done` events inside one `Session.stream()` call. The session returns to `idle`
only after continuation policy stops.

```python
async def drive(session, message):
    async for event in session.stream(message):
        match event:
            case TextChunk(text=text):
                ...  # Paint the latest text delta.
            case ToolCall(id=call_id, name=name, arguments=arguments):
                ...  # Create or update a tool card by id.
            case Suspended(interactions=interactions):
                for gate in interactions:
                    await session.respond(gate.request_id, ...)
            case Error(code=code, message=message):
                ...  # Render the failure.

    if session.state.pending and session.state.pending.ready:
        async for event in session.resume():
            ...  # Continue the resumed turn.
```

An application may stop consuming events without interrupting the runtime only if it
continues draining in another task. To stop work, call `session.interrupt()`;
cancellation closes the provider stream, records a cancelled transcript turn,
checkpoints the closed exchange, and returns the session to `idle`.

## Models, credentials, and cache behavior

### Select a provider model

Set `provider` and `model` on `AgentConfiguration` when LangMesh should build the
provider adapter. `model_identifier="provider/model"` on `Session` overrides that
profile for one run.

```python
agent = AgentConfiguration(
    name="reviewer",
    provider="anthropic",
    model="claude-sonnet-4-5",
    system_prompt="Review changes and cite evidence.",
)

session = Session(
    agent,
    directory="/srv/checkout",
    model_identifier="openai/gpt-5.2",
    providers={"openai": "sk-…"},
)
```

The `providers` mapping is split into explicit provider credentials and endpoint values; the
caller's mapping is never mutated. Environment variables still take precedence. Account-backed providers
(`chatgpt`, `cursor`) use the replaceable `Credentials` port in `SessionComponents`.

### Supply a model object

Pass any LangChain `BaseChatModel` through `SessionComponents.model` when the
application owns provider construction, routing, retries, or testing.

```python
components = SessionComponents(model=application_model)
session = Session(agent_without_provider, directory="/srv/checkout", components=components)
```

The model must implement `bind_tools()` and streaming. LangMesh binds one stable ordered
tool schema when the runtime is constructed. A custom adapter with provider-native
checkpoints or local cache diagnostics may additionally satisfy `DurableModelCache`:
`model_cache_snapshot()` returns JSON-safe state and `restore_model_cache(snapshot)`
validates and adopts it. `Session` then persists that state beside its conversation
without knowing the provider's representation.

### Preserve provider caches

Stable instructions and the tool schema form the reusable prefix. LangMesh preserves
that prefix by construction:

- `SessionComponents` is frozen and snapshots sequence fields.
- Prior conversation messages are append-only until an explicit compaction.
- Session identity, paths, confinement, machine and user snapshots, feature state, and
  background events are a marked conversation message; a changed digest appends a
  replacement instead of rebuilding the stable instructions.
- The goal and permission reviewers inherit the main conversation and stable tool
  schema, then append their private instructions.
- Tools supplied before the first call stay fixed in the reusable schema. A live
  `grant_tool()` is an explicit capability change and therefore an intentional one-call
  divergence at the tools segment. See
  [Granting a tool to a session](composition.md#granting-a-tool-to-a-session).
- Steering appends at a provider boundary; it never edits an earlier message.
- Permission-mode changes apply during execution without rewriting model history.
- Session checkpoints include bounded request baselines, rolling Claude anchors, and
  account-scoped Cursor resumptions, so rebuilding a runtime does not make an otherwise
  reusable request locally unknowable.

`PromptComposer` runs only when the stable instructions are built. Call
`Session.refresh_prompt()` after changing an application-owned source that the composer
reads; that explicit refresh invalidates the instructions cache. The exact instructions
and their construction revision are checkpointed, while dynamic context lives in the
checkpointed conversation. The hook surface cannot rewrite provider requests, so reload
reconstructs the same prefix by construction.

Usage events expose provider-reported cache reads and writes, the reusable prefix, and
the first divergence from the preceding request (`cache_read_tokens`,
`cache_write_tokens`, `cache_prefix_reusable`, `reusable_prefix_tokens`, `segments`,
`shared_segments`, `divergence`). Local reuse means the previously sent eligible prefix
stayed byte-identical; an actual provider hit still depends on minimum token thresholds,
retention TTL, routing, account identity, and whether an earlier concurrent request
finished warming the entry. Use these values together to verify a custom model adapter
instead of inferring cache behavior from latency alone.
