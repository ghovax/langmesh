# Email

You write to the agent's mailbox with a subject and a body. It replies in that thread and can mail you progress while it works. The mail process is a **client** of `langmeshd`, the same way the desktop app is: it talks over the daemon's unix socket with the capability token, and it never embeds a library `Session`. Quoted reply history is stripped, so each inbound message is that email's own content.

This is not XMPP and not the GitHub mention Action. GitHub mentions run the library in a short-lived job. Mail sits in front of a long-running daemon on a machine that stays up — typically a small Linux VPS.

## What happens

1. `langmesh mail` starts the daemon if it is not up, then proves IMAP login and SMTP auth and IDLEs the mailbox (`aioimaplib`, RFC 2177). It does not poll from inside a running turn.
2. A new UNSEEN message from an allowlisted sender is fetched. Automatic replies, bounces, and mail the agent sent (From is the mailbox address, or a Message-ID it minted) are ignored. Gmail plus-addresses share one inbox: the allowlisted owner mailing `user+agent@gmail.com` from `user@gmail.com` is not treated as the mailbox talking to itself. Plus-addresses and `googlemail.com` still count as the same account for the allow-list, so listing `user@gmail.com` (or `@gmail.com`) takes those aliases too. Replies go to `Reply-To` when the message has one.
3. The HTML part is the body. Quoted replies are the containers the mail client wrapped around the previous message; those nodes are removed and the rest is converted with `markdownify` so the agent sees markdown. `text/plain` is used only when there is no HTML, or when the HTML is only the quoted thread.
4. The thread is keyed as `email:{mailbox}:{root-message-id}` from `References` / `In-Reply-To` / `Message-ID`. An In-Reply-To of a prior outbound also continues the same session. That key maps to one daemon session, stored under `$XDG_DATA_HOME/langmesh/mail.sqlite`.
5. The opening turn is one JSON object with `subject` and `message`. Later mail on the same thread is only `message`. `session.create` mints the session on the first mail (`permission_mode: automatic` by default). IDLE keeps running while a turn is in flight: a follow-up on a live session is steered into that turn; a mail that arrives when the session is idle starts a new turn.
6. The session speaks through `submit_email`, the same idea as `submit_github_comment` on GitHub. `kind` `progress` mails a short status and keeps working. `kind` `reply` mails the answer and ends the turn. The agent writes markdown; it is rendered as HTML and sent as `multipart/alternative` with that HTML as the preferred part. Assistant prose in the transcript is not mailed.

Later mail is discovered by IDLE, not by polling from inside a turn. A different thread can run at the same time; one IMAP connection fetches only between IDLEs.

## Pause, idle, reboot

Cheap VPS hosts suspend. Containers get paused or replaced. The mail client is written for that:

- Every inbound message is a sqlite job under `$XDG_DATA_HOME/langmesh/mail.sqlite` **before** IMAP `\Seen` is set. UNSEEN is only how new mail is found. The file uses DELETE journaling so a volume snapshot of that path is the whole job queue (no `-wal` sidecar).
- Jobs move `discovered → submitted → posted → seen`. A crash or freeze leaves the job on disk; the next start continues from that step, including **before** IMAP is up. `submit_email` with `kind` `reply` is what posts; IMAP `\Seen` is last.
- `session.send` carries a stable client `messageId`. If the session is already working, the mail is steered into that turn; otherwise it starts a new one. A crash cannot duplicate a message the daemon already accepted.
- A mapped thread is never replaced just because the daemon was restarting or the unix socket was stale. Only `session.get` saying the session ended (or does not exist) mints a new one.
- If the daemon itself died mid-turn, the turn is interrupted and marked retryable. Mail calls `session.retry` rather than pasting the user text again.
- IMAP, SMTP, and daemon sockets are not trusted across a suspend: TCP keepalive, a boot-time/monotonic clock check every two seconds, and a NOOP after every IDLE. `clock.note()` is only used after a **new IMAP connect**, so a freeze during IDLE cannot be swallowed. systemd units use `Restart=always` and `TimeoutStopSec=60`. A container must mount `/srv/langmesh/xdg` as a volume so the job file and the daemon history survive. The entrypoint unlinks a stale daemon socket left on that volume.

