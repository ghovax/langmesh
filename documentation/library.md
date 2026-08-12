# LangMesh as a library

**The library is the bottom of the stack, and everything else is built on it** — see the [documentation index](README.md) for the four layers and what each knows about your machine.

`Session` runs an agent in your own process. It reads no path you did not give it, resolves no name against anything, and writes nothing merely because it was imported or constructed. An agent can deliberately maintain workspace-owned observational memory with its ordinary Bash capability; LangMesh does not hide that file behind a special tool or session database. That is what makes it embeddable without divorcing durable project knowledge from the project.

Everything a session needs can be built in code:

```python
import asyncio
from langmesh import AgentConfiguration, Catalogue, FilesystemConfiguration, SandboxConfiguration, Session

reviewer = AgentConfiguration(
    name="reviewer",
    description="Reads a change and reports what it would break.",
    system_prompt="You review changes. Name the risk, or say there is none.",
    sandbox=SandboxConfiguration(filesystem=FilesystemConfiguration(writable=[])),
    provider="anthropic",
    model="claude-opus-4-5",
)

async def main() -> None:
    async with Session(
        reviewer,
        directory="/srv/checkout",
        catalogue=Catalogue(agents={"reviewer": reviewer}),
        providers={"anthropic": "sk-ant-…"},
    ) as session:
        answer = await session.ask("What would break if I removed the retry loop in the fetcher?")

asyncio.run(main())
```

No configuration file and no `$HOME`. The agent, its prompt, its permission mode and its credentials are all values in the program. The `.agents` observation store is created only if the agent explicitly records an observation.

Use it for three things:

- Embed the harness in another program.
- Write a terminal interface that shares code with the browser one, instead of reimplementing it.
- Run a one-shot agent in a script.

## Taking what the machine has

A program that _is_ running on someone's machine — a CLI, a scheduled job — can ask for the machine's agents deliberately. The import says what it is doing:

```python
from langmesh import Session
from langmesh.daemon.machine import load_agent, load_catalogue, load_configuration

custom_configuration = load_configuration(seed=False)
directory = "/Users/you/code/project"

async with Session(
    load_agent("general-assistant", directory, configuration=custom_configuration),
    directory=directory,
    configuration=custom_configuration,
    catalogue=load_catalogue(custom_configuration, directory),
) as session:
    answer = await session.ask("What does this project do?")
```

Four lines that touch the machine, each one written by you. `langmesh.daemon.machine` is the only module that knows XDG exists, and `langmesh.Session` never imports it.

## What you give up

A library session is an object, not a process. It has none of the three properties `langmeshd` exists to provide:

| Property                             | Library                                            | Daemon                                   |
| ------------------------------------ | -------------------------------------------------- | ---------------------------------------- |
| Addressable from outside             | No                                                 | Yes — a socket, a token, `langmesh send` |
| Outlives the program that made it    | No                                                 | Yes                                      |
| Crash-isolated                       | No — a tool that exhausts memory takes you with it | No — sessions share the daemon's process |
| Peers (`create_session` and friends) | Only if you supply `peers`                         | Yes                                      |
| Confinement of tool children         | **Identical**                                      | **Identical**                            |

Confinement surprises people, so here it is plainly. A session was never sandboxed; its _tool children_ are, and a child is confined at the moment it is spawned. That is the same code on both paths, and it is why hosting sessions together costs nothing in confinement.

## The seams

Everything durable that belongs to a session is a constructor argument with an interface behind it. Workspace resources are the location-level seam: skills, memories, instructions, MCP configuration, observational memory, attachments, and all path-native tool work resolve through one `WorkspaceResourcesLike` interface. `WorkspaceResources` is the standard adapter and uses fsspec's `AbstractFileSystem`, `FSMap`, protocol registry, and transaction API instead of defining another storage ecosystem. Bash remains intentionally POSIX-facing; non-local resources are materialized into a leased temporary directory and changes are published through fsspec after every completed tool batch and at session close.

