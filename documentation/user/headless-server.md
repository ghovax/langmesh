# Run LangMesh on a server

The harness is a Python library plus a daemon; nothing about the daemon requires the machine it
runs on to have a screen. `langmeshd` will run headless on a low-end Linux VPS — a single core
and a gigabyte of RAM is plenty — and that is how you give real, always-on cloud agents a home:
the compute, the files, and the credentials live on the VPS, and your desktop stays a client.

What does **not** work on a headless Linux box are the macOS-only parts: the desktop app, and the
screen-control tools. Everything an agent does with a shell, the filesystem, the network, MCP
servers, peer sessions, goals, and its durable history is fully supported.

## Install

Python 3.13 and `uv` are the only requirements.

```sh
curl -LsSf https://astral.sh/uv/install.sh | sh
uv tool install langmesh
```

That puts `langmesh` (the CLI, which only serves) and `langmeshd` (the daemon) on your `PATH`.
Alternatively install in a virtualenv: `uv venv && uv pip install langmesh`.

## Run it as a service

```ini
# /etc/systemd/system/langmeshd.service
[Unit]
Description=LangMesh agent daemon
After=network.target

[Service]
ExecStart=/root/.local/bin/langmeshd
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

```sh
sudo systemctl daemon-reload
sudo systemctl enable --now langmeshd
```

On first boot the daemon seeds `~/.config/langmesh/configuration.yaml`. Add a provider key there
(or set it as an environment variable, which wins over the file), and give the server's user the
`.agents/` tree your agents and skills live in.

## Reach it

The daemon binds loopback and guards itself with a capability token. Carry it off the machine
with a transport you choose:

- **SSH tunnel.** Forward the daemon's port to your laptop: `ssh -L 8823:127.0.0.1:8823 vps`. The
  port the daemon publishes is written under its runtime directory; the token sits beside it.
- **Tailscale.** Install Tailscale on the VPS and on your laptop, then point the desktop app's
  connect flow at the machine's tailnet address.

A remote agent created on the server is a normal session: it keeps its transcript, its goals, and
its approvals, and it is reachable from anywhere you can reach the daemon.

## Keep it small

- The daemon owns one `sqlite` database and the conversation history; a low-end VPS has room for
  thousands of sessions.
- Set the `LANGMESH` XDG state directories if you want them under `/srv` rather than `/root`.
- The daemon is the single process that needs to stay up; everything else (the app, `serve`) is a
  client you can close and reopen.
