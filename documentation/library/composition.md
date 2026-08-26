# Runtime composition

LangMesh separates three concerns:

| Value | Owns | Changes while running? |
| --- | --- | --- |
| `RuntimeProfile` | Agent, global configuration, identity, directories, confinement, parent | No |
| `RuntimeComponents` | Model, replaceable ports, hooks, middleware, supplied tools, features, host services | No; replace the value before construction |
| `SessionComponents` | `RuntimeComponents` plus checkpoints, attachments, credentials, and tracing | No; `Session` owns their lifetime |

The daemon uses the same `RuntimeProfile` and `RuntimeComponents` API as an embedder. The core never imports daemon state: product persistence connects through the ports (a `GoalReviewJournal`, a `JobStore`, a transcript), and the library's own plugins receive only the internal `PluginHost`.

The library forces nothing. A runtime runs exactly the tools and features you hand it — no default battery — and the product composes its own set in `langmeshd.features`.

## Direct runtime construction

Use `Session` unless your application already owns checkpointing, resource leases, and turn serialization. Direct construction suits a scheduler or another session host.

```python
from langmesh import AgentRuntime, MemoryJobStore, MemoryTranscript, RuntimeComponents, RuntimeProfile

profile = RuntimeProfile(
    agent=agent,
    configuration=configuration,
    session_id="session-018f",
    working_directory="/srv/checkout",
    permission_mode="ask",
    sandbox=sandbox_profile,
)
components = RuntimeComponents(
    jobs=MemoryJobStore(),
    transcript=MemoryTranscript(),
)
runtime = AgentRuntime(profile, components)

async for event in runtime.stream("Inspect the current change."):
    ...  # Handle each typed TurnEvent as it arrives.
```

`RuntimeProfile` requires a non-empty session id and an absolute working directory. `RuntimeComponents` validates structural ports at construction, copies mutable sequences to tuples, and rejects an unknown `tool_gate` (only `"ask"` or `"none"`).

## Session composition

`SessionComponents` extends `RuntimeComponents` with ownership seams. `for_runtime()` projects the session-ownership fields out and leaves exactly what an `AgentRuntime` consumes.

```python
from langmesh import Session, SessionComponents

components = SessionComponents(
    jobs=MemoryJobStore(),
    transcript=MemoryTranscript(),
)
session = Session(
    agent,
    directory="/srv/checkout",
    configuration=configuration,
    components=components,
)
```

The constructor keeps run facts (directory, identity, permission mode, confinement, provider/model) outside the component value, so a persistence adapter cannot silently change confinement or identity.

## Component reference

| Component | Port or adopted interface | Default |
| --- | --- | --- |
| `model` | LangChain `BaseChatModel` | Built from the agent and configuration |
| `catalogue` | `CatalogueLike` | Packaged prompts and caller-supplied values; no machine lookup |
| `jobs` | `JobStore` | `MemoryJobStore` in `Session` |
| `observer` | `Observer` | Audit observations dropped |
| `approvals` | `Approvals` | Interactive gates suspend |
| `transcript` | `Transcript` | `MemoryTranscript` in `Session` |
| `sessions` | `SessionAccess` | Peer-session tools absent |
| `mcp_servers` | `MCPServers` | MCP tools absent |
| `file_leases` | `FileLeases` | No cross-session mutation coordination |
| `permissions` | `PermissionPolicy` | The per-agent `PermissionEvaluator` |
| `prompt_composer` | `PromptComposer` | Catalogue `system_prompt` template |
| `application_tools` | Caller-supplied `BaseTool` sequence that may replace names in the complete roster | No supplied tools |
| `available_tools` | Complete `BaseTool` sequence | No tools |
| `tool_gate` | `"ask"` / `"none"` | `"ask"` |
| `hooks` | Any combination of the two hook protocols | None |
| `middleware` | `ToolMiddleware` sequence | None |
| `related_turns` | Async turn reader | `read_turn` unavailable |
| `features` | Sequence of `Feature` instances | No features — a plain model turn |
| `services` | Opaque plugin bundle the host supplies | `None` |
| `machine_snapshot` | Probe of the machine by the host | Minimal platform-only snapshot |
| `user_context` | Snapshot of how you work, probed by the host | None |

