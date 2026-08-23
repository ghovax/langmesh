# Development

LangMesh has three parts:

- The **Python image**: two packages — the `langmesh` library (the harness) and the `langmeshd` product (daemon, CLI, REST, worker, dictation) — entered as one executable, `langmesh` or `langmeshd`.
- The **Next.js web UI**, shared with the phone.
- The **Tauri desktop shell**. In development you run the daemon and the UI directly; the packaged app is built only for releases.

## Toolchain

The repo ships a **Nix flake devshell** that pins bun, Rust, `cargo-tauri`, and `pkg-config`. With [direnv](https://direnv.net), `direnv allow` loads it on entry; otherwise run `nix develop`. The Python harness runs from a local virtualenv managed with [uv](https://docs.astral.sh/uv/): `uv sync` creates `.venv` and installs the project and its dependencies.

## Running it

The interface is the surface you work in; the daemon it talks to is started by `langmesh serve` or by the app. To run the daemon in the foreground, the fastest way to watch a traceback, start it by name:

```shell
uv run python -m langmeshd langmeshd
```

- One image, two entry points, chosen by the first argument: `langmesh` (the CLI, whose only verb is serving) and `langmeshd` (the daemon that hosts sessions). A bare launch lands in the CLI.
- A session is an object the daemon builds and holds, not a process; creating one costs about as much as constructing the object, so there is no pool waiting.
- It listens on a unix socket in your runtime directory, and for GUI clients on an ephemeral loopback port. The port, the capability token, the pid, and the lock are published under the runtime directory.

State follows XDG, all of it created on first run: configuration in `~/.config/langmesh/`, durable state in `~/.local/share/langmesh/`, logs in `~/.local/state/langmesh/`. Add provider keys in the configuration file, in the settings panel, or through environment variables. See the [Configuration guide](../user/configuration.md).

## Running the web UI

| Command | What it does |
|---|---|
| `cd web && bun install` | Once. |
| `./scripts/web-development.sh` | Http://localhost:3000, wired to the daemon already running. |

Start the daemon first; the script starts the daemon when one is not already answering and points the page straight at it. Run the script from an **ordinary shell, not inside `nix develop`**: the devshell rewrites `TMPDIR`, the runtime directory hangs off it, and a daemon started outside the devshell is invisible to anything started inside it.

Useful scripts (in `web/`):

- `bun run lint` — lint the UI.
- `bun run tauri:dev` / `bun run tauri:build` — the desktop shell (see below).
- `bun run build` — production static export (runs the event-schema check first).
- `bun run build:events` — regenerate the TypeScript event schema from the Python models (`scripts/generate_event_schema.py`). Run it whenever the event contract changes.

The package layering runs the library first, then the product on top: `langmesh.base` → `langmesh.protocol` → `langmesh.computer` → `langmesh.runtime`, with `langmeshd` hosting it. Three invariants ride on the layering, none of them visible in a diff, so check each by hand when you touch its area:

- **`langmesh/computer/` is never imported at module level** by the runtime. It pulls in PyObjC, which is heavy, and most sessions never drive the screen.
- **Nothing reaches the network at import.** A catalogue fetch or a model-catalogue fetch at module scope would block the daemon's boot behind a stranger's endpoint, so live fetches happen at run time.
- **The runtime keeps no process-wide state.** Nothing under `runtime/` parks a caller's argument in a module global, installs a signal handler, or registers an exit hook; one process may host more than one session.

A new setting is a field on a configuration model, and then three things that are not in the code with it. The schema walk finds the field on its own, so the settings panel has it from the moment it exists. But the panel draws it with **a label and a sentence from `shared/messages/*.json`**, in every locale, and the [configuration reference](../user/configuration.md) needs **a row**.

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
- **Never interpolate an exception into a log message.** An exception's message is human copy, so stapling it onto an event reads as a sentence inside a label. Pass the traceback with `exc_info=True`, or the fields with `langmesh.base.primitives.errors.describe`, and leave the message an event.

## Building and signing

There are **two artifacts**, built independently, because the app is a client of the daemon rather than its container. Building one never rebuilds the other.

- Build the daemon first: `packaging/build-daemon.sh`. It is one image with two entry points, daemon and CLI, and is a no-op when nothing that goes into it has changed; set `FORCE=1` to rebuild anyway.
- Then the desktop app, a Tauri shell with no Python in it at all: `cd web && bun run tauri:build`.
- The first freezes the harness with PyInstaller into `packaging/dist/LangMesh Computer Use.app` and smoke-tests it. The second produces `web/src-tauri/target/release/bundle/macos/LangMesh.app`.
- It does **not** build a disk image for the daemon: installing locally is a `ditto` of the `.app`. Use `bun run tauri:dmg` when you want a disk image for the app.
- The rest of the time is Rust, and it is not incremental: cargo disables incremental compilation for the `release` profile, and the shell is invalidated on every run because `next build` rewrites `web/out`, which Tauri's build script watches. A Python-only change needs `packaging/build-daemon.sh` and a daemon restart, nothing more.

The smoke test runs the frozen daemon under **throwaway XDG directories**, which is load-bearing: with your own directories it would find the lock held by the daemon you already run and stand down, and the probe would then find that daemon's socket answering, a green result for a binary the probe never exercised.

For the full step-by-step with expected output, see [Installation](../user/installation.md#every-step-and-what-you-should-see).

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

## Checks

The repository ships **no unit-test suite**. It ships two guards and one verification harness:

| Command | What it does |
|---|---|
| `uv run ruff check src/` | Lint. |
| `uv run basedpyright` | Type-check the library and daemon as one import graph; a library file that reaches into `langmeshd` is flagged the same way a stale attribute is. |
| `uv run pytest` | The retrieval harness under `tests/retrieval/`, plus any tests you add (`testpaths = ["tests"]`, `asyncio_mode = "auto"`). |
| `cd web && bun run build` | Regenerates and diffs the event schema, then type-checks the UI. |

## Project layout

**`src/langmesh/`** — the library, in import order:

| Module | What lives there |
|---|---|
| `base/` | Configuration, XDG paths, confinement, contracts/ports, persistence, content, identity (providers, credentials), primitives |
| `protocol/` | A2A cards, DTOs, the wire contract, the event schema |
| `computer/` | macOS screen-control bridges: native apps and Chrome |
| `runtime/` | The agent loop, prompts, tools, models, and the plugin seam (`features/`) |
| `__init__.py` | The library surface: `langmesh.Session` and its seams |

**`src/langmeshd/`** — the product, which hosts the library:

| Module | What lives there |
|---|---|
| `commons/` | Configuration I/O, the one sqlite database, state, brokers, services |
| `daemon/` | `langmeshd`: registry, lifecycle, host, machine loaders, agent files, API, scheduler, observation watcher |
| `worker/` | What a hosted session is made of: its executor, its verbs, its turn loop, peers |
| `rest/` | The FastAPI surface the browser uses |
| `cli/` | The `langmesh serve` command |
| `dictation/` | The local speech transcriber |
| `features.py` | The daemon's plugin assembly: which features a hosted session runs |
| `__main__.py` | argv dispatch: `langmesh`, `langmeshd` |

**Everything else:**

| Path | What lives there |
|---|---|
| `.agents/` | Bundled agents, skills, and MCP server configuration |
| `shared/` | The renderer-free catalogue both clients read: messages, labels, the generated wire event union |
| `web/` | The desktop app: Next.js UI, and the Tauri shell in `src-tauri/` |
| `mobile/` | The phone client: an Expo WebView onto the same interface |
| `.github/` | The `@langmesh` mention Action; `GitHubReply` and real prompts live under `src/langmesh/github/` |
| `packaging/` | PyInstaller spec and signing, plus `entry.py` for the frozen build |
| `scripts/` | Event-schema generation and the web dev script |
| `examples/` | Example MCP servers |

## Building the documentation

The published site is built with MkDocs Material. Install and serve the locked documentation environment with `uv run --group docs mkdocs serve`; verify every link and API reference with `uv run --group docs mkdocs build --strict`.

The library is the core release. Start with the [library quickstart](../library/index.md), then use the product guides for [installation](../user/installation.md), the [command line](../user/installation.md), or the [desktop app](../user/app.md).
