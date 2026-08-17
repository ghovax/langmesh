



---

## Agent system

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

### Agents

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

Each agent is a profile a session can be created with, and the daemon serves [A2A](https://github.com/google/A2A) for every session it hosts. A session that needs a peer creates one with its `create_session` tool, over the control plane your terminal uses. See [Tools](agents-and-tools.md#composing-with-other-sessions) for how a peer reports back.

Bundled agents:

| Agent               | Role                                                   |
| ------------------- | ------------------------------------------------------ |
| `general-assistant` | A capable default for everyday tasks.                  |
| `reviewer`          | Skeptical planning and verification before building.   |
| `code-investigator` | Reads and explains a codebase without changing it.     |
| `code-implementer`  | Writes and edits code against a clear plan.            |

### Skills

A skill is a `SKILL.md`: a focused capability the agent loads **only when relevant**, keeping the system prompt lean. Frontmatter carries a `description` the model uses to decide when to load it, through the `load_skill` tool.

```markdown
---
name: coding
title: Code patterns, conventions, and implementation discipline
description: Load before writing, editing, refactoring, or reviewing code.
enabled: true
---

## Coding Patterns and Implementation Discipline

...
```

Bundled skills include `coding`, `data-visualization`, `literature-search`, `langmesh-configuration`, and `context7-mcp`.

### Memory

`.agents/memories/*.md` are passages the user records. Only their metadata is injected into the prompt; the agent reads a memory's body **on demand**, so context stays small while knowledge accumulates across sessions.

Observational memory is separate. `.agents/observations.sqlite` is current workspace knowledge maintained deliberately by an agent through Bash. It holds only current rows; Git provides history. The system prompt carries only a compact descriptor (resolved path, revision, per-ledger counts, timestamp extent) and a stable instruction to retrieve a relevant slice when prior work may matter. Exact retrieval uses SQLite; semantic retrieval exports minified JSONL into a fresh disposable Semble index. The `observational-memory` skill defines the retrieval and atomic write protocols; `consolidate-observations` runs only when the user explicitly invokes it.

### MCP servers

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

---

## Tools

A session acts through tools, and every tool call runs inside the session's [confinement](configuration.md#confinement). A call that stays inside it runs without asking anybody. A call that asks to reach past it pauses under `ask` and reaches you as a prompt in the app; under `automatic` it never pauses, and the reviewer allows or refuses it. The description the model reads is in the repo, one Markdown file per tool in `src/langmesh/runtime/tools/descriptions/`.

There is no delegation tool and no in-process sub-agent. A session that needs a peer creates one with `create_session`, which reaches the same control plane your terminal does. See [Composing with other sessions](#composing-with-other-sessions).

### The built-in surface

**Shell and files**

| Tool            | What it does |
| --------------- | ------------ |
| `bash`          | Run shell commands. Sandboxed to the workspace by default; per-command rules per agent. |
| `download_file` | Download a file from a URL to disk. |

There are no dedicated `find_files` or `search_content` tools; for literal file-name and content search, use `bash` with ripgrep (`rg`) and `fd`.

There is likewise no proprietary observational-memory tool. The active location owns `.agents/observations.sqlite`; the system prompt carries only its compact descriptor, and agents retrieve relevant entries through Bash and the `observational-memory` skill when prior work may matter.

A session can also install what it does not have. Where the machine has Nix, each session gets a package profile of its own on `PATH` and installs into it with an ordinary `nix profile add nixpkgs#<name>`. What it installs belongs to that session and is deleted with it, and the confinement is untouched. See [Configuration](configuration.md#the-session-toolbox).

**Web**

| Tool         | What it does |
| ------------ | ------------ |
| `search_web` | Search the web (Exa-backed fallback). |
| `fetch_url`  | Fetch and read a page via a tiered engine: Jina, then Firecrawl, then direct. |

**Orchestration and knowledge**

| Tool                         | What it does |
| ---------------------------- | ------------ |
| `set_tasks` / `update_tasks` | Maintain a task list for a multi-step job. |
| `update_goal`                | Set the outcome the session is working toward, what it is for, and what would prove it reached. |
| `read_turn`                  | Read a sibling turn handed to this session from outside. |
| `load_skill`                 | Load a `SKILL.md` capability on demand. |
| `ask_user`                   | Ask the user a question and wait for the answer. |

A goal is not a longer task list. The task list is the steps; the goal is the outcome the steps are for. When a turn ends with actionable tracked tasks unfinished, the harness opens a hidden reminder turn that asks the session to reassess the user's requests, add any omitted work, and continue instead of merely describing it. Explicitly blocked tasks wait for the person, and a bounded continuation allowance prevents a stale list from running forever.

An open goal keeps going through its independent review machinery. If both obligations exist, the review runs first and its continuation carries the task reminder too, so neither mechanism is disabled and only one serialized next turn opens. The linked read-only reviewer session inherits the complete conversation and independently inspects the workspace with tools. Its live transcript is available in the goal-review panel without appearing as an ordinary sidebar conversation. It reads the user's requests and formal goal, challenges the working agent's claims, and submits what is still unproven, whether the contract is complete, whether the outcome is reached, and the exact next message when work remains. That generated message is shown once in the chat under "Relayed from the goal review agent," links back to its review transcript, and opens the next turn.

That review is written to push. It only accepts a goal as reached after independently exploring the relevant system, naming what proves each condition, and establishing that the contract omitted nothing necessary. When the goal is too weak, the reviewer sends checkable additions back and makes replacing the goal the first part of the next turn. It only accepts an impasse once no route is left. What stops a run instead is an allowance for how long it goes with nobody watching, after which the goal is parked and waits for you, or you call it off from the bar above the composer. See [Architecture](../internal/architecture.md#goals).

**Peer sessions**

### Composing with other sessions

| Tool              | What it does |
| ----------------- | ------------ |
| `create_session`  | Create an idle peer session. Its `agent` argument enumerates the profiles actually installed. Returns as soon as the peer exists. |
| `message_session` | Message another session, one you created or the one that created you. A session already mid-turn has the message steered into that turn at its next safe point; an idle one starts a new turn. A session parked on a human decision takes neither, and the call reports that. |
| `read_session`    | One session's state: its profile, whether its process is alive, whether a turn is in flight, whether it is waiting on a human. |
| `list_sessions`   | The sessions this one created. Its own subtree, not the machine's. |

The caller is the parent, always; it is not an argument. That puts a peer inside the tree, inside the reaper, and under the permission clamp, so a peer can never hold authority its parent does not have.

The peer starts with a copy of its parent's model-facing conversation, while keeping its own agent profile, permissions, process, and context window. The unfinished `create_session` tool call is excluded, so its history begins at a valid message boundary.

**A peer answers by messaging.** When the peer finishes, it calls `message_session` on the session that created it; that session's id is in its context as `parent_session`. `create_session` therefore does not wait, and there is no handle to hold. Nothing reconstructs a result: the peer decides its own answer at the moment it knows, and the reply wakes the caller.

That message arrives as a **peer turn**, not a user turn. The wire carries the distinction under the harness's one extension key, `urn:langmesh:ext:turn:v1`, which sends `kind` plus `peerSender` to name the sender. Without that, a peer's report would reach the model as an instruction from the person it works for, and appear in the transcript as words the user never wrote.

A peer that dies before it reports cannot say so; that is the one thing the harness says for it. The daemon tells the parent when it reaps a child, with the child's id and the reason it ended.

**Agents on other hosts**

`list_remote_agents` and `message_remote_agent` are separate verbs, because a remote agent is a separate bargain: it runs on someone else's machine, at someone else's cost, with no shared history and no access to this filesystem. Present only when one is registered.

**MCP servers**

`call_mcp_server_tool`, `list_mcp_tools`, `list_mcp_resources`, and `read_mcp_resource` bridge to any configured [MCP server](agents-and-tools.md#mcp-servers).

### Screen control (`control_screen`)

LangMesh drives the live screen through one tool, `control_screen`, covering native macOS apps and **your own Chrome**. Its Python script both finds elements and acts on them. It is **macOS-only** and **opt-in**, gated by `computer_control.enabled`, which is off by default. See [Configuration guide](configuration.md#execution-and-permissions).

**Finding.** Inside the script, `find_many(query)` and `find_one(query)` take a plain-language query. They return ranked elements to act on, not pixels. Each element carries a stable `id`, its role, its text, and its context. On native apps this reads the accessibility tree; on Chrome it reads the page's real semantic structure, iframes included, through the Chrome DevTools Protocol via Playwright. It also surfaces the page's own network and API requests, so the agent can find the endpoints the page calls. `find_one` returns the single best match and raises if the top matches are indistinguishable.

**Acting.** The same script drives the elements a find returned, addressing them by `id` or by a query resolved the same way. It uses trusted input: click, type, scroll, `evaluate`, and the like. The script is ordinary Python, so a whole task is a single call that can loop, branch, and call the page's own API. On the browser, `evaluate` can replay the page's own authenticated API in-page, reusing the logged-in session.

**Targets.** Every window and tab has a platform-minted id, and the current list is carried into the agent's context each turn, so there is no listing tool and no round trip. An application is not a target: two Finder windows are two places. Every window is listed, including ones behind others, minimized, or on another Space, because synthesized input is posted to the process rather than the screen. Each entry carries `can`, the vocabulary that place answers to.

**What changed.** The result reports what each action changed, in `changed`: the globals that moved (`title`, `focus`, `selection`, and on a page `url`) and what became newly present. An action that replaced the document reports `navigated`. An action that changed nothing says `changed: []`, which is the signal that a click missed or a pane had not loaded.

**Tabs.** The script chooses which page it is on: `tabs()` lists every open tab; `tab(id)` switches and brings it to the front; `new_tab(url)` opens one; `close_tab(id)` closes one. The listing covers all of your tabs, not only the ones the agent opened, and nothing stops an agent from closing a tab it did not open; it has an instruction instead, because these tabs are your working state.

**Frames.** An `iframe` is its own document with its own origin and its own session. Element ids are already frame-scoped, and `evaluate(..., frame="f1")` and `read(frame="f1")` run inside that document; that is the only way to reach one through the credentials it holds.

LangMesh attaches to **the Chrome you already use**, with your real logins and sessions. It never launches the browser, quits it, or copies it. It reads structure, not pixels: there is no screenshot path for computer use, so a drawn surface such as a canvas or WebGL exposes nothing to find.

**Enable it:**

- Grant **Accessibility** permission to LangMesh for native apps (System Settings, then Privacy and Security, then Accessibility). macOS matches the permission to the app's code identity, so a signed build keeps the grant across updates. See the [Development guide](../internal/development.md#building-and-signing).
- Turn on Chrome's remote-debugging toggle once for the browser surface: open `chrome://inspect` and enable it under the remote-debugging option.
- Set `computer_control.enabled: true` in the configuration (off by default).

> [!NOTE]
> Typing fills a field without submitting unless the agent explicitly asks to, so it never posts a form by accident.

**What counts as changing something.** A script's own calls are read for the primitives that change state: `click`, `type`, `choose`, `upload`, `drag`, `evaluate`, `press`, `navigate`, `new_tab`, `close_tab`, `caret`, and `select`. A script that names none of them runs with only the reading primitives in its namespace. One that names any is put to whoever decides for the session, unless a `screen` rule already names that primitive. `evaluate` is on the acting side because it runs arbitrary JavaScript in a page you are signed in to; `navigate` is there too because on many sites a URL is a command, such as `/logout` or `/items/12/delete`. In an ordinary session the harness examines a script that acts; it does not block it. In a [read-only session](configuration.md#permission-modes) it refuses the script outright.

### Where the definitions live

A built-in tool is one `Tool` unit: its schema, description, and handler joined in `langmesh.runtime.tools.units`.

- Schemas and the model-facing `StructuredTool`s: `src/langmesh/runtime/tools/registry.py`.
- The execution of each built-in, over the shared `ToolServices` bundle: `src/langmesh/runtime/tools/handlers.py`.
- Descriptions the model reads, one markdown file per tool: `src/langmesh/runtime/tools/descriptions/`.
- The dispatch preamble (permission, validation, location, policy) and the batch runner: `src/langmesh/runtime/tools/dispatch.py`.
- Model-facing message templates: `src/langmesh/runtime/prompts/` and `src/langmesh/computer/messages/`.
- The guidance a session gets for screen control: `src/langmesh/runtime/prompts/computer_control_guidance.md`.

The runtime never hard-codes a tool name: it dispatches whatever `Tool` units the session was composed with. A tool runs inside the session's own process, so its blast radius is that session: its working directory, its permission mode, and its own MCP server connections. Under the `worktree` strategy the working directory is the session's own git worktree.