`SessionComponents` additionally owns `checkpoints`, `artifacts`, `attachments`, `credential_store`, and `tracer_provider`.

A feature instance belongs to one session because attachment gives it session-specific context and state. Construct fresh feature instances when composing another session; attempting to share one now fails during runtime construction instead of silently redirecting its state to the last session that attached it.

There is no `compaction`, `compaction_preparation`, `continuations`, or `goal_review_journal` field. Those are features now: pass their instances through `features`, and any ports they need through `services` or a constructor argument:

```python
from langmesh.runtime.plugins.compaction import Compaction

components = SessionComponents(
    features=[
        Compaction(),
    ],
)
```

The `application_tools` field accepts ordinary LangChain `BaseTool` values. Tools supplied before the runtime is built join its initial provider schema; `Session.grant_tool()` changes that schema deliberately when a live session genuinely gains a new callable capability.

## Cache stability

Components are fixed for a runtime because the model-visible tool schemas and static instructions form the provider-cache prefix. Runtime controls such as steering, permission-mode changes, and goal state are append-only or applied at execution boundaries; none rewrites an earlier model message. Interaction with the cache is measured and reported on each `Usage` event (`cache_prefix_reusable`, `reusable_prefix_tokens`, `segments`, `divergence`), so a custom model adapter can be verified rather than inferred.

`PromptComposer` runs only when the cached prompt is built, and `Session.refresh_prompt()` is the explicit invalidation boundary. No hook may rewrite a provider request or its system-message prefix. Ordinary turns, steering, tool results, and background results append to durable conversation state without changing the prompt or tool schema.

## Tools, hooks, and policy

### Granting a tool to a session

Every tool a session can call is one **`Tool` unit**: its provider-visible schema, its description, and the handler that runs it. The tools that ship in the library core (`call_mcp_server_tool`, the peer-session tools, `read_turn`, `load_skill`) live in `langmesh.runtime.tools.registry` and `langmesh.runtime.tools.sessions`; the rest — `bash`, the web tools, goal and task tools, `control_screen`, `ask_user`, and the hidden verdict tools — are contributed by plugins. The runtime dispatches every call generically by name, so there is no hard-coded routing and a caller's tool of the same name simply replaces a built-in's execution and schema.

Every tool a session runs, yours and the harness's alike, carries two shared argument fields injected once at registration: a required `explanation` (why the call is happening, in words the person watching reads) and a required `access_request` (what it says about changing anything and what it needs beyond confinement). They are described in `descriptions/explanation.md` and `descriptions/access_request.md`.

**The library forces no tools.** A `Session` composes its tool set explicitly and starts with none.

Three ways to compose a session's tools:

- **The whole roster.** Pass `SessionComponents(available_tools=[...])` to run exactly those tools, or `available_tools=()` to run with no tools at all.
- **Supplied tools.** Pass `tools=[...]` before the first call for a stable initial schema, or call `session.grant_tool(...)` later when the session truly gains a new capability. A tool whose name the session already runs replaces its implementation and schema.
- **Plugin tools.** Compose a feature that contributes tools — `Bash()`, `Web()`, `ComputerUse()`, `Interaction()`, `Continuation()`, `GoalReviewFeature()`, `Compaction()`, `PermissionReview()` — each gated by the agent's `tools_enabled`.

```python
from langchain_core.tools import tool
from langmesh import Session, SessionComponents

@tool
async def incident_lookup(service: str) -> list[dict]:
    """Return open incidents for a service."""
    return await incidents.open_for(service)

# The whole roster, and nothing else:
session = Session(
    agent, directory="/srv/checkout",
    components=SessionComponents(available_tools=(incident_lookup,)),
)

# One addition on top of nothing, gated by default:
session = Session(agent, directory="/srv/checkout", tools=[incident_lookup])

# Nothing at all:
session = Session(
    agent, directory="/srv/checkout",
    components=SessionComponents(available_tools=()),
)
```

A caller-supplied tool is gated by default (`tool_gate="ask"`, so every call asks); set `"none"` only when the surrounding application already enforces the tool's authority.

#### Appending later, mid-session

