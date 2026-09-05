# Installation

LangMesh targets **macOS on Apple Silicon (`aarch64`)**. The screen-control tools (`control_screen`) and the desktop app are macOS-specific. The harness itself is portable Python, but the desktop experience is built for the Mac.

## Build from source

LangMesh is **two artifacts**, built independently, because the app is a *client* of the daemon rather than its container. The packaged daemon bundle carries the harness, the `langmesh` command, and `langmeshd` — one frozen binary entered two ways — in one signed image. The app is a window that finds a daemon and talks to it. Build them in either order; neither build triggers the other.

You need [Nix](https://nixos.org) (the flake devshell pins everything else, `uv` included) and optionally [direnv](https://direnv.net).

### Build checklist

| #   | Run                                                                                                | What it does                                                                          | You should see                                                           | Takes                        |
| --- | -------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------- | ------------------------------------------------------------------------ | ---------------------------- |
| 1   | `git clone https://github.com/ghovax/langmesh.git && cd langmesh`                                  |                                                                                       |                                                                          | seconds                      |
| 2   | `direnv allow` or `nix develop`                                                                    | Loads uv, bun, rustc, cargo, cargo-tauri, pkg-config                                  | `dev env loaded: uv 0.x, bun 1.x, rustc 1.x`                             | first time, minutes          |
| 3   | `uv sync`                                                                                          | Creates `.venv` with the project and the dev group, so PyInstaller arrives here       | Resolution and install log                                               | about 1 minute               |
| 4   | `cd web && bun install && cd ..`                                                                   | UI dependencies                                                                       |                                                                          | about 1 minute               |
| 5   | `packaging/build-daemon.sh`                                                                        | Freezes the harness, then smoke-tests it in an isolated set of XDG directories        | `freezing the harness…`, then `ok: langmeshd answers on its own socket`  | several minutes              |
| 6   | `packaging/create-signing-certificate.sh` (once per machine)                                       | Makes the persistent identity "LangMesh Local Codesign"                               | A keychain prompt                                                        | seconds                      |
| 7   | `packaging/sign-app.sh "packaging/dist/LangMesh Computer Use.app"`                                 | Signs the daemon `--deep` with its entitlements                                       | `signed …`, then `Identifier=` and `Authority=`                          | seconds                      |
| 8   | `ditto "packaging/dist/LangMesh Computer Use.app" "/Applications/LangMesh Computer Use.app"`       | Installs the harness                                                                  |                                                                          | seconds                      |
| 9   | `ln -sf "/Applications/LangMesh Computer Use.app/Contents/MacOS/langmesh" /usr/local/bin/langmesh` | Puts the frozen `langmesh` command on your `PATH`; its `langmeshd` entry point is selected by the first argument | May need `sudo` | seconds |
| 10  | `cd web && bun run tauri:build`                                                                    | Rust compile plus a static export. No Python in this command                          | `LangMesh.app` and a `.dmg` under `web/src-tauri/target/release/bundle/` | first time, about 10 minutes |
| 11  | `packaging/sign-app.sh web/src-tauri/target/release/bundle/macos/LangMesh.app`                     | Signs the app plainly with the same identity, so both fold into one Accessibility row | `signed …`                                                               | seconds                      |
| 12  | `ditto` that `LangMesh.app` to `/Applications`                                                     | Installs the window                                                                   |                                                                          | seconds                      |
| 13  | Open `LangMesh.app`                                                                                | Starts the daemon and opens the window                                                | First run seeds `~/.config/langmesh/configuration.yaml`                  | seconds                      |
| 14  | Add a provider key under **Settings → Providers** (or in `~/.config/langmesh/configuration.yaml`)  | A model to run on                                                                     |                                                                          |                              |

### Things that will catch you

| Symptom                                                                          | Cause                                                                                                                                 | Fix                                                                                                                     |
| -------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------- |
| The app opens but this machine's daemon never answers                            | The separately installed daemon bundle is missing or could not start                                                                  | Install `LangMesh Computer Use.app`, then reopen LangMesh; the daemon's failure is in its log under the state directory |
| Computer control keeps asking for Accessibility after every rebuild              | The daemon serving you is the checkout's (`uv run langmesh`), whose code identity is the Python interpreter, not the signed image     | The daemon's status reports `image.frozen`. If it is `false`, stop that daemon and start the installed one              |
| Two `langmesh` on your `PATH` behave differently                                 | The checkout's and the installed one share `~/.config/langmesh/` and the runtime directory, so whichever daemon started first owns it | `which -a langmesh`, and check `image.executable` in the daemon's status                                                |
| `ln -sf … /usr/local/bin/langmesh` is denied                                     | `/usr/local/bin` is root-owned                                                                                                        | `sudo ln -sf …`, or symlink into `~/.local/bin` and put that on `PATH`                                                  |
| `packaging/build-daemon.sh` says "daemon up to date" after you changed something | The freshness guard decided nothing that goes into the freeze had changed                                                             | `FORCE=1 packaging/build-daemon.sh`                                                                                     |

The certificate and signing commands are optional for a build that only runs. They are necessary for a **stable Accessibility grant**: without it, every rebuild is a new code identity and macOS asks again.

### Gatekeeper

The locally built app is **self-signed, not Apple-notarized**, so macOS Gatekeeper may refuse its first launch with an "unidentified developer" or "damaged" message. Clear it once by right-clicking `LangMesh.app`, choosing **Open**, and choosing **Open** again, or run:

```shell
xattr -dr com.apple.quarantine /Applications/LangMesh.app
```

### Permissions the app may ask for

- **Accessibility** is required for the screen-control tools (`control_screen`) to read and act on native apps. LangMesh prompts you and deep-links to the right settings pane. Grant it to LangMesh.
- **Chrome remote debugging** is required for the screen-control tools to drive your own Chrome. LangMesh shows a one-click prompt that opens `chrome://inspect`. Enable the remote-debugging toggle once.

Neither is needed for plain chat or the file, shell, and web tools.

Both artifacts carry the same `CFBundleName` and identifier, so one certificate over both keeps them a single **LangMesh** row. See the [Development guide](../internal/development.md#building-and-signing).

## The `langmesh` command

`langmesh` has two long-running clients: **serve** makes the interface available over HTTP with the daemon behind it, and **mail** IDLEs a mailbox and drives the same daemon as a client. Everything else a person does with the harness happens in the interface (the desktop app, or the browser the serve command exposes) or over the daemon's API.

```shell
langmesh serve
langmesh mail
langmesh mail check
```

`mail check` proves configuration and secret files, IMAP login, and SMTP auth without IDLEing or starting the daemon. See [Email](email.md).

| Flag           | What it does                                                                                                                                                                 |
| -------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `-p`, `--port` | Port to listen on. Default `8824` (`8825` with `--reach`).                                                                                                                   |
| `--host`       | Address to bind. Default `127.0.0.1`.                                                                                                                                        |
| `--open`       | Also open a browser at the served address. Off by default.                                                                                                                   |
| `--reach`      | Serve the paired door: a durable pairing token, a `langmesh://pair#…` link to scan, and every request gated by the token. For your own devices, over a transport you choose. |

This serves the same interface the desktop app embeds, so a browser is a client like any other. It **proxies** the daemon rather than pointing the browser at it: the page never sees the daemon's capability token, and there is no CORS to configure. `serve` starts the daemon if it is not running, and stops a daemon it started when it exits; a daemon someone else was already running is left alone.

> [!WARNING] Whatever can reach this address can drive the daemon, because this server holds the token. It binds `127.0.0.1` for that reason. `--host` exists for tunnelling deliberately; if you use it, put authentication in front.

**The paired door (`--reach`) is for your devices only.** It prints a `langmesh://pair#…` link carrying the address and a durable token; a phone scans or pastes it and is then the only thing the door answers to. What carries the door off the machine is a transport you choose — `tailscale serve` terminates TLS at your `*.ts.net` name and proxies to the loopback port; an SSH tunnel is the other common path — so the outer path is yours and the pairing token the inner one. Nothing binds past loopback, and deleting the pairing token unpairs every device.

Needs the interface to have been built (`cd web && bun run build` in a checkout). The packaged build carries it.

### The daemon

The daemon itself is `langmeshd`: in the packaged macOS image, it is the `langmesh` executable entered with `langmeshd` as its first argument; in a Python checkout, run `python -m langmeshd langmeshd`. It is a separate process the interface talks to. `serve` and the desktop app start it when needed; it keeps running when the interface window or serve process goes away. Its status and endpoint are reported by the interface, or read from the files it publishes into the runtime directory (`port`, `token`, `pid`, and the unix `socket`).

### What is not here

There are no session, configuration, or account verbs. Creating and messaging sessions, answering permission requests, recurring work, remote agents, configuration, and sign-in all happen in the interface, or programmatically against the daemon's API — except `mail`, which is the IMAP/SMTP client described in [Email](email.md). A session composes with its peers through [tools](agent-system.md), over the same control plane; it does not shell out to this command.

### Output and exit codes

Diagnostics go to stderr; the exit code carries the outcome.

| Exit code | Meaning                                                                                                        |
| --------- | -------------------------------------------------------------------------------------------------------------- |
| `0`       | Served, then exited normally.                                                                                  |
| `1`       | The interface could not be served (not built, port taken, daemon failed to start), or mail was not configured. |
| `130`     | Interrupted with Ctrl-C.                                                                                       |
| `141`     | A pipe closed under it.                                                                                        |

## Run LangMesh on a server

The harness is a Python library plus a daemon; nothing about the daemon requires the machine it runs on to have a screen. `langmeshd` will run headless on a low-end Linux VPS — a single core and a gigabyte of RAM is plenty — and that is how you give real, always-on cloud agents a home: the compute, the files, and the credentials live on the VPS, and your desktop stays a client.

What does **not** work on a headless Linux box are the macOS-only parts: the desktop app, and the screen-control tools. Everything an agent does with a shell, the filesystem, the network, MCP servers, peer sessions, goals, and its durable history is fully supported.

### Install

Python 3.13 and `uv` are the only requirements. Build from source on the server, since the packaged bundles are macOS images:

```sh
curl -LsSf https://astral.sh/uv/install.sh | sh
git clone https://github.com/ghovax/langmesh.git && cd langmesh
uv sync
```

That installs the `langmesh` CLI into the project's `.venv`. The Python daemon entry point is `uv run python -m langmeshd langmeshd`; the packaged macOS image exposes both roles through the frozen `langmesh` executable.

### Run it as a service

```ini
## /etc/systemd/system/langmeshd.service
[Unit]
Description=LangMesh agent daemon
After=network.target

[Service]
ExecStart=/srv/langmesh/.venv/bin/python -m langmeshd langmeshd
Restart=on-failure
WorkingDirectory=/srv/langmesh
Environment=XDG_CONFIG_HOME=/srv/langmesh/xdg/config
Environment=XDG_DATA_HOME=/srv/langmesh/xdg/data
Environment=XDG_STATE_HOME=/srv/langmesh/xdg/state
Environment=XDG_CACHE_HOME=/srv/langmesh/xdg/cache

[Install]
WantedBy=multi-user.target
```

```sh
sudo systemctl daemon-reload
sudo systemctl enable --now langmeshd
```

On first boot the daemon seeds `~/.config/langmesh/configuration.yaml`. Add a provider key as a secret file under `$XDG_DATA_HOME/langmesh/secrets/`, and give the server's user the `.agents/` tree your agents and skills live in.

### Reach it

The daemon binds loopback and guards itself with a capability token. Carry it off the machine with a transport you choose:

- **SSH tunnel.** Forward the daemon's port to your laptop. The port the daemon publishes is written under its runtime directory; the token sits beside it.
- **Tailscale.** Install Tailscale on the VPS and on your laptop, then pair from **Settings, then Connection** using the `langmesh://pair#…` link `langmesh serve --reach` prints at the machine's tailnet address.

A remote agent created on the server is a normal session: it keeps its transcript, its goals, and its approvals, and it is reachable from anywhere you can reach the daemon.

### Email in front of the daemon

Mail is a second long-running client, not a second daemon. `langmesh mail` IDLEs an allowlisted mailbox, strips quoted reply history, and drives `session.create` / `session.send` on loopback. Replies go out over SMTP in the same thread. See [Email](email.md). Fill `configuration.yaml` (`email.address`, `email.machine`, `email.allow_from`) and the secret files, run `uv run langmesh mail check` until it prints `ready`, then either `uv run langmesh mail` on this machine or, on a VPS, install both systemd units so the mail client comes back with the daemon. A new thread is addressed to `local+machine@domain`, not the untagged mailbox.

```sh
sudo packaging/mail/install.sh
```

The script writes `/etc/systemd/system/langmeshd.service` and `langmesh-mail.service`, copies policy and secrets onto `/srv/langmesh/xdg`, then enables them. Pass `--prefix DIR` to install somewhere other than `/srv/langmesh`.

### Keep it small

- The daemon owns one `sqlite` database and the conversation history; a low-end VPS has room for thousands of sessions.
- Set the XDG directories if you want them under `/srv` rather than `/root`.
- The daemon is the process sessions live in; the app and `serve` are clients you can close and reopen. `langmesh mail` must stay up for IDLE, but unfinished jobs are on disk and resume when it comes back.

## `@claude` on GitHub

[Install the LangMesh Agent GitHub App](https://github.com/apps/langmesh-agent) on a personal account or organization, then configure its provider, model, and API key through the App setup page. Opening an issue or pull request starts the first response; later comments trigger a response only when they contain the standalone mention `@claude`, regardless of casing, or reply to the bot. Other words, names, aliases, and App logins are ignored. Repositories need no workflow, YAML policy, App ID, provider setting, API key, or secret. On an issue it can open a draft pull request; on a pull request it updates that branch. See [Universal GitHub App](github.md).
