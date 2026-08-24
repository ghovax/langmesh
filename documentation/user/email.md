# Email

You write to the agent's mailbox with a subject and a body. It replies in that thread and can mail you progress while it works. The mail process is a **client** of `langmeshd`, the same way the desktop app is: it talks over the daemon's unix socket with the capability token, and it never embeds a library `Session`. Quoted reply history is stripped, so each inbound message is that email's own content.

This is not XMPP and not the GitHub mention Action. GitHub mentions run the library in a short-lived job. Mail sits in front of a long-running daemon on a machine that stays up — typically a small Linux VPS.

## What happens

1. `langmesh mail` starts the daemon if it is not up, then proves IMAP login and SMTP auth and IDLEs the mailbox (`aioimaplib`, RFC 2177). It does not poll from inside a running turn.
2. A new UNSEEN message from an allowlisted sender is fetched. Automatic replies, bounces, and mail the agent sent (From is the plus-tagged mailbox, or a Message-ID it minted) are ignored. Gmail plus-addresses share one inbox: the allowlisted owner mailing `user+vps@gmail.com` from `user@gmail.com` is not treated as the mailbox talking to itself. Plus-addresses and `googlemail.com` still count as the same account for the allow-list, so listing `user@gmail.com` (or `@gmail.com`) takes those aliases too. Replies go to `Reply-To` when the message has one.
3. The HTML part is the body. Quoted replies are the containers the mail client wrapped around the previous message; those nodes are removed and the rest is converted with `markdownify` so the agent sees markdown. `text/plain` is used only when there is no HTML, or when the HTML is only the quoted thread.
4. The thread is keyed as `email:{mailbox}:{root-message-id}` from `References` / `In-Reply-To` / `Message-ID`. An In-Reply-To of a prior outbound also continues the same session. That key maps to one daemon session, stored under `$XDG_DATA_HOME/langmesh/mail.sqlite`.
5. The opening turn is one JSON object with `subject` and `message`. Later mail on the same thread is only `message`. `session.create` mints the session on the first mail (`permission_mode: automatic` by default). IDLE keeps running while a turn is in flight: a follow-up on a live session is steered into that turn; a mail that arrives when the session is idle starts a new turn.
6. The session speaks through `submit_email`, the same idea as `submit_github_comment` on GitHub. `kind` `progress` mails a short status and keeps working. `kind` `reply` mails the answer and ends the turn. The agent writes markdown; it is rendered as HTML and sent as `multipart/alternative` with that HTML as the preferred part. Assistant prose in the transcript is not mailed.

Later mail is discovered by IDLE, not by polling from inside a turn. A different thread can run at the same time; one IMAP connection fetches only between IDLEs.

## Which machine

Several hosts can IDLE the same mailbox. Each sets `email.machine` to a lowercase slug (`vps`, `laptop`). SMTP From is `local+machine@domain`, so you keep a separate email thread per machine — write `agent+vps@…` for the VPS, `agent+laptop@…` for the laptop:

- A **new thread** (not a reply) starts a **new session** on the machine named by the plus-tag. The tag must name this host; a missing tag is left UNSEEN rather than guessed.
- A **reply** is a steering message into that same conversation (`In-Reply-To` / the thread map), even if the plus-tag is missing. Mail tagged for a different machine is left UNSEEN so that host can take it.

IMAP still logs in as the account without the plus. If `email.address` already has a plus-tag, it must equal `email.machine`.

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

