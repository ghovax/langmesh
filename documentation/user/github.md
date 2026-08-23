# GitHub mentions

A comment that contains `@langmesh[bot]` on an issue or pull request starts a library session in a GitHub Action. `@langmesh` still works so existing comments keep firing; `@langmesh[bot]` is the handle to use. A later mention on the same thread continues that session. The agent answers both: an issue with file edits opens a draft pull request; a pull-request mention updates that PR.

This is the library in a short-lived job, not the daemon. The workflow lives at `.github/workflows/langmesh.yml`; the session is composed in `langmesh.github.mention`. The job posts an acknowledgement immediately, then updates that same comment when the work is done. The text that replaces the acknowledgement is collected by the `GitHubReply` plugin through `submit_github_comment` — model prose is not posted. Failures are written to the Action log with the same logger the daemon uses; the thread only gets a short, user-facing note and a link to that log. Real prompts (the system prompt, the turn, the tool description, the missing-call reminder, the invalid-model note) are markdown templates under `src/langmesh/github/prompts/`. Short strings the Action itself writes (the acknowledgement, a commit message, a pull-request title, `Done.`) stay in code.

## How to cite the agent

GitHub's `@` box only suggests **users**, **teams**, and **installed GitHub Apps**. It does not suggest free text.

| What you type | What GitHub does | What LangMesh does |
|---|---|---|
| `@langmesh[bot]` | Mentions the App bot, if one is installed. Does **not** notify a person named `langmesh`. After that bot has commented on the thread, GitHub recommends this handle when you type `@lang`. | Starts (or continues) the session. This is the handle to use. |
| `@langmesh` | Treated as a **user** mention. If someone owns the `langmesh` login, they get a notification. GitHub will not suggest our agent for this spelling. | Still starts the session, so old comments keep working. Do not use this for new mentions. |
| Quote reply on a bot comment | Inserts a blockquote. It does **not** address the bot and does **not** start a turn. | Nothing, unless the new comment also contains `@langmesh[bot]`. |
| `` `@langmesh[bot]` `` in backticks, or the handle inside a fenced code block | Rendered as code, not a mention. | Ignored. |

Write the handle in the comment body as `@langmesh[bot]`, the same way you would address a teammate. A follow-up on the same issue or pull request is another comment that cites `@langmesh[bot]` again. You do not need to quote the previous reply.

The agent's own comments must not contain `@langmesh` or `@langmesh[bot]`. That keeps a reply from looking like a new mention; bot authors are ignored in any case.

Until a LangMesh GitHub App is installed and has posted on a thread, GitHub has no bot to suggest, so you type `@langmesh[bot]` yourself. That still runs the Action, and it still will not notify a person named `langmesh`.

## Turn it on

