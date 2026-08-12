# LangMesh — Documentation

Detailed guides for installing, configuring, understanding, and developing LangMesh. For a high-level overview, start with the [project README](../README.md).

**They are a stack, not separate products.** The library is the bottom of it, and everything else is built on top:

| Layer                     | What it is                                                                 | What it knows about your machine         |
| ------------------------- | -------------------------------------------------------------------------- | ---------------------------------------- |
| `langmesh.Session`        | The harness: turn loop, tools, prompts, permissions                        | Nothing. Every value is one you passed   |
| `langmesh.daemon.machine` | Turns a home directory into what `Session` takes                           | The XDG paths, and your `.agents`        |
| `langmeshd`               | Supervision: the live sessions, the register of what exists, the databases | Everything, and it is the right place to |
| `langmesh`, and the app   | Clients of the daemon                                                      | Where the daemon is                      |

Start with the layer you are actually using.

| If you want to…                                                                     | Read                                                |
| ----------------------------------------------------------------------------------- | --------------------------------------------------- |
| **Embed the harness in your own program** — `import langmesh`, no daemon, no socket | [As a library](library.md)                          |
| **Drive it from a terminal** — create, send, attach, approve                        | [The `langmesh` command](cli.md)                    |
| **Use the macOS app**                                                               | [The desktop app](app.md)                           |
| **Reach it from your phone**                                                        | [`langmesh reach`](cli.md#reaching-it-from-a-phone) |

Then the rest, in the order they build on each other. [Architecture](architecture.md) defines the words the others use, so it comes first:

| Guide                             | What's in it                                                                                                                                                           |
| --------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| [Architecture](architecture.md)   | The vocabulary, the four layers, how a message becomes work, and [what is recorded about prompt caching](architecture.md#prompt-caching-and-what-is-recorded-about-it) |
| [Installation](installation.md)   | Download and Gatekeeper, or building from source                                                                                                                       |
| [As a library](library.md)        | `langmesh.Session` in your own process, and every seam you can replace                                                                                                 |
| [The `langmesh` command](cli.md)  | Every verb, the session states, JSON and exit codes                                                                                                                    |
| [The desktop app](app.md)         | The window, decisions, environments, and screen control                                                                                                                |
| [Agent system](agent-system.md)   | Authoring agents, skills, memory, and MCP servers                                                                                                                      |
| [Configuration](configuration.md) | Providers, keys, permissions, MCP, and every config key                                                                                                                |
| [Tools](tools.md)                 | The full tool surface, including screen control (`control_screen`)                                                                                                     |
| [Development](development.md)     | The dev environment, running the pieces, building the app                                                                                                              |
| [Security](../SECURITY.md)        | What the agent sends, and what confines it                                                                                                                             |

## The shortest thing that works

```python
import asyncio
from langmesh import AgentConfiguration, Catalogue, FilesystemConfiguration, SandboxConfiguration, Session

reviewer = AgentConfiguration(
    name="reviewer",
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
        answer = await session.ask("What would break if I removed the retry loop?")

asyncio.run(main())
```

That reads nothing from your home directory and starts no daemon. Observational memory is the active location's `.agents/observations.sqlite`; an agent creates or changes it deliberately through Bash, while the runtime reads it for presentation, fold verification, and a constant-sized prompt descriptor containing only path, revision, counts, and timestamp extent. Relevant entries are retrieved progressively on demand. No file is created merely by importing LangMesh.

The library exposes the same read side directly:

```python
from langmesh import ObservationRegistry, ObservationRegistryError, WorkspaceResources

resources = WorkspaceResources.local("/srv/checkout")
registry = ObservationRegistry(resources)
try:
    snapshot = await registry.load()
except ObservationRegistryError as error:
    registry_error = error
else:
    directives = snapshot["entries"]["directives"]
    revision = snapshot["revision"]
```

Use `registry.watch()` as an async iterator when a library UI or service needs every committed change without polling.

The whole workspace can also be virtual. `WorkspaceResources` uses fsspec, so `Session(agent, resources=WorkspaceResources.memory(...))` or `WorkspaceResources.from_url(...)` supplies skills, memories, MCP configuration, attachments, observational memory, and Bash-visible files through one established interface. See [As a library](library.md#the-seams) for materialization, synchronization, overlays, provider-native watch adapters, and examples.

To swap one, pass an object with the right methods. Each seam is a `typing.Protocol`: no base class to inherit, and no import of LangMesh in your type. [As a library](library.md) has the full table and a worked Redis checkpoint store.

## Where LangMesh keeps your things

Runtime state never lives in the repository. LangMesh follows the XDG convention:

- Configuration in **`~/.config/langmesh/`**
- Durable session state, including `history.sqlite` and `background.sqlite`, in **`~/.local/share/langmesh/`**; each source workspace or selected location owns **`.agents/observations.sqlite`** beside its `mcp.json`
- Sockets in the runtime directory
- Logs in **`~/.local/state/langmesh/`**
- Caches in **`~/.cache/langmesh/`**

The [Configuration guide](configuration.md) explains the settings worth explaining, and the [configuration reference](configuration-reference.md) lists every one of them.
