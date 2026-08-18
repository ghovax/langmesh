# LangMesh

LangMesh is an agent harness with two delivery layers: the `langmesh` Python library is the configurable core; the daemon, command line, and web interface are product adapters built on that core.

## Choose your layer

| Need | Use | Read |
| --- | --- | --- |
| Embed an agent in a Python process | `langmesh.Session` | [Library quickstart](library/index.md) |
| Compose the runtime directly | `AgentRuntime`, `RuntimeProfile`, and `RuntimeComponents` | [Composition](library/composition.md) |
| Keep sessions durable and remotely addressable | `langmeshd` | [Architecture](internal/architecture.md) |
| Operate sessions | CLI or desktop app | [Command line](user/installation.md) or [desktop app](user/app.md) |

## Core guarantees

- Runtime facts and replaceable capabilities are separate immutable values.
- A `Session` has one structural phase: idle, running, suspended, compacting, or retrying.
- A suspended tool batch is checkpointed with its plans and decisions, so process restart cannot repeat completed side effects.
- Tool policy runs before hooks; hooks may remove approved calls but cannot add calls.
- Model-visible history is append-only. Reviewer and continuation requests preserve the existing provider-cache prefix.
- Compaction preparation, compaction, continuation, persistence, workspaces, locations, MCP server connections, and peer sessions are replaceable library ports.

Start with the [library quickstart](library/index.md) when embedding LangMesh, or [installation](user/installation.md) when using the product.
