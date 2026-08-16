# Development

LangMesh has three parts:

- The **Python image**: one executable, entered as `langmesh` or `langmeshd`. It carries the harness.
- The **Next.js web UI**.
- The **Tauri desktop shell**. In development you run the daemon and the UI directly; the packaged app is built only for releases.

## Toolchain

The repo ships a **Nix flake devshell** that pins bun, Rust, `cargo-tauri`, and `pkg-config`. With [direnv](https://direnv.net), `direnv allow` loads it on entry; otherwise run `nix develop`. The Python harness runs from a local virtualenv managed with [uv](https://docs.astral.sh/uv/): `uv sync` creates `.venv` and installs the project and its dependencies.

## Running it

The CLI starts the daemon on its first command, so usually there is nothing to launch:

```shell
uv run langmesh create --agent general-assistant --directory ~/code/project
uv run langmesh send "$id" "What does this project do?" --wait
```

- The [`langmesh` command](cli.md) is the full surface. To run the daemon in the foreground, the fastest way to watch a traceback, start it by name: `uv run python -m langmesh langmeshd`.
- One image, two entry points, chosen by the first argument: `langmesh` (the CLI) and `langmeshd` (the daemon that hosts sessions). A bare launch lands in the CLI. `langmesh daemon stop` takes down a foreground daemon and its sessions with it.
- A session is an object the daemon builds and holds, not a process; creating one costs about as much as constructing the object, so there is no pool waiting.
- It listens on a unix socket in your runtime directory, and for GUI clients on an ephemeral loopback port. `langmesh daemon endpoint` reports the port and the capability token.

State follows XDG, all of it created on first run: configuration in `~/.config/langmesh/`, durable state in `~/.local/share/langmesh/`, logs in `~/.local/state/langmesh/`. Add provider keys with `langmesh configure`, in the configuration file, or through environment variables. See the [Configuration guide](configuration.md).

## Running the web UI

| Command | What it does |
|---|---|
| `cd web && bun install` | Once. |
| `./scripts/web-development.sh` | Http://localhost:3000, wired to the daemon already running. |

Start the daemon first; the script starts a local bridge on port 8824 when one is not already answering, and the development page reads that stable address from its runtime descriptor. Run the script from an **ordinary shell, not inside `nix develop`**: the devshell rewrites `TMPDIR`, the runtime directory hangs off it, and a daemon started outside the devshell is invisible to anything started inside it.

Useful scripts (in `web/`):

- `bun run lint` — lint the UI.
- `bun run tauri:dev` / `bun run tauri:build` — the desktop shell (see below).
- `bun run build` — production static export (to `web/out`).
- `bun run build:events` — regenerate the TypeScript event schema from the Python models (`scripts/generate_event_schema.py`). Run it whenever the event contract changes.

Outside `web/`, the package layering runs `base`, then `protocol`, then `computer`/`locations`, then `runtime`, then `worker`; the daemon sits on top and hosts the sessions. Three invariants ride on the layering, none of them visible in a diff, so check each by hand when you touch its area:

- **`computer/` is never imported at module level.** It pulls in PyObjC, which is heavy, and most sessions never drive the screen.
- **Nothing reaches the network at import.** A catalogue fetch at module scope blocks the daemon's boot behind a stranger's endpoint.
- **The runtime keeps no process-wide state.** Nothing under `runtime/` parks a caller's argument in a module global, installs a signal handler, or registers an exit hook; one process may host more than one session.

A new setting is a field on the configuration model, and then three things that are not in the code with it. The schema walk finds the field on its own, so `langmesh configure` and the settings panel both have it from the moment it exists. But the panel draws it with **a label and a sentence from `shared/messages/*.json`**, in every locale, and the [configuration reference](configuration-reference.md) needs **a row**.

## Running the desktop app in dev

| Command | What it does |
|---|---|
| `langmesh serve` | Starts the checkout daemon the development app should use. |
| `cd web` | |
| `bun run tauri:dev` | Launches the Tauri window against the dev UI. |

Start the checkout daemon before the development window so testing never silently switches to the installed production build. Automatic local startup belongs only to a release app.

## Logs and copy

Two vocabularies, and they are not the same thing.

- **A log message is an event name.** Lowercase, no terminal punctuation; the facts go in fields, not the sentence. Acronyms and proper nouns keep their capitals wherever they fall, because those are spellings rather than casing.
- **Human copy is prose.** The interface catalog, an `HTTPException` `detail`, an `RpcError`, CLI output: sentence case with terminal punctuation. A fragment used as a label or a chip stays lowercase.
- **Never interpolate an exception into a log message.** An exception's message is human copy, so stapling it onto an event reads as a sentence inside a label. Pass the traceback with `exc_info=True`, or the fields with `langmesh.base.errors.describe`, and leave the message an event.

## Building and signing

There are **two artifacts**, built independently, because the app is a client of the daemon rather than its container. Building one never rebuilds the other.

- Build the daemon first: `packaging/build-daemon.sh`. It is one image with two entry points, daemon and CLI, and is a no-op when nothing that goes into it has changed; set `FORCE=1` to rebuild anyway.
- Then the desktop app, a Tauri shell with no Python in it at all: `cd web && bun run tauri:build`.
- The first freezes the harness with PyInstaller into `packaging/dist/LangMesh Computer Use.app` and smoke-tests it. The second produces `web/src-tauri/target/release/bundle/macos/LangMesh.app`.
- It does **not** build a disk image: installing locally is a `ditto` of the `.app`. Use `bun run tauri:dmg` when you want one to hand out.
- The rest of the time is Rust, and it is not incremental: cargo disables incremental compilation for the `release` profile, and the shell is invalidated on every run because `next build` rewrites `web/out`, which Tauri's build script watches. A Python-only change needs `packaging/build-daemon.sh` and a daemon restart, nothing more.

The smoke test runs the frozen daemon under **throwaway XDG directories**, which is load-bearing: with your own directories it would find the lock held by the daemon you already run and stand down, and the probe would then find that daemon's socket answering, a green result for a binary the probe never exercised.

For the full step-by-step with expected output, see [Installation](installation.md#every-step-and-what-you-should-see).

### Stable code-signing

The screen-control tools (`control_screen`) need the macOS **Accessibility** grant, which is tied to code identity. Every session runs inside the daemon for exactly this reason, so one grant covers the fleet. Both artifacts carry the same `CFBundleName` and identifier, so signing both with one persistent identity keeps them a single **LangMesh** row that survives rebuilds:

- Create the self-signed identity in your login keychain once: `packaging/create-signing-certificate.sh`.
- Sign after each build, either artifact or both: `packaging/sign-app.sh "packaging/dist/LangMesh Computer Use.app"` and `packaging/sign-app.sh web/src-tauri/target/release/bundle/macos/LangMesh.app`.
- The daemon is signed `--deep` with `packaging/Entitlements.plist`; the app needs no entitlements, so it signs plain. The identity is self-signed, so Gatekeeper still warns on other machines until a build is Apple-notarized.

### Installing the daemon

```shell
ditto "packaging/dist/LangMesh Computer Use.app" "/Applications/LangMesh Computer Use.app"
ln -sf "/Applications/LangMesh Computer Use.app/Contents/MacOS/langmesh" /usr/local/bin/langmesh
```

The symlink is what puts `langmesh` and `langmeshd` on your `PATH`, both entering the same signed image. Running from a checkout (`uv run langmesh …`) works for everything except a stable Accessibility grant, since the interpreter is then the code identity.

## Tests

The repository ships **no unit-test suite**. It ships a **verification battery** instead: specific, falsifiable claims the architecture rests on, each checked by doing it.

| Command | What it does |
|---|---|
| `uv run ruff check src/ scripts/` | Lint. |
| `cd web && bun run build` | Regenerates and diffs the event schema, then type-checks. |

Each stage gets its own temporary XDG roots and its own daemon, and cleans up after itself, so a run touches nothing of yours. Exit status is the number of failures. Two stages need a real machine and are skipped elsewhere, which is the reason to run this locally at least once: `macos-confinement` answers the questions only macOS can answer (does the Accessibility grant cover the daemon, and does `sandbox-exec` still confine a tool child), and counts threads with mach.

Beyond the battery: lint with `uv run ruff check`, and drive the affected path through the CLI directly. `pyproject.toml` is already set up for `pytest` (`testpaths = ["tests"]`, `asyncio_mode = "auto"`), so a `tests/` directory is picked up if you add one.

## Project layout

**`src/langmesh/`** — the Python image, in import order:

| Module | What lives there |
|---|---|
| `base/` | Configuration, XDG paths, skills, ports, the catalogue |
| `protocol/` | A2A cards, DTOs, the wire contract |
| `computer/` | macOS screen-control bridges: native apps and Chrome |
| `locations/` | Where files live: local, SSH, containers |
| `runtime/` | The agent loop, prompts, tools, models |
| `worker/` | What a session is made of: its executor, its verbs, its turn loop |
| `__init__.py` | The library surface: `langmesh.Session` and its seams |
| `workspace/` | Projects, locations, settings, terminals |
| `daemon/` | `langmeshd`: registry, lifecycle, the session host, machine loaders |
| `rest/` | The REST surface the browser uses; never imports `daemon` |
| `cli/` | The `langmesh` command and its renderers |
| `__main__.py` | argv dispatch: `langmesh`, `langmeshd` |

**Everything else:**

| Path | What lives there |
|---|---|
| `.agents/` | Bundled agents, skills, memories, and MCP server configuration |
| `web/` | The desktop app: Next.js UI, and the Tauri shell in `src-tauri/` |
| `packaging/` | PyInstaller freeze and signing, plus `entry.py` for the frozen build |
| `scripts/` | Layering, import and translation checks; the verification battery |
| `examples/` | Example MCP servers |
