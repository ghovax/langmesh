Read a sibling turn in this session by its id. It returns the turn's current status and
its artifact, which is the deliverable.

- Use this to coordinate with a sibling A2A task id that reached you from outside: check
  whether the sibling finished, read what it produced, build on that.
- **Not** for a background result or a peer session: `search_web` handles (`search-…`)
  and background `bash` handles (`bg-…`) are not readable tasks — their results reach
  you on their own, so never call `read_turn` on one and never poll. To look at a peer
  session, use `read_session`.

Arguments:

- `turn_id` — The id of a sibling turn that reached you from outside.
