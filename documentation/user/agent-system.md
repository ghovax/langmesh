# Agent system

Everything that shapes how LangMesh behaves, its agents, their reusable skills, its memory, and its tool servers, is **plain Markdown and JSON on disk**. There are three sources, and entries with the same name are merged by precedence:

- **Bundled:** the profiles shipped with the harness — always present, the base source.
- **Global:** `~/.agents/` — available everywhere.
- **Project-local:** `.agents/` in the working directory you point an agent at.

A project-local entry **overrides** a global or bundled one with the same name, so a repository can ship its own agents and skills without touching your global setup. The daemon seeds `~/.agents/` from the bundled set on first run, non-destructively, and re-reads all three sources live. The library, embedded, reads none of this by default: it composes agents, skills, and memories in code unless you hand it a catalogue.

Everything sits under `.agents/`:

| Path                   | What it holds                                          |
| ---------------------- | ------------------------------------------------------ |
| `agents/<id>/AGENT.md` | The whole profile: frontmatter and the prompt body.    |
| `skills/<id>/SKILL.md` | A reusable capability, loaded on demand.               |
| `memories/*.md`        | User-recorded passages, loaded on demand like skills.  |
| `observations.sqlite`  | Agent-maintained current knowledge for this workspace. |
| `mcp.json`             | MCP server configuration.                              |
| `remote-agents.json`   | Agents registered on other hosts.                      |

### Agents

An agent is a directory with one file in it: **`AGENT.md`**, spelled that way for the same reason a skill is `SKILL.md`. YAML frontmatter says what the agent is; the body is its system prompt.

```markdown
---
name: reviewer
title: Reviewer
description: Reviews a plan or a claim before it is acted on.
role: primary
enabled: true
model: deepseek-v4-flash
provider: opencode-go
reasoning_effort: high
permission_mode: automatic
tools_enabled:
  - bash
  - read_turn
  - load_skill
  - set_tasks
  - update_tasks
  - update_goal
  - search_web
  - fetch_url
  - download
  - list_mcp_tools
  - call_mcp_server_tool
  - list_mcp_resources
  - read_mcp_resource
tools:
  bash:
    background_allowed: true
    permissions:
      rm *: ask
      sudo *: deny
---

You are a rigorous reviewer. Your job is to make sure a plan or a claim is sound before
anything is built on it: verify what it rests on, question what it assumes, and say
plainly what is vague or unproven.
```

The frontmatter fields are: `name`, `title`, `description`, `role`, `aliases`, `color`, `enabled`, `skills`, `model`, `provider`, `reasoning_effort`, `permission_mode`, `sandbox`, `tools`, and `tools_enabled`. Most are what they sound like. Two deserve a note:

