# Configuration reference

Every setting LangMesh has, in the order the settings panel presents them: what you decide first at the top, the numbers underneath everything at the bottom.

A setting is addressed by its dotted path, and the same path works everywhere: in `~/.config/langmesh/configuration.yaml`, and as the key the interface writes. Nothing is written to that file until you change it; a setting you never touched follows the default.

To read or change a setting, edit `~/.config/langmesh/configuration.yaml` (a setting you never touched may be absent; omit it and the default applies) or use the interface's settings panel, which walks the same schema. To unset a setting, remove its line from the file rather than writing the default into it.

The settings panel shows the same set, with the name and explanation in the interface language. The words there and the words here are the same: they live in `shared/messages/`, keyed by these paths.

## Agent defaults

What a session runs under when its creator does not say.

| Setting                 | Type                | Default | What it is for |
| ----------------------- | ------------------- | ------- | -------------- |
| `agent.permission_mode` | `ask` / `automatic` | `ask`   | Who answers when a session asks to reach past its confinement when neither the person nor its agent profile chooses a mode. |

## Workspaces

Where a session's tools run.

| Setting              | Type                           | Default | What it is for |
| -------------------- | ------------------------------ | ------- | -------------- |
| `workspace.strategy` | `none` / `branch` / `worktree` | `none`  | Where a session's work happens: the project directory itself, a branch of its own, or a git worktree of its own. |

## Confinement

What a session's tool children may do, enforced by the operating system.

| Setting                        | Type                             | Default | What it is for |
| ------------------------------ | -------------------------------- | ------- | -------------- |
| `sandbox.enforce`              | `required` / `preferred` / `off` | `required` | What to do on a machine that cannot enforce confinement: refuse to start the session, run with resource limits only, or do not confine. |
| `sandbox.filesystem.readable`  | list                             | `~/.agents` `~/.config` `~/.local` `~/.ssh` `~/.gitconfig` `~/.gitignore_global` `~/.cargo` `~/.rustup` `~/.npmrc` `~/.nvm` `~/.pyenv` `~/.docker` `~/.netrc` `~/Library/Keychains` | Paths under your home a tool child may read. The system is readable and is not listed. |
| `sandbox.filesystem.writable`  | list                             | `$WORKSPACE` `$TMPDIR` `/tmp` `$XDG_CACHE_HOME` `~/.cache` | Paths a tool child may write. Deliberately narrower than readable. |
| `sandbox.filesystem.grantable` | list                             | — | Paths an agent may be granted at runtime without asking. Empty means every request is put to you. |
| `sandbox.filesystem.deny`      | list                             | — | Opt-in absolute bans. Wins over readable and writable; no request opens them. |
| `sandbox.network`              | boolean                          | `true` | Whether a tool child may reach the network at all. |
| `sandbox.limits`               | map                              | `{'RLIMIT_CORE': 0, 'RLIMIT_FSIZE': 8589934592, 'RLIMIT_NPROC': 2048}` | Per-child resource limits, by their setrlimit names. |
| `sandbox.umask`                | string                           | — | The file-creation mask a tool child runs under. Empty leaves the machine's own. |
| `sandbox.nice`                 | integer                          | `0` | How far to lower a tool child's scheduling priority, so a runaway command does not take the machine with it. |

## Session toolbox

Whether a session may install the tools it needs into a profile of its own.

| Setting           | Type    | Default | What it is for |
| ----------------- | ------- | ------- | -------------- |
| `toolbox.enabled` | boolean | `true`  | Let each session install the tools it needs into a package profile of its own, deleted with the session. |

## Conversation compaction

How conversation history is compacted as it grows.

| Setting                                  | Type    | Default | What it is for |
| ---------------------------------------- | ------- | ------- | -------------- |
| `compaction.automatic`                   | boolean | `true` | Reclaim context on its own as it fills. Manual compaction works either way. |
| `compaction.reclaim_at_fraction`         | number  | `0.85` | Recommended preparation boundary. A private local-Bash segment first updates the current observational registry and advances its revision; compaction follows only after validation succeeds. |
| `compaction.output_reserve_fraction`     | number  | `0.1` | Share held back as safety space for the preparation segment and the answer. |
| `compaction.recent_working_set_fraction` | number  | `0.15` | Share of the usable window kept verbatim after older history is discarded. Sized in tokens rather than turns. |
| `compaction.summary_attempts`            | integer | `3` | How many times the hidden summarizer may be asked again after reviewing but not submitting; once exhausted, the compaction stops and the conversation is left unchanged until it is retried. |
| `goal_review.maximum_attempts`           | integer | `3` | How many times a reviewer that investigated but never submitted is asked again on a narrowed toolset before the goal carries unchanged. |

