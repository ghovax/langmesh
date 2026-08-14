# Events and driving patterns

`Session.stream()`, `resume()`, `compaction()`, and `retry()` yield a closed `TurnEventUnion`. Dispatch on the variant class; the `EventType` enum is available for generic transports.

| Event | Meaning | Typical action |
| --- | --- | --- |
| `TextChunk` | Assistant text delta | Paint immediately |
| `Thinking` / `ThinkingDone` | Reasoning delta and boundary | Update a collapsible reasoning region |
| `ToolCall` | Partial or complete tool request | Create or update one tool card by id |
| `ToolResult` | Tool completion | Close the matching card |
| `Suspended` | Durable permission or question batch | Collect decisions, call `respond()`, then `resume()` |
| `Steering` | Mid-turn user message accepted | Reconcile optimistic UI by message id |
| `Usage` | Latest request and cumulative token/cache data | Update usage telemetry |
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