A fake "Done." is never mailed because a wait timed out. Nothing is mailed until `submit_email` itself lands.

## Turn it on

On the machine that runs `langmeshd`:

1. The bundled `reviewer` profile is the default (`~/.agents/agents/reviewer/AGENT.md` on a laptop, or `.agents/agents/reviewer` in this checkout on a VPS). Set `email.agent` only if you want a different one.
2. Give the daemon a provider key, in `~/.config/langmesh/configuration.yaml` or that provider's environment variable. The bundled reviewer needs `OPENCODE_API_KEY`. `LANGMESH_MAIL_ADDRESS` in the environment is enough to enable the client without setting `email.enabled` in the file. Mail waits until that key is present, the same way it waits for the mailbox password.
3. On a headless Linux host, set `sandbox.enforce` to `preferred` (or `off`) so sessions can start without a confinement backend. Set `sandbox.network: true` if the agent should reach the network. `install.sh` already does this.
4. Put the mailbox in the same file, or in the environment. Environment variables win over the file. Gmail, Fastmail, Yahoo, and iCloud fill IMAP/SMTP hosts from the address; you still need an app password. Outlook and Hotmail hosts are inferred too, but many of those accounts want OAuth, which this client does not speak.

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

Gmail needs an [app password](https://support.google.com/accounts/answer/185833) and IMAP enabled. The client fills `imap.gmail.com` / `smtp.gmail.com` from a `gmail.com` address. Fastmail, Yahoo, and iCloud (`icloud.com` / `me.com`) are the same. Outlook and Hotmail still get inferred hosts, but many of those accounts no longer accept an app password (they want OAuth, which this client does not speak). A Gmail plus-address (`custom+mail@gmail.com`) still authenticates as the account without `+mail`. Gmail copies the 16-character app password with spaces; those spaces are display-only and are stripped. SMTP port `465` uses implicit TLS. If a VPS blocks outbound `587`, the client retries `465` on the same host. Do not commit the password.

```sh
uv run langmesh mail check
uv run langmesh mail
```

`mail check` loads `mail.env`, lists anything still missing, and proves IMAP login, `UID SEARCH UNSEEN`, and SMTP auth without IDLEing and without starting the daemon. A refused login includes the server's reason (wrong app password versus IMAP disabled). `mail` starts `langmeshd` when it is not listening, then proves IMAP and SMTP and IDLEs. Both commands load `mail.env` from `LANGMESH_MAIL_ENV`, `./mail.env`, `/srv/langmesh/mail.env`, or `packaging/mail/mail.env` — already-set environment values win, so you do not need `xargs`. The daemon and each mailbox session load that file the same way, so an already-running `langmeshd` still sees a `mail.env` you fill after it started. If the mailbox, allow-list, or the agent profile's provider key is not configured yet, `mail` waits and re-reads `mail.env` and the configuration file instead of exiting. A systemd or Docker `langmeshd` that is already binding the socket is waited on rather than started a second time. Logs go to stderr and `$XDG_STATE_HOME/langmesh/langmesh-mail.log`.

On a VPS, copy `packaging/mail/mail.env.example` to `mail.env`, fill it, run `uv run langmesh mail check`, and point `install.sh` at that file so systemd gets every key (including extra provider keys) rather than a reconstructed subset:

```sh
cp packaging/mail/mail.env.example mail.env
chmod 600 mail.env
# edit mail.env
uv run langmesh mail check
sudo env LANGMESH_MAIL_ENV="$PWD/mail.env" packaging/mail/install.sh
```

`install.sh` copies `mail.env` to `/srv/langmesh/mail.env` as the systemd `EnvironmentFile` and enables `langmeshd` and `langmesh-mail`. Filling `mail.env` and re-running the `LANGMESH_MAIL_ENV` command refreshes that file. A later install does not replace `/srv/langmesh/xdg` (the job queue and daemon history) or an existing `mail.env` unless you point `LANGMESH_MAIL_ENV` at a new file. Then send mail to `email.address` from an allowlisted From. Progress and the reply arrive in that thread; a further reply — including a reply to a progress note — continues the same session with only the new body.

## What you still have to supply

The mail client cannot create a mailbox, a DNS name, or a cloud VM by itself. After this checkout is installed, only these human-in-the-loop steps stand between you and an end-to-end send. The usual mailbox is a Gmail plus-address on an account you already have:

1. **Gmail IMAP.** Settings → See all settings → Forwarding and POP/IMAP → Enable IMAP.
2. **App password.** Google Account → Security → 2-Step Verification → [App passwords](https://support.google.com/accounts/answer/185833). Mint one for Mail. Gmail shows it as four groups of four; the spaces are display-only and are stripped.
3. **`mail.env`.** Copy `packaging/mail/mail.env.example` to `mail.env` (mode `0600`) and fill:

   ```
   LANGMESH_MAIL_ADDRESS=custom+langmesh@gmail.com
   LANGMESH_MAIL_ALLOW_FROM=custom@gmail.com
   LANGMESH_MAIL_PASSWORD=the-app-password
   OPENCODE_API_KEY=the-reviewer-key
   ```

   Allowlisting `custom@gmail.com` also takes `custom+tag@gmail.com` and `custom@googlemail.com`. Fastmail, Yahoo, and iCloud are the same three keys with that provider's app password; a non-Gmail address still infers hosts when the domain is known.
4. **Prove it.** `uv run langmesh mail check` must print `ready` (IMAP login, UNSEEN search, and SMTP auth) before you leave this machine or install systemd. Anything it lists is still HITL. A Gmail refusal names Authentication failed versus IMAP not enabled.
5. **A host that stays up**, with `$XDG_DATA_HOME` (or the `/srv/langmesh/xdg` volume) persisted across replace. Choose one:
   - This machine: `uv run langmesh mail`, then send mail to `LANGMESH_MAIL_ADDRESS` from the allowlisted From.
   - You already have a Linux VPS: copy this checkout there, fill `mail.env`, run the `install.sh` command above.
   - Fly.io: set `FLY_API_TOKEN` (and `LANGMESH_MAIL_ENV=mail.env`) and run `packaging/mail/provision.sh`. The script creates the app, a persistent `langmesh_xdg` volume, imports secrets (comments and empty lines stripped), and deploys from the checkout root.
   - Hetzner or DigitalOcean: set `HCLOUD_TOKEN` (with `hcloud` on PATH) and `LANGMESH_HCLOUD_SSH_KEY`, or `DIGITALOCEAN_ACCESS_TOKEN` (with `doctl` on PATH) and `LANGMESH_DO_SSH_KEY`, plus `LANGMESH_MAIL_ENV`. Or set `LANGMESH_VPS_HOST` to SSH into a machine you already have.
6. **DNS**, only if the mailbox should live on a domain you own. A Gmail/Fastmail/iCloud address does not need this.

Editing `mail.env` after the units have started still needs `systemctl restart langmeshd langmesh-mail` so both processes see it. Mail will not IDLE until the provider key is present — otherwise the first email's turn 401s for half an hour. The bundled reviewer talks to OpenCode Go (`opencode-go/deepseek-v4-flash`), so that is `OPENCODE_API_KEY` unless you change `email.agent`. `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `OPENROUTER_API_KEY`, and so on also work for those providers.

The daemon still binds loopback. Mail never exposes the capability token. Do not publish `langmeshd`'s port on the public internet; SSH or Tailscale if you also want the app. Persist `$XDG_DATA_HOME` (or the `/srv/langmesh/xdg` volume) across VM and container replacement, or in-flight mail cannot resume.

## Settings

Every field is in the [configuration reference](configuration.md#email). A change to the mail section is picked up on the next reconnect, or within a few seconds if the client is still waiting for a complete config. Editing `mail.env` still needs `systemctl restart langmeshd langmesh-mail` because systemd only reads that file at start, and the worker needs the provider key and SMTP password too.