## User snapshot

Whether the system prompt describes how you work on this machine.

| Setting                      | Type    | Default | What it is for |
| ---------------------------- | ------- | ------- | -------------- |
| `user_context.enabled`       | boolean | `false` | Include a snapshot of how you work, your editor, habits, machine, in the system prompt. |
| `user_context.refresh_hours` | number  | `6`     | How old that snapshot may get before it is rebuilt. The rebuild runs in the background. |

## Screen control

Driving the screen.

| Setting                                               | Type    | Default | What it is for |
| ----------------------------------------------------- | ------- | ------- | -------------- |
| `computer_control.enabled`                            | boolean | `false` | Let the agent drive native applications and your browser. It also needs Accessibility granted in System Settings. |
| `computer_control.settle.poll_seconds`                | number  | `0.05` | How often to re-check whether the surface has settled. |
| `computer_control.settle.give_up_seconds`             | number  | `1.5` | The longest to wait before reading it anyway. |
| `computer_control.retrieval.multilingual_rank_model`  | string  | `minishlab/M2V_multilingual_output` | The static embedding that ranks by meaning across languages. Also the model whose plain cosine backs the relevance floor. Empty turns it off. |
| `computer_control.retrieval.english_rank_model`       | string  | `minishlab/potion-base-32M` | A second static embedding, ranked alongside the first. Stronger on queries that describe what an element is for. Empty turns it off. |
| `computer_control.retrieval.lexical_gate_short_words` | integer | `3` | Queries of this many words or fewer are treated as a label quoted off the screen, and the character similarity counts in full. |
| `computer_control.retrieval.lexical_gate_long_words`  | integer | `7` | Queries of this many words or more are treated as a description of a purpose, and the character similarity is ignored. |

## Dictation

Speaking to the composer instead of typing.

| Setting                                                      | Type    | Default | What it is for |
| ------------------------------------------------------------ | ------- | ------- | -------------- |
| `dictation.enabled`                                          | boolean | `false` | Let the composer take speech. The model runs on this machine. |
| `dictation.model`                                            | string  | `mlx-community/parakeet-tdt-0.6b-v3` | The speech model dictation downloads and runs locally. |
| `dictation.timing.minimum_transcription_timeout_seconds`     | number  | `30.0` | The floor on how long one transcription may take before it is treated as wedged. |
| `dictation.timing.transcription_timeout_realtime_multiplier` | number  | `0.5` | Added to the floor, per second of audio. |
| `dictation.timing.maximum_attempts`                          | integer | `2` | How many workers one recording may be given. |
| `dictation.timing.worker_shutdown_seconds`                   | number  | `2.0` | How long a worker is given to exit on its own before it is killed. |

## Model providers

| Setting     | Type | Default | What it is for |
| ----------- | ---- | ------- | -------------- |
| `providers` | map  | — | Credentials for each model provider. The provider's own environment variable wins over anything stored here. |

## Web search

| Setting       | Type   | Default | What it is for |
| ------------- | ------ | ------- | -------------- |
| `exa.api_key` | string | — | Exa API key. The EXA_API_KEY environment variable wins over this. |

## Page fetching (Jina)

| Setting        | Type   | Default | What it is for |
| -------------- | ------ | ------- | -------------- |
| `jina.api_key` | string | — | Jina API key, which raises the rate limit. JINA_API_KEY wins over this. |

## Page fetching (Firecrawl)

| Setting             | Type   | Default | What it is for |
| ------------------- | ------ | ------- | -------------- |
| `firecrawl.api_key` | string | — | Firecrawl API key. FIRECRAWL_API_KEY wins over this. |
| `firecrawl.api_url` | string | — | A self-hosted Firecrawl instance to use instead of the hosted API. |

## Web fetch

| Setting               | Type   | Default | What it is for |
| --------------------- | ------ | ------- | -------------- |
| `web_fetch.proxy_url` | string | — | Route direct fetches and file downloads through an HTTP or SOCKS proxy. Credentials may be embedded as `http://user:pass@host:port`. |

