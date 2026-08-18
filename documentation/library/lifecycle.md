# Lifecycle and control

`Session.state` is the control authority. Its `phase` is one of `idle`, `running`, `suspended`, `compacting`, or `retrying`; invalid combinations such as running and suspended are unrepresentable.

## Suspension and resume

A gated batch emits `Suspended` and becomes a durable `PendingTurn`. Inspect the gates, record one `Approval` per request, then resume the same batch.

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
        consume(event)
```

`PendingTurn` stores the serialized execution plans and completed decisions. A restored `Session` with the same id and checkpoint store resumes without rerunning calls that finished before suspension.

Call `cancel_pending()` to close the dangling tool calls without executing them. New user work is rejected until a pending batch is resumed or cancelled.

## Programmatic approval

Use `Approvals` when policy can decide before suspension. Return `None` to leave a gate interactive.

```python
class ReadOnlyApprover:
    async def decide(self, gate):
        if gate.kind == "permission" and not getattr(gate.escape, "writes", ()):
            return Approval(True, "This application pre-approves reads.")
        return None


components = SessionComponents(approvals=ReadOnlyApprover())
```

An approver exception escalates the gate; it never grants authority.

## Interrupts and steering

```python
session.interrupt()                 # Cancel the active model read and foreground work.
session.interrupt_tool(call_id)     # Cancel one active tool call.
session.background_tool(call_id)    # Detach one eligible foreground call.
await session.steer("Check the migration too.")
```

`steer()` appends the message at the next safe provider boundary and returns whether the runtime accepted it. It does not rewrite the active request or prior conversation. `background_jobs()` returns immutable snapshots for status rendering.

## Live policy and locations

`await session.set_permission_mode("automatic")` changes the next tool decision and reconsiders unanswered suspended gates. `session.set_locations(...)` changes resolution for the next tool call. Both preserve the existing conversation prefix.

## Failure and retry

Provider and tool failures checkpoint the conversation tail. `retry()` continues that tail without appending the user's message again.

```python
try:
    await session.ask("Complete the migration.")
except Exception:
    async for event in session.retry():
        consume(event)
```

Compaction failures use `compaction()` instead. A blocked compaction must succeed before new user work is accepted.

## Session close

`aclose()` cancels only this session's background jobs, releases its resource lease, closes MCP server connections it opened, and unbinds its credentials and tracer. It does not shut down process-global browser or job runners owned by another session.




## Events and driving patterns

`Session.stream()`, `resume()`, `compaction()`, and `retry()` yield a closed `TurnEventUnion`. Dispatch on the variant class; the `EventType` enum is available for generic transports.

| Event | Meaning | Typical action |
| --- | --- | --- |
| `TextChunk` | Assistant text delta | Paint immediately |
| `Thinking` / `ThinkingDone` | Reasoning delta and boundary | Update a collapsible reasoning region |
| `ToolCall` | Partial or complete tool request | Create or update one tool card by id |
| `ToolResult` | Tool completion | Close the matching card |
| `Suspended` | Durable permission or question batch | Collect decisions, call `respond()`, then `resume()` |
| `Steering` | Mid-turn user message accepted | Reconcile optimistic UI by message id |
| `Usage` | Latest request and cumulative token and cache data | Update usage telemetry |
| `CompactionStarted` / `CompactionDone` | Context compaction lifecycle | Show compaction state and reclaimed size |
| `GoalReviewStarted` / `GoalReviewProgress` / `GoalReviewFinished` | Independent goal review | Render review status separately from assistant prose |
| `Checkpoint` | Tool batch became durable | Commit a product high-water mark if needed |
| `Error` | Structured turn or tool failure | Render its code and parameters |
| `Done` | One model turn completed | Read final text and stop this stream |

`Done` ends a turn, not the session. Autonomous goal or task continuation can produce several `Done` events inside one `Session.stream()` call. The session returns to `idle` only after continuation policy stops.

```python
async def drive(session, message):
    async for event in session.stream(message):
        match event:
            case TextChunk(text=text):
                await client.text(text)
            case ToolCall(id=call_id, name=name, arguments=arguments):
                await client.tool(call_id, name, arguments)
            case Suspended(interactions=interactions):
                for gate in interactions:
                    decision = await client.decide(gate)
                    await session.respond(gate.request_id, decision)
            case Error(code=code, message=message):
                await client.error(code, message)

    if session.state.pending and session.state.pending.ready:
        async for event in session.resume():
            await client.event(event)
```

An application may stop consuming events without interrupting the runtime only if it continues draining in another task. To stop work, call `Session.interrupt()`; cancellation closes the provider stream, records a cancelled transcript turn, checkpoints the closed exchange, and returns the session to `idle`.


## Models, credentials, and cache behavior

### Select a provider model

Set `provider` and `model` on `AgentConfiguration` when LangMesh should build the provider adapter. `model_identifier="provider/model"` on `Session` overrides that profile for one run.

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

The `providers` mapping is copied into the session's `Configuration`; the caller's value is never mutated. Environment variables still take precedence. Account-backed providers use the replaceable `Credentials` port in `SessionComponents`.

### Supply a model object

Pass any LangChain `BaseChatModel` through `SessionComponents.model` when the application owns provider construction, routing, retries, or testing.

```python
components = SessionComponents(model=application_model)
session = Session(agent_without_provider, directory="/srv/checkout", components=components)
```

The model must implement `bind_tools()` and streaming. LangMesh binds one stable ordered tool schema when the runtime is constructed.

### Preserve provider caches

The static system prompt and tool schema form the reusable prefix. LangMesh preserves that prefix by construction:

- `RuntimeComponents` is frozen and snapshots sequence fields.
- Prior conversation messages are append-only until an explicit compaction.
- Permission and goal reviewers inherit the main conversation and stable tool schema, then append their private instructions.
- A tool granted to a session is described by an appended conversation message, not a schema change, so the prefix holds at any moment. See [Granting a tool to a session](composition.md#granting-a-tool-to-a-session).
- Steering appends at a provider boundary; it never edits an earlier message.
- Permission-mode and location changes apply during execution without rewriting model history.

`PromptComposer` runs only when the cached system prompt is built. Call `Session.refresh_prompt()` after changing an external source that the composer reads; that explicit refresh invalidates the static prompt cache. A `BeforeModelHook` runs on every request and can intentionally change the prefix, so cache-sensitive hooks should leave the first system message untouched.

Usage events expose provider-reported cache reads, the reachable prefix, and the first divergence from the preceding request. Use these values to verify a custom model adapter instead of inferring cache behavior from latency alone.
