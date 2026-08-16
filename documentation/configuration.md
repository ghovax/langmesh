# Configuration

Runtime configuration lives in **`$XDG_CONFIG_HOME/langmesh/configuration.yaml`** (`~/.config/langmesh/configuration.yaml` unless you set `XDG_CONFIG_HOME`). It is created on first run from a built-in template and is the source of truth for credentials, permissions, and feature toggles. The repository never contains a filled-in copy.

Three ways to change it, all writing the same file:

- `langmesh configure` from the terminal. `--all` lists every setting with its default; no argument lists only what you changed; a name reads or sets one setting; `--unset` removes it. A name the schema does not define, or a value it would reject, is refused with the reason rather than written.
- **Settings** in the desktop app.
- Editing the file directly, which the next thing to start reads.

> [!IMPORTANT]
> Every credential can also be set through an environment variable, which takes precedence over the file. That lets you run a daemon without writing secrets to disk. Never commit a filled-in configuration or `.env`; see the [security policy](https://github.com/ghovax/langmesh/blob/main/SECURITY.md).

A change applies to whatever starts **next**. A running session keeps the configuration it was built with, with a few exceptions the daemon pushes out: the sandbox, computer control, and the user-context snapshot each ask live sessions to rebuild.

Three places say something about a setting, and each says a different thing. **This document** is the narrative, for the settings worth explaining at length. The **[configuration reference](configuration-reference.md)** is the list, one row per setting. **`langmesh configure`** reads the running code, so it is the only one that can tell you what _this machine_ is set to.

Names the schema does not define are **refused**, not ignored.

## Where everything lives

LangMesh follows the XDG Base Directory convention rather than one dot-directory:

| Path                         | What is there                                                               |
| ---------------------------- | --------------------------------------------------------------------------- |
| `$XDG_CONFIG_HOME/langmesh/` | `configuration.yaml`                                                        |
| `$XDG_DATA_HOME/langmesh/`   | `history.sqlite`, `background.sqlite`, uploads, the file-URL signing secret |
| `$XDG_STATE_HOME/langmesh/`  | logs                                                                        |
| `$XDG_CACHE_HOME/langmesh/`  | caches                                                                      |
| `$XDG_RUNTIME_DIR/langmesh/` | the daemon's socket, port, and token, and one socket per session            |

The runtime directory is `0700`, and the token files inside it are `0600`. When `XDG_RUNTIME_DIR` is unset, as on macOS, the fallback is a per-user directory under the system temporary directory.

## Model providers

Set an `api_key` for the providers you use. Most resolve through LiteLLM's built-in endpoints; any OpenAI-compatible provider may also set `base_url`.

```yaml
providers:
  anthropic: { api_key: "" }
  openai: { api_key: "" }
  google: { api_key: "" }
  openrouter: { api_key: "" }
  xai: { api_key: "" }
  deepseek: { api_key: "" }
  groq: { api_key: "" }
  mistral: { api_key: "" }
  opencode: { api_key: "", base_url: "https://opencode.ai/zen/go/v1" }
  custom: { api_key: "", base_url: "" }
```

Each provider also reads an environment variable, which takes precedence over the file:

| Provider     | Environment variable                                |
| ------------ | --------------------------------------------------- |
| `anthropic`  | `ANTHROPIC_API_KEY`                                 |
| `openai`     | `OPENAI_API_KEY`                                    |
| `google`     | `GOOGLE_GENERATIVE_AI_API_KEY`, or `GEMINI_API_KEY` |
| `openrouter` | `OPENROUTER_API_KEY`                                |
| `xai`        | `XAI_API_KEY`                                       |
| `deepseek`   | `DEEPSEEK_API_KEY`                                  |
| `groq`       | `GROQ_API_KEY`                                      |
| `mistral`    | `MISTRAL_API_KEY`                                   |

`custom` takes any OpenAI-compatible endpoint, which is why it needs a `base_url` as well. Around forty providers are registered; the registry in `src/langmesh/base/providers.py` is the full list.

You can also **sign in with a ChatGPT or a Cursor subscription** instead of pasting a key, in Settings, under Providers. Neither has a key to store: both live as OAuth tokens in the data directory's `oauths/` folder, one file per provider, written `0600` inside a `0700` directory. They stay out of `configuration.yaml` deliberately, because that file is digest-synced and would thrash on every silent token refresh. Which models each plan serves is discovered live from the account.

**Which model a session uses** is not set here; it belongs to the agent profile, in that agent's `AGENT.md` frontmatter (`model` and `provider`). See [Agent system](agent-system.md#agents). A profile pinned to a provider you have no credentials for fails on its first call.

## Web search and retrieval

```yaml
exa: { api_key: "" }
jina: { api_key: "" }
firecrawl: { api_key: "", api_url: "" }
web_fetch: { proxy_url: "" }
```

| Setting     | What it serves                | Environment variable                     |
| ----------- | ----------------------------- | ---------------------------------------- |
| `exa`       | `search_web`                  | `EXA_API_KEY`                            |
| `jina`      | `fetch_url`, on the free tier | `JINA_API_KEY`                           |
| `firecrawl` | `fetch_url`                   | `FIRECRAWL_API_KEY`, `FIRECRAWL_API_URL` |
| `web_fetch` | An outbound proxy             | `FETCH_PROXY`                            |

`fetch_url` uses a tiered engine: Jina Reader first, then Firecrawl, then a direct fetch. Each tier is optional; an unset key skips it. `proxy_url` overrides the standard `HTTPS_PROXY` and `ALL_PROXY` for the fetch and download tools only.

## Hosted integrations

Composio's hosted gateway joins the ordinary set of MCP servers when enabled; it is not a second path, and tool gating sees it as another server. `composio.api_key` also reads `COMPOSIO_API_KEY`.

## Execution and permissions

```yaml
sandbox: { enforce: "required" }
workspace: { strategy: "none" }
agent: { permission_mode: "ask" }
computer_control: { enabled: false }
user_context: { enabled: false, refresh_hours: 6 }
toolbox: { enabled: true }
```

`workspace.strategy` is one of `none`, `branch`, or `worktree`. It is resolved once, when the session is created. A `worktree` session runs its tools in its own git worktree, so parallel sessions on one repository do not tread on each other.

`agent.permission_mode` is the mode a session gets when none is asked for. It is a default, not a ceiling: `langmesh create --mode` overrides it, and a child is clamped against its parent either way.

`computer_control` turns on the macOS screen tools (`control_screen`); it is opt-in. `user_context` puts a snapshot of how you work into the prompt; it is opt-in too.

### The session toolbox

What a session may _reach_ is the confinement's question. What a session _has_ is this one. Until they were separated they gave the same answer: a missing tool and a forbidden path both came back as `Operation not permitted`, so an agent read a gap in its toolkit as a boundary and went looking for a way around it.

```yaml
toolbox:
  enabled: true
```

With it on, each session gets a package profile of its own at the front of its `PATH`, and `nix profile add nixpkgs#jq` installs into that profile with no flag and no path. The packages come from the shared read-only store; what the session owns is a directory of symlinks under `~/.local/state/langmesh/sessions/<id>`, deleted when the session is reaped. Your own profile is never written to, and the confinement is unchanged.

It needs [Nix](https://nixos.org). On a machine without it there is no toolbox, and the agent is told nothing about installing anything.

### Confinement

What a session's tool children may do, a `bash` command or a `control_screen` script. The operating system enforces this; the harness does not infer it from the text of a command.

```yaml
sandbox:
  enforce: required
  filesystem:
    readable: ["~/.config", "~/.ssh", "~/.gitconfig", "~/.cargo", "~/.npmrc", "~/Library/Keychains"]
    writable: ["$WORKSPACE", "$TMPDIR", "/tmp", "$XDG_CACHE_HOME"]
    deny: []
    grantable: []
  network: true
  limits:
    RLIMIT_CORE: 0
    RLIMIT_FSIZE: 8589934592
    RLIMIT_NPROC: 2048
  umask: "0077"
  nice: 0
```

`enforce` is one of `required`, `preferred`, or `off`. `limits` are POSIX rlimits under their own names and units; `umask` is `umask(2)` and `nice` is `nice(2)`. Only the filesystem and the network have no POSIX spelling, and they are the two that need a platform behind them.

**The filesystem.** The system stays readable; the lists govern your home, which is closed by default. `readable` is the allowlist that keeps toolchains working. `writable` is narrower still. `deny` is an opt-in absolute ban that wins over both; nothing decided at runtime reaches past it. The shipped defaults keep credential and configuration directories readable, including `~/Library/Keychains` so Git's macOS credential helper works inside the sandbox. `$WORKSPACE` is the session's own directory.

**Asking for more.** An agent that needs a path outside these lists asks for it, on the call that needs it, with `access_request`. That request is the only thing that raises a prompt. An approval holds for the rest of that session and never reaches a peer. `grantable` lists the paths an agent may be given without a prompt; it is empty by default, so every request is asked about.

**When a command hits the wall.** A command the operating system refuses is not simply failed. Its first run was confined and could not have been otherwise, so whatever it managed before the refusal happened inside the box, which makes a second run with more reach safe to offer. Under `ask` you are shown the command and what the refusal looked like; under `automatic` the reviewer answers. Your `deny` list still holds through it.

**The backend.** macOS uses [`sandbox-exec`](https://keith.github.io/xcode-man-pages/sandbox-exec.1.html) with a generated Seatbelt profile; Linux uses [Landlock](https://docs.kernel.org/userspace-api/landlock.html) plus a network namespace. Apple has deprecated `sandbox-exec` since 10.15, and LangMesh depends on it anyway, because nothing else confines a single child process. If Apple removes it, the boot-time probe fails and `enforce` decides what happens.

**`enforce`.** `required` (the default) refuses to create a session when no backend is available, naming what is missing. `preferred` runs with the POSIX half only, limits, mask, priority, a scoped environment, which is hygiene rather than a boundary. `off` does not confine.

The harness resolves a session's confinement when it **creates** the session, and nothing widens it afterwards. It clamps it against the session that created it: path sets intersect, so a peer never gets a wider filesystem than its creator holds. An agent profile may narrow it further with its own `sandbox:` block.

> [!NOTE]
> Commands run against a **remote location** are not confined: they execute on another machine, where a boundary drawn by this process has no meaning.

### Permission modes

A session's mode says **who answers** when a call asks to reach past its confinement. It says nothing about what the session may do; that is the `sandbox` block above, enforced by the operating system.

| Mode        | Behaviour                                                                                                                                                                  |
| ----------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `ask`       | The person running the session answers. The turn parks until they do.                                                                                                      |
| `automatic` | A reviewer answers: it allows or refuses the request, and never asks. For work nobody is watching. A refusal reaches the agent as a refused tool call, with a reason. |

There is **no bypass mode** and no standing "always allow": the only runtime decisions are allow-once and deny. A session's mode is chosen when the harness creates it and can be changed afterwards by the person running it; a session can never change its own. A session created by another is never looser than its parent, and tightening a session tightens the subtree it created.

**A read-only session** is not a mode. It is a confinement with nowhere writable: `langmesh create --read-only`, or a `sandbox:` block that lists no `writable` paths. Nothing about a command's text decides it, so no spelling of a write gets past.

Three tools take per-call rules on each agent, the three whose calls can be named: `bash` by its command (`sudo *: deny`, `rm -rf *: ask`), `mcp` by `server.tool` (`*.delete_*: deny`), and `screen` by the primitive a script reaches for (`evaluate: deny`). The longest matching pattern wins. A `deny` refuses the call outright in both modes; a reviewer may not overrule a rule you wrote.

`bash` ships with a short list of prefixes already set to `ask` or `deny`, because the confinement answers "where can this reach" and not "how much of the workspace survives this": `rm -rf .` is entirely inside the boundary, and so is `git reset --hard`. Your own entry at the same pattern replaces the shipped one.

## Conversation compaction

```yaml
compaction:
  automatic: true
  reclaim_at_fraction: 0.85
  output_reserve_fraction: 0.1
  recent_working_set_fraction: 0.15
  summary_attempts: 3

goal_review:
  maximum_attempts: 3
```

When a conversation reaches its recommended preparation threshold, LangMesh appends one private pre-compaction notice inside the reserved context buffer. The segment exposes only local Bash. The agent must atomically bring the active workspace's `.agents/observations.sqlite` up to date and advance `registry_meta.revision`, including a revision-only acknowledgement when nothing durable changed. LangMesh verifies that the revision advanced, then asks the model for the summary through `submit_compaction_summary`; once collected, the older turns are dropped and the session continues with the system prompt, the summary, and the recent working set word for word.

- `output_reserve_fraction` is held back for the answer the model is about to write; everything else here is a share of what remains.
- `reclaim_at_fraction` is the recommended preparation boundary, not a hard cutoff.
- `recent_working_set_fraction` is how much stays verbatim, measured in tokens rather than turns.
- `summary_attempts` is how many times the hidden summarizer may be asked again after reviewing but not submitting; once exhausted, the compaction stops and the conversation is left unchanged until it is retried.

`goal_review.maximum_attempts` is the same bound for the goal reviewer: after a reviewer that investigated but never submitted, it is asked again on a narrowed toolset up to this many times, then the goal carries unchanged.

Observations are workspace-owned current state and explicit. Agents retrieve and maintain them through Bash using the `observational-memory` skill. The daemon watches each active location's registry through native filesystem notifications and shares one watcher across its sessions. A committed revision broadcasts a complete validated snapshot to the memory panel. The system prompt receives only progressive-disclosure metadata, never observation rows.

## Tool tuning

How much of a model's context tool output may occupy, and how patient the tools are. Size and count caps are token budgets derived from the **live** model context window, so a small model gets tight caps and a large one gets room. Timeouts do not depend on the window and answer only to `timeout_multiplier`.

```yaml
tuning:
  context_share:
    text: 0.25
    results: 0.15
  timeout_multiplier: 1.0
  defaults:
    action_timeout: 10000
    grep_results: 1024
```

| Setting                 | What it does                                                   |
| ----------------------- | -------------------------------------------------------------- |
| `context_share.text`    | The share one result's text may fill: output, fetched pages.   |
| `context_share.results` | The share a set of results may fill: matches, lines, records.  |
| `timeout_multiplier`    | `2.0` doubles every wait, for a slow machine. `1.0` is neutral. |
| `defaults`              | Overrides one value by its own name; every duration is in seconds. |

Those three move whole families. `defaults` is the escape hatch for a single value. Its keys are the names in `langmesh.base.tuning.Tunable`, the same idea as `sandbox.limits` using `setrlimit` constant names. An unknown name is an error at load. An override replaces the value the code ships with, so `context_share` and `timeout_multiplier` still apply on top.

`langmesh configure --all` lists every setting with what it ships at and what this machine runs on; what each one is _for_ is in the [configuration reference](configuration-reference.md). [`configuration.example.yaml`](configuration.example.yaml) is the same surface as a file at its shipped values. Read it; do not copy it over your own configuration, because everything in it is already the default.

## Screen control

```yaml
computer_control:
  enabled: false
  settle:
    poll_seconds: 0.05
    give_up_seconds: 1.5
  retrieval:
    multilingual_rank_model: "minishlab/M2V_multilingual_output"
    english_rank_model: "minishlab/potion-base-32M"
    lexical_gate_short_words: 3
    lexical_gate_long_words: 7
```

`enabled` drives native macOS apps and your own Chrome, and it is opt-in. After an action, the harness **polls** a surface until it stops changing; it does not sleep for a fixed guess. `poll_seconds` is how often it re-checks, and `give_up_seconds` is the longest it waits before reading anyway.

### How a screen is ranked

`find_one` and `find_many` score every element three ways and add the results: two static embeddings read what the query means, and a character similarity reads how it is spelled.

| Setting                    | What it does |
| -------------------------- | ------------ |
| `multilingual_rank_model`  | Ranks by meaning across languages. Also backs the relevance floor. Empty turns it off. |
| `english_rank_model`       | A second embedding, ranked beside the first. Better on queries that describe a purpose. Empty turns it off. |
| `lexical_gate_short_words` | At or below this, a query is a quoted label and its spelling counts in full. |
| `lexical_gate_long_words`  | At or above this, a query is a description and its spelling is ignored. Linear between the two. |

The two models are added, not chosen between: used alone the English one is worse on native windows, used together they beat either alone. Clearing both leaves BM25.

The gate keeps the character similarity from doing harm. A short query is a label read off the screen, so its spelling is the strongest evidence available. A long query shares no spelling with anything, and a character similarity is never silent, so past `lexical_gate_long_words` it is dropped.

`find_one`'s willingness to answer at all is separate, under `tuning.defaults.find_one_margin`, and it is fitted against this ranking, so change one and re-fit the other.

## MCP servers

`mcp.servers` mirrors what `.agents/mcp.json` declares and is normally edited there. See [Agent system](agent-system.md#mcp-servers). A folder's own servers join the shared pool when a session in that folder starts; the pool only grows, so no other session loses its servers.

## Remote peers

```yaml
remote_agents:
  agents: {}
```

Agents on other hosts, resolved by their A2A card and reached with `langmesh remote`. Normally registered in `~/.agents/remote-agents.json` or from Settings rather than written here. A remote agent is not a session: LangMesh does not own its lifecycle, cannot set its permission mode, and keeps no transcript of it.

## Telemetry

Off by default. When enabled, spans and token usage are exported over OTLP to an endpoint you choose; LangMesh ships nothing anywhere on its own.

```yaml
telemetry:
  enabled: false
  exporter: { endpoint: "", protocol: "http/protobuf", headers: {} }
  sample_ratio: 1.0
```

**There is no default agent setting**, here or anywhere. `langmesh create --agent` is required, and no profile is the one to fall back to. Add your own under `~/.agents/agents/<id>/` or `.agents/agents/<id>/` in a working directory. See [Agent system](agent-system.md).
