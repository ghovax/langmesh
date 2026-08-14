Send a message to another session: one you created, or the one that created you.

**Upward, this is how you report back.** A message to the session that created you is your deliverable and the only thing that reaches whoever waits — not your transcript, your tool calls or your reasoning. Make it stand on its own: what you found, the evidence, what you changed, and whatever is still uncertain. Sending it does not end your turn.

**Downward, this is how you brief a peer and follow up.** The message you send after `create_session` is what sets the peer working, so state the specific goal, paths, constraints, and the shape of the answer you want back. A message to a peer already working is steered into its current turn at the next safe point, so you can redirect it without a wait; a message to an idle one starts its next turn.

The call returns as soon as the harness accepts the message; there is nothing to wait for and nothing to poll. A reply reaches you on its own, as a new message.

This call takes these arguments:

- `session` — The id of the session to send to — one you created, or the one that created you. Required.
- `message` — What to send. Downward this is the focused brief; upward it is the entire report.
- `explanation` — A short reason for the message, in the words the user reads. The interface shows it as this call's label.