```python
from langmesh import Session, WorkspaceResources

resources = WorkspaceResources.from_url("memory://review-workspace")
await resources.write(".agents/memories/conventions.md", b"Use public interfaces.\n")
await resources.write("src/example.py", b"def answer():\n    return 42\n")

async with Session(reviewer, resources=resources, providers={"anthropic": "sk-ant-…"}) as session:
    answer = await session.ask("Review src/example.py")
    await session.refresh_resources()  # explicit safe boundary for changes made outside the lease
```

`WorkspaceResources.local(path)`, `.memory(files)`, `.from_url(url, **storage_options)`, and `.from_mapper(fsspec.FSMap)` cover the common adapters. `OverlayResources(base, writable=upper)` composes read layers with one writable upper. Optional fsspec drivers such as `s3fs`, `gcsfs`, `adlfs`, and `sshfs` remain application dependencies rather than LangMesh dependencies. A custom implementation may satisfy `WorkspaceResourcesLike` directly, but path normalization, event readiness, and transactional publication remain its contract.

The observational-memory read side is an initialized object, not a collection of path-taking helpers. `ObservationRegistry(resources)` binds configuration and lifetime once. `describe()` validates the SQLite storage and returns only path, existence, revision, per-ledger counts, and the earliest/latest update timestamps without loading payloads; `load()` additionally validates every timestamp and payload and returns `{"revision": int, "entries": {"observations": [...], "directives": [...]}}` without creating a file; `watch()` installs its event subscription before the initial read and then yields only distinct fully validated committed snapshots with the same descriptor under `metadata`, without polling. This separation makes progressive disclosure explicit: use the descriptor to decide whether targeted retrieval is warranted instead of injecting or loading an ever-growing ledger. A configured session exposes the same object as `session.observations`:

```python
from langmesh import ObservationRegistry, ObservationRegistryError, WorkspaceResources

resources = WorkspaceResources.local("/srv/checkout")
registry = ObservationRegistry(resources)

async def registry_summary() -> dict:
    return await registry.describe()

async def current_constraints() -> list[str]:
    snapshot = await registry.load()
    return [
        entry["claim"]
        for entry in snapshot["entries"]["observations"]
        if entry["category"] == "constraint"
    ]

async def mirror_registry() -> None:
    try:
        async for snapshot in registry.watch():
            await publish(snapshot["revision"], snapshot["entries"])
    except ObservationRegistryError as error:
        await report_registry_problem(str(error))
```

Construct `ObservationRegistry(custom_resources, configuration=custom_configuration)` for a virtual backend. For watching, the backend must expose a push-based `ResourceChangeSource`; an object-store adapter can bridge S3 events, Pub/Sub, inotify on a mounted volume, or another provider-native feed. Subscription construction is the readiness boundary: the adapter must be listening before `subscribe()` returns, and it must publish only after the corresponding object transaction is readable. Backends without such a source raise `ResourceWatchUnsupported` rather than silently polling.

These APIs intentionally do not write. Mutation stays compositional: an agent uses Bash, Python's standard `sqlite3`, the schema in the `observational-memory` skill, and one atomic transaction. The daemon uses the same validated reader behind one shared watcher per active location; it broadcasts full revision snapshots to clients and queues schema feedback for each live agent's next safe model-call boundary.

The constructor accepts `configuration=custom_configuration`; use that when `AGENTS_ROOT_DIRECTORY` is not the default `.agents`, so lookup and watching resolve the same location-specific registry as `Session`.

