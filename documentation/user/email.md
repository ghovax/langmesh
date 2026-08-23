# Email

A message to a configured mailbox starts a daemon session. A reply in that thread continues it. The mail process is a **client** of `langmeshd`, the same way the desktop app is: it talks over the daemon's unix socket with the capability token, and it never embeds a library `Session`. Quoted reply history is stripped before the turn, so each inbound message is that email's own content.

This is not XMPP and not the GitHub mention Action. GitHub mentions run the library in a short-lived job. Mail sits in front of a long-running daemon on a machine that stays up — typically a small Linux VPS.

## What happens

1. `langmesh mail` starts the daemon if it is not up, then IDLEs the mailbox (`aioimaplib`, RFC 2177). It does not poll from inside a running turn.
2. A new UNSEEN message from an allowlisted sender is fetched. Automatic replies, bounces, and mail from the mailbox itself are ignored.
3. `email-reply-parser` keeps this message's body and drops the quoted thread. HTML-only mail is reduced with `markdownify` first.
4. The thread is keyed as `email:{mailbox}:{root-message-id}` from `References` / `In-Reply-To` / `Message-ID`. That key maps to one daemon session, stored under `$XDG_DATA_HOME/langmesh/mail-threads.sqlite`.
5. `session.create` mints the session on the first mail in a thread (`permission_mode: automatic` by default). Later mail on the same thread reuses it. `session.send` with `serialize: true` waits for any in-flight turn, then starts this email as its own turn.
6. When the turn ends, `aiosmtplib` sends the assistant's visible text as an in-thread SMTP reply (`In-Reply-To` and `References` set).

Later mail waits in the inbox until the current message for that thread is finished. A different thread can be next in the drain; one IMAP session fetches serially so FETCH never races IDLE.

## Pause, idle, reboot

Cheap VPS hosts suspend. Containers get paused or replaced. The mail client is written for that:

- Every inbound message is a sqlite job under `$XDG_DATA_HOME/langmesh/mail-threads.sqlite` **before** IMAP `\Seen` is set. UNSEEN is only how new mail is found. The file uses DELETE journaling so a volume snapshot of that path is the whole job queue (no `-wal` sidecar).
- Jobs move `discovered → submitted → completed → posted → seen`. A crash or freeze leaves the job on disk; the next start continues from that step, including **before** IMAP is up. SMTP reuses the same `Message-ID` so a retried send is the same mail.
- `session.send` carries a stable client `messageId` and `serialize: true`. The daemon will not start a second copy of a message it already accepted, and it will not steer this mail into some other turn. Each email is its own turn, taken when the session is free.
- A mapped thread is never replaced just because the daemon was restarting or the unix socket was stale. Only `session.get` saying the session ended (or does not exist) mints a new one.
- If the daemon itself died mid-turn, the turn is interrupted and marked retryable. Mail calls `session.retry` rather than pasting the user text again. Reply text is taken only from that mail's turn, never from a neighbour.
- IMAP, SMTP, and daemon sockets are not trusted across a suspend: TCP keepalive, a boot-time/monotonic clock check every two seconds, and a NOOP after every IDLE. `clock.note()` is only used after a **new** connect, so a freeze during IDLE cannot be swallowed. systemd units use `Restart=always` and `TimeoutStopSec=60`. A container must mount `/srv/langmesh/xdg` as a volume so the job file and the daemon history survive. The entrypoint unlinks a stale daemon socket left on that volume.

A fake "Done." is never mailed because a wait timed out. The wait re-attaches and reads durable history; only that text is posted.

## Turn it on

On the machine that runs `langmeshd`:

1. The bundled `reviewer` profile is the default (`~/.agents/agents/reviewer/AGENT.md` on a laptop, or `.agents/agents/reviewer` in this checkout on a VPS). Set `email.agent` only if you want a different one.
2. Give the daemon a provider key, in `~/.config/langmesh/configuration.yaml` or that provider's environment variable.
3. On a headless Linux host, set `sandbox.enforce` to `preferred` (or `off`) so sessions can start without a confinement backend. Set `sandbox.network: true` if the agent should reach the network. `install.sh` already does this.
4. Put the mailbox in the same file, or in the environment. Environment variables win over the file. Gmail, Fastmail, Outlook, and Yahoo fill IMAP/SMTP hosts from the address; you still need an app password.

```yaml
email:
  enabled: true
  address: "agent@example.com"
  allow_from:
    - "you@example.com"
  agent: "reviewer"
  permission_mode: automatic
  imap:
    host: "imap.example.com"
    port: 993
    username: "agent@example.com"
    password: ""
  smtp:
    host: "smtp.example.com"
    port: 587
    username: "agent@example.com"
    password: ""
```

Gmail needs an [app password](https://support.google.com/accounts/answer/185833) and IMAP enabled. The client fills `imap.gmail.com` / `smtp.gmail.com` from a `gmail.com` address. Fastmail, Outlook, and Yahoo are the same. Do not commit the password; `LANGMESH_MAIL_IMAP_PASSWORD` and `LANGMESH_MAIL_SMTP_PASSWORD` (or `LANGMESH_MAIL_PASSWORD` for both) override the file.

```sh
uv run langmesh mail
```

`mail` starts `langmeshd` when it is not listening, then IDLEs. If the mailbox is not configured yet, it waits and re-reads the file instead of exiting. Logs go to stderr and `$XDG_STATE_HOME/langmesh/langmesh-mail.log`.

On a VPS, install both systemd units from `packaging/mail/` so the daemon and the mail client restart on boot. `packaging/mail/install.sh` does that: it installs Python 3.13 and uv if needed, syncs this checkout, writes the units, and enables them. It reads mailbox and provider credentials from `LANGMESH_MAIL_*` and `LANGMESH_*_API_KEY` in the environment.

```sh
sudo LANGMESH_MAIL_ADDRESS=agent@gmail.com \
     LANGMESH_MAIL_ALLOW_FROM=you@gmail.com \
     LANGMESH_MAIL_PASSWORD=... \
     OPENROUTER_API_KEY=... \
     packaging/mail/install.sh
```

Then send mail to `email.address` from an allowlisted From. The first reply is the agent's turn; a further reply in that thread continues the same session with only the new body.

## What you still have to supply

The mail client cannot create a mailbox or a cloud VM by itself. You need:

- IMAP and SMTP reachability for `email.address` (an app password, not your ordinary login, on Gmail).
- At least one allowlisted From (`LANGMESH_MAIL_ALLOW_FROM`).
- A provider key the agent profile can use.
- A host that stays up. `packaging/mail/provision.sh` will try Fly.io (with a persistent `langmesh_xdg` volume), Hetzner, or DigitalOcean when those tokens are already in the environment; otherwise you bring a VPS and run `install.sh` on it.

The daemon still binds loopback. Mail never exposes the capability token. Do not publish `langmeshd`'s port on the public internet; SSH or Tailscale if you also want the app. Persist `$XDG_DATA_HOME` (or the `/srv/langmesh/xdg` volume) across VM and container replacement, or in-flight mail cannot resume.

## Settings

Every field is in the [configuration reference](configuration.md#email). A change to the mail section is picked up on the next reconnect, or within a few seconds if the client is still waiting for a complete config. Editing `mail.env` still needs `systemctl restart langmesh-mail` because systemd only reads that file at start.
