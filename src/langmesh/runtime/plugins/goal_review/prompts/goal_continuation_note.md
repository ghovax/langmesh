Work is still pending on your goal, and this turn changed nothing about it:

{{ goal }}

You have not touched what it needs, so you should continue now rather than stop. Take the next concrete step toward the end state with the tools available — the goal exists to be reached. Mark it `satisfied` with `update_goal` only once you hold the result, `blocked` if something genuinely stops it, or keep it `active` and keep working.

---

## Escape hatch — when to stop instead of looping

**To stop the goal entirely**, call `update_goal` with `status: "blocked"` (include a `blocker` explaining what is in the way and what would clear it), `status: "parked"` to set it aside until a person resumes it, or `status: "cleared"` if it is no longer wanted. A `satisfied` mark is also a stop, but only when the end state is truly held.

**Detect a loop before you continue — this is critical:**

- **Repeated turns:** The last 2-3 turns contain the same reasoning, same plan, or same tool sequence with no new outcome. If your current turn would repeat the previous turn verbatim, you are looping.
- **Same tool, same error:** The identical tool call (same `command`, same `path`, same `pattern`) returned the same `Error returned` or same `stderr` as last time. Re-issuing it will not make it pass.
- **No new evidence:** A turn that produced **no new file changes, no new test results, no new verified output, and no new search hits**. If you have nothing new to show, you have not advanced.
- **Reasoning without action:** You are writing about what you *would* do instead of calling a tool that does it.

**If you detect any of the above, do not retry the same failing action.** **Stop and escape:**

1. **Try a different route immediately:** Change the tool, change the input, or shrink to the smallest verifiable step. A different directory, a different pattern, a single file read — anything that yields new information.
2. **If no route can pass, mark the blocker honestly** rather than spinning. A clear `blocked` with evidence is progress; a loop is not. Repeating a failing command verbatim will not make it pass.

This reminder is sent **behind the scenes as a system note** — it does not appear in the user-visible transcript, so state what you did and what it showed so the next step can be read.

