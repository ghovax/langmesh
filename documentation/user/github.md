# GitHub mentions

A comment that contains `@langmesh` on an issue or pull request starts a library session in a GitHub Action. A later mention on the same thread continues that session.

This is the library in a short-lived job, not the daemon. The workflow lives at `.github/workflows/langmesh.yml`; the session is composed in `langmesh.github.mention`.

## Turn it on

1. Add a repository secret named `ANTHROPIC_API_KEY` (or another provider key, such as `OPENAI_API_KEY`).
2. Keep the workflow file on the default branch. GitHub runs comment workflows from that copy.

## What it will and will not do

- Only **owners**, **members**, and **collaborators** are answered. Other comments are ignored.
- Pull requests from forks are ignored.
- Two mentions on the same thread wait their turn rather than overlapping.
- The agent may edit files. The Action then commits a topic branch (`langmesh/issue-N` or the pull request's own branch) and opens or updates a pull request. It never pushes `main` or `master`.
- Tool calls run unattended (`automatic`). Force-push and pushes to the default branch are denied. Network is off for shell children; the GitHub token is never written into the checkout.

A follow-up `@langmesh` on the same issue or pull request restores the saved conversation from the workflow cache.
