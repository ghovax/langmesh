# Library quickstart

`Session` owns one embedded runtime, its checkpoint store, and its control state. It starts no daemon and reads no machine-level configuration; a bare library catalogue has no agents, skills, or memories from disk. Plugin behavior (the tools, goal review, compaction, permissions, and the rest) is composed explicitly through `SessionComponents`.

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
        ...  # Use `answer`.


asyncio.run(main())
```

Use `stream()` when the caller needs live text, tool activity, usage, suspension, compaction, or goal-review events.

```python
from langmesh import Done, TextChunk, ToolCall

async for event in session.stream("Run the focused tests and explain any failure."):
    match event:
        case TextChunk(text=text):
            ...  # Paint the latest text delta.
        case ToolCall(id=call_id, name=name, arguments=arguments):
            ...  # Create or update a tool card by id.
        case Done(text=answer):
            ...  # The turn finished.
```

`stream()` yields a closed `TurnEventUnion`; dispatch on the variant class. See [Lifecycle and driving](lifecycle.md) for the whole contract.

## Add capabilities

Replaceable behavior belongs in one `SessionComponents` value. The value is frozen and snapshots sequence inputs as tuples, so caller mutation cannot change a live runtime accidentally.

The library ships no default battery. A `SessionComponents()` with nothing else is a plain model turn with no tools; every tool and every sub-behavior is a `Feature` you compose:

```python
from langmesh import Session, SessionComponents
from langmesh.runtime.plugins.background import BackgroundJobsFeature
from langmesh.runtime.plugins.bash import Bash
from langmesh.runtime.plugins.web import Web

session = Session(
    agent,
    directory="/srv/checkout",
    providers={"anthropic": "sk-ant-…"},
    components=SessionComponents(features=[BackgroundJobsFeature(), Bash(), Web()]),
)
```

Now the agent can run shell commands and search/fetch the web. Everything else the product runs — goal review, compaction, permission gating, autonomous continuation, observational memory, background jobs, screen control, session naming, asking you questions — is the same `Feature` seam. See [Composition](composition.md#composing-a-sessions-features).

Hooks, middleware, and a compaction strategy ride the same value:

```python
from langmesh import MaximumToolCalls, Session, SessionComponents
from langmesh.runtime.plugins.compaction import Compaction, KeepRecentTurns


class BlockDetachedShell:
    """A hook. One turn is allowed a bounded number of tool calls."""

    async def before_tools(self, calls):
        return calls  # Narrow to the calls the permission barrier approved.


components = SessionComponents(
    hooks=(MaximumToolCalls(30), BlockDetachedShell()),
    features=[Compaction(strategy=KeepRecentTurns(24))],
)

session = Session(agent, directory="/srv/checkout", components=components)
```

## Supplied tools

Pass predictable tools to `Session(..., tools=[...])` so they join the initial stable provider schema, or add or replace one later with `session.grant_tool(...)`. A live grant intentionally changes the next request's tool segment, then becomes the reusable schema for following calls. A caller-supplied tool is gated by default.

```python
from langchain_core.tools import tool


@tool
async def incident_lookup(service: str) -> list[dict]:
    """Return open incidents for a service."""
    return await incidents.open_for(service)


session = Session(agent, directory="/srv/checkout", tools=[incident_lookup])

# Later, mid-session:
session.grant_tool(current_incident)
```

See [Granting a tool to a session](composition.md#granting-a-tool-to-a-session).

## Next

- [Composition](composition.md) explains every configured value, the plugin seam, and the product boundary.
- [Lifecycle and driving](lifecycle.md) covers suspension, resume, interrupts, steering, retries, and the complete stream contract.
- [Compaction, continuation, and persistence](persistence.md) covers history compaction, autonomous work, checkpoints, artifacts, transcripts, observational memory, and background jobs.
- [GitHub mentions](../user/github.md) is the library running in a GitHub Action when someone writes `@langmesh[bot]` on an issue or a pull request, or replies to the bot. An issue with file edits opens a draft PR; a pull-request mention updates that PR. Provider and model come from the profile named in `.github/langmesh.yaml`. The job reads `github.api_key` from `.github/secrets/` (or the XDG secrets directory), acknowledges at once, and updates that comment; real prompts are markdown under `src/langmesh/github/prompts/`.
