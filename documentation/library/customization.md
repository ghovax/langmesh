# Tools, hooks, and policy

## Supplied tools

LangMesh adopts LangChain `BaseTool` values. Supplied tools pass through location resolution, permission evaluation, hooks, middleware, execution, and transcript recording.

```python
from langchain_core.tools import tool
from langmesh import SessionComponents


@tool
async def incident_lookup(service: str) -> list[dict]:
    """Return open incidents for a service."""
    return await incidents.open_for(service)


components = SessionComponents(tools=(incident_lookup,))
```

The default `supplied_tool_gate="ask"` fails closed. Supplied tools cannot shadow a built-in tool. An agent's built-in allow-list does not remove application-supplied tools, while its explicit disabled list still applies.

Set `toolset` when the application owns the complete tool roster. LangMesh still adds the cache-stable internal verdict schemas to the model binding and makes them inert outside their matching internal review capability.

## Permission policy

`PermissionPolicy` decides whether an enabled call shape is allowed to reach the gate or executor. It is distinct from `Approvals`, which answers a gate.

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

Hooks implement any one of three independent structural protocols:

- `BeforeModelHook.before_model(messages)` may return a changed request list.
- `BeforeToolsHook.before_tools(calls)` may return only objects already present in the approved batch.
- `AfterTurnHook.after_turn(summary)` observes the terminal summary.

```python
class BlockNetworkBatch:
    async def before_tools(self, calls):
        return [call for call in calls if call["name"] not in {"fetch_url", "search_web"}]


components = SessionComponents(hooks=(BlockNetworkBatch(),))
```

A hook failure is logged and skipped. A tool hook cannot add calls because LangMesh retains only returned objects from the approved batch. `MaximumToolCalls` resets after each completed or cancelled turn and continues counting across a suspended/resumed batch.

## Middleware

Middleware wraps one public `ToolInvocation`. The first configured layer is outermost.

```python
import time


class TimedTools:
    async def run(self, call, proceed):
        started = time.monotonic()
        try:
            return await proceed(call)
        finally:
            metrics.observe(call.name, time.monotonic() - started)


components = SessionComponents(middleware=(TimedTools(),))
```

Middleware may rewrite `call.arguments`, short-circuit, retry, or translate an exception. Its failures remain tool failures because middleware is part of execution rather than observation.

## Prompt layers

`PromptComposer` receives every cache-stable system-prompt layer as a named `PromptLayer`. Use it to select, order, wrap, or relocate application guidance without forking the runtime.

```python
class ApplicationPrompt:
    order = ("agent_prompt", "instructions", "context", "skills", "memories")

    def compose(self, layers):
        available = {layer.name: layer.content for layer in layers}
        return "\n\n".join(available[name] for name in self.order if name in available)


components = SessionComponents(prompt_composer=ApplicationPrompt())
```

Keep headings and other markdown in a template, never generated in code. The default composer already renders the catalogue's `system_prompt` template over the same layers; this composer only changes what reaches it. `BeforeModelHook` remains the final seam for changing the exact message list of one provider request; using it to rewrite the first system message intentionally invalidates that request's provider-cache prefix.

## Attachments

`Attachments.compose()` controls how application-owned paths become a provider input. It returns an `AttachmentInput` containing the model value, paths to grant, and the number of images omitted for a text-only model.

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

The default `PathAttachments` includes structured metadata and inlines bounded image data only when the selected model advertises vision support. Custom composers must return only paths the application intends the runtime to grant.

## Execution locations

`Location` names where path-native calls run. A custom `LocationExecutor` lets an application target a container, remote job service, or another substrate without changing the turn loop.

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

With multiple remote-only locations, a tool must select one explicitly. Omission never picks an arbitrary remote environment.

## Peer sessions

Supply `SessionAccess` to enable peer-session tools. The port owns identity, creation, messaging, listing, and teardown; the core assumes no socket or daemon.

```python
components = SessionComponents(sessions=application_session_graph)
```

The inherited conversation passed to `SessionAccess.create()` excludes a dangling tool-call tail, so a child never begins with invalid model history.

## MCP servers

Supply an initialized `MCPServers` implementation or the standard `MCPServerManager`.

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
