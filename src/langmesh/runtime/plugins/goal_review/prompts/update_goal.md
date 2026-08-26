Set the session's single goal: the outcome the work is for, and what would prove it reached.

- Never set a goal on your own initiative. Set one only when the user explicitly asks for a goal to be set — never because work looks multi-step, ambitious or long-running. When no goal was asked for, do not call this tool at all.
- **Goal mode is not a task list.** For complex multi-step work — many distinct steps, ordering, or parallel pieces — use the task list (`set_tasks` / `update_tasks`) instead; the goal is a single completion contract, a totally different mechanism. Set a goal only where the user asks for one concrete end state that takes several calls, edits or checks to reach; skip it for a small one-shot answer. Set it early — at the point you understand what is being asked, not once you are nearly done.
- You set the goal and you own its `status`. Use `status` to say where it stands: `active` while you keep working, `satisfied` once you believe the end state holds, `blocked` when something genuinely stops it, `parked` to set it aside, `cleared` when it is no longer wanted. `parked` and `cleared` apply directly.
{{ reviewer_clause }}
{{ agent_clause }}
- Do not end a turn on an open goal without saying where it stands. If the goal is genuinely met, mark it `satisfied` rather than declaring victory in prose and walking away — {{ satisfied_consequence }}. If you are not done, keep `status` `active` and continue, or mark a blocker honestly.

Arguments:
- `goal` — The end state, written so it is either true or not: "the importer handles paginated responses and the existing tests still pass", not "work on the importer".
- `purpose` — What that end state is for, in the user's terms; the reason a closed route can be told apart from a lost goal.
- `requirements` — The minimum conditions that must hold, each one something a reader could go and look at. Vague ones ("tests pass", "it works") make a goal that cannot be audited.
- `status` — Where the goal stands: `active` (default), `satisfied`, `blocked`, `parked`, or `cleared`.