```python
await session.ask("Inspect the recent incidents.")

@tool
def current_incident() -> dict:
    """The most recent incident, for the report."""
    return incidents.latest()

session.grant_tool(current_incident)
```

`grant_tool` works at any moment, including after turns have run. Because providers only emit structured calls for tools in the request schema, a mid-session grant rebinds the real schema instead of pretending prose can declare a function. That next call is an intentional cache divergence at the `tools` segment; subsequent calls reuse the new prefix. The `Session` retains the caller-owned tool object when it releases and rebuilds its runtime. A new process must reconstruct code capabilities at composition, while the data checkpoint restores conversation and runtime state. Supply predictable tools at construction when cache continuity matters.

The same binding mechanism powers the internal reviewers. The goal reviewer receives `submit_goal_review`, the compaction summarizer receives `submit_compaction_summary`, and the automatic permission reviewer receives `permission_decision`; the working session never carries any of them, so no tool is ever a no-op that exists only to be inert.

### Permission policy

`PermissionPolicy` rejects disallowed argument shapes before execution (the one shipped rule is `check_bash_background`). It is distinct from the gating plugin's `plan_tool_calls`/`review_automatic_gate` and from `Approvals`, which answers a gate.

```python
class ServicePolicy:
    def check_bash_background(self):
        raise PermissionError("Detached shell commands are disabled.")

components = SessionComponents(permissions=ServicePolicy())
```

### Hooks

Hooks implement either of two structural protocols:

- `BeforeToolsHook.before_tools(calls)` may return only calls already present in the approved batch.
- `AfterTurnHook.after_turn(summary)` observes the terminal summary.

```python
class BlockNetworkBatch:
    async def before_tools(self, calls):
        return [call for call in calls if call["name"] not in {"fetch_url", "search_web"}]

components = SessionComponents(hooks=(BlockNetworkBatch(),))
```

A hook failure is logged and skipped. A tool hook cannot add calls: LangMesh keeps only calls from the approved batch (`MaximumToolCalls` resets after each completed or cancelled turn and keeps counting across a suspended and resumed batch).

### Middleware

Middleware wraps one public `ToolInvocation` (`name` and `arguments`). The first configured layer is outermost.

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

The prompt lives in its own file, `prompts/system_prompt.md`, and each `{{ name }}` placeholder inside it names one layer sitting among the file's own prose. The renderer drops any layer that resolves empty, so a layer is present exactly when its value is non-empty:

| Layer placeholder (in order) | What it provides |
| --- | --- |
| `{{ agent_context }}` | Stable guidance for an agent whose session can be messaged by peers. |
| `{{ instructions }}` | The person's recorded instructions. |
| `{{ user_environment }}` | Stable guidance for interpreting the opt-in user snapshot carried in session context. |
| `{{ skills }}` | The vault of skills the agent may load. |
| `{{ memories }}` | The recorded memories. |
| `{{ agent_prompt }}` | The agent profile's own system prompt. |
| `{{ peer_sessions }}` | How to compose with other sessions. |
| `{{ mcp_servers }}` | The configured MCP servers. |

The `{{ toolbox }}` layer is the last one: the session's own package-profile instructions (install a missing command with `nix profile add nixpkgs#<name>` from `prompts/toolbox.md`), rendered only when `toolbox.enabled` is set and the machine has Nix, and dropped like any other empty layer otherwise. Headings and other markdown belong in that file, never generated in code.

```python
from langmesh import PromptTemplates

class ApplicationPrompt:
    def __init__(self, template: str):
        self._prompts = PromptTemplates({"system_prompt": template})

    def compose(self, layers):
        available = {layer.name: layer.content for layer in layers}
        return self._prompts.load("system_prompt", available)

components = SessionComponents(prompt_composer=ApplicationPrompt("{{ instructions }}\n\n{{ agent_prompt }}"))
```

Shipped files use the same renderer through `PackagePromptLoader`: a directory of `{{ name }}` templates beside the code, not strings in Python.

```python
from pathlib import Path

from langmesh import PackagePromptLoader

prompts = PackagePromptLoader(Path(__file__).parent / "prompts")
text = prompts.load("system_prompt", {"instructions": "Be brief."})
```

