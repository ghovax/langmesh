Run a bash command and return its output. This is how you read the filesystem, search it, and change it.

The command runs to completion and you get its real output. Set `background=True` only for long work whose result you do not need before your turn continues — a build, a test suite, a development server, a broad scan. A backgrounded command returns at once with a task identifier; its result reaches you when it finishes and the harness re-engages you then. Never background a command whose output you need next, and never run a backgrounded command again.

## Operating on the filesystem

Survey before you read: use the narrowest command that answers the question — a matcher for the lines that mention something, a count or a list for a size, a structural query for a definition — and read a whole file only when you genuinely need all of it. Batch what belongs together: combine independent reads into one call, chain deterministic steps with `&&` and pipes, and join the results that feed a later step. Keep a read and the edit that depends on it in separate calls, and keep the step that computes separate from the step that overwrites. Verify by reading back what you wrote, since an exit code says a command ran, not that it did the right thing.

## Work in parallel batches

Issue several independent tool calls together in the same turn instead of one at a time; there is no ceiling on how many run at once. Within a single bash call, combine every independent query, read, or check into one command — several searches, listings, greps, or Python evaluations chained with `&&`, `|`, or a heredoc — with no ceiling on how many queries one call may contain. Keep a read and the edit that depends on it in separate calls, but batch all independent reads together and all independent edits together.

Python is your Swiss-army knife and there is no task it is out of scope for: use it for parsing, restructuring, querying, analyzing, generating, and orchestrating work of any shape, and compose it freely with other commands — pipe its output through further shell tools, wrap several shell commands inside a Python heredoc, or have one call do many independent things at once. Prefer it over shell string surgery whenever it would be clearer, safer, or faster, and do not ration it. Semble is a key tool for locating code and mechanisms semantically; use it freely, treat it as cheap enough to run repeatedly with a fresh disposable index, and combine it with exact searches to verify and narrow results.

## Nothing you run may ask a question

No terminal is attached and nobody can type into it: a command that waits for input waits until timeout, then backgrounds still waiting. Say up front what an interactive command would have asked — pass the answering flag (`-y`, `--non-interactive`, `--no-input`), stop pagers before they start, and never open an editor, a REPL, a bare interpreter, an interactive `git`, or anything that asks for a password, including `sudo`. Where a step truly needs a person, use `ask_user` or tell the user to run it themselves.

## Saying what a command reaches

Use `access_request` to state whether the command changes anything, adding paths or the network only for reach beyond your confinement. Read that confinement first: a write outside it fails with a permission error that names no path. Ask for the narrowest reach that does the work.

This call takes these arguments:

- `command` — The shell command to run.
- `location` — Which workspace location runs the command — its URI or name, from the locations listed in your context. Defaults to the local filesystem. Pass it only to reach a different, remote location.
- `access_request` — What this command says about changing anything, and what it needs beyond the session's confinement. Always set `mutates`; add `writes`, `reads`, or `network` only for reach the confinement does not already provide.
- `explanation` — Why the task needs this command.
- `background` — Run the command in the background instead of waiting for it. Use this for long work whose result you do not need now.
- `timeout` — How many seconds to wait for the command before it moves to the background, where its result reaches you when it finishes. Raise it for a command you want to wait longer for. It does not kill the command.
