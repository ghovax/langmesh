# Compaction and continuation

History management has two independent policies: preparation establishes that durable knowledge is safe; compaction chooses which model messages remain. Both are features you compose.

### Preparation

`CompactionPreparation` has four operations:

```python
class CompactionPreparation:
    def instruction(self, default: str): ...
    async def baseline(self): ...
    async def completed(self, baseline): ...
    async def describe(self): ...
```

`ObservationCompactionPreparation` is the standard handoff: it captures the observational-memory revision, runs the private preparation instruction with local foreground Bash only, and compacts only after the revision advances. `DirectCompactionPreparation` opens no preparation turn.

Choose direct compaction explicitly when the application has no external memory handoff. It is the default `Compaction` preparation:

```python
from langmesh.runtime.plugins.compaction import Compaction, DirectCompactionPreparation

components = SessionComponents(
    features=[Compaction(strategy=None, preparation=DirectCompactionPreparation(), summarizer=None)],
)
```

For another durable system, implement the port. `baseline()` must return checkpoint-safe data; `completed()` must prove the handoff committed after that baseline. Returning `None` for the instruction records preparation immediately.

### Compaction

`Compaction.should_compact(state)` is the cheap automatic trigger. `compact(state)` returns the model messages retained after a compaction.

```python
class KeepDecisions:
    def should_compact(self, state):
        return state.context_window > 0 and state.context_tokens > state.context_window * 0.7

    async def compact(self, state):
        decisions = [message for message in state.messages if is_decision(message)]
        return [await summarize(state.messages), *decisions]
```

The `Compaction` feature rejects a strategy that reclaims no messages, restores the original conversation on failure, emits `CompactionDone(ok=False)`, and blocks later input until the fold succeeds.

With the built-in strategy, the runtime appends one private compaction instruction to the existing conversation and asks the model to answer with a `submit_compaction_summary` tool call. That request is the system prompt, the whole existing conversation, and one appended instruction, so the provider-cache prefix is preserved and only the new tail is uncached. Once collected, the older turns are dropped and the session continues with the system prompt, the summary, and the recent working set word for word. The summary sits as the first message after the system prompt, becomes part of the cached leading block, and is never a user-visible chat row.

