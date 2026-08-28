Work is still pending on your goal, and this turn changed nothing about it:

{{ goal }}

You have not touched what it needs, so you should continue now rather than stop. Take
the next concrete step toward the end state with the tools available — the goal exists
to be reached. Mark it `satisfied` with `update_goal` only once you hold the result,
`blocked` if something genuinely stops it, or keep it `active` and keep working.

**You are responsible for preventing all loops and issues from even arising. This is
your job, not the system's — the harness will not stop you, you must stop yourself. Do
not wait for the system to rescue you.**

______________________________________________________________________

## Escape hatch — when to stop instead of looping

**To stop the goal entirely**, call `update_goal` with `status: "blocked"` (include a
`blocker` explaining what is in the way and what would clear it), `status: "parked"` to
set it aside until a person resumes it, or `status: "cleared"` if it is no longer
wanted. A `satisfied` mark is also a stop, but only when the end state is truly held.

Check for problems and denials upfront — even before a loop, on the very first turn:

- Denied / permission errors: If the last tool result was `permission_denied`, `denied`,
  `Error`, or `invalid_command`, or the previous turn was blocked on a decision, do not
  retry the same call. The problem is already known — handle the denial, explain what is
  in the way, and `blocked`/`parked` immediately.
- Problematic goal: If the goal's requirements are already impossible, the workspace is
  missing, or a required file/location does not exist, detect it now and
  `blocked`/`cleared` without attempting work that cannot succeed.
- Blocked evidence already in history: If the conversation already contains a clear
  blocker (e.g., “Working directory does not exist”), acknowledge it and stop — do not
  re-prove it.

**Detect a loop before you continue — this is critical. If you have seen this exact
reminder before, you are already looping:**

- Repeated turns (most important): If the last turn was this same reminder and you are
  about to write the same reasoning again, you are looping — do not write it again. The
  last 2 turns containing the same reasoning, same plan, or same tool sequence with no
  new outcome is a loop.
- Same tool, same error: The identical tool call (same `command`, same `path`, same
  `pattern`) returned the same `Error returned` or same `stderr` as last time.
  Re-issuing it will not make it pass.
- No new evidence: A turn that produced no new file changes, no new test results, no new
  verified output, and no new search hits. If you have nothing new to show, you have not
  advanced.
- Reasoning without action: You are writing about what you *would* do instead of calling
  a tool that does it.

If you detect any of the above — especially a repeated turn — do not retry the same
failing action. Call it off way sooner:

1. On the second identical reminder with no progress, stop reasoning and escape
   immediately: Call `update_goal` with `status: "cleared"` (if the user's last
   instruction was to not do the work), `status: "parked"` (to pause), or
   `status: "blocked"` with a `blocker` (if genuinely stuck). Do this on the second
   occurrence, not the fifth.
1. Otherwise, try a different route immediately: Change the tool, change the input, or
   shrink to the smallest verifiable step. A different directory, a different pattern, a
   single file read — anything that yields new information.
1. If no route can pass, mark the blocker honestly rather than spinning. A clear
   `blocked` with evidence is progress; a loop is not. Repeating a failing command
   verbatim will not make it pass.

This reminder is sent **behind the scenes as a system note** — it does not appear in the
user-visible transcript, so state what you did and what it showed so the next step can
be read.
