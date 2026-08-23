# GitHub mentions

A comment that contains `@langmesh` on an issue or pull request starts a library session in a GitHub Action. A later mention on the same thread continues that session.

This is the library in a short-lived job, not the daemon. The workflow lives at `.github/workflows/langmesh.yml`; the session is composed in `langmesh.github.mention`. The comment that lands on the thread is collected by the `GitHubReply` plugin through `submit_github_comment` — model prose is not posted. Real prompts (the system prompt, the turn, the tool description, the missing-call reminder, the invalid-model note) are markdown templates under `src/langmesh/github/prompts/`. Short strings the Action itself writes (a commit message, a pull-request title, `Done.`) stay in code.

## Turn it on

1. Set the repository secret `LANGMESH_API_KEY` to the API key for the provider you want.
2. Optionally set the repository variable `LANGMESH_MODEL` to `provider/model`. The value splits on the **first** slash: `anthropic/claude-sonnet-4-5` is Anthropic's `claude-sonnet-4-5`, and `openrouter/anthropic/claude-sonnet-4-5` is OpenRouter's `anthropic/claude-sonnet-4-5`. When the variable is unset, the Action uses `anthropic/claude-sonnet-4-5`.
3. Keep the workflow file on the default branch. GitHub runs comment workflows from that copy.

The left-hand side of `LANGMESH_MODEL` is a LangMesh provider name (`anthropic`, `openai`, `openrouter`, and the rest of the catalogue). The Action does not keep a short list of which of those you may pick: whatever you put left of the slash is the provider, and `LANGMESH_API_KEY` is handed to it. If you would rather use the provider's usual environment variable (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, …), add it to the job `env` in the workflow; the library still reads those names.

## What it will and will not do

- Only **owners**, **members**, and **collaborators** are answered. Other comments are ignored.
- Pull requests from forks are ignored.
- Two mentions on the same thread wait their turn rather than overlapping.
- The agent may edit files. The Action then commits a topic branch (`langmesh/issue-N` or the pull request's own branch) and opens or updates a pull request. It never pushes `main` or `master`.
- Tool calls run unattended (`automatic`). Force-push and pushes to the default branch are denied. Network is off for shell children; the GitHub token is never written into the checkout.
- The GitHub comment must be submitted with `submit_github_comment`. If the turn ends without that call, the session reminds the model a few times, then posts `Done.`
- Long threads keep the last 24 turns and drop the rest, with no summarizer call.

A follow-up `@langmesh` on the same issue or pull request restores the saved conversation from the workflow cache.