The verdict tool exists only in the summarizer's lane and is bound into that hidden session's provider schema, so the working session never carries a no-op verdict tool. See [Granting a tool to a session](composition.md#granting-a-tool-to-a-session).

The summary is best-effort by construction. A provider error, an empty reply, or a model that writes prose instead of calling the tool falls back to the plain tail compaction, which never blocks the session. Supply your own distillation through the `Compaction` feature's `summarizer` port to replace the model call:

```python
from langchain_core.messages import SystemMessage

from langmesh import CompactionSummaryState
from langmesh.runtime.plugins.compaction import Compaction


class ServiceSummarizer:
    async def summarize(self, state: CompactionSummaryState):
        reply = await cheaper_model.ainvoke(
            [SystemMessage(content=state.system_prompt), *state.messages]
        )
        return str(reply.content or "").strip() or None


components = SessionComponents(
    features=[Compaction(strategy=None, preparation=DirectCompactionPreparation(), summarizer=ServiceSummarizer())],
)
```

Keep the summary request as real messages rather than a rendered string, so the provider-cache prefix survives the custom call too. A custom `Compaction` strategy owns the whole compaction instead, including whether a summary message exists; the summarizer port only decorates the built-in policy.

### Goal and task continuation

`ContinuationPolicy` independently decides whether an open goal and actionable tracked tasks may start another autonomous turn, and is passed to the `Continuation` feature:

```python
from langmesh.runtime.plugins.continuation import Continuation, DefaultContinuationPolicy

# The shipped policy keeps open goals and unfinished tasks going on their own.
components = SessionComponents(features=[Continuation(policy=DefaultContinuationPolicy())])
```

The standard policy keeps an open goal and unfinished tracked tasks going on their own, with no hard turn cap. Each plugin that has a next obligation contributes its own message — the goal its reminder or review prose, tracked tasks their note — staged as separate messages within one next turn, so the obligations never race or open competing turns.

A new user message resumes a parked goal. Clearing a goal goes through the goal feature (`update_goal` sets it; the review that closes it is separate).

## Persistence adapters

The library never selects a user file, walks a home directory, or starts its own database. `SessionComponents` accepts structural interfaces for checkpoints, artifacts, background jobs, transcripts, credentials, file leases, peer sessions, attachments, and other application-owned behavior. The defaults are memory implementations where a neutral default is meaningful; the daemon supplies its own durable adapters at its composition root.

### Checkpoints

`Checkpoints.save()` and `Checkpoints.load()` exchange a typed `SessionCheckpoint`, not an undocumented mapping. Its `conversation`, `session`, and `pending` fields are explicit. `SessionSnapshot` in turn exposes the live permission mode, typed feature values, retry state, provider cache state, the exact stable instructions with their construction revision, and any accepted `PendingInput` that has not yet joined the conversation. Dynamic session context is an ordinary marked message in `conversation`, so a changed value appends rather than mutating the restored prefix.

```python
import sqlite3
from langmesh import Session, SessionComponents, SQLiteCheckpoints

connection = sqlite3.connect("sessions.sqlite")
components = SessionComponents(checkpoints=SQLiteCheckpoints(connection))
session = Session(agent, session_id="review-1", directory="/srv/checkout", components=components)
```

The connection is caller-owned, so `sqlite3.connect(":memory:")` and a file connection use the same adapter and lifecycle. `SQLiteCheckpoints` performs each replacement in an explicit transaction. `MemoryCheckpoints` makes defensive typed copies so its behavior matches a serialized adapter rather than leaking mutable aliases.

A remote adapter implements the same protocol and uses `to_data()` and `from_data()` only at its transport boundary:

```python
from langmesh import SessionCheckpoint

class RedisCheckpoints:
    async def save(self, session_id: str, checkpoint: SessionCheckpoint):
        await redis.set(f"langmesh:{session_id}", json.dumps(checkpoint.to_data()))

    async def load(self, session_id: str):
        value = await redis.get(f"langmesh:{session_id}")
        return SessionCheckpoint.from_data(json.loads(value)) if value else None
```

### Artifacts

`Artifacts.create()` returns an incremental `ArtifactWriter`; closing it yields a typed `ArtifactReference`, and `Artifacts.read()` gives the embedding the bytes by identifier. The default `MemoryArtifacts` performs no filesystem I/O. Tools return artifact identifiers instead of inventing output paths, including `download`, fetched-page overflow, and Bash logs.

```python
from langmesh import MemoryArtifacts, SessionComponents

artifacts = MemoryArtifacts()
components = SessionComponents(artifacts=artifacts)
session = Session(agent, directory="/srv/checkout", components=components)

writer = await session.artifacts.create("report.txt", "text/plain")
await writer.write(b"application-owned output")
reference = await writer.close()
content = await session.artifacts.read(reference.identifier)
```

The daemon's `FileArtifacts` adapter chooses a daemon-owned directory, serializes concurrent writes, flushes file and directory metadata, and publishes a completed artifact with an atomic replace. A different application can store the same stream in object storage, a database, or another process without changing a tool.

### Write-ahead boundaries

An accepted input is checkpointed as `PendingInput` before the daemon publishes working state. The runtime then checkpoints before every provider request, before executing an announced tool batch, after tool results, and after appending a background result. A live permission-mode change commits before it reaches execution. A result's job record is marked delivered only after the conversation checkpoint that contains it commits. Restart replay is therefore idempotent: an input or result is either absent and replayable, or present and acknowledged, never acknowledged but missing.

The daemon's SQLite connections use WAL, `synchronous=FULL`, foreign-key enforcement, a bounded busy timeout, explicit SQLAlchemy transactions, and startup integrity checks. SQLite already provides atomicity, consistency, isolation, and durability through transactions; these settings choose the strongest ordinary local durability instead of relying on ambient defaults. Cache-only in-memory indexes are advanced only after their SQL transaction exits successfully.

Checkpoint decoders reject malformed enums, missing typed fields, and invalid collection shapes instead of silently dropping corrupt state. Memory adapters recursively detach nested values so a caller cannot mutate a saved checkpoint through an alias. `Session.aclose()` restores before saving when necessary and discards the runtime only after that save commits, which makes the ordinary act/save/close/reload cycle lossless and makes a close failure retryable.

LangGraph checkpointers solve graph-superstep persistence. LangMesh does not execute a `StateGraph`, so installing a second checkpointer would duplicate and potentially disagree with the runtime's provider, tool, suspension, and background-job boundaries. The `Checkpoints` port deliberately provides the same application-level choice of in-memory or durable storage without coupling the turn loop to LangGraph.

### Transcript, audit, and background jobs

`Transcript` records one `TurnSummary` per completed or cancelled turn. `Observer` receives transient audit `Observation` values and cannot fail a turn. `BackgroundJobs` records detached work through `JobStore`; `MemoryJobStore` is process-local, while a durable implementation enables restart recovery. Duplicate job identifiers are rejected before their coroutine starts, preventing an idempotent retry from repeating an external effect.

Filesystem mutation is never an implicit persistence behavior of these interfaces. An explicit tool may of course modify a path the caller authorized, and the library reads its own shipped prompt assets as package resources. Project catalogues, observation databases, configuration files, worktrees, uploads, and daemon state remain application-layer concerns.
