# Compaction and continuation

History management has two independent policies: preparation establishes that durable knowledge is safe; compaction chooses which model messages remain.

### Preparation

`CompactionPreparation` has four operations:

```python
class CompactionPreparation:
    def instruction(self, default: str) -> str | None: ...
    async def baseline(self): ...
    async def completed(self, baseline) -> bool: ...
    async def describe(self) -> dict: ...
```

`Session` and the daemon default to `ObservationCompactionPreparation`, which captures the observational-memory revision, runs the private preparation instruction with local foreground Bash only, and compacts only after the revision advances. `AgentRuntime` used directly defaults to `DirectCompactionPreparation`, which opens no preparation turn.

Choose direct compaction explicitly when the application has no external memory handoff:

```python
from langmesh import DirectCompactionPreparation, SessionComponents


components = SessionComponents(
    compaction_preparation=DirectCompactionPreparation(),
)
```

For another durable system, implement the port. `baseline()` must return checkpoint-safe data; `completed()` must prove the handoff committed after that baseline. Returning `None` for the instruction records preparation immediately.

### Compaction

`Compaction.should_compaction(state)` is the cheap automatic trigger. `compaction(state)` returns the model messages retained after a compaction.

```python
class KeepDecisions:
    def should_compaction(self, state):
        return state.context_window > 0 and state.context_tokens > state.context_window * 0.7

    async def compact(self, state):
        decisions = [message for message in state.messages if is_decision(message)]
        return [await summarize(state.messages), *decisions]
```

The runtime rejects a strategy that reclaims no messages, restores the original conversation on failure, emits `CompactionDone(ok=False)`, and blocks later input until `Session.compaction()` succeeds.

With the built-in compaction, the runtime appends one private compaction instruction to the existing conversation and asks the model to answer with a `submit_compaction_summary` tool call. That request is the system prompt, the whole existing conversation, and one appended instruction, so the provider-cache prefix is preserved and only the new tail is uncached. The collected summary then continues the session as the system prompt, the summary, and the newest turns in that order. The summary sits as the first message after the system prompt, becomes part of the cached leading block, and is never a user-visible chat row.

The tool is carried on every request, alongside the other internal verdict tools, so the schema and therefore the cache prefix never changes. It exists only in the summarizer's lane: it is granted to that hidden session as a `ToolGrant`, so outside a compaction instruction there is nothing to call and no no-op verdict to enforce. See [Granting a tool to a session](composition.md#granting-a-tool-to-a-session).

The summary is best-effort by construction. A provider error, an empty reply, or a model that writes prose instead of calling the tool falls back to the plain tail compaction, which never blocks the session. It always runs on the built-in compaction; supply your own distillation through `SessionComponents(compaction_summarizer=...)` to replace the model call:

```python
from langchain_core.messages import SystemMessage

from langmesh import CompactionSummaryState, SessionComponents


class ServiceSummarizer:
    async def summarize(self, state: CompactionSummaryState) -> str | None:
        reply = await cheaper_model.ainvoke(
            [SystemMessage(content=state.system_prompt), *state.messages]
        )
        return str(reply.content or "").strip() or None


components = SessionComponents(compaction_summarizer=ServiceSummarizer())
```

Keep the summary request as real messages rather than a rendered string, so the provider-cache prefix survives the custom call too. A custom `Compaction` strategy owns the whole compaction instead, including whether a summary message exists; the summarizer port only decorates the built-in policy.

### Goal and task continuation

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


## Resources and persistence

### Workspace resources

`WorkspaceResourcesLike` owns the location's files, instructions, skills, MCP server declaration, attachments, and observational memory. Local resources expose their path directly. Other fsspec backends are materialized once for path-native tools and synchronized after completed tool batches and at close.

```python
from langmesh import Session, WorkspaceResources


resources = WorkspaceResources.memory(
    {
        "README.md": "# Review target",
        ".agents/instructions/review.md": "Cite every finding.",
    }
)

async with Session(agent, resources=resources) as session:
    await session.ask("Add a concise usage section.")

updated = await resources.read("README.md")
```

`OverlayResources(base, writable=upper)` composes read layers with one writable upper layer. Remote adapters may supply a push-based `ResourceChangeSource`; LangMesh does not poll when watching is unsupported.

Call `refresh_resources()` at an idle boundary to adopt external changes, and `sync_resources()` to publish materialized changes immediately.

### Observational memory

`session.observations` is a configured `ObservationRegistry` over the same resources. `describe()` returns bounded metadata, `load()` returns the validated current snapshot, and `watch()` yields committed revisions without polling.

```python
descriptor = await session.observations.describe()
snapshot = await session.observations.load()

async for changed in session.observations.watch():
    publish(changed["revision"], changed["entries"])
```

These APIs are read-only. The agent changes `.agents/observations.sqlite` through the documented atomic Bash protocol.

### Checkpoints

`Checkpoints` stores the conversation, goal, tasks, compaction control, retry state, and any suspended `PendingTurn`.

```python
class RedisCheckpoints:
    async def save(self, session_id, state):
        await redis.set(f"langmesh:{session_id}", json.dumps(state))

    async def load(self, session_id):
        value = await redis.get(f"langmesh:{session_id}")
        return json.loads(value) if value else None
```

Pass the same session id and store to resume in another `Session` instance.

### Transcript and audit

`Transcript` records one `TurnSummary` per completed or cancelled turn. It is not an A2A task store; the daemon translates core events into its product transcript through adapters.

`Observer` receives transient audit `Observation` values. It may return an awaitable, but its failure is logged and cannot fail the turn.

### Background jobs

`JobStore` records detached work. `MemoryJobStore` is process-local; a durable implementation enables restart recovery. `Session.background_jobs()` exposes active snapshots, while completion is delivered to the model through the normal turn path.

### Workspaces and file leases

`SessionWorktreeManager` is the standard opt-in workspace implementation:

```python
components = SessionComponents(workspace=SessionWorktreeManager())
session = Session(agent, directory="/srv/checkout", components=components)
runtime_directory = await session.prepare_worktree("worktree")
```

Prepare the workspace before the runtime is built. `FileLeases` coordinates mutations across sessions; `FileLeaseManager` is the standard implementation.
