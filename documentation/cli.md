# The `langmesh` command

`langmesh` drives LangMesh from a terminal. It adds nothing the control plane does not have; it is the ergonomic face of it. Anything you can do here, you can also do from the desktop app.

This command is for people. A session composes with its peers through [tools](tools.md#the-built-in-surface), over the same control plane; it does not shell out to this command. A typed call carries the caller's identity, which an argv string cannot.

That is enforced rather than merely advised. The daemon takes a caller's identity on its unix socket from the kernel. Every command a session runs inherits that session's process session, so `langmesh` run from inside a session is attributed to it and scoped the way its own tools are: it can create, message, inspect, and end sessions in its own subtree, and nothing else. From your own terminal, nothing is scoped.

The daemon starts itself on your first command. There is no mandatory "start the service" step. `langmesh serve` is for when you want it up on its own, and `langmesh run` skips it entirely: one turn in your terminal, with no control plane at all.

## The shape of it

A **session** is one durable conversation running one agent. You create it empty, send it work, and read what it produced. Creating and working are separate steps on purpose: the same session takes a second task, can be attached to, and can be inspected in between.

```shell
id=$(langmesh create --agent general-assistant --directory ~/code/project)
langmesh send "$id" "What does this project do?" --wait
langmesh ps
```

`create` prints the bare session id on stdout, which is what makes `id=$(langmesh create …)` work in a shell script.

## Creating a session

```text
langmesh create [-a AGENT] [-C DIRECTORY] [-m MODE] [-p PROJECT] [-P PARENT] [-t TITLE]
```

| Flag | What it does |
|------|--------------|
| `-a`, `--agent` | **Required.** The agent profile to run. There is no default. |
| `-C`, `--directory` | The working directory. Project-local agents, skills, and MCP servers are resolved from here. |
| `-m`, `--mode` | `ask` or `automatic`: who answers when a call asks to reach past the session's confinement. The mode a session starts under; the person running it can change it later, and the change reaches the turn in flight. |
| `--read-only` | Give the session a confinement with nowhere writable. Not a mode: the operating system refuses every write. |
| `-p`, `--project` | The project this session belongs to. |
| `-P`, `--parent` | The session creating this one. Without `--mode` the child inherits the parent's; either way it is clamped to no looser a mode than its parent and is reaped when the parent ends. A parent running `automatic` can only create children that also run `automatic`, because a child that stops to ask would be asking nobody. Defaults to `$SESSION_ID`, which every session exports. |
| `-t`, `--title` | A label for the session list. Unset, the session names itself after its first message. |

This is the **only** place a session's agent and its directory are set, and nothing changes either afterwards. Its permission mode is the one piece that moves. A mistyped `--agent` is refused at creation with the list of profiles that do exist, rather than minting a session that fails when first messaged.

## Sending work

```text
langmesh send <session> <message> [-w|--wait]
```

Pass `-` as the message to read it from stdin, which is how you send a file or a heredoc.

`send` returns as soon as the message is accepted, printing the accepted turn. With `--wait` it follows the session until it goes idle and then prints the last turn, what the session produced.

A message that arrives while the session is mid-turn is **injected into that turn** at its next safe point rather than starting a second one. A session **parked on a decision** takes no message at all, because starting a turn would discard the parked one; `send` reports that and exits non-zero, naming what the session is waiting on:

```console
$ langmesh send "$id" "and check the tests too"
langmesh: not sent — the session is waiting on a permission decision for "cat /srv/app/notes.txt"
```

Answer it with `langmesh allow` (or in the app), then send.

## Watching

| Command | What it does |
|---|---|
| `langmesh ps [-a\|--all]` | What exists. `--all` includes sessions that ended. |
| `langmesh get <session>` | One session in detail. |
| `langmesh tree <session>` | A session and everything it created. |
| `langmesh attach <session>` | Follow it live until you interrupt. |
| `langmesh wait <session>` | Block until idle, then print the result. |
| `langmesh history <session> [-n N]` | Prints the last N turns. |

`ps` prints the session records as a JSON array. Three fields say what a session is, and they are separate because they answer different questions:

| Field | Meaning |
|-------|---------|
| `lifecycle` | Does it still exist? `live` or `ended`. **Durable**, it survives a daemon restart; `outcome` (`exited`/`failed`) and `exit_reason` say how and why. |
| `activity` | What it is doing now: `working`, `waiting` (parked on a decision), `idle`, `asleep` (not hosted; the next message builds its executor in about 60 ms), or `ended`. Derived on every read and never stored. |
| `awaiting_input` | Parked on a permission request or a question. It needs you. |

A session the daemon is not hosting is the normal resting state, not an error. An idle session sleeps immediately, and reads never wake anything.

`attach` prints one JSON object per line as the session streams. Each carries a `kind`: `snapshot`, `history` (one complete compacted turn, newest first), `history_done`, `delta` (a model text or reasoning chunk), `live` (a tool call, tool result, permission request, or other structured event), `turn` (a turn started or ended, which is what `wait` waits for), and `done` (the session ended). It ends when the session does; Ctrl-C stops watching without affecting the session.

```shell
langmesh attach "$id" | jq -r 'select(.kind == "delta") | .chunks[]'
```

## Answering a session

When a session needs permission it parks, `awaiting_input` goes true, and `attach` emits a frame carrying the request and its id. Answer it with that id:

```text
langmesh allow <session> <request>
langmesh deny  <session> <request>
```

There is no "always allow" and no bypass mode: every decision is allow-once or deny.

## Ending a session

```text
langmesh kill <session>
```

Ends the session and everything under it, children first, so a child never observes a dead parent. "Everything under it" means the session's whole **process session**, not just the worker: every shell command it ran, and everything those commands started, carries its session id. A dev server left holding a port goes down with the session that started it. A process survives one way only: it calls `setsid` and leaves the session, at which point it is yours to stop.

## Recurring work

A **schedule** is a prompt written down with a cron line and the settings to run it under. It fires on its own, starting a session each time.

```text
langmesh schedule create <name> --cron <cron> --prompt <text> -a <agent> -w <workspace> -m <mode> [--timezone TZ] [-C DIRECTORY]
langmesh schedule list | show <id> | pause <id> | resume <id> | delete <id> | run <id>
```

`--mode` is **required**, because nobody is watching when this fires. `--timezone` takes an IANA name and defaults to this machine's, so a cron line means the hour you meant. `run` fires a schedule now without moving its next window; `pause` stops it firing without deleting it.

## Agents on other hosts

| Command | What it does |
|---|---|
| `langmesh remote` | The registered remote agents, with their live health. |
| `langmesh remote <name> <message>` | Hand one a message and print what it produced. |

A remote agent is not a session. It runs on someone else's machine, at their cost. It has no shared history and no access to this filesystem. Registered in `~/.agents/remote-agents.json` by card URL, or from **Settings, then Remote agents**. LangMesh refuses a card that redirects to a private or loopback address, unless you opt in with `allow_private`.

## The interface in a browser

| Command | What it does |
|---|---|
| `langmesh serve` | Serves the interface on `http://127.0.0.1:8824`. |
| `langmesh serve --port 9000` | On a different port. |
| `langmesh serve --host 0.0.0.0` | On a different address, read the warning below first. |
| `langmesh serve --open` | Also open a browser at it. Off by default. |

This serves the same interface the desktop app embeds, so a browser is a client like any other. It **proxies** the daemon rather than pointing the browser at it: the page never sees the daemon's capability token, and there is no CORS to configure.

> [!WARNING]
> Whatever can reach this address can drive the daemon, because this server holds the token. It binds `127.0.0.1` for that reason. `--host` exists for tunnelling deliberately; if you use it, put authentication in front.

Needs the interface to have been built (`cd web && bun run build` in a checkout). The packaged build carries it.

## Reaching it from a phone

| Command | What it does |
|---|---|
| `langmesh reach` | Serve the phone's door, and print a pairing code. |
| `langmesh reach pair` | Print the pairing code for a door already open. |
| `langmesh reach rotate` | Mint a new token, unpairing every device. |
| `-p`, `--port` | The loopback port Tailscale proxies to. Default 8825. |
| `--interface [PORT]` | Serve the interface from a running dev server instead of the built export. |

The same proxy as `langmesh serve`, with two differences that let it leave the machine. **It authenticates**: a request without the reach token gets a 401 and never touches the daemon, and websocket handshakes are checked too. **Its token is durable**, kept in `~/.local/share/langmesh/reach-token` rather than minted per boot, so a paired device stays paired across a restart.

It still binds `127.0.0.1`, and no flag changes that. What carries it off the machine is `tailscale serve`, which puts a listener on your tailnet, terminates TLS with a certificate for this machine's `*.ts.net` name, and proxies to that loopback port. Tailscale needs three things on for your tailnet, in this order: **MagicDNS**, **HTTPS Certificates**, then **Serve**. `langmesh reach` refuses to start until they are done and says which one is missing.

> [!WARNING]
> The pairing code carries a bearer token with full control of the daemon. Show it to a phone, not to a room. `langmesh reach rotate` invalidates every device holding the old one.

It serves no browser interface, deliberately: that bundle authenticates by being on the same machine as the daemon, so it carries no reach token. `langmesh serve` is the browser's door; this is the phone's.

## The desktop app

| Command | What it does |
|---|---|
| `langmesh app` | Start the daemon if needed, then launch the app. |

The app is a **client** and contains no daemon. The release app and this command both start the separately installed local daemon when needed, then read the port and token it publishes. The daemon stays independent and continues running when the window quits. It is addressed by bundle identifier rather than by name, so renaming or moving it does not break this. macOS only.

## Serving, and the daemon

| Command | What it does |
|---|---|
| `langmesh serve` | Make LangMesh available: the control plane and the interface in front of it. Runs in the foreground. |
| `langmesh daemon --start` | Start the daemon alone, without an interface. |
| `langmesh daemon status` | What it is running, and where. |
| `langmesh daemon stop` | Stop it, and its sessions' processes with it. |
| `langmesh daemon restart` | Replace it; your sessions survive. |
| `langmesh daemon endpoint` | The loopback port and capability token. |

`serve` makes LangMesh available; `daemon` is the lifecycle of the process behind it. `restart` **keeps your sessions**: each loses its executor and comes back asleep, picking up where it left off on the next message; `sessions_slept` says how many. `stop` and `restart` signal the process group; they do not call the API, because a daemon wedged badly enough to need stopping may not answer its own socket.

`langmesh daemon status` reports whether the daemon is up and how many sessions it knows about, including how many it hosts right now. It never starts anything, because a status check that silently launched the service could never report the absence it was asked about. Pass `--start` if you want that.

`endpoint` prints a secret, which is why it is a verb you ask for rather than something `status` volunteers. It is what you need to point a desktop client at a daemon over SSH: `ssh workstation langmesh daemon endpoint`.

## Configuration

| Command | What it does |
|---|---|
| `langmesh configure --all` | Every setting there is, with its default. |
| `langmesh configure` | Only what you have changed. |
| `langmesh configure agent.permission_mode` | Read one. |
| `langmesh configure agent.permission_mode automatic` | Set one. |
| `langmesh configure agent.permission_mode --unset` | Remove one, back to its default. |

`--all` walks the **schema**, so it lists every setting that exists, not only the ones written down. With no argument it prints a JSON object of dotted path to value for what is actually set. With a setting, it prints that setting's value bare; the explanation and the source go to stderr, so a script reading stdout never has to strip them.

Values are interpreted the way the file holds them: `true`, `8`, and `[]` land as a boolean, a number, and a list. `null` spells null; `none` does not, because it is a real value (`workspace.strategy: none` is the default) — use `--unset` to remove a setting. A name the schema does not define, or a value it would reject, is refused with the reason, and the file is left as it was. Changes apply to what starts **next**. See the [Configuration guide](configuration.md) for what each setting means.

## Output, exit codes, and pipes

**Everything on stdout is plumbing.** A read prints the control plane's payload as JSON. A stream prints one JSON object per line. A verb whose answer is a single value prints that value bare, which is what makes `id=$(langmesh create …)` work. There is no formatting layer, no colour, and no `--json` flag to remember. Pipe through `jq .` when you want it laid out for a person.

Diagnostics go to stderr and outcomes go to the exit code, so neither contaminates the data.

| Exit code | Meaning |
|-----------|---------|
| `0` | Success. |
| `1` | The call failed: no such session, the daemon unreachable, an unknown setting. |
| `2` | The arguments were wrong (argparse). |
| `130` | Interrupted with Ctrl-C. |
| `141` | A pipe closed under it (`langmesh ps \| head`). |

## One turn, without a daemon

```shell
langmesh run "What does this project do?"
langmesh run -C ~/code/project --agent reviewer "What changed on this branch, and is it safe to ship?"
echo "summarise this" | langmesh run -
langmesh run --allow "run the tests and tell me what failed"
```

`run` is the whole harness with none of the control plane. It drives `langmesh.Session` in this process, prints the agent's prose as it arrives, and exits. No session record, no address, no crash isolation: reach for `create` and `send` when you want any of those. `--allow` answers every permission gate with yes; without it, a turn that needs a decision stops and says so. `--json` prints the turn events instead of the prose.

## Signing in

| Command | What it does |
|---|---|
| `langmesh auth login` | Open the browser, sign in to ChatGPT. |
| `langmesh auth status` | Who is signed in, if anyone. |
| `langmesh auth logout` | |

Only ChatGPT works this way; every other provider takes an API key through `langmesh configure`. It is a verb, not a setting, because you cannot type the credential: it is an OAuth exchange that lands on a loopback callback.

## Talking to a session directly

`langmesh` reaches the daemon over its unix socket and posts every command to it, `send` included; the daemon relays to the owning session. You can also address a session yourself. Each session serves [A2A](https://github.com/google/A2A) on `$XDG_RUNTIME_DIR/langmesh/sessions/<id>.sock`, and `create` returns the capability token that authorises driving it. Discovery is open: a session's card at `/.well-known/agent-card.json` says what it is. Every other call must present the token.

That is the whole composition model. A peer is not a special kind of thing; it is a session, addressed the way you address any session.
