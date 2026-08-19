# LangMesh

LangMesh is an agent harness delivered in two packages that ship as one image. `langmesh` is the **library**: the configurable harness core you embed in a Python process. `langmeshd` is the **product**: the daemon, command line, and web interface built on that core. The library reads nothing from disk it was not given; the product reads your machine's configuration and `.agents` trees.

Everything beyond a plain model turn — the tools, goal review, compaction, permission gating, autonomous continuation, observational memory, background jobs — is a **plugin** composed by whoever runs a session. The library names no plugin; the product composes its own set.

## Choose your layer

| Need | Use | Read |
| --- | --- | --- |
| Embed an agent in a Python process | `langmesh.Session` | [Library quickstart](library/index.md) |
| Compose the runtime directly | `RuntimeProfile`, `RuntimeComponents` | [Composition](library/composition.md) |
| Compose and write plugins | `Feature`, `PluginContext`, `SessionComponents(features=...)` | [Composition](library/composition.md) |
| Keep sessions durable and remotely addressable | `langmeshd` | [Architecture](internal/architecture.md) |
| Operate sessions | CLI or desktop app | [Command line](user/installation.md) or [desktop app](user/app.md) |

## Core guarantees

- Run facts and replaceable capabilities are separate immutable values.
- A `Session` has one structural phase: idle, running, suspended, compacting, or retrying.
- A suspended tool batch is checkpointed with its plans and decisions, so process restart cannot repeat completed side effects.
- Tool policy runs before hooks; hooks may remove approved calls but cannot add calls.
- Model-visible history is append-only; granting a tool appends a description instead of rewriting the schema, so the provider-cache prefix never moves.
- The library forces no tools and no plugins. A session runs exactly what you compose, and the product composes its own bundle.

Start with the [library quickstart](library/index.md) when embedding LangMesh, or [installation](user/installation.md) when using the product.
