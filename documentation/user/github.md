# GitHub mentions

A comment that contains `@langmesh[bot]` on an issue or pull request starts a library session in a GitHub Action. `@langmesh` still works so existing comments keep firing. The user account [`@langmesh`](https://github.com/langmesh) already exists, so GitHub will not let you register an App named `LangMesh` and there is no `langmesh[bot]` to install. Type `@langmesh[bot]` anyway to start a turn; after you install your own App, type that App's `@slug[bot]` instead. A later mention on the same thread continues that session. The agent answers both: an issue with file edits opens a draft pull request; a pull-request mention updates that PR.

This is the library in a short-lived job, not the daemon. The workflow lives at `.github/workflows/langmesh.yml`; the session is composed in `langmesh.github.mention`. The job posts an acknowledgement as soon as it has a GitHub token — before checkout and Python. The agent writes that same comment through `submit_github_comment`: a short progress note when the direction changes (`kind` `progress`), then the reply (`kind` `reply`). Model prose is not posted. Failures are written to the Action log with the same logger the daemon uses; the thread only gets a short, user-facing note and a link to that log. Real prompts (the system prompt, the turn, the tool description, the missing-call reminder, the invalid-model note) are markdown templates under `src/langmesh/github/prompts/`. Short strings the Action itself writes (the acknowledgement, a commit message, a pull-request title, `Done.`) stay in code.

## How to cite the agent

GitHub's `@` box only suggests **users**, **teams**, and **installed GitHub Apps**. It does not suggest free text.

| What you type | What GitHub does | What LangMesh does |
|---|---|---|
| `@langmesh[bot]` | Looks for a bot login that cannot be registered: GitHub reserved `LangMesh` for [`@langmesh`](https://github.com/langmesh). This spelling does **not** notify that user. GitHub will not suggest it. | Starts (or continues) the session. Use this until you have your own App. |
| `@your-slug[bot]` | Mentions the App you installed (for example `@langmesh-agent[bot]`). After that bot has commented, GitHub recommends this handle. | Starts the session for `@langmesh-…[bot]`, or when `LANGMESH_MENTION` is this handle. Use this once the App is installed. |
| `@langmesh` | Mentions the [`@langmesh`](https://github.com/langmesh) user. They get a notification. | Still starts the session, so old comments keep working. Do not use this. |
| Quote reply on a bot comment | Inserts a blockquote of the previous comment. | Starts a turn when that quote is a reply to the bot, even without a handle. |
| A handle inside backticks or a fenced code block | Rendered as code, not a mention. | Ignored. |

Write the handle in the comment body the same way you would address a teammate. A follow-up on the same issue or pull request is another comment that cites that handle again. You do not need to quote the previous reply.

The agent's own comments must not contain `@langmesh`, `@langmesh[bot]`, or your `LANGMESH_MENTION` handle. That keeps a reply from looking like a new mention; bot authors are ignored in any case.

Until your App is installed and has posted on a thread, GitHub has no bot to suggest, so you type `@langmesh[bot]` yourself.

## Turn it on

1. Enable Actions on the repository. Under **Settings → Actions → General → Workflow permissions**, allow GitHub Actions to create and approve pull requests. The workflow asks for write access to contents, issues, and pull requests.
2. Set the repository secret `LANGMESH_API_KEY` to the API key for the provider you want.
3. Optionally set the repository variable `LANGMESH_MODEL` to `provider/model`. The value splits on the **first** slash: `anthropic/claude-sonnet-4-5` is Anthropic's `claude-sonnet-4-5`, and `openrouter/anthropic/claude-sonnet-4-5` is OpenRouter's `anthropic/claude-sonnet-4-5`. When the variable is unset, the Action uses `anthropic/claude-sonnet-4-5`.
4. Keep the workflow file on the default branch. GitHub runs comment workflows from that copy.
5. Optionally install a GitHub App so comments come from your bot and GitHub recommends that handle. See [Install a GitHub App so GitHub can suggest the bot](#install-a-github-app-so-github-can-suggest-the-bot).

The left-hand side of `LANGMESH_MODEL` is a LangMesh provider name (`anthropic`, `openai`, `openrouter`, and the rest of the catalogue). The Action does not keep a short list of which of those you may pick. `LANGMESH_API_KEY` is tried first, then that provider's catalogue environment variables, then `{PROVIDER}_API_KEY` with hyphens turned into underscores. If you would rather use the provider's usual name (`ANTHROPIC_API_KEY`, `DASHSCOPE_API_KEY`, `GEMINI_API_KEY`, …), add it to the job `env` in the workflow.

## Install a GitHub App so GitHub can suggest the bot

You do this once, in the GitHub UI. The Action cannot register the App for you. After it is installed and has posted once, typing `@` on that thread should offer your bot.

### 1. Register the App

On your user account open [Register a new GitHub App](https://github.com/settings/apps/new). For an organization, use **Organization settings → GitHub Apps → New GitHub App**.

Fill it in as follows:

- **GitHub App name:** an available name that still contains `LangMesh`, for example `LangMesh Agent`. GitHub turns that into a slug (`langmesh-agent`) and a bot login (`langmesh-agent[bot]`). The name `LangMesh` is reserved for the [`@langmesh`](https://github.com/langmesh) user, so GitHub rejects it with *Name is reserved for the account @langmesh*. The name must be unique on GitHub. Keep `langmesh` in the slug so a comment that cites the bot still contains `@langmesh` and the workflow starts.
- **Homepage URL:** `https://github.com/ghovax/langmesh` (or this repository's URL).
- **Identifying and authorizing users:** leave the callback and setup URLs empty. This App does not sign people in.
- **Webhook:** uncheck **Active**. The mention job is a comment workflow, not an App webhook.
- **Repository permissions:**
  - **Contents:** Read and write (push the topic branch)
  - **Issues:** Read and write (acknowledgement and reply)
  - **Pull requests:** Read and write (draft PRs and PR comments)
  - **Workflows:** Read and write (push changes under `.github/workflows/`)
  - **Metadata:** Read-only (GitHub adds this)
- **Account permissions:** none.
- **Where can this GitHub App be installed?** Only on this account.

Create the App.

### 2. Take the credentials

On the App's settings page (the URL looks like `https://github.com/settings/apps/langmesh-agent`):

1. Copy **App ID**. That integer is `LANGMESH_APP_ID`.
2. Under **Private keys**, click **Generate a private key**. GitHub downloads a `.pem` file. The whole file, including the `BEGIN` and `END` lines, is `LANGMESH_APP_PRIVATE_KEY`.
3. Note the slug in that URL. The bot login is `@<slug>[bot]`.

Keep the PEM out of git. Repository secrets are the only place it should live.

### 3. Install it on this repository

On the same App page, open **Install App**, choose the account, and grant access to **only** this repository (or to every repository, if you want the same bot everywhere you run the workflow).

You should then see an installation at a URL like `https://github.com/settings/installations/…` with your App listed.

### 4. Give the Action the secrets

In the repository: **Settings → Secrets and variables → Actions**.

- Secret `LANGMESH_APP_ID` — the numeric App ID
- Secret `LANGMESH_APP_PRIVATE_KEY` — the PEM contents
- Variable `LANGMESH_MENTION` — `@<slug>[bot]`, for example `@langmesh-agent[bot]`

The slug will not be `langmesh`. Set `LANGMESH_MENTION` to `@<slug>[bot]` so that handle is first-class. A comment that cites `@langmesh-…[bot]` is also recognized without the variable.

The next mention job mints an installation token from those secrets, then posts and pushes as that bot. Commits use that bot as the author. The App secrets are optional. Without them the job still runs as `github-actions[bot]`, and GitHub will not suggest a bot handle.

### 5. Check that citing works

On an issue or a same-repo pull request, comment `@<slug>[bot]` (or `@langmesh[bot]`) as an owner, member, or collaborator. You should see the acknowledgement appear as your bot. On a longer turn the agent may replace that text with a short status, then with the final reply. On the next comment, type `@` — GitHub should offer that bot.

## What it will and will not do

- Only **owners**, **members**, and **collaborators** are answered. Other comments are ignored.
- Pull requests from forks are ignored.
- Two mentions on the same thread wait their turn rather than overlapping.
- The job posts an acknowledgement as soon as it has a token — before installing Python. The agent **edits that comment** through `submit_github_comment` when the direction of the work changes, then overwrites it with the finished reply or a short failure note. It does not add a second comment for the result.
- `@langmesh[bot]` on an **issue** does the work. If files will change, the agent looks at existing branches and open pull requests first and reuses one that already is this issue's work. Only when nothing fits does it create `langmesh/<slug>-<four-hex-digits>` itself: at most three content words, then four hexadecimal digits. The agent commits; the Action pushes and opens a **draft** pull request. A later mention on that issue continues that draft. The Action never marks it ready — a person does that.
- `@langmesh[bot]` on a **pull request** does the work on that PR's own branch and pushes commits. It does not open a second PR and does not change whether the PR is a draft.
- It never pushes `main` or `master`. Tool children cannot `git push`; the job is the only publisher.
- Tool calls run unattended (`automatic`). Shell children start offline; a command that needs the network asks for it on that call. The GitHub token is never written into the checkout.
- The GitHub comment is `submit_github_comment`. A `progress` note keeps the turn open. A `reply` is the answer and ends the turn. If the turn ends without that reply, the session reminds the model until it submits. An empty reply is posted as `Done.`
- The agent writes the commit subject from the request, in the style of this repository. The job does not invent one.
- Long threads keep the last 24 turns and drop the rest, with no summarizer call.

A follow-up `@langmesh[bot]` on the same issue or pull request continues the same library session. The id is stable per thread (`github:{repository}:{issue|pull}:{number}`). After a turn, the conversation and the provider cache state are written to `.github/langmesh/session.sqlite`. That directory is gitignored and is what the workflow caches — it is not committed; the Action also unstages it before pushing file edits, so a workflow change under `.github/workflows/` can still land. The cache key is the repository plus the thread number, and `restore-keys` lets the next job load the previous run's sqlite. The job saves that cache even when the turn fails, so a later mention still restores the prefix. `session.ask` restores that checkpoint before the new mention. The mention system prompt is the same on every job so the instructions and tool schema stay a reusable provider-cache prefix; it does not assume a particular host repository. Which issue or pull request this is, and which branch HEAD is on, are appended on the turn. GitHub can evict a cache; a miss starts a fresh conversation.
