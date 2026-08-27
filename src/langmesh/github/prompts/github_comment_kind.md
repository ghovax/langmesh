Which of the two comments this call writes onto the acknowledgement.

- `progress` — a quick "here's where this is headed"; the acknowledgement updates in
  place and you keep working. Use this when meaningful work will continue.
- `reply` — the finish they can read in passing; the acknowledgement updates in place
  and the turn stops. Use this for a brief answer or when the work is complete. Prose,
  lists, and tables are allowed when concise. Address the known `comment_author`
  directly with `@username`, such as `@ghovax`, when present and not the App's own
  account. Use only the username supplied in the turn or a username read from GitHub;
  never guess one.

A brief turn needs one `reply` call and no `progress` call. Do not narrate every
command. Until a `reply` lands, the turn is still open. A `progress` call must not
mention or address a user; reserve direct `@username` mentions for the final `reply` so
progress does not create extra notifications. Never use emoji, ASCII art, diagrams, or
jargon. Use clear human language and keep it concise.
