Create new tasks in the task list. A task can depend on another.

- Reach for this early: the moment two or more things wait, or one request holds distinct parts, create the entries. Make one entry per request and use `dependencies` to set the order.
- **The list accumulates, and never replaces.** A new request joins what is already there; five requests mean five entries, and an earlier pending one is never dropped to make room.
- Use this to break complex work into steps that run in parallel or in order. A task with no dependency can start at once; one with a dependency waits for it to complete.
- Keep each task short and factual, tied to work somebody can observe. Skip the list for work your next response finishes. Once created, keep it true to reality with `update_tasks`.

Arguments:

- `tasks` — A list of task objects. Each holds:
  - `title` (required) — A short phrase naming the task, like a tool call's explanation.
  - `description` (required) — What somebody must do.
  - `dependencies` (optional) — A list of task identifiers this task waits for, given as plain indexes, such as ["1", "3", ...].
