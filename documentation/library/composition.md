# Runtime composition

LangMesh separates three concerns:

| Value | Owns | Changes while running? |
| --- | --- | --- |
| `RuntimeProfile` | Agent, global configuration, identity, directories, confinement, locations, parent, reviewer capability | No |
| `RuntimeComponents` | Model and replaceable runtime capabilities | No; replace the value before construction |
| `SessionComponents` | All runtime components plus checkpoints, credentials, workspace management, and tracing | No; `Session` owns their lifetime |

The daemon uses the same `RuntimeProfile` and `RuntimeComponents` API as an embedder. Product persistence connects through `GoalReviewJournal`; the core never imports daemon or worker state.

## Direct runtime construction

Use `Session` unless your application already owns checkpointing, resource leases, and turn serialization. Direct construction suits a scheduler or another session host.

```python
from langmesh import AgentRuntime, RuntimeComponents, RuntimeProfile

profile = RuntimeProfile(
    agent=agent,
    configuration=configuration,
    session_id="session-018f",
    working_directory="/srv/checkout",
    permission_mode="ask",
    sandbox=sandbox_profile,
)
components = RuntimeComponents(
    model=model,
    catalogue=catalogue,
    jobs=job_store,
    transcript=transcript,
    permissions=permission_policy,
    file_leases=file_leases,
)
runtime = AgentRuntime(profile, components)

async for event in runtime.stream("Inspect the current change."):
    consume(event)
```

`RuntimeProfile` requires a non-empty session id and an absolute working directory. `RuntimeComponents` validates structural ports at construction and copies mutable sequences to tuples.

## Session composition

`SessionComponents` extends `RuntimeComponents` with ownership seams.

```python
from langmesh import Session, SessionComponents

components = SessionComponents(
    model=model,
    catalogue=catalogue,
    checkpoints=checkpoints,
    jobs=job_store,
    transcript=transcript,
    credentials=credentials,
    workspace=workspace_manager,
    tracer_provider=tracer_provider,
)
session = Session(
    agent,
    directory="/srv/checkout",
    configuration=configuration,
    components=components,
)
```

The constructor keeps run facts (directory, identity, permission mode, confinement, model identifier, locations) outside the component value, so a persistence adapter cannot silently change confinement or identity.

## Component reference

| Component | Port or adopted interface | Default |
| --- | --- | --- |
| `model` | LangChain `BaseChatModel` | Built from the agent and configuration |
| `catalogue` | `CatalogueLike` | Project plus packaged catalogue; no home-directory lookup |
| `jobs` | `JobStore` | `MemoryJobStore` in `Session` |
| `observer` | `Observer` | Audit observations dropped |
| `approvals` | `Approvals` | Interactive gates suspend |
| `transcript` | `Transcript` | `MemoryTranscript` in `Session` |
| `sessions` | `SessionAccess` | Peer-session tools absent |
| `mcp_servers` | `MCPServers` | Session starts servers declared by its explicit workspace lease |
| `file_leases` | `FileLeases` | No cross-session mutation coordination |
| `permissions` | `PermissionPolicy` | Built-in evaluator |
| `prompt_composer` | `PromptComposer` | Catalogue `system_prompt` template |
| `tools` | `BaseTool` or `ToolGrant` sequence | No supplied tools |
| `toolset` | Complete `BaseTool` sequence | Built-in registry filtered by the agent || `hooks` | Any combination of the three hook protocols | None |
| `middleware` | `ToolMiddleware` sequence | None |
| `compaction` | `Compaction` | Token-bounded recent working set |
| `compaction_preparation` | `CompactionPreparation` | Observational-memory preparation in `Session` and the daemon; direct compaction in bare `AgentRuntime` |
| `continuations` | `ContinuationPolicy` | Active tuning allowances |
| `synchronize_resources` | Async callable | No synchronization |
| `related_turns` | Async turn reader | `read_turn` unavailable |
| `goal_listener` | Goal callback | No callback |
| `goal_review_journal` | `GoalReviewJournal` | Review events stream without product transcript persistence |

`SessionComponents` additionally owns `checkpoints`, `attachments`, `credentials`, `workspace`, and `tracer_provider`.

