# Tools, hooks, and policy

## Granting a tool to a session

A supplied tool is any LangChain `BaseTool`. Pass tools at creation, or grant one later.

```python
from langchain_core.tools import tool
from langmesh import Session, SessionComponents

@tool
async def incident_lookup(service: str) -> list[dict]:
    """Return open incidents for a service."""
    return await incidents.open_for(service)

session = Session(agent, directory="/srv/checkout", tools=[incident_lookup])
```

The tool becomes dispatchable immediately. Its description and JSON schema are appended to the conversation as a message, so the model learns to call it without the bound tool schema changing. That is what keeps the provider-cache prefix intact.

A granted tool cannot shadow a built-in tool. It is gated by default (`supplied_tool_gate="ask"`, so every call asks); set `"none"` only when the surrounding application already enforces the tool's authority.

### Granting later, mid-session

```python
await session.ask("Inspect the recent incidents.")

@tool
def current_incident() -> dict:
    """The most recent incident, for the report."""
    return incidents.latest()

session.grant_tool(current_incident)
```

`grant_tool` works at any moment, including after turns have run. It is append-only, so it never rewrites earlier messages and never bursts the cache.

### The `ToolGrant` value

`tools` accepts either a bare tool or a `ToolGrant`. Use `ToolGrant` when you want to name the wrapper explicitly; `as_tool_grants` normalizes a mixed sequence.

```python
from langmesh import ToolGrant, as_tool_grants

grants = as_tool_grants([incident_lookup, ToolGrant(current_incident)])
session = Session(agent, directory="/srv/checkout", tools=grants)
```

The same mechanism powers the internal reviewers. The goal reviewer receives `submit_goal_review` as a grant and the compaction summarizer receives `submit_compaction_summary`; the working session never carries either, so no tool is ever a no-op that exists only to be inert.

## Supplying the whole tool roster

Set `toolset` when the application owns the complete tool list. LangMesh still adds the cache-stable internal verdict schemas to the model binding and keeps them inert outside their matching internal review capability.

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