1. Enable Actions on the repository. Under **Settings → Actions → General → Workflow permissions**, allow GitHub Actions to create and approve pull requests. The workflow asks for write access to contents, issues, and pull requests.
2. Set the repository secret `LANGMESH_API_KEY` to the API key for the provider you want.
3. Optionally set the repository variable `LANGMESH_MODEL` to `provider/model`. The value splits on the **first** slash: `anthropic/claude-sonnet-4-5` is Anthropic's `claude-sonnet-4-5`, and `openrouter/anthropic/claude-sonnet-4-5` is OpenRouter's `anthropic/claude-sonnet-4-5`. When the variable is unset, the Action uses `anthropic/claude-sonnet-4-5`.
4. Keep the workflow file on the default branch. GitHub runs comment workflows from that copy.
5. Optionally install a LangMesh GitHub App so comments come from `langmesh[bot]` and GitHub recommends the handle. See [Make GitHub recommend `@langmesh[bot]`](#make-github-recommend-langmeshbot).

The left-hand side of `LANGMESH_MODEL` is a LangMesh provider name (`anthropic`, `openai`, `openrouter`, and the rest of the catalogue). The Action does not keep a short list of which of those you may pick. `LANGMESH_API_KEY` is tried first, then that provider's catalogue environment variables, then `{PROVIDER}_API_KEY` with hyphens turned into underscores. If you would rather use the provider's usual name (`ANTHROPIC_API_KEY`, `DASHSCOPE_API_KEY`, `GEMINI_API_KEY`, …), add it to the job `env` in the workflow.

## Make GitHub recommend `@langmesh[bot]`

You do this once, in the GitHub UI. The Action cannot register the App for you. After it is installed and has posted once, typing `@lang` on that thread should offer `langmesh[bot]`.

### 1. Register the App

On your user account open [Register a new GitHub App](https://github.com/settings/apps/new). For an organization, use **Organization settings → GitHub Apps → New GitHub App**.

Fill it in as follows:

- **GitHub App name:** `LangMesh`. The slug becomes `langmesh`, and the bot login is `langmesh[bot]`. The name must be unique on GitHub. If it is taken, pick another name and set `LANGMESH_MENTION` later.
- **Homepage URL:** `https://github.com/ghovax/langmesh` (or this repository's URL).
- **Identifying and authorizing users:** leave the callback and setup URLs empty. This App does not sign people in.
- **Webhook:** uncheck **Active**. The mention job is a comment workflow, not an App webhook.
- **Repository permissions:**
  - **Contents:** Read and write (push the topic branch)
  - **Issues:** Read and write (acknowledgement and reply)
  - **Pull requests:** Read and write (draft PRs and PR comments)
  - **Metadata:** Read-only (GitHub adds this)
- **Account permissions:** none.
- **Where can this GitHub App be installed?** Only on this account.

Create the App.

### 2. Take the credentials

On the App's settings page (the URL looks like `https://github.com/settings/apps/langmesh`):

1. Copy **App ID**. That integer is `LANGMESH_APP_ID`.
2. Under **Private keys**, click **Generate a private key**. GitHub downloads a `.pem` file. The whole file, including the `BEGIN` and `END` lines, is `LANGMESH_APP_PRIVATE_KEY`.

Keep the PEM out of git. Repository secrets are the only place it should live.

### 3. Install it on this repository

On the same App page, open **Install App**, choose the account, and grant access to **only** this repository (or to every repository, if you want the same bot everywhere you run the workflow).

You should then see an installation at a URL like `https://github.com/settings/installations/…` with **LangMesh** listed.

### 4. Give the Action the secrets

In the repository: **Settings → Secrets and variables → Actions**.

- Secret `LANGMESH_APP_ID` — the numeric App ID
- Secret `LANGMESH_APP_PRIVATE_KEY` — the PEM contents

If the App slug is not `langmesh`, also set the repository **variable** `LANGMESH_MENTION` to `@your-slug[bot]`.

The next mention job mints an installation token from those secrets, then posts and pushes as `langmesh[bot]`. Commits use that bot as the author. The App secrets are optional. Without them the job still runs as `github-actions[bot]`, and GitHub will not suggest `@langmesh[bot]`.

### 5. Check that citing works

On an issue or a same-repo pull request, comment `@langmesh[bot]` as an owner, member, or collaborator. You should see the acknowledgement appear as **langmesh[bot]**, then that comment update when the turn finishes. On the next comment, type `@lang` — GitHub should offer `langmesh[bot]`.

## What it will and will not do

- Only **owners**, **members**, and **collaborators** are answered. Other comments are ignored.
- Pull requests from forks are ignored.
- Two mentions on the same thread wait their turn rather than overlapping.
- The job posts an acknowledgement at once and **edits that comment** with the reply or with a short failure note. It does not add a second comment for the result.
- `@langmesh[bot]` on an **issue** does the work. If files will change, the agent looks at existing branches and open pull requests first and reuses one that already is this issue's work. Only when nothing fits does it create `langmesh/<descriptive-name>-<four-hex-digits>` itself. The Action then commits, pushes, and opens a **draft** pull request. A later mention on that issue continues that draft. The Action never marks it ready — a person does that.
- `@langmesh[bot]` on a **pull request** does the work on that PR's own branch and pushes commits. It does not open a second PR and does not change whether the PR is a draft.
- It never pushes `main` or `master`. Tool children cannot `git push`; the job is the only publisher.
- Tool calls run unattended (`automatic`). Network is off for shell children; the GitHub token is never written into the checkout.
- The GitHub comment must be submitted with `submit_github_comment`. If the turn ends without that call, the session reminds the model until it submits. An empty submitted comment is posted as `Done.`
- Long threads keep the last 24 turns and drop the rest, with no summarizer call.

A follow-up `@langmesh[bot]` on the same issue or pull request continues the same library session. The id is stable per thread (`github:{repository}:{issue|pull}:{number}`). After a turn, the conversation and the provider cache state are written to `.github/langmesh/session.sqlite`. That directory is gitignored and is what the workflow caches — it is not committed; the Action also unstages it before pushing file edits, so a workflow change under `.github/workflows/` can still land. The cache key is the repository plus the thread number, and `restore-keys` lets the next job load the previous run's sqlite. The job saves that cache even when the turn fails, so a later mention still restores the prefix. `session.ask` restores that checkpoint before the new mention. The mention system prompt is the same on every job so the instructions and tool schema stay a reusable provider-cache prefix; which issue or pull request this is, and which branch HEAD is on, are appended on the turn. GitHub can evict a cache; a miss starts a fresh conversation.
