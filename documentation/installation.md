# Installation

LangMesh targets **macOS on Apple Silicon (`aarch64`)**. The screen-control tools (`control_screen`) and the packaged app are macOS-specific. The harness itself is portable Python, but the desktop experience is built for the Mac.

## Option 1: download the app

1. Open the [**Releases**](https://github.com/ghovax/langmesh/releases) page and download the latest `LANGMESH_<version>_aarch64.dmg`.
2. Open the `.dmg` and drag **LangMesh** into **Applications**.
3. Launch it.

### Gatekeeper

The app is **self-signed, not Apple-notarized**, so macOS Gatekeeper refuses the first launch with an "unidentified developer" or "damaged" message. This is expected. Clear it once, either way:

- Right-click `LangMesh.app`, choose **Open**, then **Open** again in the dialog, or
- From a terminal:

  ```shell
  xattr -dr com.apple.quarantine /Applications/LangMesh.app
  ```

Notarized builds are planned. Until then this one-time step is required.

### Permissions the app may ask for

- **Accessibility** is required for the screen-control tools (`control_screen`) to read and act on native apps. LangMesh prompts you and deep-links to the right settings pane. Grant it to LangMesh.
- **Chrome remote debugging** is required for the screen-control tools to drive your own Chrome. LangMesh shows a one-click prompt that opens `chrome://inspect`. Enable the remote-debugging toggle once.

Neither is needed for plain chat or the file, shell, and web tools.

## Option 2: build from source

LangMesh is **two artifacts**, built independently, because the app is a *client* of the daemon rather than its container. The daemon bundle carries the harness, the `langmesh` command, and `langmeshd`, which hosts every session, in one signed image. The app is a window that finds a daemon and talks to it. Build them in either order; neither build triggers the other.

You need [Nix](https://nixos.org) (the flake devshell pins everything else, `uv` included) and optionally [direnv](https://direnv.net).

### Every step, and what you should see

| # | Run | What it does | You should see | Takes |
|---|---|---|---|---|
| 1 | `git clone https://github.com/ghovax/langmesh.git && cd langmesh` | | | seconds |
| 2 | `direnv allow` or `nix develop` | Loads uv, bun, rustc, cargo, cargo-tauri, pkg-config | `dev env loaded: uv 0.x, bun 1.x, rustc 1.x` | first time, minutes |
| 3 | `uv sync` | Creates `.venv` with the project and the dev group, so PyInstaller arrives here | Resolution and install log | about 1 minute |
| 4 | `cd web && bun install && cd ..` | UI dependencies | | about 1 minute |
| 5 | `packaging/build-daemon.sh` | Freezes the harness, then smoke-tests it in an isolated set of XDG directories | `freezing the harness…`, then `ok: langmeshd answers on its own socket` | several minutes |
| 6 | `packaging/create-signing-certificate.sh` (once per machine) | Makes the persistent identity "LangMesh Local Codesign" | A keychain prompt | seconds |
| 7 | `packaging/sign-app.sh "packaging/dist/LangMesh Computer Use.app"` | Signs the daemon `--deep` with its entitlements | `signed …`, then `Identifier=` and `Authority=` | seconds |
| 8 | `ditto "packaging/dist/LangMesh Computer Use.app" "/Applications/LangMesh Computer Use.app"` | Installs the harness | | seconds |
| 9 | `ln -sf "/Applications/LangMesh Computer Use.app/Contents/MacOS/langmesh" /usr/local/bin/langmesh` | Puts `langmesh` and `langmeshd` on your `PATH` | May need `sudo` | seconds |
| 10 | `cd web && bun run tauri:build` | Rust compile plus a static export. No Python in this step | `LangMesh.app` and a `.dmg` under `web/src-tauri/target/release/bundle/` | first time, about 10 minutes |
| 11 | `packaging/sign-app.sh web/src-tauri/target/release/bundle/macos/LangMesh.app` | Signs the app plainly with the same identity, so both fold into one Accessibility row | `signed …` | seconds |
| 12 | `ditto` that `LangMesh.app` to `/Applications` | Installs the window | | seconds |
| 13 | Open `LangMesh.app` | Starts the daemon and opens the window | First run seeds `~/.config/langmesh/configuration.yaml` | seconds |
| 14 | Add a provider key under **Settings → Providers** (or in `~/.config/langmesh/configuration.yaml`) | A model to run on | | |

### Things that will catch you

| Symptom | Cause | Fix |
|---|---|---|
| The app opens but only ever shows the connection picker | The separately installed daemon bundle is missing or could not start | Install `LangMesh Computer Use.app`, then reopen LangMesh; the daemon's failure is in its log under the state directory |
| Computer control keeps asking for Accessibility after every rebuild | The daemon serving you is the checkout's (`uv run langmesh`), whose code identity is the Python interpreter, not the signed image | The daemon's status reports `image.frozen`. If it is `false`, stop that daemon and start the installed one |
| Two `langmesh` on your `PATH` behave differently | The checkout's and the installed one share `~/.config/langmesh/` and the runtime directory, so whichever daemon started first owns it | `which -a langmesh`, and check `image.executable` in the daemon's status |
| `ln -sf … /usr/local/bin/langmesh` is denied | `/usr/local/bin` is root-owned | `sudo ln -sf …`, or symlink into `~/.local/bin` and put that on `PATH` |
| `packaging/build-daemon.sh` says "daemon up to date" after you changed something | The freshness guard decided nothing that goes into the freeze had changed | `FORCE=1 packaging/build-daemon.sh` |

Signing (steps 6, 7, 11) is optional for a build that only runs. It is necessary for a **stable Accessibility grant**: without it, every rebuild is a new code identity and macOS asks again.

Both artifacts carry the same `CFBundleName` and identifier, so one certificate over both keeps them a single **LangMesh** row. See the [Development guide](development.md#building-and-signing).
