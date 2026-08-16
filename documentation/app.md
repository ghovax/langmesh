# The desktop app

A native macOS window over the same control plane the `langmesh` command serves. The app holds no harness of its own: it finds a daemon, talks to it, and shows you what it says. Anything the app can do, the interface in a browser can do, and the reverse.

Open **LangMesh** to start it: the release app starts the separately installed local daemon when it cannot find one, then opens the window.

## What the window shows

| Region         | What it is                                                                              |
| -------------- | --------------------------------------------------------------------------------------- |
| The sidebar    | Your projects, and the conversations you started in each                                 |
| The transcript | The conversation as it happens: prose, tool calls, tool results, and prompts for a decision |
| The composer   | Where you type. It also queues a message when the session cannot take one yet            |
| Delegated work | A panel on the right, opened from the top bar: your conversation and everything it handed off, as a tree |
| Settings       | Providers and keys, agents, environments, permissions, and the screen tools              |

- The sidebar lists conversations **you** started, and only those. A session that a session created is delegated work, not a conversation of yours: it lives in its own panel, where the tree is the point.
- The transcript is anchored at its latest edge with a column-reverse scroller. Messages stay in canonical oldest-to-newest data order while the flex layout puts the newest row at scroll offset zero, so prepend-only history loading never disturbs the visible position.
- The composer is one disabled surface when sending is impossible: controls, attachments, dictation, and text become inert and muted together, without using error red as a disabled state.
- A dot beside a session says what it is doing: a pulsing grey dot means it is working; a yellow dot means it is parked on a decision only you can make; a blue dot means it finished something while you looked elsewhere; no dot means it is idle or asleep (the next message wakes it in about 60 ms).

## Answering a decision

- When a session needs permission, the turn stops and a prompt appears above it, saying what the tool wants and why. Allow or deny, and the turn goes on.
- There is no "always allow": every decision is allow-once or deny, and a session's permission mode can change at any point from the chip under the composer, including mid-turn. See [Configuration](configuration.md#permission-modes).
- You can keep typing while the prompt is up. A parked session takes no message (accepting one would discard the parked turn), so what you type is **queued**, marked as waiting on your decision, and sent the moment you decide. It goes into the turn that resumes, so a correction reaches the work it is about.
- You can leave a session parked as long as you like: the whole turn is checkpointed on disk, and the session sleeps rather than holding a process open.

## Sending, thinking, retries, and compaction

- A locally queued message appears immediately with its stable client id, marked queued or sending until the daemon accepts that exact id. The daemon assigns `receivedAt` at its single ingestion door and echoes the same id as an `inbound_message`; live streaming and replay reduce that same durable event.
- A message arriving mid-turn is appended at the next steering boundary after the persisted response prefix, or serialized as the next turn. It cannot exist on only one side, and two concurrent senders cannot both observe an idle session and overtake each other.
- The activity label comes from real states: queue delivery, daemon acceptance, thinking events, model text, tool execution, goal review, retry, compaction. No timer manufactures "thinking"; a silence detector reports a live provider that has not produced an event, but never claims progress the backend has not emitted.
- A turn failure renders a localized error card. **Try again** calls the backend's retry verb, shows its spinner from the durable retry event, and continues the saved conversation tail; it never resends your message. Sending fresh work instead explicitly supersedes the retryable failure.
- Compaction is an explicit visible state even when automatically recommended. Before compacting, the agent receives a private Bash-only checkpoint segment and must advance the workspace observation registry at least once. A failed checkpoint or compaction leaves the conversation unchanged, renders a localized blocking card, disables every send path, and keeps queued messages outside the backend until **Retry** continues the existing operation. Success resumes any already-accepted message rather than sending it again.

## Environments

- A project is a set of **environments**; an environment says where a session's work happens. It can be a directory on this machine, or one on an SSH host. Add one in **Settings, then Environments**.
- SSH hosts come from your `~/.ssh/config`, so a host you already use is one you can pick; picking one fills the path in with that host's home directory.
- Adding or editing an environment reaches sessions already running in that workspace: they pick it up on their next turn, not only after a restart.

## Screen control

The app can drive native macOS applications and your own Chrome through the agent's `control_screen` tool. It is off until you turn it on, and needs two grants:

- **Accessibility**, asked once by macOS. It is tied to the app's code identity, so a signed build keeps the grant across updates.
- **Chrome's remote-debugging port**, which the app tells you how to enable.

The tool reads the accessibility tree and the page's structure, not screenshots. See [Tools](tools.md#screen-control-control_screen) for what it can and cannot do.

## A daemon somewhere else

The app is a client, so the daemon it talks to need not be on this machine. An environment on an SSH host runs its tools there while the daemon stays here. To put the _daemon_ on another machine, forward its port and point the app at it, as described in [Architecture](architecture.md#connections-local-remote-ssh).

## When there is no daemon

The release app asks the separately installed `LangMesh Computer Use.app` to start the local daemon and waits for its published endpoint. If that bundle is missing or cannot start, the app remains disconnected and reports the failure; a remote daemon is never started or replaced by this local recovery.

## Where to go next

- Every setting the app exposes: [Configuration](configuration.md).
- The same operations from a terminal: [The `langmesh` command](cli.md).
- Writing your own agents and skills: [Agent system](agent-system.md).
