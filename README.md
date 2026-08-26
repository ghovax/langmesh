<h1>
<img src="documentation/assets/langmesh-lockup.svg" alt="LangMesh" height="45">
</h1>

**An open coding agent you run yourself, and a harness you can edit.**

A coding agent needs more than a model. Something has to write the system prompt, give the model its tools, decide what it may run without asking, and keep the conversation from overflowing. That layer is the harness, and it decides more about the result than the model does. In most products it is closed. In LangMesh it is the code you are reading, and you can change it.

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE) ![Platform: macOS (Apple Silicon)](<https://img.shields.io/badge/platform-macOS%20(Apple%20Silicon)-black>) ![Built with Tauri, Next.js, LangChain](https://img.shields.io/badge/built%20with-Tauri%2C%20Next.js%2C%20LangChain-6E56CF)

## What it is

One conversation with an agent is a **session**. You create one, send it work, and it answers over its life. That is the only object in LangMesh, and everything below is a way of running one.

LangMesh is four layers, and the first three are two packages that ship as one image. Each layer uses the one under it and adds a single thing:

1. **The library** — `import langmesh`. `langmesh.Session` runs an agent in your own process. You give it the agent, the model, an absolute working directory, and the credentials; it performs no implicit user or project discovery, starts no daemon, and forces no plugin. Explicit tools operate only when composed, while shipped prompt assets remain package resources. This is the harness itself, and the three layers above are all built on it. See [As a library](documentation/library/index.md).
2. **The machine loaders and the daemon** — the `langmeshd` package. The machine loaders (`langmeshd.daemon.machine`) read your configuration file and the agents in your `.agents` directories and turn them into what the library takes. `langmeshd` then hosts every session. This layer knows your home directory exists; the library does not.
3. **The clients** — the `langmesh` command (`serve` for the interface, `mail` for IMAP/SMTP in front of the daemon), the macOS app, and a phone. All talk to the daemon and contain no harness of their own; anything one can do, the others can.

An agent can use these too. When a session needs help it creates a second session and messages it, over the same API your terminal uses. The helper appears in your session list, you can watch it, and it ends when its parent does. Its answer arrives as a message, in its own words.

## Why own the harness

The harness writes the system prompt, defines the tools, manages context, and sets what the agent may do. The same model does different work under different harnesses — OpenCode versus Claude Code or Codex, say. LangMesh lets you change that layer:

- **Any behavior beyond a plain model turn is a plugin.** Goal review, compaction, permission gating, autonomous continuation, observational memory, background jobs, and every tool from `bash` to `control_screen` are features composed onto a core that names none of them. A bare library session has no features at all. The product composes its own set; you compose yours. See [Composition](documentation/library/composition.md).
- **Tune the guardrails.** Permission modes and per-command rules are configuration, and the engine that enforces them is open code. When the settings are not enough, you can change how permissioning works ([Permissions](documentation/user/configuration.md#permission-modes)).
- **The agent can work on LangMesh itself.** Its prompt says that it runs LangMesh. Open the LangMesh repository as the project. The agent then reads and edits the harness, and you rebuild ([Architecture](documentation/internal/architecture.md)).

## Install

LangMesh runs on **macOS on Apple Silicon**. It ships as two downloads:

- **The daemon bundle**, which carries the harness, the daemon and the `langmesh` command in one signed image (`LangMesh Computer Use.app`).
- **The app**, which is the window that talks to it (`LangMesh.app`).

Download the latest release, install both, and open the app (or run `langmesh serve` for the browser). The build is self-signed, so Gatekeeper warns you at the first launch. You can also build from source with the Nix-pinned toolchain.

See the [Installation guide](documentation/user/installation.md) for both paths in full.

### Interchangeable model providers

LangMesh provides an optional adapter for the independent `models-provider`
package. The abstraction package does not import LangMesh; this direction is
intentional.

```python
from langmesh.models_provider import LangMeshProvider
from models_provider import ModelConfiguration

provider = LangMeshProvider(providers={"anthropic": "sk-ant-…"})
model = provider.create(ModelConfiguration(provider="anthropic", model="claude-sonnet-4-5"))
answer = model.invoke("Explain this paragraph.")
```

## Quickstart

The same harness, reached two ways. Start at the layer you want: an object in your own program, or a window.

### As a library

No daemon and no implicit home-directory discovery or persistence. The agent, its prompt, its features, and its credentials are values in your program; explicit tools may operate only within the authority you compose:

```python
import asyncio
from langmesh import AgentConfiguration, Session

reviewer = AgentConfiguration(
    name="reviewer",
    description="Reads a change and reports what it would break.",
    system_prompt="You review changes. Name the risk, or say there is none.",
    provider="anthropic",
    model="claude-opus-4-5",
)

async def main() -> None:
    async with Session(
        reviewer,
        directory="/srv/checkout",
        providers={"anthropic": "sk-ant-…"},
    ) as session:
        answer = await session.ask("What would break if I removed the retry loop in the fetcher?")

asyncio.run(main())
```

`stream()` instead of `ask()` gives typed live events, and every feature you want — including the tools themselves — is composed explicitly through `SessionComponents`:

```python
from langmesh import Session, SessionComponents
from langmesh.runtime.plugins.background import BackgroundJobs
from langmesh.runtime.plugins.bash import Bash
from langmesh.runtime.plugins.web import Web

session = Session(
    reviewer,
    directory="/srv/checkout",
    providers={"anthropic": "sk-ant-…"},
    components=SessionComponents(features=[BackgroundJobs(), Bash(), Web()]),
)
async for event in session.stream("Summarise what the test suite covers."):
    ...  # Dispatch on each typed TurnEvent as it arrives.
```

Because the library forces nothing, the agent above has no shell until `Bash()` is composed; a `SessionComponents()` with no features is a plain model turn and nothing else. `SessionComponents` carries the models, prompt and attachment composition, checkpoints, artifacts, jobs, transcripts, approvals, audit, tools, permission policy, hooks, middleware, peer sessions, MCP servers, file leases, credentials, and tracing — plus the `features` list and the `services` bundle the host hands its plugins. Run facts (directory, identity, permission mode, confinement) are passed to `Session`, never to the components, so a persistence adapter cannot silently change the boundary.

Checkpoint storage is an adapter choice. The default is isolated in-memory state; `SQLiteCheckpoints` uses a caller-owned SQLite connection, which may itself be in memory or backed by a file:

```python
import sqlite3
from langmesh import Session, SessionComponents, SQLiteCheckpoints

connection = sqlite3.connect("sessions.sqlite")
checkpoints = SQLiteCheckpoints(connection)
components = SessionComponents(checkpoints=checkpoints)
async with Session(reviewer, directory="/srv/checkout", components=components) as session:
    answer = await session.ask("Explain the persistence boundary.")
```

`SessionCheckpoint`, `SessionSnapshot`, `PendingInput`, `PendingTurn`, and every feature state are typed values with explicit fields and `to_data()`/`from_data()` storage boundaries. The library never chooses a project file or home-directory database. Tool outputs use the same rule: `MemoryArtifacts` is the neutral default, `session.artifacts.read(identifier)` exposes the bytes, and the daemon selects its own atomic file adapter. A caller may implement the `Checkpoints`, `Artifacts`, `JobStore`, `Transcript`, `CredentialStore`, or other structural protocols and inject them through `SessionComponents`.

Three more things sit around the turn: bound it, wrap its tools, decide how its history compacts. Each one is an object with a method or two, so your own is as short as the ones that ship:

```python
from langmesh import MaximumToolCalls, Session, SessionComponents

class RefuseNetworkTools:
    """A hook. Sees the batch the permission rules approved, and narrows it."""

    async def before_tools(self, calls):
        return [call for call in calls if call["name"] not in ("fetch_url", "search_web")]

class Timed:
    """Middleware. Wraps every tool call, yours and the harness's alike."""

    async def run(self, call, proceed):
        started = time.monotonic()
        try:
            return await proceed(call)
        finally:
            metrics.timing("langmesh.tool", time.monotonic() - started, tags={"tool": call.name})

from langmesh.runtime.plugins.compaction import Compaction


components = SessionComponents(
    hooks=(MaximumToolCalls(20), RefuseNetworkTools()),
    middleware=(Timed(),),
    features=[Compaction()],
)
session = Session(reviewer, directory="/srv/checkout", components=components)
```

A hook narrows and can never widen: `before_tools` runs after the permission barrier. The [library documentation](documentation/library/index.md) covers composition, lifecycle, customization, compaction, persistence, and the complete API.

### From the terminal

The command line has two long-running clients: **serve** makes the interface available over HTTP with the daemon behind it, and **mail** IDLEs a mailbox and drives the same daemon.

```console
$ langmesh serve
$ langmesh mail check
$ langmesh mail
```

`mail check` proves IMAP and SMTP without IDLEing. See [Email](documentation/user/email.md). Everything else — creating and messaging sessions, answering permission requests, watching work, recurring schedules, configuration, sign-in — happens in the interface (the app, or the browser `serve` exposes) or over the daemon's API. A session composes over that API, not by shelling out to this command.

### From the app

1. **Launch LangMesh.** The app starts the separately installed daemon when it cannot find one, then opens the window.
2. **Add a model key.** Open **Settings**, then **Providers**, and paste a key for any provider. You can also sign in with a ChatGPT or Cursor subscription. Then pick a model. Keys live in your LangMesh configuration file — see the [Configuration guide](documentation/user/configuration.md).
3. **Start a conversation.** Type a task. Approve tool calls as they come up, or relax the [permission mode](documentation/user/configuration.md#permission-modes) once you trust a flow.

The screen-control tools need a one-time Accessibility grant and Chrome's remote-debugging toggle — see the [Installation guide](documentation/user/installation.md#permissions-the-app-may-ask-for).

> [!NOTE]
> You can opt in to send a snapshot of how you work. LangMesh appends it to the model conversation as session context and sends it to your model provider. This is off by default. See [what the agent sends to your model provider](SECURITY.md#what-the-agent-sends-to-your-model-provider).

## How it compares

The closest tools are [Claude Code](https://code.claude.com) and [OpenAI Codex](https://github.com/openai/codex). Both are more mature than LangMesh. In 2026 both also drive a real browser and control native macOS apps. Codex is open source too, and it runs on models that are not OpenAI's. This table compares approaches. It does not list things that only LangMesh does.

|                    | LangMesh                                                                                                                                      | Claude Code                                                                                                                    | OpenAI Codex                                                                                                |
| ------------------ | --------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------ | ----------------------------------------------------------------------------------------------------------- |
| **License**        | Open source (MIT)                                                                                                                             | Proprietary                                                                                                                    | Open-source CLI (Apache-2.0); cloud and models are OpenAI's                                                 |
| **Models**         | Any provider, or a ChatGPT or Cursor login, per session — the screen tools included                                                           | Claude first; third-party providers for coding on the CLI and VS Code, but its browser and computer use need an Anthropic plan | GPT-5 Codex by default; the CLI can also point at OpenRouter, Ollama, LM Studio, or any compatible endpoint |
| **Where it runs**  | A harness you self-host — local, a VM, a container, or over SSH — with a native app pointed at it                                             | Proprietary client; long tasks run on Anthropic's cloud                                                                        | Local CLI, IDEs, and a desktop app; async tasks run on OpenAI's cloud                                       |
| **Screen control** | Native macOS apps and your own Chrome, read as ranked accessibility/DOM elements from a plain-language search — screenshots only when you ask | Your real Chrome session, plus macOS computer use driven by downscaled screenshots (research preview, Pro/Max)                 | In-app and Chrome-extension browser, plus background macOS computer use driven by screenshots               |
| **Reach**          | Terminal-first (`langmesh`), plus a desktop app and phone over the same API; every session is scriptable and attachable                       | Terminal, VS Code, JetBrains, desktop, web, mobile, Slack, CI, GitHub review; macOS and Windows                                | CLI, IDEs, desktop, cloud/web, Chrome, GitHub review; macOS and Windows                                     |

Three design choices distinguish LangMesh:

- **Structure, not screenshots.** LangMesh reads the screen as a semantic search over the accessibility tree and the DOM. It returns a few ranked elements. The other tools reason over screenshots. A query here costs a few elements, not an image.
- **A session is addressable, not a function call.** A session has a name, a durable record and an inbox, and it outlives whatever made it. To make a peer, a session creates another session and messages it. It uses the API that a person uses, and gets an answer as a message rather than a return value.
- **A composed script, not a click-by-click loop.** `control_screen` runs a Python program. Its primitives (`click`, `type`, `scroll`, `evaluate`) are the same on native apps and in the browser. One call can loop over rows, branch on what it finds, and call the page's own API. The other tools need one round trip for each click. LangMesh needs far fewer model turns.

The trade-off: it needs an accessibility tree or DOM to read, where a screenshot approach works on anything drawn on screen. See [Tools](documentation/user/agent-system.md).

Elsewhere they lead. They have more polish, more places to run, and deeper ecosystems. Claude Code has subagents, hooks, plugins, and an Agent SDK. Codex has cloud tasks, more plugins, and automatic PR review. All three tools gate actions behind approvals and a sandbox.

LangMesh is the small, open, model-agnostic option that you host yourself. For a mature agent on a vendor's cloud, use theirs.

## Where things live

LangMesh follows the XDG convention. It does not use a single dot-directory:

- Configuration in `~/.config/langmesh`
- Durable state in `~/.local/share/langmesh`
- Sockets in the runtime directory
- Logs in `~/.local/state/langmesh`
- Caches in `~/.cache/langmesh`

The OS clears the runtime directory when you log out. A crashed daemon therefore leaves nothing behind.

Only the holder of a session's handle can reach it. Creating a session mints a capability token (derived, never stored). Every call to a session must present that token. The daemon guards its own API the same way, with a token that it writes `0600` into the runtime directory.

That token does not say _which_ session is calling. A session runs as the same user and could read the file. So on the unix socket the daemon asks the kernel for the peer's pid. It resolves the pid to a session through the process group that every worker leads. A call is therefore attributed to whoever made it.

> [!NOTE]
> A session's permission mode can be changed while it runs, and the change reaches the turn already in flight — a conversation that starts under manual approvals and earns trust does not have to be restarted to run under a looser mode. A child gets a mode no looser than its parent's, and tightening a session tightens everything it created. See the [Security notes](SECURITY.md).

## Documentation

The full guides live in the **[Documentation](documentation/index.md)**. They cover the [library](documentation/library/index.md), the [command line](documentation/user/installation.md), the [desktop app](documentation/user/app.md), [installation](documentation/user/installation.md), agents and skills, configuration, the tool surface, the [architecture](documentation/internal/architecture.md), and development.

## Built with

[Tauri](https://tauri.app), [Next.js](https://nextjs.org), [Chakra UI](https://chakra-ui.com), [LangChain](https://www.langchain.com), [LiteLLM](https://litellm.ai), [FastAPI](https://fastapi.tiangolo.com), [Model Context Protocol](https://modelcontextprotocol.io), and [A2A](https://github.com/google/A2A)

## Contributing

Contributions are welcome — see the [Contributing guide](CONTRIBUTING.md) and the [Code of Conduct](CODE_OF_CONDUCT.md).

## License

[MIT](LICENSE) © Giovanni Gravili
