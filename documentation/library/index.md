# Library quickstart

`Session` owns one embedded runtime, its workspace lease, checkpoint store, and control state. It starts no daemon and reads no machine-level configuration unless your code explicitly uses `langmesh.daemon.machine`.

## Install and run

```python
import asyncio

from langmesh import AgentConfiguration, Session


agent = AgentConfiguration(
    name="reviewer",
    provider="anthropic",
    model="claude-sonnet-4-5",
    system_prompt="Review the requested change and cite the relevant files.",
)


async def main() -> None:
    async with Session(
        agent,
        directory="/srv/checkout",
        providers={"anthropic": "sk-ant-…"},
    ) as session:
        answer = await session.ask("What does the retry path guarantee?")


asyncio.run(main())
```

Use `stream()` when the caller needs live text, tool activity, usage, suspension, compaction, or goal-review events.

```python
from langmesh import TextChunk, ToolCall

async for event in session.stream("Run the focused tests and explain any failure."):
    match event:
        case TextChunk(text=text):
            render_text(text)
        case ToolCall(id=call_id, name=name, arguments=arguments):
            render_tool(call_id, name, arguments)
```

## Add capabilities

Replaceable behavior belongs in one `SessionComponents` value. The value is frozen and snapshots sequence inputs as tuples, so caller mutation cannot change a live runtime accidentally.

```python
from langchain_core.tools import tool
from langmesh import KeepRecentTurns, MaximumToolCalls, Session, SessionComponents


@tool
def incident_lookup(service: str) -> list[dict]:
    """Return open incidents for a service."""
    return incidents.open_for(service)


components = SessionComponents(
    tools=(incident_lookup,),
    hooks=(MaximumToolCalls(30),),
    compaction=KeepRecentTurns(24),
)

session = Session(agent, directory="/srv/checkout", components=components)
```

Supplied tools are gated by default. Set `supplied_tool_gate="none"` only when the surrounding application already enforces their authority.

## Next

- [Composition](composition.md) explains every configured value and product boundary.
- [Models and cache behavior](models.md) covers provider construction, credentials, and stable inference prefixes.
- [Lifecycle and control](lifecycle.md) covers suspension, resume, interrupts, steering, and retries.
- [Events and driving patterns](events.md) covers the complete stream contract.
- [Customization](customization.md) covers tools, policy, hooks, middleware, locations, peer sessions, and MCP servers.
- [Compaction and continuation](compaction.md) covers history compaction and autonomous work.
- [Resources and persistence](resources.md) covers virtual workspaces, checkpoints, transcripts, and background jobs.