1. The bundled `reviewer` profile is the default (`~/.agents/agents/reviewer/AGENT.md` on a laptop, or `.agents/agents/reviewer` in this checkout on a VPS). It owns tools and the prompt. Set `email.agent` only if you want a different profile.
2. Pick which catalogue provider mailbox sessions call. Set `email.provider` and `email.model` in `configuration.yaml` to overlay the profile (both, or neither). Add that provider under `providers:` the same way the rest of LangMesh does — empty `api_key` placeholder, and `base_url` when the provider needs one (`custom` always does). Write the matching secret file `providers.<id>.api_key`. Mail waits until that key is present, the same way it waits for the mailbox password. ChatGPT and Cursor subscription providers skip the key file.
3. On a headless Linux host, set `sandbox.enforce` to `preferred` (or `off`) so sessions can start without a confinement backend. Set `sandbox.network: true` if the agent should reach the network. `install.sh` already does this.
4. Put the mailbox in `configuration.yaml`. Gmail, Fastmail, Yahoo, and iCloud fill IMAP/SMTP hosts from the address. Password auth uses the secret file `email.imap.password` (an app password). Outlook and Hotmail hosts are inferred too; those accounts usually need OAuth instead of a password (`email.auth: oauth`). Proton Mail has **no** IMAP OAuth — a paid plan uses [Proton Bridge](https://proton.me/mail/bridge) on the same host with `email.auth: password` and the Bridge password.

```yaml
email:
  enabled: true
  address: "agent@example.com"
  machine: vps
  allow_from:
    - "you@example.com"
  agent: "reviewer"
  provider: anthropic
  model: claude-sonnet-4-5
  permission_mode: automatic

providers:
  anthropic:
    api_key: ""
```

Password (Gmail app password, Fastmail, Yahoo, iCloud, Proton Bridge):

```sh
mkdir -p "$XDG_DATA_HOME/langmesh/secrets"
chmod 700 "$XDG_DATA_HOME/langmesh/secrets"
printf '%s' 'the-app-password' > "$XDG_DATA_HOME/langmesh/secrets/email.imap.password"
printf '%s' 'the-provider-key' > "$XDG_DATA_HOME/langmesh/secrets/providers.anthropic.api_key"
chmod 600 "$XDG_DATA_HOME/langmesh/secrets"/*
```

Omit `email.provider` and `email.model` to keep the profile's own pair. The bundled reviewer is OpenCode Go (`opencode-go` / `deepseek-v4-flash`), which bills as `providers.opencode.api_key`. Any other catalogue id is the same shape: `providers.openai`, `providers.groq`, `providers.custom` with a `base_url`, and so on. The id in the secret file is the catalogue provider or its `credential_identifier`.

OAuth (Gmail, Microsoft 365 / Outlook, Yahoo, or a custom issuer). Authlib refreshes the token; IMAP and SMTP use the libraries' built-in XOAUTH2:

```yaml
email:
  enabled: true
  address: "agent@outlook.com"
  machine: vps
  allow_from:
    - "you@example.com"
  auth: oauth
  oauth:
    issuer: microsoft   # google | microsoft | yahoo | custom
    client_id: "your-app-id"
```

```sh
# optional, if the OAuth app is a confidential client
printf '%s' 'the-client-secret' > "$XDG_DATA_HOME/langmesh/secrets/email.oauth.client_secret"
uv run langmesh mail auth    # browser sign-in; writes email.oauth.refresh_token
chmod 600 "$XDG_DATA_HOME/langmesh/secrets"/*
```

Register `http://127.0.0.1:8765/callback` as the redirect URI on that OAuth app (override with `email.oauth.redirect_uri`). Google needs a Desktop client and the Gmail API. Microsoft needs IMAP.AccessAsUser.All and SMTP.Send. Copy the refresh-token file onto a VPS if you signed in on a laptop.

Gmail also still accepts an [app password](https://support.google.com/accounts/answer/185833) with IMAP enabled. The client fills `imap.gmail.com` / `smtp.gmail.com` from a `gmail.com` address. Fastmail, Yahoo, and iCloud (`icloud.com` / `me.com`) are the same with that provider's app password. A Gmail plus-address (`custom+mail@gmail.com`) still authenticates as the account without `+mail`. Gmail copies the 16-character app password with spaces; those spaces are display-only and are stripped when the secret file is written. SMTP port `465` uses implicit TLS. If a VPS blocks outbound `587`, the client retries `465` on the same host. Do not commit passwords or refresh tokens.

Proton Mail cannot be reached with OAuth. Paid Bridge listens on `127.0.0.1` (inferred from a `proton.me` / `protonmail.com` / `pm.me` address): IMAP 1143 and SMTP 1025. Set Bridge to **SSL** for IMAP (this client speaks implicit TLS, not IMAP STARTTLS). The secret is the Bridge password, not the Proton account password. Tuta has no IMAP.

```sh
uv run langmesh mail check
uv run langmesh mail
```

`mail check` reads `configuration.yaml` and the secret files, lists anything still missing, and proves IMAP login, `UID SEARCH UNSEEN`, and SMTP auth without IDLEing and without starting the daemon. A refused login includes the server's reason (wrong app password versus IMAP disabled). `mail auth` signs in with OAuth and writes `email.oauth.refresh_token`. `mail` starts `langmeshd` when it is not listening, then proves IMAP and SMTP and IDLEs. If the mailbox, allow-list, or the mailbox provider key is not configured yet, `mail` waits and re-reads the configuration file and secret files instead of exiting. A systemd or Docker `langmeshd` that is already binding the socket is waited on rather than started a second time. Logs go to stderr and `$XDG_STATE_HOME/langmesh/langmesh-mail.log`.

On a VPS, fill `packaging/mail/configuration.yaml` and a `secrets/` directory (see `packaging/mail/secrets/README`), run `uv run langmesh mail check`, then install:

```sh
# edit packaging/mail/configuration.yaml (address, allow_from, provider/model)
mkdir -p secrets
printf '%s' 'the-app-password' > secrets/email.imap.password
printf '%s' 'the-provider-key' > secrets/providers.<id>.api_key
chmod 600 secrets/*
uv run langmesh mail check
sudo packaging/mail/install.sh
```

`install.sh` copies `packaging/mail/configuration.yaml` (if the host has none) and `./secrets` onto `/srv/langmesh/xdg`, then enables `langmeshd` and `langmesh-mail`. Pass `--prefix DIR` to install somewhere else. A later install does not replace that xdg tree (the job queue, daemon history, and secrets already on the host). Then send mail to `email.address` from an allowlisted From. Progress and the reply arrive in that thread; a further reply — including a reply to a progress note — continues the same session with only the new body.

## What you still have to supply

The mail client cannot create a mailbox, a DNS name, or a cloud VM by itself. After this checkout is installed, only these human-in-the-loop steps stand between you and an end-to-end send. The usual mailbox is a Gmail plus-address on an account you already have:

1. **Gmail IMAP.** Settings → See all settings → Forwarding and POP/IMAP → Enable IMAP.
2. **App password.** Google Account → Security → 2-Step Verification → [App passwords](https://support.google.com/accounts/answer/185833). Mint one for Mail. Gmail shows it as four groups of four; the spaces are display-only.
3. **Policy and secrets.** Fill `email.address` and `email.allow_from` in `configuration.yaml`. Set `email.provider` and `email.model` to overlay the agent profile, or omit both to keep the profile's pair. For password auth, write `email.imap.password`. For OAuth, set `email.auth: oauth` and `email.oauth.client_id`, then `uv run langmesh mail auth` (or copy `email.oauth.refresh_token`). Write `providers.<id>.api_key` under `$XDG_DATA_HOME/langmesh/secrets/` (mode `0600`). Add that provider under `providers:` (empty `api_key`, plus `base_url` when the provider needs one). Allowlisting `custom@gmail.com` also takes `custom+tag@gmail.com` and `custom@googlemail.com`. Fastmail, Yahoo, and iCloud are the same with that provider's app password. Outlook uses OAuth. Proton uses Bridge and a password, not OAuth.
4. **Prove it.** `uv run langmesh mail check` must print `ready` (IMAP login, UNSEEN search, and SMTP auth) before you leave this machine or install systemd. Anything it lists is still HITL. A Gmail refusal names Authentication failed versus IMAP not enabled.
5. **A host that stays up**, with `$XDG_DATA_HOME` (or the `/srv/langmesh/xdg` volume) persisted across replace. Choose one:
   - This machine: `uv run langmesh mail`, then send mail to `email.address` from the allowlisted From.
   - You already have a Linux VPS: copy this checkout there, fill the YAML and `secrets/`, run the `install.sh` command above.
   - Fly.io: put policy in `packaging/mail/configuration.yaml` (`provision.fly` for app and region) and secret files on the `langmesh_xdg` volume. Set `FLY_API_TOKEN` and run `packaging/mail/provision.sh`. The script creates the app, a persistent `langmesh_xdg` volume, and deploys from the checkout root.
   - Hetzner or DigitalOcean: set `HCLOUD_TOKEN` (with `hcloud` on PATH) and `provision.hetzner.ssh_key`, or `DIGITALOCEAN_ACCESS_TOKEN` (with `doctl` on PATH) and `provision.digitalocean.ssh_key`. Or set `provision.host` to SSH into a machine you already have.
6. **DNS**, only if the mailbox should live on a domain you own. A Gmail/Fastmail/iCloud address does not need this.

Mail will not IDLE until the mailbox provider key is present — otherwise the first email's turn 401s for half an hour. `email.provider` / `email.model` overlay the agent profile for mailbox sessions only; a desktop session on the same profile is unchanged. The secret file is `providers.<id>.api_key` for that catalogue provider (OpenCode Go bills as `providers.opencode.api_key`). Native ChatGPT and Cursor providers skip the key.

The daemon still binds loopback. Mail never exposes the capability token. Do not publish `langmeshd`'s port on the public internet; SSH or Tailscale if you also want the app. Persist `$XDG_DATA_HOME` (or the `/srv/langmesh/xdg` volume) across VM and container replacement, or in-flight mail cannot resume.

## Settings

Every field is in the [configuration reference](configuration.md#email). A change to the mail section is picked up on the next reconnect, or within a few seconds if the client is still waiting for a complete config. Replacing a secret file is picked up the same way.
