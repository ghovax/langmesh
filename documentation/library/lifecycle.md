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
