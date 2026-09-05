# Architecture

## The words this uses

| Term                 | What it means                                                                                                                        |
| -------------------- | ------------------------------------------------------------------------------------------------------------------------------------ |
| **Session**          | One conversation with an agent. A durable record; the daemon holds a live executor for it only while it works.                       |
| **Turn**             | One exchange within a session: a message in, the model's work, and everything it said and did before stopping.                       |
| **Harness**          | The code between the model and your machine: the turn loop, tools, plugins, prompts, permissions. `langmesh.Session` is the harness. |
| **Feature** (plugin) | A replaceable sub-behavior composed onto the core — a tool, goal review, compaction, permissions. The core names none of them.       |
| **Control plane**    | The daemon's RPC/API. Every client reaches a session through it, so a caller is identified and scoped in one place.                  |
| **Location**         | Where a session's tools actually run: this machine, or an SSH host. Distinct from which daemon the interface is talking to.          |
| **Home daemon**      | The `langmeshd` that owns this page — this machine's. Tauri reports it; next-dev injects it; `langmesh serve` is same-origin.        |
| **Paired machine**   | Another `langmeshd` remembered from a pairing link. Switching stays on this page; address and token travel together.                 |
| **Peer**             | A session created by another session. Not a special kind of thing, an ordinary session.                                              |