The `tools` field accepts bare tools or `ToolGrant` values. A `Session` built with `tools=[...]` merges them into the components. A tool granted later, through `Session.grant_tool`, is described to the model by an appended conversation message rather than a schema change. See [Granting a tool to a session](composition.md#granting-a-tool-to-a-session).

## Cache stability

Components are fixed for a runtime because the model-visible tool schemas and static instructions form the provider-cache prefix. Runtime controls such as steering, permission-mode changes, locations, and goal state are append-only or applied at execution boundaries; none rewrites an earlier model message. A custom prompt composer should produce the same output until the application explicitly calls `Session.refresh_prompt()`.



---

## Tools, hooks, and policy

### Granting a tool to a session

Every tool a session can call is one **`Tool` unit**: its model-facing schema, its description, and the handler that runs it. The built-in tools (`bash`, `search_web`, `set_tasks`, and the rest) are shipped with the library as an optional inventory in `langmesh.runtime.tools.registry`; the runtime holds the set of units the session was composed with and dispatches every call generically by name, so there is no hard-coded routing and a caller's tool of the same name simply replaces a built-in's implementation.

**The library forces no tools.** A `Session` composes its tool set explicitly and starts with none. The shipped implementations are something the caller opts into and assembles, never something injected by default.

Three ways to compose a session's tools:

- **The whole roster.** Pass `SessionComponents(toolset=[...])` to run exactly those tools, or `toolset=()` to run with no tools at all.
- **Additions.** Pass `tools=[...]`, or call `session.grant_tool(...)` later. A tool whose name the session already runs replaces its implementation.
- **The agent profile, at the application layer.** The daemon reads an agent's declared `tools_enabled` and assembles its built-ins from the registry; an agent that declares none runs with none.

```python
from langchain_core.tools import tool
from langmesh import Session, SessionComponents

@tool
async def incident_lookup(service: str) -> list[dict]:
    """Return open incidents for a service."""
    return await incidents.open_for(service)

## Exactly these tools, and nothing else.
session = Session(
    agent, directory="/srv/checkout",
    components=SessionComponents(toolset=(incident_lookup,)),
)

## Add one tool on top of nothing; "bash" would be your implementation, not ours.
session = Session(agent, directory="/srv/checkout", tools=[incident_lookup])

## No tools at all.
session = Session(
    agent, directory="/srv/checkout",
    components=SessionComponents(toolset=()),
)
```

A caller-supplied tool is gated by default (`supplied_tool_gate="ask"`, so every call asks); set `"none"` only when the surrounding application already enforces the tool's authority.

#### Appending later, mid-session

```python
await session.ask("Inspect the recent incidents.")

@tool
def current_incident() -> dict:
    """The most recent incident, for the report."""
    return incidents.latest()

session.grant_tool(current_incident)
```

`grant_tool` works at any moment, including after turns have run. Its description and schema are appended to the conversation as a message rather than bound into the provider schema, so the model learns to call it without the cache prefix moving.

#### The `ToolGrant` value

`tools` accepts either a bare tool or a `ToolGrant`. Use `ToolGrant` when you want to name the wrapper explicitly; `as_tool_grants` normalizes a mixed sequence.

```python
from langmesh import ToolGrant, as_tool_grants

grants = as_tool_grants([incident_lookup, ToolGrant(current_incident)])
session = Session(agent, directory="/srv/checkout", tools=grants)
```

The same mechanism powers the internal reviewers. The goal reviewer receives `submit_goal_review` and the compaction summarizer receives `submit_compaction_summary`; the working session never carries either, so no tool is ever a no-op that exists only to be inert.

#### How the built-ins are built

Each built-in `Tool` in `langmesh.runtime.tools.units` joins three pieces that used to live apart:

- Its **schema** in `langmesh.runtime.tools.registry` (the LangChain `StructuredTool` the model binds),
- Its **description** in `langmesh/runtime/tools/descriptions/*.md`,
- Its **handler** in `langmesh.runtime.tools.handlers` (the execution, over the same `ToolServices` bundle a caller's tool uses).

The registry's schema tools are fully functional on their own: invoked directly, they resolve the current dispatch services and run the same handler. There are no no-op stubs left.

### Permission policy

`PermissionPolicy` decides whether an enabled call shape reaches the gate or the executor. It is distinct from `Approvals`, which answers a gate.

```python
class ServicePolicy:
    def check_tool(self, tool_name, /, **arguments):
        if tool_name == "incident_lookup" and arguments["service"] not in allowed_services:
            raise PermissionError("That service is outside this tenant.")

    def check_bash_background(self):
        raise PermissionError("Detached shell commands are disabled.")

components = SessionComponents(permissions=ServicePolicy())
```

### Hooks

Hooks implement any one of three structural protocols:

- `BeforeModelHook.before_model(messages)` may return a changed request list.
- `BeforeToolsHook.before_tools(calls)` may return only calls already present in the approved batch.
- `AfterTurnHook.after_turn(summary)` observes the terminal summary.

```python
class BlockNetworkBatch:
    async def before_tools(self, calls):
        return [call for call in calls if call["name"] not in {"fetch_url", "search_web"}]

components = SessionComponents(hooks=(BlockNetworkBatch(),))
```

A hook failure is logged and skipped. A tool hook cannot add calls, because LangMesh keeps only calls from the approved batch. `MaximumToolCalls` resets after each completed or cancelled turn and keeps counting across a suspended and resumed batch.

### Middleware

Middleware wraps one public `ToolInvocation`. The first configured layer is outermost.

```python
class TimedTools:
    async def run(self, call, proceed):
        started = time.monotonic()
        try:
            return await proceed(call)
        finally:
            metrics.observe(call.name, time.monotonic() - started)

components = SessionComponents(middleware=(TimedTools(),))
```

Middleware may rewrite `call.arguments`, short-circuit, retry, or translate an exception. A middleware failure is still a tool failure, because middleware is part of execution, not observation.

### Prompt layers

`PromptComposer` receives every cache-stable system-prompt layer as a named `PromptLayer`. Use it to select, order, wrap, or relocate application guidance without forking the runtime.

The prompt lives in its own file, `prompts/system_prompt.md`:

```markdown
{{ agent_prompt }}

{{ instructions }}

{{ context }}

{{ skills }}

{{ memories }}
```

Each placeholder names one layer, and the renderer drops any that resolve empty. Headings and other markdown belong in that file, never generated in code.

```python
from langmesh.base.configuration import PromptLoader

class ApplicationPrompt:
    def __init__(self, prompts_directory):
        self._prompts = PromptLoader(prompts_directory)

    def compose(self, layers):
        available = {layer.name: layer.content for layer in layers}
        return self._prompts.load("system_prompt", available)

components = SessionComponents(prompt_composer=ApplicationPrompt("prompts"))
```

The default composer already renders the catalogue's `system_prompt` template over the same layers; this composer only changes what reaches it. `BeforeModelHook` remains the final seam for changing one provider request's exact message list. Rewriting the first system message with it intentionally invalidates that request's provider-cache prefix.

### Attachments

`Attachments.compose()` controls how application-owned paths become a provider input. It returns an `AttachmentInput` with the model value, the paths to grant, and the number of images omitted for a text-only model.

```python
from langmesh import AttachmentInput, SessionComponents

class MetadataOnlyAttachments:
    def compose(self, message, attachments, model_identifier, inline_image_bytes):
        paths = tuple(str(path.resolve(strict=True)) for path in attachments)
        return AttachmentInput(
            value={"request": message, "attachments": paths},
            paths=paths,
        )

components = SessionComponents(attachments=MetadataOnlyAttachments())
```

The default `PathAttachments` includes structured metadata and inlines bounded image data only when the selected model advertises vision support. A custom composer must return only the paths the application intends the runtime to grant.

### Execution locations

`Location` names where path-native calls run. A custom `LocationExecutor` lets an application target a container, a remote job service, or another substrate without changing the turn loop.

```python
from langmesh import Location, Session

locations = (
    Location(name="checkout", kind="local", base_directory="/srv/checkout"),
    Location(
        name="staging",
        kind="remote",
        base_directory="/srv/app",
        host_alias="staging-ssh",
    ),
)
session = Session(agent, directory="/srv/checkout", locations=locations)
```

With several remote-only locations, a tool must select one explicitly. An omission never picks an arbitrary remote environment.

### Peer sessions

Supply `SessionAccess` to enable the peer-session tools. The port owns identity, creation, messaging, listing, and teardown; the core assumes no socket or daemon.

```python
components = SessionComponents(sessions=application_session_graph)
```

The conversation inherited by a child excludes a dangling tool-call tail, so the child never begins with invalid model history.

### MCP servers

Supply an initialized `MCPServers` implementation, or the standard `MCPServerManager`.

```python
from langmesh import MCPServerConfiguration, MCPServerManager, SessionComponents

manager = MCPServerManager(
    {
        "context7": MCPServerConfiguration(
            transport="streamable_http",
            url="https://mcp.context7.com/mcp",
            enabled=True,
        )
    }
)
await manager.start()
components = SessionComponents(mcp_servers=manager)
```

When no manager is supplied, `async with Session(...)` starts only the servers declared in the explicit workspace's `.agents/mcp.json` and closes those connections with the session.

---

## Features and plugins

A session runs a plain model turn by itself. Everything else — goal review, compaction, permission
gates, autonomous continuation, the observational-memory ledger, background jobs — is a **feature**
you compose onto the core. The core knows none of them: it runs its turn and, at fixed points,
calls the hooks the installed features implement. Features do not know each other either; if one
is interested in what another produces, it subscribes to an event on the shared bus and reacts.

### The seam

The public surface lives in `langmesh.runtime.features`:

- `Feature` — the hooks a feature implements. Hooks you omit are no-ops.
- `PluginContext` — what a feature is given to live: identity, configuration, its templates, the bus.
- `PluginBus` — the decoupled channel between features. `subscribe(type, handler)` to hear an event, `emit(event)` to publish one.
- `Features` — the installed set, which the harness reaches with `features.by_type(SomeFeature)`.

The hooks are the points in the turn where a feature can act:

| Hook | When the core calls it |
| --- | --- |
| `compose_context(context)` | building the turn's model-facing context |
| `compose_prompt(variables)` | building the system prompt's named sections |
| `prepare_request(messages)` | the exact request about to leave |
| `should_maintain` / `begin_maintenance` / `advance_maintenance` / `run_maintenance` / `maintenance_ready` / `valid_during_maintenance` / `maintenance_tool_schemas` / `fail_maintenance` | holding the loop to reclaim context |
| `plan_tool_calls` / `resolve_gates` / `review_automatic_gate` | gating a batch of tool calls |
| `drain()` | turn-driven events (e.g. finished background jobs) |
| `snapshot()` / `restore(snapshot)` | durable session state beside the checkpoint |
| `attach(context, host)` | installation; the library's own features keep the internal host here |

### Composing a session's features

The application layer composes which features a session runs. `Session` composes the shipped
battery by default; pass your own list to change it. A feature you leave out simply is not there.

```python
from langmesh import Session, SessionComponents
from langmesh.runtime.features.plugins.goal_review import GoalReviewFeature
from langmesh.runtime.features.plugins.compaction import Compaction

session = Session(
    agent,
    directory="/srv/checkout",
    components=SessionComponents(
        features=[
            GoalReviewFeature(journal=journal),
            Compaction(strategy=custom_strategy),
        ],
    ),
)
```

The shipped classes are ordinary classes: construct them with the ports they declare (a journal,
a strategy, a store) and hand the instances over. `features=()` runs a plain session with no
features at all. `Session`'s default battery is `langmesh.runtime.features.battery.default_features`.

### Writing a feature

A feature is a subclass of `Feature` implementing the hooks it needs:

```python
from langmesh.runtime.features import Feature, PluginContext

class MyFeature(Feature):
    def __init__(self, *, some_port: int = 0) -> None:
        self._port = some_port

    def compose_context(self, context: dict) -> None:
        context["custom_thing"] = {"value": self._port}
```

A feature that wants to hear what others publish subscribes in `attach`:

```python
    def attach(self, context: PluginContext, host=None) -> None:
        context.bus.subscribe(CustomEvent, self._on_custom_event)
```

The library's own plugins additionally receive the internal `PluginHost` — grouped views of the
conversation, boundary, tools, window, turn machinery, and bookkeeping. A caller's plugin never
needs it; the hooks and the context are the whole surface.

### Prompts are configurable

Each shipped plugin keeps its own prompt templates in its `prompts/` directory beside its code —
shipping them is an arbitrary choice, not a hardcoded part of the core. A template resolves from
the catalogue's overrides first, then the plugin's own directory, then the shared set. Supply a
`Catalogue(prompts={...})` (or any catalogue whose `prompt_override` answers a name) to override
any plugin template from code; edit the plugin's `prompts/` files to change the shipped ones.
