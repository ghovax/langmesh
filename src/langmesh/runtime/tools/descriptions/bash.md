Run a bash command and return its real output; this is how you read, search and change the filesystem.

- Use `background=True` only for long work whose result you do not need before the turn continues (a build, a test suite, a dev server, a broad scan). It returns at once with a task id; its result reaches you when it finishes. Never background a command whose output you need next, and never run a backgrounded command again.

Operating on the filesystem:
- Survey before you read: use the narrowest command that answers — a matcher, count, list or structural query; read a whole file only when you genuinely need all of it.
- Batch what belongs together: combine independent reads into one call, chain deterministic steps with `&&` and pipes, and join results that feed a later step. Keep a read and its dependent edit in separate calls; verify by reading back what you wrote — an exit code says it ran, not that it did the right thing.

Parallel batches:
- Issue several independent tool calls in the same turn; within one bash call combine every independent query, read or check.
- Python is your Swiss-army knife: parsing, restructuring, querying, orchestrating. Prefer `uv run python`, `uvx` for ephemeral tools, `uv run --with <pkg> python` for ad-hoc packages; bare `python` only when `uv` is unavailable. Prefer Python over shell string surgery. Semble is cheap — use it freely with fresh disposable indexes, and combine it with exact searches.

Nothing interactive:
- No terminal is attached and nobody can type into it: a command that waits for input waits until timeout, then backgrounds still waiting. Pass the answering flag (`-y`, `--non-interactive`, `--no-input`), stop pagers before they start, and never open an editor, REPL, bare interpreter, interactive `git`, or anything that asks for a password, including `sudo`. Where a step truly needs a person, use `ask_user`.

Saying what a command reaches:
- Use `access_request` to state whether the command changes anything, adding paths or network only for reach beyond confinement. Ask for the narrowest reach that does the work.

Arguments:
- `command` — The shell command to run.
- `location` — Which workspace location runs the command — its URI or name, from the locations listed in your context. Defaults to the local filesystem. Pass it only to reach a different, remote location.
- `access_request` — What this command says about changing anything, and what it needs beyond the session's confinement. Always set `mutates`; add `writes`, `reads`, or `network` only for reach the confinement does not already provide.
- `explanation` — Why the task needs this command.
- `background` — Run the command in the background instead of waiting for it. Use this for long work whose result you do not need now.
- `timeout` — How many seconds to wait for the command before it moves to the background, where its result reaches you when it finishes. Raise it for a command you want to wait longer for. It does not kill the command.