- **`tools_enabled`** is the list of tool names the profile may use; the daemon composes exactly that set onto each session. An agent that declares none runs with none. Which tool names exist is covered under [Tools](#tools).
- **`permission_mode`** is the mode a session starts with when its creator does not choose one: `ask`, `automatic`, or `allow` (see [Permission modes](configuration.md#permission-modes)). A profile pinned to a provider you have no credentials for fails on its first call.

Bundled agents:

| Agent      | Role                                                                                                                                                     |
| ---------- | -------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `reviewer` | Reviews a plan or a claim before it is acted on: reads what exists, questions assumptions, and only proceeds once the plan is clear and logically sound. |

### Skills

A skill is a `SKILL.md`: a focused capability the agent loads **only when relevant**, keeping the system prompt lean. Frontmatter carries a `title` and a `description` the model uses to decide when to load it, through the `load_skill` tool.

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

Bundled skills:

| Skill                      | What it does                                                                                                                                                 |
| -------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `observational-memory`     | Retrieve and maintain the workspace's observational memory, using a disposable Semble index for semantic retrieval and SQLite for exact lookup and mutation. |
| `consolidate-observations` | Review and consolidate the workspace's observational memory. Runs only on explicit user invocation.                                                          |
| `context7-mcp`             | Search libraries' documentation online through the Context7 MCP server.                                                                                      |

### Memory

`.agents/memories/*.md` are passages the user records. Only their metadata is injected into the prompt; the agent reads a memory's body **on demand**, so context stays small while knowledge accumulates across sessions.

Observational memory is separate. `.agents/observations.sqlite` is current workspace knowledge maintained deliberately by an agent through Bash. It holds only current rows; Git provides history. Append-only session context carries a compact descriptor (resolved path, revision, per-ledger counts, timestamp extent, and a `status` of `ok`, `missing`, or `broken`), while stable instructions explain how to retrieve a relevant slice when prior work may matter. A missing or broken registry is reported through that descriptor, never read as empty — the agent is told the state and can repair it. Exact retrieval uses SQLite; semantic retrieval exports minified JSONL into a fresh disposable Semble index. The `observational-memory` skill defines the retrieval and atomic write protocols; `consolidate-observations` runs only when the user explicitly invokes it.

### MCP servers

`.agents/mcp.json` registers [Model Context Protocol](https://modelcontextprotocol.io) servers under `mcpServers`. Both `stdio` and `streamable_http` transports are supported. The bundled set ships `context7` and `semble` enabled, and a disabled `echo` demo that exercises a plain stdio server:

```json
{
  "mcpServers": {
    "context7": {
      "enabled": true,
      "transport": "streamable_http",
      "url": "https://mcp.context7.com/mcp",
      "stateful": true
    },
    "semble": {
      "enabled": true,
      "transport": "stdio",
      "command": "uvx",
      "args": ["--from", "semble[mcp]", "semble"],
      "stateful": true
    }
  }
}
```

Their tools and resources reach the agent through `call_mcp_server_tool`, `list_mcp_tools`, `list_mcp_resources`, and `read_mcp_resource`. The repository includes a complete [stdio server example](https://github.com/ghovax/langmesh/tree/main/examples/mcp).

## Tools

A session acts through tools, and every tool call runs inside the session's [confinement](configuration.md#confinement). A call that stays inside it runs without asking anybody. A call that asks to reach past it pauses under `ask` and reaches you as a prompt in the app; under `automatic` the reviewer allows or refuses it; under `allow` it runs without asking (the confinement still applies).

Every tool carries two shared argument fields: a required `explanation` (why the call is happening, in words the person watching reads) and a required `access_request` (what it says about changing anything and what it needs beyond confinement).

There is no delegation tool and no in-process sub-agent. A session that needs a peer creates one with `create_session`, which reaches the same control plane your terminal does. See [Peer sessions](#peer-sessions).

### The tool surface

The library core ships the orchestration and bridge tools; the plugins contribute the rest. Nothing is injected: a session's tools are the set its agent profile declares plus what its host composes.

**Shell and files**

| Tool   | What it does                                                                                                                                                       |
| ------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `bash` | Run a shell command inside the confinement. Arguments: `command`, `location` (a workspace location; contributed by the locations plugin), `background`, `timeout`. |

There are no dedicated `find_files` or `search_content` tools; for literal file-name and content search, use `bash` with ripgrep (`rg`) and `fd`.

A session can also install what it does not have. Where the machine has Nix, each session gets a package profile of its own on `PATH` and installs into it with an ordinary `nix profile add nixpkgs#<name>`. What it installs belongs to that session and is deleted with it, and the confinement is untouched. See [The session toolbox](configuration.md#the-session-toolbox).

**Web**

| Tool         | What it does                                                                                                                                                                               |
| ------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `search_web` | Search the web with Exa. Arguments: `query`, `result_count` (1–10, default 5).                                                                                                             |
| `fetch_url`  | Fetch a page's content as `markdown`, `text`, or `html`, through a tiered engine: Jina, then Firecrawl, then direct. Arguments: `url`, `format`, `timeout`, `hard_deadline`, `background`. |
| `download`   | Download raw bytes into the caller-owned artifact store. Arguments: `url`, `timeout`, `hard_deadline`, `background`.                                                                       |

A search or download that runs longer than its inline window moves to the background and its result arrives on its own; the agent never polls it.

**Orchestration, knowledge, and interaction**

| Tool                         | What it does                                                                                    |
| ---------------------------- | ----------------------------------------------------------------------------------------------- |
| `set_tasks` / `update_tasks` | Maintain a task list for a multi-step job.                                                      |
| `update_goal`                | Set the outcome the session is working toward, what it is for, and what would prove it reached. |
| `read_turn`                  | Read a sibling turn handed to this session from outside, by id.                                 |
| `load_skill`                 | Load a `SKILL.md` capability on demand.                                                         |
| `ask_user`                   | Ask the user one or more questions and wait for the answers.                                    |

A goal is not a longer task list. The task list records the work; the goal is the outcome that work serves. When a turn ends with actionable tracked tasks unfinished, the harness opens a hidden reminder turn that asks the session to reassess the user's requests, add any omitted work, and continue instead of merely describing it. Explicitly blocked tasks wait for the person, and a bounded continuation allowance prevents a stale list from running forever.

An open goal keeps going through its own status machinery. The agent owns the goal's `status` through `update_goal`: staying `active` while working, marking `satisfied` or `blocked` when it believes the work or a blockage is real, or `parked`/`cleared` to set it aside. A `satisfied` or `blocked` mark is settled by the independent secondary review (see [Goals](../internal/architecture.md#goals)), which confirms it only on evidence and overrides a mark the work does not support; an open goal a turn ends on without a mark is re-opened with a light continuation reminder. See [Configuration](configuration.md#conversation-compaction).

### Peer sessions

| Tool              | What it does                                                                                                                                                                                   |
| ----------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `create_session`  | Create an idle peer session. Its `agent` argument enumerates the profiles actually installed. Returns as soon as the peer exists.                                                              |
| `message_session` | Message another session, one you created or the one that created you. A session already mid-turn has the message steered into that turn at its next safe point; an idle one starts a new turn. |
| `read_session`    | One session's state: its profile, whether its process is alive, whether a turn is in flight, whether it is waiting on a human.                                                                 |
| `list_sessions`   | The sessions this one created. Its own subtree, not the machine's.                                                                                                                             |

The caller is the parent, always; it is not an argument. That puts a peer inside the tree, inside the reaper, and under the permission clamp, so a peer can never hold authority its parent does not have.

The peer starts with a copy of its parent's model-facing conversation, while keeping its own agent profile, permissions, process, and context window. The unfinished `create_session` tool call is excluded, so its history begins at a valid message boundary. When the peer finishes, it calls `message_session` on the session that created it, whose id is in its context as its parent; there is no handle to wait on, and nothing reconstructs a result. A peer that dies before it reports cannot say so; the daemon tells the parent when it reaps a child.

**Agents on other hosts**

`list_remote_agents` and `message_remote_agent` are separate verbs, because a remote agent is a separate bargain: it runs on someone else's machine, at someone else's cost, with no shared history and no access to this filesystem. Present only when one is registered.

**MCP servers**

`call_mcp_server_tool`, `list_mcp_tools`, `list_mcp_resources`, and `read_mcp_resource` bridge to any configured [MCP server](agent-system.md#mcp-servers).

**The hidden verdict tools**

Three tools exist only in internal lanes and never in a working session's roster: `submit_goal_review` for the goal reviewer, `submit_compaction_summary` for the compaction summarizer, and `permission_decision` for the automatic permission reviewer. Each is bound only in its hidden session, so outside that instruction there is nothing to call and no inert verdict to enforce.

### Where the definitions live

A built-in tool is one `Tool` unit: its schema, description, and handler joined at registration, with the shared `explanation` and `access_request` fields injected on every schema.

- **Core schemas and execution:** `src/langmesh/runtime/tools/registry.py` (the MCP bridge and `read_turn`/`load_skill`) and `src/langmesh/runtime/tools/sessions.py` (the peer-session and remote-agent tools), with event-rich handlers in `src/langmesh/runtime/tools/handlers.py`.
- **Plugin tools and their handlers:** `src/langmesh/runtime/plugins/<name>/` — `bash`, `web` (`search_web`, `fetch_url`, `download`), `interaction` (`ask_user`), `continuation` (`set_tasks`/`update_tasks`), `goal_review` (`update_goal`), `computer_use` (`control_screen`).
- **Descriptions the model reads:** one markdown file per tool — `src/langmesh/runtime/tools/descriptions/` for the core tools, and each plugin's `prompts/` directory for plugin tools.
- **Dispatch (permission, validation, policy) and the batch runner:** `src/langmesh/runtime/tools/dispatch.py`.
- **Model-facing message templates:** `src/langmesh/runtime/prompts/`.

The runtime never hard-codes a tool name: it dispatches whatever `Tool` units the session was composed with. A tool runs inside the session's own process, so its blast radius is that session: its working directory, its permission mode, and its own MCP server connections. Under the `worktree` strategy the working directory is the session's own git worktree.

### Screen control (`control_screen`)

LangMesh drives the live screen through one tool, `control_screen`, contributed by the computer-use plugin and covering native macOS apps and **your own Chrome**. Its Python script both finds elements and acts on them. It is **macOS-only** and **opt-in**, gated by `computer_control.enabled`, which is off by default. See [Configuration guide](configuration.md#screen-control).

**Finding.** Inside the script, `find_many(query)` and `find_one(query)` take a plain-language query. They return ranked elements to act on, not pixels. Each element carries a stable `id`, its role, its text, and its context. On native apps this reads the accessibility tree; on Chrome it reads the page's real semantic structure, iframes included, through the Chrome DevTools Protocol via Playwright. It also surfaces the page's own network and API requests, so the agent can find the endpoints the page calls. `find_one` returns the single best match and raises if the top matches are indistinguishable.

**Acting.** The same script drives the elements a find returned, addressing them by `id`. It uses trusted input: `click`, `type`, `scroll`, `evaluate`, and the like. The script is ordinary Python, so a whole task is a single call that can loop, branch, and call the page's own API. On the browser, `evaluate` can replay the page's own authenticated API in-page, reusing the logged-in session.

**Targets.** Every window and tab has a platform-minted id, and the current list is carried into the agent's context each turn, so there is no listing tool and no round trip. An application is not a target: two Finder windows are two places. Synthesized input is posted to the process, so the listing includes windows behind others, minimized, or on another Space.

**Tabs.** The script chooses which page it is on. The listing covers all of your tabs, not only the ones the agent opened, and nothing stops an agent from closing a tab it did not open; it has an instruction instead, because these tabs are your working state.

LangMesh attaches to **the Chrome you already use**, with your real logins and sessions. It never launches the browser, quits it, or copies it. It reads structure, not pixels: there is no screenshot path for computer use, so a drawn surface such as a canvas or WebGL exposes nothing to find.

**Enable it:**

- Grant **Accessibility** permission to LangMesh for native apps (System Settings, then Privacy and Security, then Accessibility). macOS matches the permission to the app's code identity, so a signed build keeps the grant across updates.
- Turn on Chrome's remote-debugging toggle once for the browser surface: open `chrome://inspect` and enable it under the remote-debugging option.
- Set `computer_control.enabled: true` in the configuration (off by default).

> [!NOTE] Typing fills a field without submitting unless the agent explicitly asks to, so it never posts a form by accident.

A script that names a state-changing primitive — `click`, `type`, `choose`, `upload`, `drag`, `evaluate`, `press`, `navigate`, `new_tab`, `close_tab`, `caret`, `select` — is put to whoever decides for the session, unless a `screen` rule already names that primitive. `evaluate` is on the acting side because it runs arbitrary JavaScript in a page you are signed in to; `navigate` is there too because on many sites a URL is a command. In an ordinary session the harness examines a script that acts; it does not block it. In a [read-only session](configuration.md#permission-modes) it refuses the script outright.
