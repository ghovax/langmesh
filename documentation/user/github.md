# GitHub mentions

A comment that contains `@langmesh` on an issue or pull request starts a library session in a GitHub Action. A later mention on the same thread continues that session.

This is the library in a short-lived job, not the daemon. The workflow lives at `.github/workflows/langmesh.yml`; the session is composed in `langmesh.github.mention`. Every string the Action shows or sends — the system prompt, the turn, commit and pull-request text, replies — is a markdown template under `src/langmesh/github/prompts/`, loaded with the package prompt loader.

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
- Long threads compact with the library's ordinary window summarizer, not by dropping recent turns.

A follow-up `@langmesh` on the same issue or pull request restores the saved conversation from the workflow cache.
