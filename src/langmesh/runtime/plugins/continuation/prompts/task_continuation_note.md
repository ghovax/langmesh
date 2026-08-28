Tracked work remains unfinished:

{{ tasks }}

Continue working now. First reassess the user's requests and the conversation: identify
anything you should have done that is missing from the task list, and add it with
`set_tasks`. Keep the list truthful with `update_tasks`, then use the available tools to
advance and complete the work instead of merely describing what you would do. Mark work
complete only after verifying it; if something genuinely cannot advance, mark it blocked
and explain the concrete blocker.

**You are responsible for preventing all loops and issues from even arising. This is
your job, not the system's — the harness will not stop you, you must stop yourself. Do
not wait for the system to rescue you.**

______________________________________________________________________

## Escape hatch — when to stop instead of looping

**To stop a task that cannot proceed**, call `update_tasks` with `status: "blocked"` and
a concrete `blocker` describing what is in the way and what would clear it. A task that
is done is marked `completed`; a task no longer wanted is marked `cancelled`. Use these
honestly — they are the intended way to end work that cannot continue.

Check for problems and denials upfront — even before a loop, on the very first turn:

- Denied / permission errors: If the last tool result was `permission_denied`, `denied`,
  `Error`, or `invalid_command`, or the previous turn was blocked on a decision, do not
  retry the same call. The problem is already known — handle the denial, explain what is
  in the way, and `blocked` immediately.
- Problematic requirements: If the task's requirements are already impossible, a file is
  missing, or a location does not exist, detect it now and `blocked`/`cancelled` without
  attempting work that cannot succeed.
- Blocked evidence already in history: If the conversation already contains a clear
  blocker, acknowledge it and stop — do not re-prove it.

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
   immediately: Call `update_tasks` with `status: "blocked"`/`"cancelled"` or mark the
   individual task `blocked`. Do this on the second occurrence, not the fifth.
1. Otherwise, try a different route immediately: Change the tool, change the input, or
   shrink to the smallest verifiable step (e.g., `ls` a single file instead of a broad
   search; read one file instead of globbing).
1. If no route can pass, mark the blocker honestly rather than spinning. A clear
   `blocked` with evidence is progress; a loop is not.

This reminder is sent **behind the scenes as a system note** — it will not appear as a
user message, but what you do next *will* be read, so make it checkable. State what you
did and what it showed.
