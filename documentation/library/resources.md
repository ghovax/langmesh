# Resources and persistence

## Workspace resources

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

## Observational memory

`session.observations` is a configured `ObservationRegistry` over the same resources. `describe()` returns bounded metadata, `load()` returns the validated current snapshot, and `watch()` yields committed revisions without polling.

```python
descriptor = await session.observations.describe()
snapshot = await session.observations.load()

async for changed in session.observations.watch():
    publish(changed["revision"], changed["entries"])
```

These APIs are read-only. The agent changes `.agents/observations.sqlite` through the documented atomic Bash protocol.

## Checkpoints

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

## Transcript and audit

`Transcript` records one `TurnSummary` per completed or cancelled turn. It is not an A2A task store; the daemon translates core events into its product transcript through adapters.

`Observer` receives transient audit `Observation` values. It may return an awaitable, but its failure is logged and cannot fail the turn.

## Background jobs

`JobStore` records detached work. `MemoryJobStore` is process-local; a durable implementation enables restart recovery. `Session.background_jobs()` exposes active snapshots, while completion is delivered to the model through the normal turn path.

## Workspaces and file leases

`SessionWorktreeManager` is the standard opt-in workspace implementation:

```python
components = SessionComponents(workspace=SessionWorktreeManager())
session = Session(agent, directory="/srv/checkout", components=components)
runtime_directory = await session.prepare_worktree("worktree")
```

Prepare the workspace before the runtime is built. `FileLeases` coordinates mutations across sessions; `FileLeaseManager` is the standard implementation.
