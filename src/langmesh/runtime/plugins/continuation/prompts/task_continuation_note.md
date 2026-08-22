Tracked work remains unfinished:

{{ tasks }}

Continue working now. First reassess the user's requests and the conversation: identify anything you should have done that is missing from the task list, and add it with `set_tasks`. Keep the list truthful with `update_tasks`, then use the available tools to advance and complete the work instead of merely describing what you would do. Mark work complete only after verifying it; if something genuinely cannot advance, mark it blocked and explain the concrete blocker.

---

## Escape hatch — when to stop instead of looping

**To stop a task that cannot proceed**, call `update_tasks` with `status: "blocked"` and a concrete `blocker` describing what is in the way and what would clear it. A task that is done is marked `completed`; a task no longer wanted is marked `cancelled`. Use these honestly — they are the intended way to end work that cannot continue.

**Detect a loop before you continue — this is critical:**

- **Repeated turns:** The last 2-3 turns contain the same reasoning, same plan, or same tool sequence with no new outcome. If your current turn would repeat the previous turn verbatim, you are looping.
- **Same tool, same error:** The identical tool call (same `command`, same `path`, same `pattern`) returned the same `Error returned` or same `stderr` as last time. Re-issuing it will not make it pass.
- **No new evidence:** A turn that produced **no new file changes, no new test results, no new verified output, and no new search hits**. If you have nothing new to show, you have not advanced.
- **Reasoning without action:** You are writing about what you *would* do instead of calling a tool that does it.

**If you detect any of the above, do not retry the same failing action.** **Stop and escape:**

1. **Try a different route immediately:** Change the tool, change the input, or shrink to the smallest verifiable step (e.g., `ls` a single file instead of a broad search; read one file instead of globbing).
2. **If no route can pass, mark the blocker honestly** rather than spinning. A clear `blocked` with evidence is progress; a loop is not.

**Avoid endless loops by choosing a different tool, a different input, or a smaller verifiable step.** This reminder is sent **behind the scenes as a system note** — it will not appear as a user message, but what you do next *will* be read, so make it checkable. State what you did and what it showed.

