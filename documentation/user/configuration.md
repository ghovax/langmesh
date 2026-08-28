# Configuration

Runtime configuration lives in **`$XDG_CONFIG_HOME/langmesh/configuration.yaml`**
(`~/.config/langmesh/configuration.yaml` unless you set `XDG_CONFIG_HOME`). The daemon
creates it on first run from a built-in template
(`langmeshd/commons/configuration.yaml`) and owns the file thereafter; the library never
writes it. Every daemon write is validated, synced, and atomically replaced at mode
`0600`, so a stopped process cannot leave a partial YAML document. It is the source of
truth for policy: permissions, feature toggles, addresses, and hosts. Credentials are
separate `0600` files. The repository never contains a filled-in copy.

Two ways to change policy, both writing the same YAML file. Settings also writes secret
files when you paste a key.

- **Settings** in the desktop app.
- **Editing the file directly**, which the daemon watches and the next session build
  reads.

> [!IMPORTANT] Credentials live as `0600` files under
> `$XDG_DATA_HOME/langmesh/secrets/`, one file per value (`providers.anthropic.api_key`,
> `email.imap.password`). Policy — address, allow-lists, agent names, ports — lives in
> this YAML document. Never commit a filled secret file on a public repository; see the
> [security policy](https://github.com/ghovax/langmesh/blob/main/SECURITY.md).
> Environment variables are not configuration. The hosted GitHub App keeps installation
> provider keys encrypted in its service database.

A change applies to whatever starts **next**. A running session keeps the configuration
it was built with, with a few exceptions the daemon pushes out: configuration, sandbox,
computer control, and the user-context snapshot each ask live sessions to rebuild.

Three places say something about a setting, and each says a different thing. **This
document** is the narrative, for the settings worth explaining at length. The **settings
panel** reads the running schema, so it can tell you what _this machine_ is set to. A
name the schema does not define is **refused**, not ignored. The daemon validates its
`daemon`, `dictation`, `composio`, `email`, and `provision` sections separately and
passes only library-owned sections to `langmesh.Configuration`.

## Where everything lives

LangMesh follows the XDG Base Directory convention rather than one dot-directory:

| Path                         | What is there                                                                                                                              |
| ---------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------ |
| `$XDG_CONFIG_HOME/langmesh/` | `configuration.yaml`                                                                                                                       |
| `$XDG_DATA_HOME/langmesh/`   | `history.sqlite`, `background.sqlite`, `mail.sqlite`, uploads, `oauths/`, `secrets/`, the file-URL signing secret, the reach pairing token |
| `$XDG_STATE_HOME/langmesh/`  | logs (`langmeshd.log`)                                                                                                                     |
| `$XDG_CACHE_HOME/langmesh/`  | caches                                                                                                                                     |
| `$XDG_RUNTIME_DIR/langmesh/` | the daemon's socket, port, pid, lock, and token                                                                                            |

The runtime directory is `0700`, and its daemon handshake files are `0600`. When
`XDG_RUNTIME_DIR` is unset, as on macOS, the fallback is a per-user directory under the
system temporary directory. The OS clears the runtime directory when you log out, so a
crashed daemon leaves nothing behind. Private values that must survive logout, including
the session-token master key, Reach pairing token, and secret files, live in the XDG
data directory and are created atomically with mode `0600`. A container or GitHub Action
that needs another disk sets `XDG_DATA_HOME`; there is no separate secrets-directory
variable.

## Model providers

Set an `api_key` for the providers you use, as a secret file named
`providers.<id>.api_key`. Most resolve through LiteLLM's built-in endpoints; any
OpenAI-compatible provider may also set `base_url` in this YAML file. Mailbox sessions
use the same map: add any catalogue id under `providers:` and point `email.provider` /
`email.model` at it (or keep the agent profile's pair).

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
  opencode: { api_key: "", base_url: "https://opencode.ai/zen/v1" }
  commandcode: { api_key: "", base_url: "https://api.commandcode.ai/provider/v1" }
  custom: { api_key: "", base_url: "" }
```

`custom` takes any OpenAI-compatible endpoint, which is why it needs a `base_url` as
well. Around fifty providers are registered (Azure, Alibaba, Vercel, Cerebras, Cohere,
DeepInfra, Hyperbolic, Hetzner, and more); the registry in
`langmesh.base.identity.providers` is the full list.

You can also **sign in with a ChatGPT or a Cursor subscription** instead of pasting a
key, in Settings, under Providers. Neither has a key to store: both live as OAuth tokens
in the data directory's `oauths/` folder, one file per provider, written `0600` inside a
`0700` directory. They stay out of `configuration.yaml` deliberately, because that file
is digest-synced and would thrash on every silent token refresh. Which models each plan
serves is discovered live from the account.

**Which model a session uses** is not set here; it belongs to the agent profile, in that
agent's `AGENT.md` frontmatter (`model` and `provider`). See
[Agent system](agent-system.md#agents). A profile pinned to a provider you have no
credentials for fails on its first call.

## Web search and retrieval

```yaml
exa: { api_key: "" }
jina: { api_key: "" }
firecrawl: { api_key: "", api_url: "" }
web_fetch:
  proxy_url: ""
  timeout_seconds: 30
  download_timeout_seconds: 120
  minimum_useful_characters: 64
```

| Setting                               | What it serves                                                   | Secret file                    |
| ------------------------------------- | ---------------------------------------------------------------- | ------------------------------ |
| `exa`                                 | `search_web`                                                     | `exa.api_key`                  |
| `jina`                                | `fetch_url`, on the free tier                                    | `jina.api_key`                 |
| `firecrawl`                           | `fetch_url`                                                      | `firecrawl.api_key`            |
| `web_fetch.proxy_url`                 | An outbound proxy                                                | — (YAML `web_fetch.proxy_url`) |
| `web_fetch.timeout_seconds`           | How long one engine is given                                     | —                              |
| `web_fetch.download_timeout_seconds`  | How long a download is given                                     | —                              |
| `web_fetch.minimum_useful_characters` | Below this a page is a wall or stub, so the next engine is tried | —                              |

`fetch_url` uses a tiered engine: Jina Reader first, then Firecrawl, then a direct
fetch. Each tier is optional; an unset key skips it. `proxy_url` overrides the standard
`HTTPS_PROXY` and `ALL_PROXY` for the fetch and download tools only.

## Hosted integrations

Composio's hosted gateway joins the ordinary set of MCP servers when enabled; it is not
a second path, and tool gating sees it as another server. `composio` is an app-owned
section in the same file. The key is the secret file `composio.api_key`.

## Execution and permissions

```yaml
sandbox: { enforce: "required" }
workspace: { strategy: "none" }
agent: { permission_mode: "ask" }
computer_control: { enabled: false }
user_context: { enabled: false, refresh_hours: 6 }
toolbox: { enabled: true }
```

`workspace.strategy` is one of `none`, `branch`, or `worktree`. It is resolved once,
when the session is created. A `worktree` session runs its tools in its own git
worktree, so parallel sessions on one repository do not tread on each other.

`agent.permission_mode` is the mode a session gets when none is asked for. It is a
default, not a ceiling: a session's creation can override it, and a child is clamped
against its parent either way. The modes are `ask`, `automatic`, and `allow`.

`computer_control` turns on the macOS screen tools (`control_screen`); it is opt-in.
`user_context` puts a snapshot of how you work into the prompt; it is opt-in too.

### The session toolbox

What a session may _reach_ is the confinement's question. What a session _has_ is this
one. Until they were separated they gave the same answer: a missing tool and a forbidden
path both came back as `Operation not permitted`, so an agent read a gap in its toolkit
as a boundary and went looking for a way around it.

```yaml
toolbox:
  enabled: true
```

With it on, each session gets a package profile of its own at the front of its `PATH`,
and `nix profile add nixpkgs#jq` installs into that profile with no flag and no path.
The packages come from the shared read-only store; what the session owns is a directory
of symlinks under `$XDG_STATE_HOME/langmesh/sessions/<id>`
(`~/.local/state/langmesh/sessions/<id>` by default; the toolbox root), deleted when the
session is reaped. Your own profile is never written to, and the confinement is
unchanged. The confinement grants the shared Nix store read and execute access so these
profile links work as commands; writes remain limited to the session profile.

It needs [Nix](https://nixos.org). On a machine without it there is no toolbox, and the
agent is told nothing about installing anything.

### Confinement

What a session's tool children may do, a `bash` command or a `control_screen` script.
The operating system enforces this; the harness does not infer it from the text of a
command.

```yaml
sandbox:
  enforce: required
  filesystem:
    readable: ["~/.config", "~/.ssh", "~/.gitconfig", "~/.cargo", "~/.npmrc", "~/Library/Keychains"]
    writable: ["$WORKSPACE", "$TMPDIR", "/tmp", "$XDG_CACHE_HOME"]
    deny: []
    grantable: []
  network: false
  limits:
    RLIMIT_CORE: 0
    RLIMIT_FSIZE: 8589934592
    RLIMIT_NPROC: 2048
  umask: "0077"
  nice: 0
```

`enforce` is one of `required`, `preferred`, or `off`. `limits` are POSIX rlimits under
their own names and units; `umask` is `umask(2)` and `nice` is `nice(2)`. Only the
filesystem and the network have no POSIX spelling, and they are the two that need a
platform behind them. `network` is **off by default**; turn it on if a tool child may
reach the network at all.

**The filesystem.** The system stays readable; the lists govern your home, which is
closed by default. `readable` is the allowlist that keeps toolchains working. `writable`
is narrower still. `deny` is an opt-in absolute ban that wins over both; nothing decided
at runtime reaches past it. The shipped defaults keep credential and configuration
directories readable, including `~/Library/Keychains` so Git's macOS credential helper
works inside the sandbox. `$WORKSPACE` is the session's own directory.

**Asking for more.** An agent that needs a path outside these lists asks for it, on the
call that needs it, with `access_request`. That request is the only thing that raises a
prompt. An approval holds for the rest of that session and never reaches a peer.
`grantable` lists the paths an agent may be given without a prompt; it is empty by
default, so every request is asked about.

**When a command hits the wall.** A command the operating system refuses is not simply
failed. Its first run was confined and could not have been otherwise, so whatever it
managed before the refusal happened inside the box, which makes a second run with more
reach safe to offer. Under `ask` you are shown the command and what the refusal looked
like; under `automatic` the reviewer answers. Your `deny` list still holds through it.

**The backend.** macOS uses
[`sandbox-exec`](https://keith.github.io/xcode-man-pages/sandbox-exec.1.html) with a
generated Seatbelt profile; Linux uses
[Landlock](https://docs.kernel.org/userspace-api/landlock.html) plus a network
namespace. Apple has deprecated `sandbox-exec` since 10.15, and LangMesh depends on it
anyway, because nothing else confines a single child process. If Apple removes it, the
boot-time probe fails and `enforce` decides what happens.

**`enforce`.** `required` (the default) refuses to create a session when no backend is
available, naming what is missing. `preferred` runs with the POSIX half only, limits,
mask, priority, a scoped environment, which is hygiene rather than a boundary. `off`
does not confine.

The harness resolves a session's confinement when it **creates** the session, and
nothing widens it afterwards. It clamps it against the session that created it: path
sets intersect, so a peer never gets a wider filesystem than its creator holds. An agent
profile may narrow it further with its own `sandbox:` block.

> [!NOTE] Commands run against a **remote location** are not confined: they execute on
> another machine, where a boundary drawn by this process has no meaning.

### Permission modes

A session's mode says **who answers** when a call asks to reach past its confinement. It
says nothing about what the session may do; that is the `sandbox` block above, enforced
by the operating system.

| Mode        | Behaviour                                                                                                                                                             |
| ----------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `ask`       | The person running the session answers. The turn parks until they do.                                                                                                 |
| `automatic` | A reviewer answers: it allows or refuses the request, and never asks. For work nobody is watching. A refusal reaches the agent as a refused tool call, with a reason. |
| `allow`     | No gate at all: every call runs as if it had whatever it asked for. The confinement still applies to what a call may touch; only the asking is skipped.               |

A session's mode is chosen when the harness creates it and can be changed afterwards by
the person running it; a session can never change its own. A session created by another
is never looser than its parent, and tightening a session tightens the subtree it
created, so an unattended session (`automatic` or `allow`) can only create sessions that
also run unattended.

**A read-only session** is not a mode. It is a confinement with nowhere writable:
created with a `sandbox:` block that lists no `writable` paths. Nothing about a
command's text decides it, so no spelling of a write gets past.

Three tools take per-call rules on each agent, the three whose calls can be named:
`bash` by its command (`sudo *: deny`, `rm -rf *: ask`), `mcp` by `server.tool`
(`*.delete_*: deny`), and `screen` by the primitive a script reaches for
(`evaluate: deny`). The longest matching pattern wins. A `deny` refuses the call
outright in all modes; a reviewer may not overrule a rule you wrote.

`bash` ships with a short list of prefixes already set to `ask` or `deny`, because the
confinement answers "where can this reach" and not "how much of the workspace survives
this": `rm -rf *`, `rm -fr *`, `rm -r *`, `git reset --hard*`, `git clean -*`,
`git push --force*`, `sudo *`, `chmod -R *`, `chown -R *`, `dd *` are set to `ask`, and
`mkfs*`, `shutdown*`, `reboot*` to `deny`. Your own entry at the same pattern replaces
the shipped one.

## Conversation compaction

```yaml
compaction:
  automatic: true
  reclaim_at_fraction: 0.85
  output_reserve_fraction: 0.1
  recent_working_set_fraction: 0.15
  maximum_context_tokens: 0
```

When a conversation reaches its recommended preparation threshold, LangMesh appends one
private pre-compaction notice inside the reserved context buffer. The segment exposes
only local Bash. The agent must atomically bring the active workspace's
`.agents/observations.sqlite` up to date and advance `registry_meta.revision`, including
a revision-only acknowledgement when nothing durable changed. LangMesh verifies that the
revision advanced, then asks the model for the summary through
`submit_compaction_summary`; once collected, the older turns are dropped and the session
continues with the system prompt, the summary, and the recent working set word for word.
A summarizer that stops without submitting is reminded until it does — emitting the tool
call correctly is the model's own responsibility, and only the person's stop ends the
wait.

- `output_reserve_fraction` is held back for the answer the model is about to write;
  everything else here is a share of what remains.
- `reclaim_at_fraction` is the recommended preparation boundary, not a hard cutoff.
- `recent_working_set_fraction` is how much stays verbatim, measured in tokens rather
  than turns.
- `maximum_context_tokens` optionally bounds the context used to schedule compaction.
  Zero uses the model's available context without another bound.

`goal_review` governs how a goal the agent marked is settled. The agent owns its goal's
`status` through the `update_goal` tool (`active`, `satisfied`, `blocked`, `parked`,
`cleared`). `parked` and `cleared` are administrative and apply directly. A goal left
`active` and unmarked is simply re-opened with a light continuation reminder until it is
reached or the person stops it.

```yaml
goal_review:
  settlement: reviewer
```

Who settles a `satisfied` or `blocked` mark is `goal_review.settlement`:

- `reviewer` (the default): the mark is not final by itself. After the working turn
  ends, an independent reviewer inspects the work and either confirms the mark or
  overrides it (an unsupported `satisfied` becomes `unmet`, sending the goal back to
  work). The reviewer is asked again until it submits a verdict — modelling correctly is
  the model's own responsibility, and nothing puts a price on honesty.
- `agent`: the working agent's mark is final and the session ends. There is no second
  reviewer session.

Observations are workspace-owned current state and explicit. Agents retrieve and
maintain them through Bash using the `observational-memory` skill. The daemon watches
each active location's registry through native filesystem notifications and shares one
watcher across its sessions. A committed revision broadcasts a complete validated
snapshot to the memory panel. The append-only session context receives only
progressive-disclosure metadata, never observation rows. A registry that is missing or
no longer matches its schema is itself reported as metadata (`status: missing|broken`
with a problem message), so an agent hears about the state and repairs it rather than
silently working without memory; the pre-columnar JSON-schema format is never read or
migrated.

## Attachments

```yaml
attachments:
  inline_image_megabytes: 20.0
```

`inline_image_megabytes` is the ceiling on an image inlined into a conversation, since a
huge image would blow up the persisted conversation it is inlined into. Above it, or for
a model without vision, the model gets the file path instead.

## Tool limits

How much output tools may return and how patient they are. Every limit is a plain value
under `limits`; nothing is scaled or inferred, so what you set is what runs:

```yaml
limits:
  output_tokens: 16384
  web_search_maximum: 16
```

The keys are the fields of `langmesh.base.primitives.limits.Limits`. An unknown name is
an error at load, and the settings panel lists each with its shipped value. See
[the reference below](#the-limits).

The settings panel lists every library setting with what it ships at and what this
machine runs on; app-owned settings use their dedicated panels, and daemon lifecycle
settings remain file-only. What each setting is _for_ is in the reference below. The
shipped template (`langmeshd/commons/configuration.yaml`) is the minimal first-run
document; the schema supplies every omitted default.

## Screen control

```yaml
computer_control:
  enabled: false
  retrieval:
    multilingual_rank_model: "minishlab/M2V_multilingual_output"
    english_rank_model: "minishlab/potion-base-32M"
    lexical_gate_short_words: 3
    lexical_gate_long_words: 7
```

`enabled` drives native macOS apps and your own Chrome, and it is opt-in. After an
action, the harness **polls** a surface until it stops changing; it does not sleep for a
fixed guess. The polling cadence and the retrieval limits live with the other numbers
under `limits` (`settle_poll_seconds`, `settle_give_up_seconds`, `settle_stable_reads`,
and the `find_*` family).

### How a screen is ranked

`find_one` and `find_many` score every element three ways and add the results: two
static embeddings read what the query means, and a character similarity reads how it is
spelled.

| Setting                    | What it does                                                                                                |
| -------------------------- | ----------------------------------------------------------------------------------------------------------- |
| `multilingual_rank_model`  | Ranks by meaning across languages. Also backs the relevance floor. Empty turns it off.                      |
| `english_rank_model`       | A second embedding, ranked beside the first. Better on queries that describe a purpose. Empty turns it off. |
| `lexical_gate_short_words` | At or below this, a query is a quoted label and its spelling counts in full.                                |
| `lexical_gate_long_words`  | At or above this, a query is a description and its spelling is ignored. Linear between the two.             |

The two models are added, not chosen between: used alone the English one is worse on
native windows, used together they beat either alone. Clearing both leaves BM25.

The gate keeps the character similarity from doing harm. A short query is a label read
off the screen, so its spelling is the strongest evidence available. A long query shares
no spelling with anything, and a character similarity is never silent, so past
`lexical_gate_long_words` it is dropped.

`find_one`'s willingness to answer at all is separate, under `limits.find_one_margin`,
and it is fitted against this ranking, so change one and re-fit the other.

## MCP servers

`mcp.servers` mirrors what `.agents/mcp.json` declares and is normally edited there. See
[Agent system](agent-system.md#mcp-servers). A folder's own servers join the shared pool
when a session in that folder starts; the pool only grows, so no other session loses its
servers.

## Remote peers

```yaml
remote_agents:
  agents: {}
```

Agents on other hosts, resolved by their A2A card. Normally registered in
`~/.agents/remote-agents.json` or from Settings rather than written here. A remote agent
is not a session: LangMesh does not own its lifecycle, cannot set its permission mode,
and keeps no transcript of it.

## Telemetry

Off by default. When enabled, spans and token usage are exported over OTLP to an
endpoint you choose; LangMesh ships nothing anywhere on its own.

```yaml
telemetry:
  enabled: false
  exporter: { endpoint: "", protocol: "http/protobuf", headers: {} }
  sample_ratio: 1.0
```

**There is no default agent setting**, here or anywhere. Every session is created with
an explicit agent, and no profile is the one to fall back to. Add your own under
`~/.agents/agents/<id>/` or `.agents/agents/<id>/` in a working directory. See
[Agent system](agent-system.md).

## Configuration reference

Every setting LangMesh has, with library settings in the order the settings panel
presents them and app-owned settings named separately.

A setting is addressed by its dotted path, and the same path works everywhere: in
`~/.config/langmesh/configuration.yaml`, and as the key the interface writes. Nothing is
written to that file until you change it; a setting you never touched follows the
default.

To read or change a setting, edit `~/.config/langmesh/configuration.yaml` (a setting you
never touched may be absent; omit it and the default applies) or use the interface's
settings panel, which walks the same schema. To unset a setting, remove its line from
the file rather than writing the default into it.

For library settings, the panel's names and explanations live in `shared/messages/`,
keyed by these exact paths.

### Agent defaults

What a session runs under when its creator does not say.

| Setting                 | Type                          | Default | What it is for                                                                                                              |
| ----------------------- | ----------------------------- | ------- | --------------------------------------------------------------------------------------------------------------------------- |
| `agent.permission_mode` | `ask` / `automatic` / `allow` | `ask`   | Who answers when a session asks to reach past its confinement when neither the person nor its agent profile chooses a mode. |

### Workspaces

Where a session's tools run.

| Setting              | Type                           | Default | What it is for                                                                                                   |
| -------------------- | ------------------------------ | ------- | ---------------------------------------------------------------------------------------------------------------- |
| `workspace.strategy` | `none` / `branch` / `worktree` | `none`  | Where a session's work happens: the project directory itself, a branch of its own, or a git worktree of its own. |

### Confinement

What a session's tool children may do, enforced by the operating system.

| Setting                        | Type                             | Default                                                                                                                                                                                              | What it is for                                                                                                                          |
| ------------------------------ | -------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------- |
| `sandbox.enforce`              | `required` / `preferred` / `off` | `required`                                                                                                                                                                                           | What to do on a machine that cannot enforce confinement: refuse to start the session, run with resource limits only, or do not confine. |
| `sandbox.filesystem.readable`  | list                             | `~/.agents` `~/.config` `~/.local` `~/.nix-profile` `~/.ssh` `~/.gitconfig` `~/.gitignore_global` `~/.cargo` `~/.rustup` `~/.npmrc` `~/.nvm` `~/.pyenv` `~/.docker` `~/.netrc` `~/Library/Keychains` | Paths under your home a tool child may read. The system is readable and is not listed.                                                  |
| `sandbox.filesystem.writable`  | list                             | `$WORKSPACE` `$TMPDIR` `/tmp` `$XDG_CACHE_HOME` `~/.cache`                                                                                                                                           | Paths a tool child may write. Deliberately narrower than readable.                                                                      |
| `sandbox.filesystem.grantable` | list                             | —                                                                                                                                                                                                    | Paths an agent may be granted at runtime without asking. Empty means every request is put to you.                                       |
| `sandbox.filesystem.deny`      | list                             | —                                                                                                                                                                                                    | Opt-in absolute bans. Wins over readable and writable; no request opens them.                                                           |
| `sandbox.network`              | boolean                          | `false`                                                                                                                                                                                              | Whether a tool child may reach the network at all.                                                                                      |
| `sandbox.limits`               | map                              | `{'RLIMIT_CORE': 0, 'RLIMIT_FSIZE': 8589934592, 'RLIMIT_NPROC': 2048}`                                                                                                                               | Per-child resource limits, by their setrlimit names.                                                                                    |
| `sandbox.umask`                | string                           | —                                                                                                                                                                                                    | The file-creation mask a tool child runs under. Empty leaves the machine's own.                                                         |
| `sandbox.nice`                 | integer                          | `0`                                                                                                                                                                                                  | How far to lower a tool child's scheduling priority, so a runaway command does not take the machine with it.                            |

### Session toolbox

Whether a session may install the tools it needs into a profile of its own.

| Setting           | Type    | Default | What it is for                                                                                           |
| ----------------- | ------- | ------- | -------------------------------------------------------------------------------------------------------- |
| `toolbox.enabled` | boolean | `true`  | Let each session install the tools it needs into a package profile of its own, deleted with the session. |

### Conversation compaction

How conversation history is compacted as it grows.

| Setting                                  | Type    | Default | What it is for                                                                                                                                                                                |
| ---------------------------------------- | ------- | ------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `compaction.automatic`                   | boolean | `true`  | Reclaim context on its own as it fills. Manual compaction works either way.                                                                                                                   |
| `compaction.reclaim_at_fraction`         | number  | `0.85`  | Recommended preparation boundary. A private local-Bash segment first updates the current observational registry and advances its revision; compaction follows only after validation succeeds. |
| `compaction.output_reserve_fraction`     | number  | `0.1`   | Share held back as safety space for the preparation segment and the answer.                                                                                                                   |
| `compaction.recent_working_set_fraction` | number  | `0.15`  | Share of the usable window kept verbatim after older history is discarded. Sized in tokens rather than turns.                                                                                 |
| `compaction.maximum_context_tokens`      | integer | `0`     | Optional context bound used to schedule compaction and size the recent working set. Zero uses the model's available context.                                                                  |

### Goal review

How an agent-marked goal is settled.

| Setting                  | Type   | Default    | What it is for                                                                                                                                                            |
| ------------------------ | ------ | ---------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `goal_review.settlement` | choice | `reviewer` | Who settles a `satisfied` or `blocked` mark: an independent reviewer (`reviewer`), or the working agent (`agent`), in which case that mark is final and the session ends. |

### Attachments

How the files a person attaches may cost the conversation.

| Setting                              | Type   | Default | What it is for                                                                                      |
| ------------------------------------ | ------ | ------- | --------------------------------------------------------------------------------------------------- |
| `attachments.inline_image_megabytes` | number | `20.0`  | The ceiling on an image inlined into the persisted conversation. Above it, the model gets the path. |

### User snapshot

Whether the model-facing session context describes how you work on this machine.

| Setting                      | Type    | Default | What it is for                                                                           |
| ---------------------------- | ------- | ------- | ---------------------------------------------------------------------------------------- |
| `user_context.enabled`       | boolean | `false` | Include a snapshot of how you work, your editor, habits, and machine in session context. |
| `user_context.refresh_hours` | number  | `6`     | How old that snapshot may get before it is rebuilt. The rebuild runs in the background.  |

### Screen control

Driving the screen.

| Setting                                               | Type    | Default                             | What it is for                                                                                                                 |
| ----------------------------------------------------- | ------- | ----------------------------------- | ------------------------------------------------------------------------------------------------------------------------------ |
| `computer_control.enabled`                            | boolean | `false`                             | Let the agent drive native applications and your browser. It also needs Accessibility granted in System Settings.              |
| `computer_control.retrieval.multilingual_rank_model`  | string  | `minishlab/M2V_multilingual_output` | The static embedding that ranks by meaning across languages. Empty turns it off.                                               |
| `computer_control.retrieval.english_rank_model`       | string  | `minishlab/potion-base-32M`         | A second static embedding, ranked alongside the first. Empty turns it off.                                                     |
| `computer_control.retrieval.lexical_gate_short_words` | integer | `3`                                 | Queries of this many words or fewer are treated as a label quoted off the screen, and the character similarity counts in full. |
| `computer_control.retrieval.lexical_gate_long_words`  | integer | `7`                                 | Queries of this many words or more are treated as a description of a purpose, and the character similarity is ignored.         |

### Dictation

Speaking to the composer instead of typing. An app-owned section in the same file.

| Setting                                                      | Type    | Default                              | What it is for                                                                   |
| ------------------------------------------------------------ | ------- | ------------------------------------ | -------------------------------------------------------------------------------- |
| `dictation.enabled`                                          | boolean | `false`                              | Let the composer take speech. The model runs on this machine.                    |
| `dictation.model`                                            | string  | `mlx-community/parakeet-tdt-0.6b-v3` | The speech model dictation downloads and runs locally.                           |
| `dictation.timing.minimum_transcription_timeout_seconds`     | number  | `30.0`                               | The floor on how long one transcription may take before it is treated as wedged. |
| `dictation.timing.transcription_timeout_realtime_multiplier` | number  | `0.5`                                | Added to the floor, per second of audio.                                         |
| `dictation.timing.maximum_attempts`                          | integer | `2`                                  | How many workers one recording may be given.                                     |
| `dictation.timing.worker_shutdown_seconds`                   | number  | `2.0`                                | How long a worker is given to exit on its own before it is killed.               |

### Daemon lifecycle

Process-level timings owned by the daemon and read only from the configuration file.

| Setting                             | Type   | Default   | What it is for                                                         |
| ----------------------------------- | ------ | --------- | ---------------------------------------------------------------------- |
| `daemon.startup_seconds`            | number | `45.0`    | How long a client waits for a daemon it started to become reachable.   |
| `daemon.probe_interval_seconds`     | number | `0.05`    | How often a client checks whether a daemon is listening or has exited. |
| `daemon.probe_connect_seconds`      | number | `0.5`     | How long one daemon connection probe may wait.                         |
| `daemon.session_idle_sleep_seconds` | number | `18000.0` | How long an idle hosted session retains its worker before sleeping.    |

### Email

IMAP IDLE plus SMTP in front of the daemon. An app-owned section in the same file. Off
until you enable it. The mail process (`langmesh mail`) is a **client** of `langmeshd`.
`langmesh mail check` proves IMAP and SMTP without IDLEing. `langmesh mail auth` writes
the OAuth refresh token. Mail sessions speak through `submit_email` (`progress` or
`reply`); markdown is rendered as HTML in the outbound message. See [Email](email.md).
Password auth uses `email.imap.password` / `email.smtp.password`. OAuth uses
`email.oauth.refresh_token` (and optional `email.oauth.client_secret`). The model key is
`providers.<id>.api_key` for whatever catalogue provider mailbox sessions call.

| Setting                      | Type    | Default                          | What it is for                                                                                                                                                                                                                             |
| ---------------------------- | ------- | -------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `email.enabled`              | boolean | `false`                          | Run the mail client against this mailbox.                                                                                                                                                                                                  |
| `email.address`              | string  | —                                | The mailbox. IMAP logs in as the account (Gmail strips a plus-tag). SMTP From is `local+machine@domain`.                                                                                                                                   |
| `email.machine`              | string  | —                                | This host's plus-tag (`vps`, `laptop`). Required when mail is enabled. A new thread to `local+machine@domain` starts a session here; a reply steers that conversation. If `email.address` already has a plus-tag, it must equal this slug. |
| `email.allow_from`           | list    | `[]`                             | Mailboxes (or `@domain`) whose mail is taken. Everyone else is ignored. A Gmail plus-address or `googlemail.com` alias matches `user@gmail.com` and `@gmail.com`.                                                                          |
| `email.agent`                | string  | `reviewer`                       | The agent profile each mail thread session runs. Defaults to the bundled `reviewer`. Tools and the prompt come from this profile.                                                                                                          |
| `email.provider`             | string  | —                                | Optional catalogue provider overlay for mailbox sessions. Must be set together with `email.model`. Omit both to keep the profile's provider.                                                                                               |
| `email.model`                | string  | —                                | Optional catalogue model overlay for mailbox sessions. Must be set together with `email.provider`.                                                                                                                                         |
| `email.auth`                 | string  | `password`                       | How IMAP and SMTP authenticate: `password` or `oauth` (XOAUTH2 via Authlib, aioimaplib, and aiosmtplib).                                                                                                                                   |
| `email.oauth.issuer`         | string  | —                                | `google`, `microsoft`, `yahoo`, or `custom`. Empty infers google/microsoft/yahoo from `email.address`. Proton is not an issuer.                                                                                                            |
| `email.oauth.client_id`      | string  | —                                | OAuth app id. Required when `email.auth` is `oauth`.                                                                                                                                                                                       |
| `email.oauth.tenant`         | string  | `common`                         | Microsoft tenant (`common`, `consumers`, or a directory id).                                                                                                                                                                               |
| `email.oauth.token_url`      | string  | —                                | Token endpoint. Filled from the issuer unless `custom`.                                                                                                                                                                                    |
| `email.oauth.authorize_url`  | string  | —                                | Authorize endpoint. Filled from the issuer unless `custom`.                                                                                                                                                                                |
| `email.oauth.scopes`         | list    | —                                | Override the issuer's scopes.                                                                                                                                                                                                              |
| `email.oauth.redirect_uri`   | string  | `http://127.0.0.1:8765/callback` | Loopback URI `langmesh mail auth` registers and listens on.                                                                                                                                                                                |
| `email.oauth.client_secret`  | string  | —                                | Unused in YAML. The live secret is the file `email.oauth.client_secret`. Optional for public clients.                                                                                                                                      |
| `email.oauth.refresh_token`  | string  | —                                | Unused in YAML. The live secret is the file `email.oauth.refresh_token`, written by `langmesh mail auth`.                                                                                                                                  |
| `email.working_directory`    | string  | —                                | Where that session's tools run. Empty means the daemon's current directory. `install.sh` and the Docker entrypoint set this to `/srv/langmesh`.                                                                                            |
| `email.permission_mode`      | string  | `automatic`                      | Who answers gates for a mail session: `ask`, `automatic`, or `allow`.                                                                                                                                                                      |
| `email.idle_timeout_seconds` | number  | `60.0`                           | How long one IMAP IDLE waits before cycling, so NAT and server idle limits cannot drop the socket silently.                                                                                                                                |
| `email.turn_timeout_seconds` | number  | `1800.0`                         | How long to wait for this mail's turn before giving up and retrying on the next reconnect. A timeout never invents a reply.                                                                                                                |
| `email.imap.host`            | string  | —                                | IMAP server. Inferred for Gmail, Fastmail, Outlook, Yahoo, iCloud, and Proton Bridge (`127.0.0.1`) from `email.address` when empty.                                                                                                        |
| `email.imap.port`            | integer | `993`                            | IMAP port. A Proton address defaults to Bridge's 1143.                                                                                                                                                                                     |
| `email.imap.username`        | string  | —                                | IMAP login. Empty means `email.address`. A Gmail plus-address authenticates as the account without the `+tag`.                                                                                                                             |
| `email.imap.password`        | string  | —                                | Unused in YAML. The live secret is the file `email.imap.password`.                                                                                                                                                                         |
| `email.imap.mailbox`         | string  | `INBOX`                          | Which folder to IDLE.                                                                                                                                                                                                                      |
| `email.imap.ssl`             | boolean | `true`                           | Implicit TLS (typical for 993).                                                                                                                                                                                                            |
| `email.smtp.host`            | string  | —                                | SMTP server. Inferred from `email.address` the same way as IMAP when empty.                                                                                                                                                                |
| `email.smtp.port`            | integer | `587`                            | SMTP port. When this is 587 and implicit TLS is off, a failed connect is retried on 465. A Proton address defaults to Bridge's 1025.                                                                                                       |
| `email.smtp.username`        | string  | —                                | SMTP login. Empty means the IMAP username.                                                                                                                                                                                                 |
| `email.smtp.password`        | string  | —                                | Unused in YAML. The live secret is the file `email.smtp.password`. The IMAP password is used when SMTP is the inferred provider host. A custom relay is not authenticated with the IMAP secret.                                            |
| `email.smtp.start_tls`       | boolean | `true`                           | Upgrade with STARTTLS (typical for 587).                                                                                                                                                                                                   |
| `email.smtp.use_tls`         | boolean | `false`                          | Implicit TLS (typical for 465). Mutually exclusive with STARTTLS. Port 465 implies this.                                                                                                                                                   |

### Provision

`packaging/mail/provision.sh` reads this section from
`packaging/mail/configuration.yaml`. The running daemon and mail client do not. Cloud
CLIs still take their own tokens (`FLY_API_TOKEN`, `HCLOUD_TOKEN`,
`DIGITALOCEAN_ACCESS_TOKEN`).

| Setting                          | Type   | Default         | What it is for                                                                               |
| -------------------------------- | ------ | --------------- | -------------------------------------------------------------------------------------------- |
| `provision.host`                 | string | —               | SSH target for a machine you already have (`root@203.0.113.10`). When set, no VM is created. |
| `provision.name`                 | string | `langmesh-mail` | Hetzner server or DigitalOcean droplet name.                                                 |
| `provision.fly.app`              | string | `langmesh-mail` | Fly.io app name.                                                                             |
| `provision.fly.region`           | string | `iad`           | Fly.io region.                                                                               |
| `provision.hetzner.image`        | string | `ubuntu-24.04`  | Hetzner image.                                                                               |
| `provision.hetzner.type`         | string | `cpx11`         | Hetzner type.                                                                                |
| `provision.hetzner.location`     | string | `fsn1`          | Hetzner location.                                                                            |
| `provision.hetzner.ssh_key`      | string | —               | hcloud SSH key name. Required to create a Hetzner server.                                    |
| `provision.digitalocean.region`  | string | `nyc1`          | DigitalOcean region.                                                                         |
| `provision.digitalocean.ssh_key` | string | —               | DigitalOcean SSH key fingerprint or id. Required to create a droplet.                        |

### Model providers

| Setting     | Type | Default | What it is for                                                                                   |
| ----------- | ---- | ------- | ------------------------------------------------------------------------------------------------ |
| `providers` | map  | —       | Policy for each model provider (`base_url`). API keys are secret files `providers.<id>.api_key`. |

### Web search and fetching

| Setting                               | Type    | Default | What it is for                                                                               |
| ------------------------------------- | ------- | ------- | -------------------------------------------------------------------------------------------- |
| `exa.api_key`                         | string  | —       | Unused in YAML. The live secret is the file `exa.api_key`.                                   |
| `jina.api_key`                        | string  | —       | Unused in YAML. The live secret is the file `jina.api_key`.                                  |
| `firecrawl.api_key`                   | string  | —       | Unused in YAML. The live secret is the file `firecrawl.api_key`.                             |
| `firecrawl.api_url`                   | string  | —       | A self-hosted Firecrawl instance to use instead of the hosted API.                           |
| `web_fetch.proxy_url`                 | string  | —       | Route direct fetches and file downloads through an HTTP or SOCKS proxy.                      |
| `web_fetch.timeout_seconds`           | number  | `30`    | How long one engine is given before the cascade moves on.                                    |
| `web_fetch.download_timeout_seconds`  | number  | `120`   | How long a download is given.                                                                |
| `web_fetch.minimum_useful_characters` | integer | `64`    | Below this, a page is a wall or a stub rather than the content, so the next engine is tried. |

### Composio

| Setting                    | Type    | Default                            | What it is for                                                   |
| -------------------------- | ------- | ---------------------------------- | ---------------------------------------------------------------- |
| `composio.enabled`         | boolean | `false`                            | Expose Composio's hosted gateway as one MCP server.              |
| `composio.url`             | string  | `https://connect.composio.dev/mcp` | The hosted MCP URL from the Composio dashboard's "connect" page. |
| `composio.api_key`         | string  | —                                  | Unused in YAML. The live secret is the file `composio.api_key`.  |
| `composio.server_name`     | string  | `composio`                         | The MCP server name its tools appear under.                      |
| `composio.timeout_seconds` | number  | `60`                               | How long one call to that gateway waits.                         |

### MCP servers

| Setting       | Type | Default | What it is for                                                                                           |
| ------------- | ---- | ------- | -------------------------------------------------------------------------------------------------------- |
| `mcp.servers` | map  | —       | MCP servers, by name. Edited on the MCP panel, which is where a server's command and credentials belong. |

### Remote peers

| Setting                | Type | Default | What it is for                                    |
| ---------------------- | ---- | ------- | ------------------------------------------------- |
| `remote_agents.agents` | map  | —       | Remote peers, by name. Edited on the peers panel. |

### Telemetry

| Setting                       | Type                     | Default         | What it is for                                                      |
| ----------------------------- | ------------------------ | --------------- | ------------------------------------------------------------------- |
| `telemetry.enabled`           | boolean                  | `false`         | Export traces at all.                                               |
| `telemetry.exporter.endpoint` | string                   | —               | Where traces are sent. Empty sends none.                            |
| `telemetry.exporter.protocol` | `http/protobuf` / `grpc` | `http/protobuf` | The OTLP protocol that collector speaks.                            |
| `telemetry.exporter.headers`  | map                      | —               | Headers sent with every export, for a collector that authenticates. |
| `telemetry.sample_ratio`      | number                   | `1.0`           | Share of traces exported. `1.0` exports every one.                  |

### The limits

How large, how many, and how patient the tools are: the fields of
`langmesh.base.primitives.limits.Limits`, each addressable under `limits`. Every
duration is in seconds. The shipped values:

| Name under `limits`              | Shipped value | What it is for                                                                                                |
| -------------------------------- | ------------- | ------------------------------------------------------------------------------------------------------------- |
| `output_tokens`                  | `16384`       | Tokens of inline output one tool may return before the rest overflows to a file.                              |
| `fetch_tokens`                   | `32768`       | Tokens of a fetched web page's text kept inline.                                                              |
| `upstream_error_detail_tokens`   | `256`         | Tokens of an upstream error body kept in the failure this harness raises.                                     |
| `web_search_maximum`             | `16`          | Ceiling on the result count a web search may ask for.                                                         |
| `web_exchanges`                  | `256`         | Recent request/response pairs a browser session keeps.                                                        |
| `web_websockets`                 | `32`          | Live websockets a browser session tracks at once.                                                             |
| `web_websocket_frames`           | `256`         | Frames retained per tracked websocket.                                                                        |
| `action_timeout`                 | `5.0`         | How long one browser action waits for its element.                                                            |
| `navigation_timeout`             | `20.0`        | How long a page load or navigation waits.                                                                     |
| `snapshot_timeout`               | `10.0`        | How long an accessibility snapshot of a page waits.                                                           |
| `browser_authorization`          | `90.0`        | How long attaching waits for the user to approve Chrome's prompt.                                             |
| `drag_timeout`                   | `8.0`         | How long a drag between two elements waits.                                                                   |
| `read_text_timeout`              | `10.0`        | How long reading a page's text waits.                                                                         |
| `frame_resolve_timeout`          | `2.0`         | How long resolving a frame reference waits.                                                                   |
| `sigterm_grace`                  | `3.0`         | How long a cancelled command or a reaped session has after SIGTERM before SIGKILL.                            |
| `bash_sync_window`               | `60.0`        | How long a shell command runs inline before it moves to the background.                                       |
| `slow_tool_sync_window`          | `10.0`        | The same inline window for fetching a URL or downloading a file.                                              |
| `web_search_sync_window`         | `10.0`        | The same inline window for a web search.                                                                      |
| `accessibility_messaging`        | `2.0`         | How long one accessibility message to an application waits.                                                   |
| `control_script`                 | `120.0`       | How long one screen-control script may run.                                                                   |
| `surface_guard_margin`           | `30.0`        | How far above the script's own limit the machinery waiting on it sits.                                        |
| `open_url`                       | `5.0`         | How long handing a URL to the system browser waits.                                                           |
| `model_silence_give_up`          | `180.0`       | How long a model stream may make no meaningful progress before the turn fails.                                |
| `settle_poll_seconds`            | `0.05`        | How often a surface is re-checked until it stops changing.                                                    |
| `settle_give_up_seconds`         | `1.5`         | The longest to wait before reading it anyway.                                                                 |
| `settle_stable_reads`            | `2`           | Identical consecutive reads that count a surface as having stopped changing.                                  |
| `type_chunk_size`                | `32`          | Characters sent per synthesized keyboard event.                                                               |
| `type_chunk_interval`            | `0.005`       | Pause between typed chunks.                                                                                   |
| `drag_steps`                     | `16`          | Segments a drag is split into, so it looks like a hand moved it.                                              |
| `drag_step_interval`             | `0.01`        | Pause between the interpolated steps of a drag.                                                               |
| `click_interval`                 | `0.01`        | Pause between successive synthesized clicks.                                                                  |
| `focus_settle`                   | `0.03`        | Pause after focusing a field, before typing into it.                                                          |
| `scroll_amount_pixels`           | `512`         | Pixels one scroll step moves a native window.                                                                 |
| `accessibility_walk_budget`      | `3.0`         | How long one read of an app's accessibility tree may take.                                                    |
| `accessibility_ready_probe`      | `0.4`         | How long the readiness poll may spend deciding whether an app's tree has built yet.                           |
| `accessibility_prewarm_interval` | `0.4`         | Pause between pre-warming the frontmost application's accessibility tree.                                     |
| `accessibility_ready_backoff`    | `0.2`         | Ceiling on the widening pause between accessibility readiness probes.                                         |
| `find_rephrasing_similarity`     | `0.45`        | How alike two screen queries must be before a second one on the same element counts as the first asked again. |
| `find_near_weight`               | `0.5`         | How much sitting beside the anchor is worth against matching the query.                                       |
| `find_anchor_margin`             | `0.02`        | How far ahead of its own runner-up a near anchor must score before a find will join on it.                    |
| `find_candidates`                | `8`           | Elements find_one weighs against its best match.                                                              |
| `find_one_margin`                | `0.2`         | How far ahead of the runner-up find_one's best match must score before it answers with one element.           |
| `find_many_ceiling`              | `64`          | Elements find_many will return however many are asked for.                                                    |
| `find_relevance_floor`           | `0.25`        | How well an element must match before find_many returns it at all.                                            |
| `session_title_attempts`         | `4`           | How many times a session asks the model to name itself before giving up.                                      |
| `permission_reviewer_attempts`   | `4`           | How many times the permission reviewer is asked before its silence counts as a refusal.                       |
| `model_catalogue_ttl`            | `60.0`        | How long the list of available models is cached.                                                              |
| `credential_refresh_leeway`      | `300.0`       | How far ahead of its expiry an access token is refreshed.                                                     |
| `oauth_poll_interval`            | `1.0`         | First pause between asks of whether a browser sign-in has completed; it widens from here.                     |
| `oauth_poll_ceiling`             | `10.0`        | Ceiling on the widening pause between sign-in polls.                                                          |
| `oauth_poll_give_up`             | `300.0`       | How long a browser sign-in is waited for before it is abandoned.                                              |
| `subscription_resume_ttl`        | `1800.0`      | How long a subscription provider's server-side conversation state stays worth resuming from.                  |
| `file_url_ttl`                   | `600.0`       | How long a signed file URL stays valid.                                                                       |
| `mcp_connect`                    | `20.0`        | How long connecting to one MCP server waits.                                                                  |
| `card_resolve`                   | `20.0`        | How long fetching a remote agent's card waits.                                                                |

### Notes on individual tunables

A few carry more reasoning than a table row holds.

- **`accessibility_walk_budget`** replaces a depth limit, which guarded the wrong
  quantity: a window six levels deep can take twice as long as one thirty-five levels
  deep, because the cost is how quickly the app answers, not how far down. Anything
  unread when it expires is reported as a region.
- **`find_anchor_margin`** below its default makes the anchor a guess, and organising a
  ranking around a guess is worse than not anchoring.
- **`find_many_ceiling`** exists because an `all=True` escape returned 590 elements and
  1.5MB on one ordinary page, ending the turn by exceeding the context window. A find is
  a ranked search; the tail of a ranking is not more answer.
- **`find_near_weight`** is fitted over anchored cases: relevance alone answers about a
  fifth and proximity alone about a fifth, while the two together answer most.
- **`find_one_margin`** is a budget rather than a discovery, and it is fitted against
  the fused ranking, not the top score. Re-fit it whenever the ranking changes.
- **`find_relevance_floor`** cuts the noise band and nothing more. Treat an empty result
  as "nothing scored above the noise", never as proof of absence, and do not raise this
  hoping to buy absence detection; it would cost real matches first.
- **`frame_resolve_timeout`** is deliberately well below the action timeout, so a frame
  that has gone waits out its budget rather than erroring.
- **`session_idle_sleep_seconds`** is not a limit default; it is a daemon setting (five
  hours by default): long enough that a working day of on-and-off use never pays a wake,
  short enough that a machine left overnight is not holding interpreters for
  conversations nobody returned to.