The default composer renders the catalogue's `system_prompt` template over the same layers. Session identity, directories, confinement, machine details, user context, feature state, and background events are deliberately absent from these layers: the runtime appends them as one marked conversation message and appends a new version only when its digest changes. A custom composer changes the static prompt construction and therefore its revision; the resulting bytes are persisted and restored exactly until an explicit refresh or a construction input changes.

### Attachments

`Attachments.compose()` controls how application-owned paths become model content and filesystem authority. It returns `ComposedAttachments`, whose names keep those responsibilities explicit: `content` is sent to the model, `granted_paths` is the exact access allowlist, and `omitted_image_count` drives the warning for pixels that were not inlined.

```python
from langmesh import ComposedAttachments, SessionComponents

class MetadataOnlyAttachments:
    def compose(self, message, attachments, model_identifier, inline_image_bytes):
        paths = tuple(attachment.path for attachment in attachments if attachment.path)
        descriptions = "\n".join(
            f"- {attachment.name}: {attachment.path or 'inline data'}"
            for attachment in attachments
        )
        return ComposedAttachments(
            content=f"{message}\n\nAttachments:\n{descriptions}",
            granted_paths=paths,
        )

components = SessionComponents(attachments=MetadataOnlyAttachments())
```

The default `AttachmentComposer` includes structured metadata and inlines bounded image data only when the selected model advertises vision support. A custom composer must list only the paths the application intends the runtime to grant; mentioning a path in `content` grants nothing by itself. The inline ceiling is configurable (`attachments.inline_image_megabytes`).

### Execution locations

A location is where files live: this machine, or an SSH host. Locations are now a plugin: `Locations()` contributes a `location` argument to the `bash` tool's schema and resolves which executor runs each call. The library core purged locations from itself; the host hands the plugin its locations through `services["locations"]`.

```python
from langmesh.runtime.plugins.locations import Locations

components = SessionComponents(features=[Locations()])
```

The session's context then carries `locations`, and a `bash` call names one with its `location` argument. With several remote-only locations, a tool must select one explicitly; an omission never picks an arbitrary remote environment.

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

The caller initializes and closes the manager it supplies. A bare library session does not discover `.agents/mcp.json`, start servers, or take ownership of another component's lifecycle; the daemon performs those application-specific steps at its composition root.

## Features and plugins

A session runs a plain model turn by itself. Everything else — goal review, compaction, permission gates, autonomous continuation, the observational-memory ledger, background jobs, screen control, session naming, the tools themselves — is a **feature** you compose onto the core. The core knows none of them: it runs its turn and, at fixed points, asks the installed features to participate. Features do not know each other either; if one is interested in what another produces, it subscribes to an event on the shared bus and reacts.

### The seam

The public surface lives in `langmesh.runtime.features`:

- `Feature` — the hooks a feature implements. Hooks you omit are no-ops.
- `PluginContext` — what a feature is given to live: identity, configuration, its templates, the bus.
- `PluginBus` — the synchronous channel between features. `subscribe(type, handler)` hears an event and `emit(event)` delivers it immediately in subscription order.
- `Features` — the installed set, reached by concrete type with `by_type(...)` or by structural contract with `capability(...)` and `require(...)`.
- `feature_prompts(name, catalogue)` — a plugin's own templates, behind the catalogue's overrides and in front of the shared set.

The hooks are the points in the turn where a feature can act. The full set a `Feature` may implement:

| Hook | When the core calls it |
| --- | --- |
| `attach(context, host)` | installation; the library's own features keep the internal host here |
| `compose_context(context)` | building the turn's model-facing context |
| `contribute_tools()` | the tools this feature adds to the session's roster |
| `contribute_tool_handlers()` | event-rich handlers supplied beside contributed tool schemas; each receives one immutable `ToolExecution` value |
| `contribute_schema_fields(tool_name)` | extra argument fields to extend another tool's contract (e.g. `location` on `bash`) |
| `required_capabilities()` | structural feature contracts that composition must satisfy |
| `compose_prompt(variables)` | building the system prompt's named sections |
| `assign_title(first_message)` | suggesting a session title |
| `prepare_request()` | append validated request-boundary state through the feature's conversation capability |
| `acknowledge_checkpoint()` | acknowledge only external state represented by the checkpoint that just committed |
| `should_maintain` / `begin_maintenance` / `advance_maintenance` / `run_maintenance` / `maintenance_ready` / `valid_during_maintenance` / `maintenance_tool_schemas` / `maintenance_violation_message` / `fail_maintenance` / `record_maintenance_handoff` / `maintenance_describe` | holding the loop to reclaim context |
| `plan_tool_calls` / `resolve_gates` / `review_automatic_gate` | gating a batch of tool calls |
| `drain()` | turn-driven events (e.g. finished background jobs) |
| `incomplete_reminder()` | a reminder when the model stopped without finishing this feature's work |
| `should_complete_turn()` | whether this feature considers the turn done without further model output |
| `blocks_input()` | why new input must be refused (a failed fold, an unrepaired registry) |
| `snapshot()` / `restore(snapshot)` | durable session state beside the checkpoint |

