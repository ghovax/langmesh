# Agent system

Everything that shapes how LangMesh behaves, its agents, their reusable skills, its memory, and its tool servers, is **plain Markdown and JSON on disk**. There are two layers, and they merge by name:

- **Global:** `~/.agents/` — available everywhere.
- **Project-local:** `.agents/` in the working directory you point an agent at.

A project-local entry **overrides** a global one with the same name, so a repository can ship its own agents and skills without touching your global setup. The server also bundles a base set, which is always present.

Everything sits under `.agents/`:

| Path                   | What it holds                                          |
| ---------------------- | ------------------------------------------------------ |
| `agents/<id>/AGENT.md` | The whole profile: frontmatter and the prompt body.    |
| `skills/<id>/SKILL.md` | A reusable capability, loaded on demand.               |
| `memories/*.md`        | User-recorded passages, loaded on demand like skills.  |
| `observations.sqlite`  | Agent-maintained current knowledge for this workspace. |
| `mcp.json`             | MCP server configuration.                              |

## Agents

An agent is a directory with one file in it: **`AGENT.md`**, spelled that way for the same reason a skill is `SKILL.md`. YAML frontmatter says what the agent is; the body is its system prompt.

```markdown
---
name: reviewer
title: Reviewer
description: A rigorous skeptic that pushes back before it builds.
role: primary
enabled: true
model: mimo-v2.5
provider: opencode
reasoning_effort: high
permission_mode: ask
tools:
  bash:
    enabled: true
    background_allowed: true
    permissions:
      sudo *: deny
      rm *: ask
  mcp:
    permissions:
      "*.delete_*": deny
---

You are the skeptic: the deliberate opposite of an agreeable assistant.
```

Each agent is a profile a session can be created with, and the daemon serves [A2A](https://github.com/google/A2A) for every session it hosts. A session that needs a peer creates one with its `create_session` tool, over the control plane your terminal uses. See [Tools](tools.md#composing-with-other-sessions) for how a peer reports back.

Bundled agents:

| Agent               | Role                                                   |
| ------------------- | ------------------------------------------------------ |
| `general-assistant` | A capable default for everyday tasks.                  |
| `reviewer`          | Skeptical planning and verification before building.   |
| `code-investigator` | Reads and explains a codebase without changing it.     |
| `code-implementer`  | Writes and edits code against a clear plan.            |

## Skills

A skill is a `SKILL.md`: a focused capability the agent loads **only when relevant**, keeping the system prompt lean. Frontmatter carries a `description` the model uses to decide when to load it, through the `load_skill` tool.

```markdown
---
name: coding
title: Code patterns, conventions, and implementation discipline
description: Load before writing, editing, refactoring, or reviewing code.
enabled: true
---

# Coding Patterns and Implementation Discipline

...
```

Bundled skills include `coding`, `data-visualization`, `literature-search`, `langmesh-configuration`, and `context7-mcp`.

## Memory

`.agents/memories/*.md` are passages the user records. Only their metadata is injected into the prompt; the agent reads a memory's body **on demand**, so context stays small while knowledge accumulates across sessions.

Observational memory is separate. `.agents/observations.sqlite` is current workspace knowledge maintained deliberately by an agent through Bash. It holds only current rows; Git provides history. The system prompt carries only a compact descriptor (resolved path, revision, per-ledger counts, timestamp extent) and a stable instruction to retrieve a relevant slice when prior work may matter. Exact retrieval uses SQLite; semantic retrieval exports minified JSONL into a fresh disposable Semble index. The `observational-memory` skill defines the retrieval and atomic write protocols; `consolidate-observations` runs only when the user explicitly invokes it.

## MCP servers

`.agents/mcp.json` registers [Model Context Protocol](https://modelcontextprotocol.io) servers under `mcpServers`. Both `stdio` and `streamable_http` transports are supported.

```json
{
  "mcpServers": {
    "context7": {
      "enabled": true,
      "transport": "streamable_http",
      "url": "https://mcp.context7.com/mcp",
      "stateful": true
    },
    "local_echo": {
      "enabled": true,
      "transport": "stdio",
      "command": "uv",
      "args": ["run", "python", "examples/mcp/echo/server.py"],
      "env": { "UV_CACHE_DIR": "/private/tmp/uv-cache" },
      "cwd": "."
    }
  }
}
```

Their tools and resources reach the agent through `call_mcp_server_tool`, `list_mcp_tools`, `list_mcp_resources`, and `read_mcp_resource`. The repository includes a complete [stdio server example](https://github.com/ghovax/langmesh/tree/main/examples/mcp).