## Composio

| Setting                    | Type    | Default | What it is for |
| -------------------------- | ------- | ------- | -------------- |
| `composio.enabled`         | boolean | `false` | Expose Composio's hosted gateway as one MCP server. |
| `composio.url`             | string  | `https://connect.composio.dev/mcp` | The hosted MCP URL from the Composio dashboard's "connect" page. |
| `composio.api_key`         | string  | — | The API key shown beside that URL. COMPOSIO_API_KEY wins over this. |
| `composio.server_name`     | string  | `composio` | The MCP server name its tools appear under. |
| `composio.timeout_seconds` | number  | `60` | How long one call to that gateway waits. |

## MCP servers

| Setting       | Type | Default | What it is for |
| ------------- | ---- | ------- | -------------- |
| `mcp.servers` | map  | — | MCP servers, by name. Edited on the MCP panel, which is where a server's command and credentials belong. |

## Remote peers

| Setting                | Type | Default | What it is for |
| ---------------------- | ---- | ------- | -------------- |
| `remote_agents.agents` | map  | — | Remote peers, by name. Edited on the peers panel. |

## Telemetry

| Setting                       | Type                     | Default         | What it is for |
| ----------------------------- | ------------------------ | --------------- | -------------- |
| `telemetry.enabled`           | boolean                  | `false`         | Export traces at all. |
| `telemetry.exporter.endpoint` | string                   | —               | Where traces are sent. Empty sends none. |
| `telemetry.exporter.protocol` | `http/protobuf` / `grpc` | `http/protobuf` | The OTLP protocol that collector speaks. |
| `telemetry.exporter.headers`  | map                      | —               | Headers sent with every export, for a collector that authenticates. |
| `telemetry.sample_ratio`      | number                   | `1.0`           | Share of traces exported. `1.0` exports every one. |

## Tuning

How large, how many, and how patient the tools are.

