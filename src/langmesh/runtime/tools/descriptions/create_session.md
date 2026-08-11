Create a peer session.

The peer starts idle and runs nothing until you brief it with `message_session` — so do both, in that order, unless you have a reason to wait. A peer runs its own agent profile with its own context, initialized with your conversation. Use one where the work truly separates: parallel investigations, a broad search across a subsystem you are not in, or review while you implement. Not for a small edit or a judgement call. A peer gathers evidence; you decide.

The peer sends you its answer as a message when it finishes, which arrives on its own and wakes you if your turn ended. So start the work, continue with whatever does not depend on it, and end your turn when everything left does. Never poll a peer.

A peer is your child — it ends when you do and never holds more access than you — and it works in the same tree, so tell it which part is its and that other agents work beside it. Nothing you can call ends a peer early; that is the person's to decide.

This call takes these arguments:

- `agent` — The agent profile the peer runs, from the list this tool enumerates. Required, and never invented.
- `working_directory` — Where the peer works. Defaults to yours.
- `explanation` — A short reason for creating this peer, in the words the user reads.