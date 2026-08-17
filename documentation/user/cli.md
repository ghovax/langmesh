# The `langmesh` command

`langmesh` has one task: **serve** — make LangMesh available over HTTP, with the daemon behind it. Everything else a person does with the harness happens in the interface (the desktop app, or the browser the serve command exposes) or over the daemon's API.

```shell
langmesh serve
```

| Flag | What it does |
|---|---|
| `-p`, `--port` | Port to listen on. Default `8824`. |
| `--host` | Address to bind. Default `127.0.0.1`. |
| `--open` | Also open a browser at the served address. Off by default. |

This serves the same interface the desktop app embeds, so a browser is a client like any other. It **proxies** the daemon rather than pointing the browser at it: the page never sees the daemon's capability token, and there is no CORS to configure. `serve` starts the daemon if it is not running, and stops a daemon it started when it exits; a daemon someone else was already running is left alone.

> [!WARNING]
> Whatever can reach this address can drive the daemon, because this server holds the token. It binds `127.0.0.1` for that reason. `--host` exists for tunnelling deliberately; if you use it, put authentication in front.

Needs the interface to have been built (`cd web && bun run build` in a checkout). The packaged build carries it.

## The daemon

The daemon itself is `langmeshd` (`python -m langmesh langmeshd`), a separate process the interface talks to. `serve` and the desktop app start it when needed; it keeps running when the interface window or serve process goes away. Its status and endpoint are reported by the interface, or read from the state directory it publishes (`port` and `token` under the runtime directory).

## What is not here

There are no session, configuration, or account verbs. Creating and messaging sessions, answering permission requests, recurring work, remote agents, configuration, and sign-in all happen in the interface, or programmatically against the daemon's API. A session composes with its peers through [tools](tools.md), over the same control plane; it does not shell out to this command.

## Output and exit codes

Diagnostics go to stderr; the exit code carries the outcome.

| Exit code | Meaning |
|-----------|---------|
| `0` | Served, then exited normally. |
| `1` | The interface could not be served (not built, port taken, daemon failed to start). |
| `130` | Interrupted with Ctrl-C. |
| `141` | A pipe closed under it. |