| Argument             | Interface                                                                            | Default                                                                                     | What it decides                                                                                                |
| -------------------- | ------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------- |
| `model`              | LangChain [`BaseChatModel`](https://python.langchain.com/docs/concepts/chat_models/) | Built from configuration                                                                    | Which model runs, and everything wrapped around it — tracing, rate limiting, a stub in tests                   |
| `checkpoints`        | `langmesh.Checkpoints`                                                               | `MemoryCheckpoints`                                                                         | Where the conversation, goal and task state are saved, and therefore whether a session can resume              |
| `jobs`               | `langmesh.JobStore`                                                                  | `MemoryJobStore`                                                                            | Where background jobs are recorded, and therefore whether one survives a restart                               |
| `observer`           | `langmesh.Observer`                                                                  | None (dropped)                                                                              | Where the audit trail goes — auto-approvals, goal changes, messages                                            |
| `approvals`          | `langmesh.Approvals`                                                                 | None (gates suspend)                                                                        | Who answers a gated tool call when there is no human                                                           |
| `peers`              | `SessionAccess`                                                                      | None (composition tools absent)                                                             | How this session reaches other sessions                                                                        |
| `sandbox`            | `langmesh.base.confinement.Profile`                                                  | Unconfined profile                                                                          | What a tool's children may do                                                                                  |
| `catalogue`          | `langmesh.Catalogue`                                                                 | The working directory's `.agents` plus the packaged base layer — **and nothing of `$HOME`** | Where agents, skills, memories, instructions and prompt templates come from                                    |
| `providers`          | `{"anthropic": "sk-..."}` or `{"custom": {"api_key": ..., "base_url": ...}}`         | Whatever the machine is configured with                                                     | Provider credentials, in code                                                                                  |
| `model_identifier`   | `"provider/model"`                                                                   | The agent profile's own                                                                     | Which model this session runs, overriding the profile                                                          |
| `configuration`      | `Configuration`                                                                      | Fresh in-memory defaults, with the per-session toolbox disabled                             | Providers, tuning and machine-level capabilities                                                               |
| `agent`              | `AgentConfiguration`                                                                 | — (required)                                                                                | The agent itself: prompt, model, permission mode, which built-in tools it has                                  |
| `tools`              | LangChain [`BaseTool`](https://python.langchain.com/docs/concepts/tools/)            | None                                                                                        | Tools the agent gains, on top of the harness's                                                                 |
| `permissions`        | A `PermissionEvaluator`-shaped object                                                | The built-in rule engine                                                                    | Whether a call is gated at all                                                                                 |
| `supplied_tool_gate` | `"ask"` / `"none"`                                                                   | `"ask"`                                                                                     | Whether a supplied tool raises a gate before it runs                                                           |
| `transcript`         | `langmesh.Transcript`                                                                | `MemoryTranscript`                                                                          | Where the record of completed turns goes                                                                       |
| `credentials`        | `langmesh.Credentials`                                                               | A `0600` file under XDG                                                                     | Where account tokens live (bypassed entirely by `model=`)                                                      |
| `locations`          | `LocationExecutor` records                                                           | Local, at `directory`                                                                       | Where tools may run — SSH, containers                                                                          |
| `resources`          | `WorkspaceResourcesLike`, normally fsspec-backed                                     | Local `directory`                                                                           | Where location-owned files and databases live; how a POSIX tool view is materialized, synchronized and watched |
| `workspace`          | `SessionWorkspaceManager`                                                            | None — **opt in via `prepare_workspace()`**                                                 | A git worktree per session                                                                                     |
| `tracer_provider`    | OpenTelemetry `TracerProvider`                                                       | The process-wide one, if configured                                                         | Where spans go, per session                                                                                    |

Two of these are interfaces we did not write. `BaseChatModel` is LangChain's, and the a2a `TaskStore` behind the daemon's turn record is a2a's. Where the ecosystem already has an interface, wrapping it would only add a second vocabulary for the same thing.

The rest are `typing.Protocol`s, which is the part that matters for you: they are _structural_. Your object satisfies one by having the right methods. There is no base class to inherit, no registry to join, and no import of LangMesh in your type.

```python
class RedisCheckpoints:
    def __init__(self, client):
        self._client = client

    async def save(self, session_id, state):
        await self._client.set(f"langmesh:{session_id}", json.dumps(state))

    async def load(self, session_id):
        raw = await self._client.get(f"langmesh:{session_id}")
        return json.loads(raw) if raw else None

session = Session(reviewer, directory="/srv/checkout", checkpoints=RedisCheckpoints(redis))
```

That class inherits nothing and imports nothing of ours. The harness accepts it because it has `save` and `load`. One that lacks `load` fails at the constructor, by name:

```text
TypeError: checkpoints: RedisCheckpoints does not satisfy Checkpoints: it is missing `load`.
```

Structural typing gives no compile-time guarantee. The check happens once per session rather than failing part-way through a turn, far from the call that supplied it.

### Your own tools

The one thing configuration cannot do is _extend_. `tools=` takes LangChain `BaseTool`s — adopted, not wrapped, so anything already written for that ecosystem works unchanged:

```python
from langchain_core.tools import tool
from langmesh import Session

@tool
def open_incidents(service: str) -> str:
    """Every open incident for a service, newest first."""
    return incidents.query(service=service, status="open")

async with Session(reviewer, directory="/srv/checkout", tools=[open_incidents]) as session:
    answer = await session.ask("Are there open incidents on the checkout service?")
```

A supplied tool goes through the _same_ preamble as every built-in: permission resolved, location resolved, policy applied. The extension point is the handler, not the pipeline. Two consequences follow:

- **It is gated unless you say otherwise.** The permission engine recognises calls by tool name, and it does not know yours, so there is no honest way to infer what it does. The default is _ask_, so a new tool cannot silently widen what a session may do. Set `supplied_tool_gate="none"` to say otherwise deliberately.
- **It cannot shadow a built-in.** A tool named `bash` that is not this harness's `bash` is a confinement surprise, not an extension point. A name collision therefore resolves to ours.
- **The agent profile's `tools_enabled` list does not filter it.** That list narrows the _harness's_ capabilities, and someone wrote it before your program existed. Otherwise a supplied tool disappears for every agent that names an explicit list.

### Building the agent itself

`agent=` takes an `AgentConfiguration` you construct. Nothing on the machine is consulted — the agent is a value your program owns:

```python
from langmesh import AgentConfiguration, BashToolConfiguration, FilesystemConfiguration, SandboxConfiguration, Session, ToolsConfiguration

reviewer = AgentConfiguration(
    name="reviewer",
    provider="anthropic",
    model="claude-sonnet-4",
    system_prompt="You review code. Be terse.",
    sandbox=SandboxConfiguration(filesystem=FilesystemConfiguration(writable=[])),
    tools_enabled=["bash", "fetch_url"],
    tools=ToolsConfiguration(
        disabled=["fetch_url"],
        bash=BashToolConfiguration(enabled=False, background_allowed=False),
    ),
)

async with Session(reviewer, directory="/srv/checkout") as session:
    answer = await session.ask("What changed on this branch, and is it safe to ship?")
```

Under-specify it and the error says what to do rather than failing obscurely:

```text
ValueError: Agent 'reviewer' names no model. Set `provider` and `model` in its profile, pass
`model_identifier="provider/model"` to `langmesh.Session`, or hand the runtime a `model=` of your own.
```

**Narrowing the built-in tools** has two complementary forms. `tools_enabled` is an allow-list, so naming one ordinary tool means naming every ordinary tool the profile needs. `tools.disabled` is a deny-list — right when an agent should have everything _except_ shell access. Both are enforced twice. A private pre-fold segment temporarily exposes only Bash as a context-safety protocol capability; its calls still run inside the session's sandbox and may address only the active local location.

The roster decides what the model is offered. The gate decides what it may run. A model can call a tool it was never offered.

`permissions=` replaces the rule engine outright, for a program whose policy is its own. `Approvals` answers a gate once the engine has decided there should be one; `permissions=` decides whether there is one at all.

### The transcript

`Checkpoints` answers "resume this conversation". `Transcript` answers "what has this session done", with one entry per completed turn. Each entry records what was asked, what came back, how it ended, and what it cost:

```python
session_id = "session-8f9c724a-ce51-41b3-83a9-f5969b22a9e2"

async with Session(reviewer, directory="/srv/checkout", session_id) as session:
    await session.ask("Audit the dependency tree and flag anything unmaintained.")

for turn in await session.transcript.turns(session_id):
    total_tokens = turn.input_tokens + turn.output_tokens
```

Deliberately **not** a2a's `TaskStore`. The daemon speaks A2A, and its record is rightly an A2A one. The library speaks no A2A. To hand it Tasks would add a protocol it does not use, for a problem it does not have.

### Credentials and the model

A library whose only way to be given an API key is a YAML file in the user's home directory is not a library. Pass them in:

```python
session = Session(
        reviewer,
    directory="/srv/checkout",
    providers={"anthropic": os.environ["ANTHROPIC_API_KEY"]},
    model_identifier="anthropic/claude-opus-4-5",
)
```

`providers` merges onto the configuration in play; it does not replace it. A program can therefore supply one key and inherit the rest. The providers' conventional environment variables keep the precedence they had, so a deployment that injects them continues to work. The long form takes a `base_url` too, for an OpenAI-compatible endpoint.

`model_identifier` overrides the agent profile's own choice. The common case for an embedder is one agent definition, run against whichever model _their_ program is configured for. To edit a profile file for a runtime choice is the wrong shape.

If you already hold a configured `BaseChatModel`, `model=` skips all of this — no credential of ours is consulted, because none is needed.

### The catalogue

One interface supplies everything the prompt is assembled from: the agent profile, the skills, the memories, the project's instructions, and the prompt templates. These differ in how the harness _parses_ them, not in how it _finds_ them.

The default matters more here than anywhere else. A library must not read another product's configuration out of your home directory, and must not walk hardcoded paths to find prompt material.

A library session's default catalogue therefore reads the working directory and the packaged agents, and nothing of `$HOME`. `langmeshd` and the CLI use `machine_catalogue`, which does read all of it, because there the person running it is the person those files describe.

Build one entirely in code when you want the prompt fully under your control:

```python
from langmesh import Catalogue, Session, Skill

catalogue = Catalogue(
    agents={"reviewer": reviewer},
    skills=[
        Skill(
            name="migration-safety",
            description="Check whether a schema change can be rolled back.",
            body=(
                "A migration is safe to ship only when it can be reversed without data loss."
                ...
            ),
        ),
    ],
    instructions="Always cite file and line.",
)
session = Session(reviewer, directory="/srv/checkout", catalogue=catalogue)
```

Unlisted prompt templates fall back to the packaged ones. You therefore opt in to replace the system prompt. You do not have to reproduce it to get started.

`FileCatalogue` is the other shipped implementation, and it reads a machine. It lives behind [`langmesh.daemon.machine`](#taking-what-the-machine-has), which is where machine-shaped things belong.

### Approvals

By default a gated tool call does what it does under the daemon: the turn emits a `Suspended` event and waits. That is right when a person watches, and wrong in a script. In a script the turn stops at a gate that nobody will answer. `ask()` therefore raises instead.

An approver decides gates in code. Answer `None` to give _no opinion_; that gate then suspends as before. You can therefore auto-approve what you understand, and still escalate the rest:

```python
from langmesh import Approval, Session

class AllowReads:
    async def decide(self, gate):
        if gate.kind == "permission" and set(gate.escape.writes) <= {"/tmp/build"}:
            return Approval(allow=True, reason="Reads are pre-approved for this job.")
        return None

async with Session(reviewer, directory="/srv/checkout", approvals=AllowReads()) as session:
    answer = await session.ask("Summarise the test failures on the current branch.")
```

An approver that raises escalates the gate rather than allowing it. A broken policy fails closed.

### Audit observer

`Observer` is the library's transient audit port; it does not read, generate, or persist workspace observational memory. It receives what the harness decided but did not say out loud: a bash command auto-approved and why, a goal set, a message appended. Turn _events_ are not this — those come out of `stream()`. The goal review is visible there as typed lifecycle and progress events, while the resulting standing is retained on the goal itself.

```python
class LogObserver:
    def observe(self, observation):
        logger.info("%s %s", observation.kind, observation.data)

session = Session(reviewer, directory="/srv/checkout", observer=LogObserver())
```

`observe` can return an awaitable. The harness schedules it; it does not await it. A synchronous implementation that appends to a list is the common case. One that writes to a database must not block the turn. An observer that raises is logged and ignored: a turn must not fail because its audit sink did.

### Telemetry and workspaces

`tracer_provider=` binds a tracer for this session rather than reconfiguring the process, so two sessions in one program can report to different places. `credentials=` is bound the same way. Both unbind when the session closes.

A git worktree per session is opt-in, because it writes to disk. Every other default here leaves nothing behind:

```python
session = Session(reviewer, directory="/srv/checkout")
runtime_directory = await session.prepare_workspace()
await session.ask("Refactor the parser to use the streaming reader, then run the tests.")
```

## Around the turn

Three seams sit around a turn rather than inside it. Each defaults to what the harness has always done, so passing none of them changes nothing.

### Bounding and watching a turn

A **hook** sees a turn as it runs, and may narrow it. It has three optional methods, and you implement only the one you need.

```python
from langmesh import MaximumToolCalls, Session

class AuditPrompts:
    async def before_model(self, messages):
        audit.write({"at": time.time(), "messages": len(messages)})
        return messages

class RefuseNetworkTools:
    async def before_tools(self, calls):
        return [call for call in calls if call["name"] not in ("fetch_url", "search_web")]

async with Session(
    reviewer,
    directory="/srv/checkout",
    hooks=[MaximumToolCalls(20), AuditPrompts(), RefuseNetworkTools()],
) as session:
    ...
```

`MaximumToolCalls` ships with the harness and has no privileges you do not — it is twelve lines that return a shorter list. That is deliberate. A cap as a `maximum_tool_calls=` argument would be the loop hardcoding one policy. A cap as a hook is proof the seam has the right shape.

**`before_tools` runs after the permission barrier**, so a hook sees only calls the rules already approved. It may return fewer, and anything it adds is dropped. A hook narrows; it can never widen, and a careless one cannot become a permission bypass.

**A hook that raises is logged and skipped.** A turn must not fail on account of something watching it.

### Wrapping a tool call

**Middleware** wraps one call, the harness's own tools and yours alike. `proceed` is the rest of the chain, so the order you write is the order they nest.

```python
class Timed:
    async def run(self, call, proceed):
        started = time.monotonic()
        try:
            return await proceed(call)
        finally:
            metrics.timing("langmesh.tool", time.monotonic() - started, tags={"tool": call.name})

class RetryTransient:
    async def run(self, call, proceed):
        for attempt in range(3):
            try:
                return await proceed(call)
            except TransientError:
                if attempt == 2:
                    raise
                await asyncio.sleep(2 ** attempt)

async with Session(reviewer, directory="/srv/checkout", pipeline=[Timed(), RetryTransient()]) as session:
    ...
```

`[Timed(), RetryTransient()]` times the retries; reversing it retries the timing.

Middleware is **not** absorbed on failure, unlike a hook. A hook watches. Middleware is in the call path and decides whether the call happens. Swallowing there would turn a retry layer's bug into a tool that silently never ran.

### Folding the conversation

At the configured threshold, the default first opens a private preparation segment that exposes only local Bash and requires the agent to atomically bring `.agents/observations.sqlite` up to date. The runtime captures `registry_meta.revision` before the segment and refuses to fold unless the revision advances; an explicit no-change acknowledgement advances only that revision. It then keeps a recent token-bounded working set. Observation payloads are never injected into the system prompt, inferred, or consolidated automatically. The prompt carries only the compact registry descriptor and progressive-disclosure guidance; relevant rows are discovered on demand, preferably through a fresh disposable Semble index, and consolidation happens only when the user invokes the `consolidate-observations` skill. A successful fold deliberately rebuilds the system prompt once so its descriptor and session context can refresh.

A direct library caller sees the same failure boundary. If the preparation Bash work fails, the registry revision does not advance, or folding reclaims nothing when it must, `CompactionDone(ok=False, error_code=...)` is emitted and subsequent `Session.stream()`/`Session.ask()` calls raise `CompactionBlockedError`. Iterate `Session.compact()` to retry; only a successful retry releases later work.

```python
from langmesh import CompactionDone

async for event in session.compact():
    if isinstance(event, CompactionDone) and not event.ok:
        logger.error("fold remains blocked: %s", event.error_code)
```

Provider or tool failures use a separate durable retry path. `Session.retry()` continues the failed conversation tail; it never appends or resends the user's message:

```python
try:
    await session.ask("Complete the migration.")
except Exception:
    async for event in session.retry():
        consume(event)
```

When the failed fold interrupted an already accepted turn, that same `compact()` iteration resumes the accepted work after the retry succeeds; the caller must not resend the user message. The preparation capability still runs inside the configured sandbox, so a session whose workspace is intentionally read-only will fail visibly until the caller supplies a writable registry location or changes that boundary.

```python
from langmesh import KeepRecentTurns

async with Session(reviewer, directory="/srv/checkout", compaction=KeepRecentTurns(20)) as session:
    ...
```

Your own strategy is two methods:

```python
class KeepDecisions:
    """Fold everything except the turns where something was decided."""

    def should_compact(self, state) -> bool:
        return state.context_tokens > 0.7 * state.context_window

    async def compact(self, state) -> list:
        decisions = [message for message in state.messages if looks_like_a_decision(message)]
        return [summarise(state.messages)] + decisions
```

`state` carries the conversation, the live context window and what it currently occupies. It is passed rather than reached for, which is what makes an alternative possible at all.

### What is not a seam

**The turn loop itself.** A `runtime=` argument would let you supply a working agent loop in twenty lines. That loop would silently drop the permission preflight, the durable suspend and resume, concurrent execution, compaction, abort and steering. Reimplement those and you have copied the loop; skip them and you have a harness that runs tools without asking.

`session.runtime` is public, so a program that genuinely wants a different architecture can drive `AgentRuntime` directly or not use `Session` at all. What the argument would add is a door that looks supported onto a room where the safety properties do not hold.

## Driving a turn

`ask()` is the convenience. `stream()` is the whole vocabulary — text chunks, tool calls, tool results, usage, suspensions and independent goal-review activity:

```python
from langmesh import GoalReviewFinished, GoalReviewProgress, Suspended, TextChunk, ToolCall

async for event in session.stream("Refactor the parser to use the streaming reader."):
    match event:
        case TextChunk(text=text):
            response_text += text
        case ToolCall(name=name):
            ...
        case Suspended(interactions=gates):
            ...
        case GoalReviewProgress(review_id=review_id, event=review_event):
            ...
        case GoalReviewFinished(standing=standing, assessment=assessment):
            ...
```

Both drive one turn, unless the agent sets itself a goal with `update_goal`. A goal is a contract for an outcome rather than a note about one, so while it is open the session keeps taking turns toward it and keeps yielding their events.

What decides whether it keeps going is not the agent, which can set a goal but cannot end one. Between turns the harness runs an isolated reviewer session that reads, searches and tests the work before submitting a verdict and, unless the goal is reached, the instruction that opens the next turn. Budget for it — a goal that runs for eight turns costs eight reviews on top of the turns themselves, on the same model the session uses. `GoalReviewStarted` identifies the review and exposes the typed goal, purpose and minimum conditions; each reviewer event is wrapped in `GoalReviewProgress`; `GoalReviewFinished` carries the terminal status, standing, contract status, assessment, evidence and continuation message. The internal assignment remains an implementation detail. The loop ends when the review reports the goal satisfied or blocked, when the allowance in `Tunable.goal_continuation_turns` runs out and the goal is parked, or when the review fails and leaves the goal unchanged. Asking again gives the allowance back and picks a parked goal up where it stopped.

The harness checkpoints the conversation, goal and task state when a call ends, including when it ends badly. A turn that raised still changed the session. To lose that is worse than to record a failure.

Resuming is giving a new `Session` the same id and the same store:

```python
store = MemoryCheckpoints()
review_id = "session-3d965dfe-21c4-4f2c-9040-290e77bea0b1"

async with Session(reviewer, directory="/srv/checkout", session_id=review_id, checkpoints=store) as first:
    await first.ask("Read src/parser.py and tell me what it assumes about its input.")

async with Session(reviewer, directory="/srv/checkout", session_id=review_id, checkpoints=store) as second:
    await second.ask("Now what would you change?")
```

`session.runtime` is the `AgentRuntime` underneath, deliberately public. A library that hides its own core forces every non-obvious use into a fork.

## When to use the daemon instead

Reach for `langmeshd` when you want one of these:

- A session that outlives the terminal that started it.
- A harness you can reach from another machine.
- Crash isolation between sessions.
- Peer composition. Those are what a control plane is _for_, and none of them can be had from an object in your process.

The two are the same harness. A daemon session is this same runtime, built and held inside the daemon, with a durable record and an address in front of it.
