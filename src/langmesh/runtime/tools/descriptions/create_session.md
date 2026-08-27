Create a peer session.

- The peer starts idle and runs nothing until you brief it with `message_session` — do
  both, in that order, unless you have a reason to wait.
- Use one where the work truly separates: parallel investigations, a broad search across
  a subsystem you are not in, or review while you implement. Not for a small edit or a
  judgement call. A peer gathers evidence; you decide.
- The peer sends its answer as a message when it finishes, which arrives on its own and
  wakes you if your turn ended: start the work, continue with what does not depend on
  it, end your turn when everything left does. Never poll a peer.
- A peer is your child — it ends when you do, never holds more access than you, works in
  the same tree: tell it which part is its and that other agents work beside it. Nothing
  you can call ends a peer early; that is the person's to decide.

Arguments:

- `agent` — The agent profile the peer runs, from the list this tool enumerates.
  Required, and never invented.
- `working_directory` — Where the peer works. Defaults to yours.