| Setting                                | Type    | Default | What it is for |
| -------------------------------------- | ------- | ------- | -------------- |
| `tuning.context_share.text`            | number  | `0.25` | Share one result's text may fill: output, fetched pages. |
| `tuning.context_share.results`         | number  | `0.15` | Share a set of results may fill: matches, lines, records. |
| `tuning.timeout_multiplier`            | number  | `1.0` | Multiplier on every wait. `2.0` doubles them for a slow machine. |
| `tuning.defaults`                      | section | — | Override one tunable by its own name. Every duration is in seconds. |
| `tuning.defaults.output_tokens`        | integer | `16000` | Tokens of inline output one tool may return before the rest overflows to a file. |
| `tuning.defaults.fetch_tokens`         | integer | `24000` | Tokens of a fetched web page's text kept inline. |
| `tuning.defaults.maximum_line_chars`   | integer | `2048` | Characters of a single over-long line kept before it is clipped. |
| `tuning.defaults.upstream_error_detail_tokens` | integer | `256` | Tokens of an upstream error body kept in the failure this harness raises. |
| `tuning.defaults.read_lines`           | integer | `2000` | Lines a file read returns when no explicit limit is given. |
| `tuning.defaults.grep_results`         | integer | `512` | Total matches one search returns. |
| `tuning.defaults.grep_per_file`        | integer | `512` | Matches one search returns from any single file. |
| `tuning.defaults.glob_results`         | integer | `1000` | Paths one glob returns. |
| `tuning.defaults.web_search_maximum`   | integer | `10` | Ceiling on the result count a web search may ask for. |
| `tuning.defaults.remote_listing`       | integer | `32768` | Paths listed on a remote machine before glob matching is applied locally. |
| `tuning.defaults.web_exchanges`        | integer | `250` | Recent request/response pairs a browser session keeps. |
| `tuning.defaults.web_websockets`       | integer | `32` | Live websockets a browser session tracks at once. |
| `tuning.defaults.web_websocket_frames` | integer | `200` | Frames retained per tracked websocket. |
| `tuning.defaults.action_timeout`       | integer | `5000` | How long one browser action waits for its element. |
| `tuning.defaults.navigation_timeout`   | integer | `20000` | How long a page load or navigation waits. |
| `tuning.defaults.snapshot_timeout`     | integer | `10000` | How long an accessibility snapshot of a page waits. |
| `tuning.defaults.connect_timeout`      | integer | `10000` | How long attaching to a browser waits. |
| `tuning.defaults.browser_authorization`| integer | `90000` | How long attaching waits for the user to approve Chrome's prompt. |
| `tuning.defaults.drag_timeout`         | integer | `8000` | How long a drag between two elements waits. |
| `tuning.defaults.screenshot_timeout`   | integer | `20000` | How long capturing a page screenshot waits. |
| `tuning.defaults.read_text_timeout`    | integer | `10000` | How long reading a page's text waits. |
| `tuning.defaults.frame_resolve_timeout`| integer | `2000` | How long resolving a frame reference waits. See the note below. |
| `tuning.defaults.sigterm_grace`        | number  | `3.0` | How long a cancelled command or a reaped session has after SIGTERM before SIGKILL. |
| `tuning.defaults.ripgrep`              | number  | `30.0` | How long one content search may run. |
| `tuning.defaults.bash_sync_window`     | number  | `60.0` | How long a shell command runs inline before it moves to the background. |
| `tuning.defaults.slow_tool_sync_window`| number  | `10.0` | The same inline window for fetching a URL or downloading a file. |
| `tuning.defaults.web_search_sync_window` | number | `10.0` | The same inline window for a web search. |
| `tuning.defaults.accessibility_messaging` | number | `2.0` | How long one accessibility message to an application waits. |
| `tuning.defaults.goal_continuation_turns` | integer | `12` | How many turns in a row a session may open for its own goal before it stops and waits for the person. |
| `tuning.defaults.task_continuation_turns` | integer | `12` | How many turns in a row unfinished tracked tasks may reopen automatically before the session waits for the person. |
| `tuning.defaults.session_title_attempts` | integer | `3` | How many times a session asks the model to name itself before giving up. |
| `tuning.defaults.permission_reviewer_attempts` | integer | `3` | How many times the permission reviewer is asked before its silence counts as a refusal. |
| `tuning.defaults.session_idle_sleep`   | number  | `18000.0` | How long a session keeps its process after its last turn before it sleeps. |
| `tuning.defaults.daemon_startup`       | number  | `45.0` | How long a command waits for a daemon it just started to become reachable. |
| `tuning.defaults.control_plane_call`   | number  | `60.0` | How long one call to the daemon waits. |
| `tuning.defaults.model_catalogue_ttl`  | number  | `60.0` | How long the list of available models is cached. |
| `tuning.defaults.credential_refresh_leeway` | number | `300.0` | How far ahead of its expiry an access token is refreshed. |
| `tuning.defaults.daemon_probe_interval`| number  | `0.05` | Pause between asks of whether another process's daemon socket answers yet. |
| `tuning.defaults.daemon_probe_connect` | number  | `0.5` | How long one connect to a daemon socket waits before it counts as unanswered. |
| `tuning.defaults.oauth_poll_interval`  | number  | `1.0` | First pause between asks of whether a browser sign-in has completed; it widens from here. |
| `tuning.defaults.oauth_poll_ceiling`   | number  | `10.0` | Ceiling on the widening pause between sign-in polls. |
| `tuning.defaults.oauth_poll_give_up`   | number  | `300.0` | How long a browser sign-in is waited for before it is abandoned. |
| `tuning.defaults.subscription_resume_ttl` | number | `1800.0` | How long a subscription provider's server-side conversation state stays worth resuming from. |
| `tuning.defaults.model_silence_give_up`| number  | `180.0` | How long a model stream may make no meaningful progress before the turn fails. |
| `tuning.defaults.file_url_ttl`         | number  | `600.0` | How long a signed file URL stays valid. |
| `tuning.defaults.mcp_connect`          | number  | `20.0` | How long connecting to one MCP server waits. |
| `tuning.defaults.card_resolve`         | number  | `20.0` | How long fetching a remote agent's card waits. |
| `tuning.defaults.remote_command`       | number  | `120.0` | How long a command on another machine may run. |
| `tuning.defaults.remote_connect`       | number  | `16.0` | How long opening an SSH connection waits. |
| `tuning.defaults.remote_control_persist` | number | `120.0` | How long a shared SSH connection lingers after its last use. |
| `tuning.defaults.control_script`       | number  | `120.0` | How long one screen-control script may run. |
| `tuning.defaults.surface_guard_margin` | number  | `30.0` | How far above the script's own limit the machinery waiting on it sits. |
| `tuning.defaults.screencapture`        | number  | `15.0` | How long capturing the screen waits. |
| `tuning.defaults.open_url`             | number  | `5.0` | How long handing a URL to the system browser waits. |
| `tuning.defaults.type_chunk_size`      | integer | `20` | Characters sent per synthesized keyboard event. |
| `tuning.defaults.drag_steps`           | integer | `12` | Segments a drag is split into, so it looks like a hand moved it. |
| `tuning.defaults.scroll_amount_pixels` | integer | `300` | Pixels one scroll step moves a native window. |
| `tuning.defaults.settle_stable_reads`  | integer | `2` | Identical consecutive reads that count a surface as having stopped changing. |
| `tuning.defaults.find_rephrasing_similarity` | number | `0.45` | How alike two screen queries must be before a second one on the same element counts as the first asked again. |
| `tuning.defaults.find_near_weight`     | number  | `0.5` | How much sitting beside the anchor is worth against matching the query. |
| `tuning.defaults.find_anchor_margin`   | number  | `0.02` | How far ahead of its own runner-up a near anchor must score before a find will join on it. |
| `tuning.defaults.find_candidates`      | integer | `5` | Elements find_one weighs against its best match. |
| `tuning.defaults.find_one_margin`      | number  | `0.2` | How far ahead of the runner-up find_one's best match must score before it answers with one element. |
| `tuning.defaults.find_many_ceiling`    | integer | `50` | Elements find_many will return however many are asked for. |
| `tuning.defaults.find_relevance_floor` | number  | `0.25` | How well an element must match before find_many returns it at all. |
| `tuning.defaults.click_interval`       | number  | `0.01` | Pause between successive synthesized clicks. |
| `tuning.defaults.drag_step_interval`   | number  | `0.01` | Pause between the interpolated steps of a drag. |
| `tuning.defaults.type_chunk_interval`  | number  | `0.005` | Pause between typed chunks. |
| `tuning.defaults.focus_settle`         | number  | `0.03` | Pause after focusing a field, before typing into it. |
| `tuning.defaults.stamped_image_side`   | integer | `2048` | Longest side, in pixels, of a screenshot annotated with element labels. |
| `tuning.defaults.accessibility_walk_budget` | number | `3.0` | How long one read of an app's accessibility tree may take. See the note below. |
| `tuning.defaults.accessibility_ready_probe` | number | `0.4` | How long the readiness poll may spend deciding whether an app's tree has built yet. |
| `tuning.defaults.accessibility_prewarm_interval` | number | `0.4` | Pause between pre-warming the frontmost application's accessibility tree. |
| `tuning.defaults.accessibility_ready_backoff` | number | `0.2` | Ceiling on the widening pause between accessibility readiness probes. |

