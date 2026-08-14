# Compaction and continuation

History management has two independent policies: preparation establishes that durable knowledge is safe; compaction chooses which model messages remain.

## Preparation

`CompactionPreparation` has four operations:

```python
class CompactionPreparation:
    def instruction(self, default: str) -> str | None: ...
    async def baseline(self): ...
    async def completed(self, baseline) -> bool: ...
    async def describe(self) -> dict: ...
```

`Session` and the daemon default to `ObservationCompactionPreparation`, which captures the observational-memory revision, runs the private preparation instruction with local foreground Bash only, and folds only after the revision advances. `AgentRuntime` used directly defaults to `DirectCompactionPreparation`, which opens no preparation turn.

Choose direct folding explicitly when the application has no external memory handoff:

```python
from langmesh import DirectCompactionPreparation, SessionComponents


components = SessionComponents(
    compaction_preparation=DirectCompactionPreparation(),
)
```

For another durable system, implement the port. `baseline()` must return checkpoint-safe data; `completed()` must prove the handoff committed after that baseline. Returning an instruction of `None` records preparation immediately.

## Folding

`Compaction.should_compact(state)` is the cheap automatic trigger. `compact(state)` returns the model messages retained after a fold.

```python
class KeepDecisions:
    def should_compact(self, state):
        return state.context_window > 0 and state.context_tokens > state.context_window * 0.7

    async def compact(self, state):
        decisions = [message for message in state.messages if is_decision(message)]
        return [await summarize(state.messages), *decisions]
```

The runtime rejects a strategy that reclaims no messages, restores the original conversation on failure, emits `CompactionDone(ok=False)`, and blocks later input until `Session.compact()` succeeds.

## Goal and task continuation

`ContinuationPolicy` independently decides whether an open goal and actionable tracked tasks may start another autonomous turn.

```python
class ServiceContinuationPolicy:
    def continue_goal(self, goal, completed_turns):
        return goal is not None and goal.is_open and completed_turns < 4

    def continue_tasks(self, unfinished_tasks, completed_turns):
        return bool(unfinished_tasks) and completed_turns < 8


components = SessionComponents(continuations=ServiceContinuationPolicy())
```

The standard `TuningContinuationPolicy` reads the independent goal and task allowances from active tuning. A goal is reviewed before its continuation message is accepted. When goal and task work are both due, LangMesh composes them into one next turn so the obligations do not race or consume each other's allowance.

A new user message restores both allowances. `await Session.clear_goal()` calls off a goal and checkpoints its final state before returning.
