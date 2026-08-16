# Tools, hooks, and policy

## Granting a tool to a session

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

# Exactly these tools, and nothing else.
session = Session(
    agent, directory="/srv/checkout",
    components=SessionComponents(toolset=(incident_lookup,)),
)

# Add one tool on top of nothing; "bash" would be your implementation, not ours.
session = Session(agent, directory="/srv/checkout", tools=[incident_lookup])

# No tools at all.
session = Session(
    agent, directory="/srv/checkout",
    components=SessionComponents(toolset=()),
)
```

A caller-supplied tool is gated by default (`supplied_tool_gate="ask"`, so every call asks); set `"none"` only when the surrounding application already enforces the tool's authority.

### Appending later, mid-session

```python
await session.ask("Inspect the recent incidents.")

@tool
def current_incident() -> dict:
    """The most recent incident, for the report."""
    return incidents.latest()

session.grant_tool(current_incident)
```

`grant_tool` works at any moment, including after turns have run. Its description and schema are appended to the conversation as a message rather than bound into the provider schema, so the model learns to call it without the cache prefix moving.

### The `ToolGrant` value

`tools` accepts either a bare tool or a `ToolGrant`. Use `ToolGrant` when you want to name the wrapper explicitly; `as_tool_grants` normalizes a mixed sequence.

```python
from langmesh import ToolGrant, as_tool_grants

grants = as_tool_grants([incident_lookup, ToolGrant(current_incident)])
session = Session(agent, directory="/srv/checkout", tools=grants)
```

The same mechanism powers the internal reviewers. The goal reviewer receives `submit_goal_review` and the compaction summarizer receives `submit_compaction_summary`; the working session never carries either, so no tool is ever a no-op that exists only to be inert.

### How the built-ins are built

Each built-in `Tool` in `langmesh.runtime.tools.units` joins three pieces that used to live apart:

- Its **schema** in `langmesh.runtime.tools.registry` (the LangChain `StructuredTool` the model binds),
- Its **description** in `langmesh/runtime/tools/descriptions/*.md`,
- Its **handler** in `langmesh.runtime.tools.handlers` (the execution, over the same `ToolServices` bundle a caller's tool uses).

The registry's schema tools are fully functional on their own: invoked directly, they resolve the current dispatch services and run the same handler. There are no no-op stubs left.

## Permission policy

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

## Hooks

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

## Middleware

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

## Prompt layers

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

## Attachments

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

## Execution locations

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

## Peer sessions

Supply `SessionAccess` to enable the peer-session tools. The port owns identity, creation, messaging, listing, and teardown; the core assumes no socket or daemon.

```python
components = SessionComponents(sessions=application_session_graph)
```

The conversation inherited by a child excludes a dangling tool-call tail, so the child never begins with invalid model history.

## MCP servers

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