## Notes on individual tunables

A few carry more reasoning than a table row holds.

- **`accessibility_walk_budget`** replaces a depth limit, which guarded the wrong quantity: a window six levels deep can take twice as long as one thirty-five levels deep, because the cost is how quickly the app answers, not how far down. Anything unread when it expires is reported as a region.
- **`find_anchor_margin`** below its default makes the anchor a guess, and organising a ranking around a guess is worse than not anchoring.
- **`find_many_ceiling`** exists because an `all=True` escape returned 590 elements and 1.5MB on one ordinary page, ending the turn by exceeding the context window. A find is a ranked search; the tail of a ranking is not more answer.
- **`find_near_weight`** is fitted over 284 anchored cases: relevance alone answers about a fifth and proximity alone about a fifth, while the two together answer 85%.
- **`find_one_margin`** is a budget rather than a discovery, and it is fitted against the fused ranking, not the top score. Re-fit it whenever the ranking changes.
- **`find_relevance_floor`** cuts the noise band and nothing more. Treat an empty result as "nothing scored above the noise", never as proof of absence, and do not raise this hoping to buy absence detection; it would cost real matches first. It stays a cosine against the query because a fused score is scaled by the query's own length.
- **`find_rephrasing_similarity`** requires both halves: likeness alone cries wolf on honest work, and same-element alone misses rephrasings.
- **`frame_resolve_timeout`** is deliberately well below the action timeout, so a frame that has gone waits out its budget rather than erroring.
- **`session_idle_sleep`** is five hours by default: long enough that a working day of on-and-off use never pays a wake, short enough that a machine left overnight is not holding interpreters for conversations nobody returned to.