`compose_prompt` is the home for stable model guidance because it runs when the cached system prompt is built. `prepare_request` cannot receive or replace the outgoing list; it may only append through the conversation capability, after which the runtime checkpoints before calling the provider. This makes a transient prefix rewrite impossible by construction.

### Composing a session's features

The application layer composes which features a session runs. The library ships no default battery: a session runs exactly the features you hand it, and a feature you leave out simply is not there.

```python
from langmesh import Session, SessionComponents
from langmesh.runtime.plugins.compaction import Compaction
from langmesh.runtime.plugins.goal_review import GoalReviewFeature

session = Session(
    agent,
    directory="/srv/checkout",
    components=SessionComponents(
        features=[
            GoalReviewFeature(journal=goal_review_journal),
            Compaction(),
        ],
    ),
)
```

The shipped classes are ordinary classes: construct them with the ports they declare and hand the instances over. Structural dependencies are validated when the runtime is built, so a `Bash` or `Web` feature without the `BackgroundCapability` it uses fails immediately with a composition error. A plugin contributes a positive tool allowlist through `contribute_tools()` and handlers only for those tools; it never removes names from an unknown global roster. `features=()` runs a plain session with no features at all.

What the product runs for a hosted session is the daemon's business, not the library's. `langmeshd.features.compose_plugins` builds the full set — goal review, compaction, permissions, continuation, observational memory, background jobs, work habits, titling, locations, bash, web, interaction, and computer use — and hands it to each executor.

### Writing a feature

A feature is a subclass of `Feature` implementing the hooks it needs:

```python
from langmesh.runtime.features import Feature

class CustomFeature(Feature):
    def __init__(self, project: str) -> None:
        self._project = project

    def compose_context(self, context: dict) -> None:
        context["project"] = self._project

    def compose_prompt(self, variables: dict[str, str]) -> None:
        variables["project"] = f"Work in the {self._project} repository."
```

A tool plugin names only the tools it provides:

```python
from langchain_core.tools import StructuredTool
from langmesh.runtime.features import Feature

async def lookup_ticket(identifier: str) -> str:
    return await ticket_service.read(identifier)

class Tickets(Feature):
    def contribute_tools(self) -> list:
        return [StructuredTool.from_function(coroutine=lookup_ticket, name="lookup_ticket", description="Read one ticket by identifier.")]
```

Composing `Tickets()` makes exactly `lookup_ticket` available. The plugin neither knows nor filters the tools contributed by another plugin.

A feature that wants to hear what others publish subscribes in `attach`:

```python
    def attach(self, context: PluginContext, host=None) -> None:
        context.bus.subscribe(CustomEvent, self._on_custom_event)
```

The library's own plugins additionally receive the internal `PluginHost` — grouped views of the conversation, boundary, tools, window, turn machinery, and bookkeeping. A caller's plugin never needs it; the hooks and the context are the whole surface.

### Prompts are configurable

Each shipped plugin deliberately keeps its package-owned prompt templates in a `prompts/` directory beside its code. Load them with `PackagePromptLoader` (`from langmesh import PackagePromptLoader`). These are shipped assets and remain filesystem package resources rather than embedded Python strings. A template resolves from the catalogue's caller-supplied override first, then the plugin's package directory, then the shared package set. Supply a `Catalogue(prompts={...})`, or any catalogue whose `prompt_override` answers a name, to override a template from code.
