# The desktop app

A native macOS window over the same control plane the `langmesh` command serves. The app
holds no harness of its own: it finds a daemon, talks to it, and shows you what it says.
Anything the app can do, the interface in a browser (or a phone) can do, and the
reverse.

Open **LangMesh** to start it: the release app starts the separately installed daemon on
this machine when it cannot find one, then opens the window.

## What the window shows

| Region | What it is | | ------------------- |
\---------------------------------------------------------------------------------------
| | The sidebar | Your workspaces, and the conversations you started in each | | The
transcript | The conversation as it happens: prose, tool calls, tool results, prompts
for a decision, and git status | | The composer | Where you type, choose an agent and a
model, and set the session's permission mode. It also queues a message when the session
cannot take one yet | | Side panels | Opened from the top bar: delegated work, goal
reviews, memory, and terminals & background jobs | | Settings | A dialog with General,
Connection, Environments, Schedules, Agents, and a schema-driven Configuration page |

- The sidebar lists conversations **you** started, and only those. A session that a
  session created is delegated work, not a conversation of yours: it lives in its own
  side panel, where the tree is the point.
- The transcript is anchored at its latest edge with a column-reverse scroller. Messages
  stay in canonical oldest-to-newest data order while the flex layout puts the newest
  row at scroll offset zero, so prepend-only history loading never disturbs the visible
  position.
- The composer is one disabled surface when sending is impossible: controls,
  attachments, dictation, and text become inert and muted together, without using error
  red as a disabled state.
- A dot beside a session says what it is doing: a pulsing grey dot means it is working;
  a yellow dot means it is parked on a decision only you can make; a blue dot means it
  finished something while you looked elsewhere; no dot means it is idle or asleep (the
  next message wakes it in about 60 ms).

## Answering a decision

- When a session needs permission, the turn stops and an overlay appears, saying what
  the tool wants and why. Allow or deny, and the turn goes on.
- There is no standing "always allow" prompt, and a session's permission mode can change
  at any point from the chip under the composer, including mid-turn. See
  [Permission modes](configuration.md#permission-modes). (Setting a session to `allow`
  mode skips the gate entirely, but the operating-system confinement still applies.)
- You can keep typing while the prompt is up. A parked session takes no message
  (accepting one would discard the parked turn), so what you type is **queued**, marked
  as waiting on your decision, and sent the moment you decide. It goes into the turn
  that resumes, so a correction reaches the work it is about.
- You can leave a session parked as long as you like: the whole turn is checkpointed on
  disk, and the session sleeps rather than holding a process open.

## Sending, thinking, retries, and compaction

- A locally queued message appears immediately with its stable client id, marked queued
  or sending until the daemon accepts that exact id. Live streaming and replay reduce
  the same durable event.
- A message arriving mid-turn is appended at the next steering boundary, or serialized
  as the next turn. Two concurrent senders cannot both observe an idle session and
  overtake each other.
- The activity label comes from real states: daemon acceptance, thinking events, model
  text, tool execution, goal review, retry, compaction. No timer manufactures
  "thinking"; a silence detector reports a live provider that has not produced an event,
  but never claims progress the backend has not emitted.
- A turn failure renders a localized error card. **Try again** calls the daemon's retry
  verb and continues the saved conversation tail; it never resends your message.
- Compaction is an explicit visible state even when automatically recommended. Before
  compacting, the agent receives a private Bash-only checkpoint segment and must advance
  the workspace observation registry at least once. A failed checkpoint or compaction
  leaves the conversation unchanged, renders a localized blocking card, and keeps queued
  messages outside the backend until **Retry** continues the existing operation.

## Workspaces and environments

- A **workspace** is a project: it holds your conversations and a set of
  **environments** (locations). An environment says where a session's work happens. It
  can be a directory on this machine, or one on an SSH host. Add one under **Settings,
  then Environments**, or with the **New workspace** dialog in the sidebar.
- SSH hosts come from your `~/.ssh/config`, so a host you already use is one you can
  pick; picking one fills the path in with that host's home directory.
- Adding or editing an environment reaches sessions already running in that workspace:
  they pick up its locations on their next turn, not only after a restart.

## Screen control

The app can drive native macOS applications and your own Chrome through the agent's
`control_screen` tool. It is off until you turn it on, and needs two grants:

- **Accessibility**, asked once by macOS. It is tied to the app's code identity, so a
  signed build keeps the grant across updates.
- **Chrome's remote-debugging port**, which the app tells you how to enable.

The tool reads the accessibility tree and the page's structure, not screenshots. See
[Screen control](agent-system.md#screen-control-control_screen) for what it can and
cannot do.

## A daemon somewhere else

The app is a client, so the daemon it talks to need not be the one on this machine. That
is a different question from where a session's **tools** run.

- **A paired daemon.** Run `langmeshd` on another host, expose its loopback door behind
  a transport you choose, add that machine's pairing link under **Settings, then
  Connection**, and switch to it in the same window. Sessions from that host appear in
  the sidebar under that machine. The desktop app remembers paired machines (see
  [`langmesh serve --reach`](installation.md#the-langmesh-command)). The agent's shell,
  files, and network all live on that host; the interface stays native.
- **An SSH location.** An environment whose `kind` is remote forwards a local port
  through `ssh -L`, so a session on the daemon you are already talking to can work on a
  machine you reach only over SSH, with nothing exposed. The daemon does not move.

When there is no daemon on this machine, the release app asks the separately installed
`LangMesh Computer Use.app` to start **this machine's** daemon and waits for its
published endpoint. If that bundle is missing or cannot start, the app remains
disconnected and reports the failure. A paired daemon is never started or replaced by
that recovery.

## Where to go next

- Every setting the app exposes: [Configuration](configuration.md).
- The same interface in a browser, and a phone over the pairing door:
  [`langmesh serve`](installation.md#the-langmesh-command).
- Writing your own agents and skills: [Agent system](agent-system.md).