[A2A](https://github.com/google/A2A) is Agent-to-Agent, Google's JSON-RPC protocol for one agent to call another. The daemon serves it for every session it hosts, so a peer and a person reach a session the same way.

## Two packages, one image

LangMesh is two Python packages that ship together as one image. The **library** (`src/langmesh`) is the harness: `langmesh.Session` drives an agent turn by turn in your process. It reads no machine configuration, starts nothing, and composes nothing you did not hand it — every tool and sub-behavior is a feature. The **product** (`src/langmeshd`) is everything that makes sessions durable and addressable: the machine loaders that turn your `.agents` trees into what the library takes, the daemon that hosts sessions, the CLI, the REST surface, dictation, and the worker machinery. The library never imports the product; the product hosts the library.

The packaged product is one executable entered two ways. `langmesh` is the command a person runs (`serve` for the interface, `mail` for IMAP/SMTP in front of the daemon); `langmeshd` is the daemon that hosts sessions. They are the same frozen image, not two binaries, so packaging stays a single specification and every session carries the signed bundle's code identity. A normal Python installation exposes the `langmesh` console script and runs the daemon with `python -m langmeshd langmeshd`. One macOS Accessibility grant covers the packaged fleet instead of prompting per session.

| Component      | Responsibility                                 |
| -------------- | ---------------------------------------------- |
| Client         | Sends work to `langmeshd` and receives events. |
| `langmeshd`    | Hosts sessions and relays their events.        |
| Session        | Runs the agent turn and publishes its result.  |
| Model provider | Supplies the model response to the session.    |

## The feature seam

A session runs a plain model turn by itself. Everything else is a feature: goal review, compaction, permission gating, autonomous continuation, background jobs, work habits, session naming, locations, and every tool from `bash` to `control_screen`. Composition is the caller's choice, never a library default. The daemon builds its bundle explicitly in `langmeshd.features`; an embedder builds its own. See [Composition](../library/composition.md).

## Sessions

- **Being hosted is an activity, not the session.** An idle session sleeps immediately: its executor is dropped, and its record and conversation stay. The next message builds a new one in tens of milliseconds.
- **There is no linger window.** An executor held alive for a message that may not come pays continuously to avoid paying occasionally, and rebuilding one is cheap. A session parked on a permission prompt is the clearest case: the suspension is already fully on disk, so holding anything for a person who may take hours buys nothing.
- **A daemon restart ends executors, never sessions.** The harness derives the capability token from the session id (HMAC over a master key); it does not store it, so a woken session gets the same token its creator got.
- The daemon serves A2A (JSON-RPC) for every session it hosts, and every client reaches the daemon: the terminal, the desktop app, another session. There is one place that identifies a caller, scopes it to its own subtree, and records it.
- **There is no in-process delegation.** A session that needs a peer creates an ordinary session and messages it. See [Composing with other sessions](../user/agent-system.md#peer-sessions). A child appears as delegated work in the app and is reaped when its parent ends.
- **The worker is not a separate process.** Each live session is an executor inside the daemon. Sessions share the daemon's process, so a native crash takes the daemon rather than one session — that is the price of a session costing a fraction of a millisecond to build and sleep. Isolation is a property of the executor and the context it runs in: one session's state cannot reach another's.

## The daemon

`langmeshd` owns everything there can only sensibly be one of, and hosts the sessions. It owns:

- The **registry** of sessions (identity, parent, permission mode, status, lifecycle);
- The **lifecycle**: building a session's executor, dropping it when the session sleeps, and reaping a subtree parent-last;
- The **databases**, as the sole writer, so exactly one process writes SQLite (`history.sqlite` and `background.sqlite`);
- The shared **brokers**: events, terminals, file leases, workspaces, signed file URLs, push notifications, MCP servers, remote agents, Composio;
- The **sessions themselves**, each an executor it builds, holds, and drops. Hosting them is why the daemon imports the runtime at boot: that import costs seconds, and paying it once makes building a session cost a fraction of a millisecond;
- Launched **workers** only for the heavyweight, disposable pieces — the dictation transcriber, a backgrounded process group — never for a session.

It serves one API two ways:

- A **unix socket**, for the CLI and for sessions;
- A **loopback TCP port**, for the desktop client, which cannot open a unix socket from a webview. The port is ephemeral and chosen at boot; both listeners require the capability token the daemon writes `0600` into the runtime directory.

A token says a caller may drive the daemon; it does not say who is calling, and on the unix socket that distinction is load-bearing. A session's own `bash` tool runs as the same user and can read that `0600` file, so attribution on tokens alone would let a session present the daemon's token and get a peer with no parent and no permission clamp.

The unix listener asks the kernel instead: `SO_PEERCRED` on Linux, or `LOCAL_PEERPID` on macOS, names the process that opened the connection. Every worker starts as a process-session leader, so `getsid` on that pid names the session it belongs to, covering the executor and every shell command underneath it. That answer wins over the token: a session is itself, whatever token it holds. A caller the kernel places in no session stays unattributed, as it should: a person's terminal, or the desktop client. That same session id is how the harness ends a session's work.

The two answers are one fact read in two directions. What the kernel calls a session is what the harness attributes to it, and what it reaps with it.

## The CLI

`langmesh` adds nothing the control plane does not have. `serve` makes the interface available over HTTP with the daemon behind it. `mail` IDLEs a mailbox and drives the same control plane as a client; mail sessions speak through `submit_email`. See [Email](../user/email.md). `serve` is a reverse proxy in front of the daemon plus a static interface server: the browser page never sees the daemon's capability token. `serve` starts the daemon if it is not running and stops a daemon it started, leaving a pre-existing daemon alone. With `--reach` it adds a token-gated pairing door for your own devices. The [CLI guide](../user/installation.md) is the reference.

Creating and messaging sessions, answering permission requests, schedules, configuration, and sign-in otherwise happen in the interface or over the daemon's API, never through extra CLI verbs.

## The app

A [Tauri](https://tauri.app) shell around a [Next.js](https://nextjs.org) UI (static export; Chakra UI). It is a **client**: it holds no agent logic, renders conversations, manages settings, and chooses which daemon to talk to.

- It holds little state of its own. The workspace you were last in, the colour mode, the locale: the durable preferences are the daemon's, read at startup and written back on change. Two windows onto one daemon agree, and a client that never ran before opens on what the daemon already knows.
- Because a webview cannot open a unix socket, the app uses the daemon's loopback listener.
- The app does not contain a daemon. For the home daemon, a release build asks the separately installed daemon bundle to start when nothing is listening, then reads the port and token that `langmeshd` publishes into the runtime directory. The daemon remains independent and outlives the window.
- The daemon is a separate installable (`packaging/build-daemon.sh`), signed with the same identity as the app so the two share one macOS Accessibility grant.
- A phone is an Expo client that opens the same web interface in a WebView and adds the two things a page alone cannot do: reading the `langmesh://pair#…` pairing code with the camera, and keeping its token in the keychain. The shared UI and catalogue live in `shared/`.
- A GitHub event is handled by the installation-level App service: opening an issue or pull request, or a human comment, starts `langmesh.github.mention`. The service receives signed webhooks, writes them to an external durable queue, mints a repository-scoped installation token, loads provider/model credentials encrypted by installation, and restores session checkpoints from the same external database. Each worker uses a temporary checkout that is deleted after processing; GitHub branches and pull requests remain the durable source for repository changes. It acknowledges and updates the same thread comment, commits as the installed App, and opens draft pull requests for issue work. See [Universal GitHub App](../user/github.md).
- GitHub deliveries for one issue or pull request carry a database-assigned sequence. The worker will not claim a newer delivery while an older one is queued or active, and a stale delivery that follows a completed newer turn is marked superseded instead of replaying the conversation out of order.

## Connections: which daemon, where work runs, how a door leaves the machine

Three questions that look alike and are not.

**Which daemon the interface talks to.** A daemon's address and its token belong together. Each `langmeshd` mints its own token at boot, so a paired daemon does not accept this machine's. The client has one **home** connection (this page's daemon) and zero or more **paired** machines:

- **Home.** The app starts the separately installed daemon when needed, then reads the port and token `langmeshd` published into the runtime directory. A next-dev page is the same home connection, learned from the env `web-development.sh` writes. A page `langmesh serve` hosts talks same-origin through the proxy, so the browser never sees the capability token.
- **Paired.** Run `langmeshd` on another host, expose its loopback door behind a transport you choose, paste the `langmesh://pair#…` link under **Settings, then Connection**, and switch in the same window. Sessions from that host appear in the sidebar as that machine. Pairing lives on the home daemon; a switch does not navigate away.

**Where a session's tools run.** That is a **location**, not a daemon. `kind: local` is this machine's filesystem; `kind: remote` is an SSH host from `~/.ssh/config`. LangMesh forwards a local port to a command channel on that host, so the harness can work there while the daemon you are talking to stays the one you chose above.

**How a pairing door leaves the machine.** `--reach` binds loopback and gates every request with a pairing token. What carries that door off the machine is a transport you choose — Tailscale `serve`, an SSH tunnel, or any other TLS front. Tailscale is one documented way, not a product type: see [installation](../user/installation.md) and the [security policy](https://github.com/ghovax/langmesh/blob/main/SECURITY.md).

Keeping them apart serves one goal: **put the compute, the files, and the credentials wherever they belong, and keep the interface native.**

## What a session has, and what it may reach

Two questions that look alike and are not. **May reach** is the confinement: an operating-system boundary around every tool child. **Has** is the session's toolbox: a package profile of its own on `PATH`, which it installs into itself.

Keeping them apart is the point. When one answer served both, a missing tool arrived as `Operation not permitted`, indistinguishable from a refused path, and an agent that cannot tell them apart treats the first as something to route around. With a toolbox, obtaining a tool always has an ordinary ending, so every refusal that remains is a real one. The toolbox is per session and dies with it: packages come out of the shared read-only store, and the session owns only symlinks.

## Permissions

A session's permission mode is chosen when the session is created and can change while it runs; the change reaches the turn already in flight. The modes are `ask`, `automatic`, and `allow`. A child gets a mode no looser than its parent's, and tightening a session tightens everything it created; `ask` is the most restrictive, and an unattended session (`automatic` or `allow`) can only create sessions that also run unattended. See the [security policy](https://github.com/ghovax/langmesh/blob/main/SECURITY.md).

## Request lifecycle

1. You send a message to a session. Every client posts to the daemon, which relays to the owning session. A session mid-turn takes the message into that turn at its next safe point; one parked on a decision takes nothing and says so, because starting a turn would discard the parked one.
1. The agent loop calls the model, which may request tool calls.
1. Each tool call is measured against the session's confinement. One that stays inside it runs. One that asks to reach past it is decided by the session's permission mode: under `ask` the session streams an overlay to answer; under `automatic` the reviewer answers; under `allow` the gate is skipped entirely and the call runs.
1. Approved tools then run: shell inside an OS-enforced confinement (`sandbox-exec` on macOS, Landlock on Linux), files on the active location, screen control against the local machine, and MCP servers through the session's own connections.
1. Results take two deliberately separate lanes:
   - Every model text and reasoning delta is published synchronously to the daemon's in-process bounded event bus and reaches attached SSE clients without a timer or database write. At semantic boundaries the accumulated content block is written once to `history.sqlite`.
   - History streams complete compacted turns continuously from newest to oldest through a durable high-water mark, while live events preempt history work. The browser prepends each turn once with stable identities, so opening cost is independent of conversation length and replay is linear.
   - Model-facing tool results follow the same separation: the required contiguous `ToolMessage` block carries only execution data; timing and lifecycle metadata live in LangChain artifacts that providers never receive; explanatory guidance is appended afterward as a distinct hidden reminder. No prior message is rewritten, so reviewer branches and later turns retain the exact provider-cache prefix.
1. The turn ends when the model stops asking for tools, unless the session holds a **goal**, in which case the goal feature drives an independent review.

An embedded session receives one absolute working directory as a run fact. The library does not discover project files, configuration, worktrees, or remote locations; the caller supplies catalogue values and adapters, while the daemon owns its filesystem-specific loaders and location plugin. Explicit shell and file tools still operate on authorized paths because mutation is their requested behavior, not an implicit persistence side effect.

## Goals

A turn ends when the model stops talking. That is the wrong unit for work asked for as an outcome, because the model can stop for reasons that have nothing to do with the outcome being real.

So a session can hold one **goal**: the end state, what the end state is for, and the conditions that must hold for it to be true. All three are written by the agent through `update_goal`, and all three are durable beside the conversation. The goal is a plugin feature.

- **Setting a goal is the whole of the agent's authority over it.** The agent owns the goal's `status` through `update_goal`: it keeps `active` while working, marks `satisfied` or `blocked` when it believes work or blockage is real, parks, or clears. Who settles a `satisfied` or `blocked` mark is `goal_review.settlement`. Under `reviewer`, the mark itself is what the secondary review audits. Under `agent`, that mark is final and the session ends; no isolated reviewer session is opened.
- **A marked status is settled by a review** when `goal_review.settlement` is `reviewer`: an isolated agent session made when a turn ends. The reviewer receives the main session's exact conversation, model, and cached static prompt, while runtime policy narrows execution to read-only work. Its verdict tool, `submit_goal_review`, is bound only in that hidden lane; the working session never carries an inert verdict tool, and the review's distinct tools segment makes its first cache boundary explicit rather than pretending prose can declare a callable function.
- The reviewer is linked to the working session and visible in the dedicated goal-review panel, working from a read-only workspace and disposable scratch directory. It forms its own opinion by reading the user's requests, searching the code, inspecting the diff, running non-mutating checks, and probing suspicious results rather than grading the working agent's claims.
- Its final action is a structured `submit_goal_review` call naming the requirements it could not prove, whether the formal contract is complete, whether the goal is unmet, satisfied, or blocked, and the exact next message when work remains. The tool accepts the verdict on any review, gated only by the reviewer's own evidence. If the reviewer stops without submitting, it is asked again until it does. Cancelling the parent session cancels the review. The review confirms an agent-marked `satisfied` or `blocked` only on evidence, and overrides a mark the work does not support, sending the goal back to reasoned work with a message. `parked` and `cleared` are administrative marks applied directly, and an open goal a turn ends on without a mark is re-opened with a light continuation reminder rather than a review.
- **No hard cap bounds autonomous work.** While a goal stays open and unmarked the session keeps being reminded to reach it — reaching it, or the person stopping it, is what ends it — and unfinished tracked tasks keep reopening their own work. The goal is visible in the app above the composer, with a control that calls it off outright, which clears it for the person.

For an unmet goal, the review's generated message and review identifier are one durable pair, sent as the next turn, consumed together when that turn opens, and streamed immediately. The chat renders it once as a user-shaped card labelled "Relayed from the goal review agent," whose review action opens the exact linked transcript. The working agent sees that message and nothing else from the verdict.

Tracked tasks have their own continuation obligation and allowance. At the completed-turn boundary, the session derives one plan containing the independent goal, review, and tasks reasons. One owned workflow drains those plans under the session's turn lock: it settles a marked status first — with the secondary review when `goal_review.settlement` is `reviewer`, or by applying the working agent's mark when it is `agent` — then opens exactly one next turn when work remains — the review's continuation message when a reviewer reverts the mark, a light goal-reminder turn when the goal stayed open and unmarked, or a task-only reminder turn when only tasks remain. A mark settled by the working agent ends the session. Each plugin contributes its own segment (the goal its reminder or review prose, tracked tasks their audit note), staged as its own separate message in order — so obligations compose without ever being merged into one text, and competing automatic turns are structurally impossible.

## Prompt caching, and what is recorded about it

Every provider bills a cached prefix at a fraction of a fresh one, and a conversation is almost entirely prefix: the system prompt, the tool schemas, and every turn that came before. A session's cost is decided less by what it does than by whether the request it sends still matches the one before it. Two rules follow, and the harness holds both.

**The persisted conversation is append-only until compaction.** Each ordinary call adds to its end and rewrites nothing, while instructions and tool schemas stay stable for the session. When a configured preparation is required, LangMesh runs that handoff before compaction; otherwise the fold proceeds directly. The compaction then drops the old head and resumes the accepted work.

- Static instructions contain only stable construction inputs: the agent prompt, catalogue guidance, configured skills and memories, package resources, feature guidance, and the initial tool schema. Session identity, directories, confinement, machine details, user context, feature state, and background events are one marked conversation message. The runtime appends that message before accepted input and only appends a replacement when its digest changes, so a runtime change extends the existing prefix instead of re-rendering its head.
- **Intentional prefix invalidations are explicit.** Compaction replaces the conversation head; `refresh_prompt()` or a stable construction change replaces the instructions; `grant_tool()` replaces the tool schema; and a model, route, settings, or account change selects another provider cache. Ordinary turns, retries, steering, interrupts, context changes, tool results, background results, and worker restarts do none of those things. Provider-native reasoning is dropped only at compaction.

**The cache is asked for by content identity.** Providers that accept `prompt_cache_key` receive a digest of the stable model, tool schema, instructions, and request settings. Separate sessions with the same stable prefix therefore route to the same upstream cache identity, while a changed model, schema, prompt, or setting cannot collide. Session-specific context follows those shared bytes as an append-only message. The initial bound tool schema stays fixed through ordinary work. A live tool grant is an explicit capability mutation: it rebinds the real provider schema, records a tools-segment divergence on the next call, and establishes the new reusable prefix thereafter. Auxiliary lanes keep independent local baselines so their requests never replace the ordinary conversation's comparison chain. Claude additionally gets rolling explicit `cache_control` breakpoints: the two newest attempted tails are retained per lane, both are marked again when reachable, and the new tail becomes the next candidate. That fallback preserves a warm breakpoint when a response or connection disappears before the provider confirms the newest write.

A provider serves the longest prefix it recognises, so a low cache read is either a request that stopped matching or one that matched and was not served. Those want opposite responses and look identical from the outside. So every model call records how it compared to the one before it: the request is cut into the segments the wire is built from, each is digested and counted, and the next call's segments are compared against them. `langmesh.runtime.cache_trace` does the measuring for the LiteLLM and Codex adapters, while Cursor preserves its provider-native checkpoint and referenced blobs. All three stores are bounded, owned by one model/session rather than process-global, included in the durable session snapshot, and restored across worker replacement. Cursor snapshots are accepted only for the account fingerprint that produced them and retain their real elapsed age, so a sign-in change or expired checkpoint cannot leak across accounts or masquerade as reusable state.

### Cache state matrix

"Cache-preserving" below means 100% of the previously sent eligible prefix remains byte-for-byte addressable; the newly appended tail is necessarily new input. It does not promise that a remote provider will report a read, because its minimum cacheable size, retention TTL, routing, account, and concurrent warm-up behavior remain outside the harness.

| Situation                                                                          | Wire and durable-state placement                                                                                                       | Local cache result                                                                         | Remote caveat                                                                                                |
| ---------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------ |
| First-ever call in a lane                                                          | Stable instructions and tools followed by the first message                                                                            | Unknown, because no local baseline exists                                                  | The provider may still reuse an independently warmed prefix.                                                 |
| Ordinary next user turn                                                            | New user message appended after the completed assistant exchange                                                                       | Cache-preserving                                                                           | The new user tail is uncached; the old prefix should be read when still retained and large enough.           |
| Separate session with the same model, tools, instructions, and settings            | The shared stable prefix and `prompt_cache_key` are identical; session context begins only after it                                    | Cache-preserving through the shared prefix                                                 | Session-specific context and later conversation are distinct tails.                                          |
| Session or feature context changes                                                 | One marked context message is appended only when its digest differs                                                                    | Cache-preserving                                                                           | The changed context is a new tail; no earlier context or instruction is rewritten.                           |
| Steering while idle                                                                | Steering becomes the next appended human message before assembly                                                                       | Cache-preserving                                                                           | Same as an ordinary turn.                                                                                    |
| Steering during provider streaming                                                 | Message waits in FIFO order until the active response finishes, then appends at the next model boundary                                | Cache-preserving                                                                           | No in-flight request is cancelled or reordered for steering.                                                 |
| Live permission-mode change                                                        | The new mode commits to the session record or checkpoint before the running runtime adopts it                                          | Cache-preserving                                                                           | It changes execution policy, not prior model bytes, and survives an immediate restart.                       |
| User interrupts provider streaming                                                 | Already received assistant chunks, or an explicit empty cancelled assistant message, close the accepted exchange                       | Cache-preserving                                                                           | The interrupted request itself already warmed or read its input; its partial output is a new tail.           |
| Transport or network drop before output                                            | A transport retry resends the exact assembled request; the attempted trace and old/new Claude anchors are retained before the response | Cache-preserving                                                                           | A request that never reached the provider cannot have warmed it; routing may move a retry.                   |
| Provider silence timeout after partial output                                      | The partial assistant message closes the exchange before later work                                                                    | Cache-preserving                                                                           | The provider may have generated more than the client received, but only received bytes become the next tail. |
| Completed tool batch                                                               | One assistant tool-call message is followed immediately by one `ToolMessage` per call, in call order                                   | Cache-preserving                                                                           | Tool outputs are new tail tokens.                                                                            |
| One tool is interrupted or fails                                                   | Its paired `ToolMessage` is appended with failure status and the original call id                                                      | Cache-preserving                                                                           | Failure changes no earlier call or result.                                                                   |
| Global stop during parallel tools                                                  | Every announced call receives a real or interrupted failure result before the exchange closes                                          | Cache-preserving                                                                           | Detached background work is not killed by foreground Stop.                                                   |
| Background job returns after the turn                                              | Its durable result is appended through the feature drain; an idle session may receive a new autonomous turn                            | Cache-preserving                                                                           | Result text is a new tail; an undelivered stored result is replayed once after restart.                      |
| Worker or daemon restarts                                                          | Conversation, request traces, Claude anchors, and account-scoped Cursor resumptions restore from the same session snapshot             | Cache-preserving when a baseline existed                                                   | A provider-side TTL may expire while the daemon is down.                                                     |
| Model, provider route, or API base changes                                         | The old model-cache snapshot is rejected while conversation history remains                                                            | Unknown on the new route                                                                   | A cache belonging to another route or account is never presented as locally reusable.                        |
| Hidden reviewer, summarizer, or permission lane                                    | The lane inherits the conversation prefix but carries its own baseline and its actually bound verdict-tool schema                      | First call may diverge at tools; retries and later calls in that lane are cache-preserving | A distinct tool schema cannot honestly reuse the main lane beyond the tools boundary.                        |
| Live `grant_tool()`                                                                | Provider-visible tools segment changes once and becomes the new stable schema                                                          | Intentional divergence                                                                     | Supplying predictable tools at construction avoids it.                                                       |
| `refresh_prompt()` or a stable construction input changes                          | First instructions segment is rebuilt explicitly                                                                                       | Intentional divergence                                                                     | Following calls reuse the refreshed prefix.                                                                  |
| Compaction                                                                         | Old conversation head is replaced by the summary and retained working set                                                              | Intentional divergence                                                                     | The compacted request establishes the next warm prefix.                                                      |
| Provider cache expired, too small, rerouted, changed account, or concurrently cold | Request bytes remain unchanged                                                                                                         | Locally cache-preserving                                                                   | Provider read may be zero and write may be nonzero; this is not a local prefix defect.                       |

| Recorded on each `Usage` event            | What it says                                                                                                                    |
| ----------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------- |
| `input_tokens`, `output_tokens`           | The call's own size, not a running total.                                                                                       |
| `cache_read_tokens`, `cache_write_tokens` | The provider's actual cache reads and writes for this call.                                                                     |
| `reasoning_tokens`                        | The call's reasoning spend.                                                                                                     |
| `cache_prefix_reusable`                   | Whether the preceding request is a complete prefix of this one; unknown without a local baseline.                               |
| `reusable_prefix_tokens`                  | Tokens of locally reusable prefix, the denominator for cache-read coverage. An estimate, counted with this harness's tokenizer. |
| `segments`, `shared_segments`             | The same comparison in pieces rather than tokens.                                                                               |
| `divergence`                              | When the prefix moved: the segment index, the current and previous segment, and whether it was rewritten in place.              |
| `cumulative`                              | Session-lifetime totals.                                                                                                        |

Reading them together is what makes a diagnosis:

- `cache_prefix_reusable` true with `cache_read_tokens` at zero means the provider was handed bytes it had already been sent and returned nothing for them: routing, not the request.
- A `divergence` with `rewritten` true is the opposite: something here rewrote a message in place.
- The first-ever call in a lane reports `cache_prefix_reusable` as unknown; a worker restart restores the last baseline, so it does not erase that diagnosis.
- `reusable_prefix_tokens` can exceed `input_tokens` by a few percent, because the two are counted with different tokenizers; a large disagreement is worth looking at.

## Where to go next

- Configure providers and behavior: [Configuration guide](../user/configuration.md).
- Author agents, skills, memory, and MCP servers: [Agent system guide](../user/agent-system.md).
- The tool surface in detail: [Tools guide](../user/agent-system.md).
