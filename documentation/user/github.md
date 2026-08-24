# GitHub mentions

A comment that contains `@langmesh[bot]` on an issue or pull request starts a library session in a GitHub Action. `@langmesh` still works so existing comments keep firing. A reply to one of the bot's comments — a quote, a review reply, or the next comment after the bot — also starts a turn, without another handle. The user account [`@langmesh`](https://github.com/langmesh) already exists, so GitHub will not let you register an App named `LangMesh` and there is no `langmesh[bot]` to install. Type `@langmesh[bot]` anyway to start a turn; after you install your own App, type that App's `@slug[bot]` instead. A later mention or reply on the same thread continues that session. The agent answers both: an issue with file edits opens a draft pull request; a pull-request mention updates that PR.

This is the library in a short-lived job, not the daemon. The workflow lives at `.github/workflows/langmesh.yml`; the session is composed in `langmesh.github.mention`. Allowed owner, member, and collaborator comments start the job; the ack step (`ack.py` plus `detect.py`) decides whether the comment is a mention or a reply to the bot, and only then posts the acknowledgement and runs the session. The job names the thread and the comment as pointers; it does not paste earlier comments into the session. That acknowledgement comes from `acknowledgement.md` and `working_comment.md`, after checkout and before the venv, rendered by `PackagePromptLoader` loaded from source. It says the job will update it as it has progress, and carries a link to the Action, whose log streams live in the GitHub UI. The agent writes that same comment through `submit_github_comment`: a short progress note when the direction changes (`kind` `progress`), then the reply (`kind` `reply`). Model prose is not posted. Failures are written to the Action log with the same logger the daemon uses; the thread only gets a short, user-facing note and a link to that log. Real prompts (the system prompt, the turn, the tool description, the missing-call reminder, the uncommitted-changes reminder, the invalid-model note, the acknowledgement, the working comment) are markdown templates under `src/langmesh/github/prompts/`. Short strings the Action itself writes (`Done.`) stay in code.

## How to cite the agent

GitHub's `@` box only suggests **users**, **teams**, and **installed GitHub Apps**. It does not suggest free text.

| What you type | What GitHub does | What LangMesh does |
|---|---|---|
| `@langmesh[bot]` | Looks for a bot login that cannot be registered: GitHub reserved `LangMesh` for [`@langmesh`](https://github.com/langmesh). This spelling does **not** notify that user. GitHub will not suggest it. | Starts (or continues) the session. Use this until you have your own App. |
| `@your-slug[bot]` | Mentions the App you installed (for example `@langmesh-agent[bot]`). After that bot has commented, GitHub recommends this handle. | Starts the session for `@langmesh-…[bot]`, or when `.github/langmesh.yaml` sets `mention` to this handle. Use this once the App is installed. |
| `@langmesh` | Mentions the [`@langmesh`](https://github.com/langmesh) user. They get a notification. | Still starts the session, so old comments keep working. Do not use this. |
| Quote reply on a bot comment | Inserts a blockquote of the previous comment. | Starts a turn when that previous comment is the bot, even without a handle. |
| Review reply on a bot comment | Sets `in_reply_to_id` to the bot's review comment. | Starts a turn, even without a handle. |
| The next comment after the bot | An ordinary issue comment with no handle and no quote. | Starts a turn when the immediately previous thread comment is the bot. |
| A handle inside backticks or a fenced code block | Rendered as code, not a mention. | Ignored. |

Write the handle in the comment body the same way you would address a teammate. A follow-up on the same issue or pull request can cite that handle again, or reply to the bot without another handle — a quote, a review reply, or the comment immediately after the bot.

The agent's own comments must not contain `@langmesh`, `@langmesh[bot]`, or your `mention` handle. That keeps a reply from looking like a new mention; bot authors are ignored in any case.

Until your App is installed and has posted on a thread, GitHub has no bot to suggest, so you type `@langmesh[bot]` yourself.

## Turn it on

1. Enable Actions on the repository. Under **Settings → Actions → General → Workflow permissions**, allow GitHub Actions to create and approve pull requests. The workflow asks for write access to contents, issues, and pull requests.
2. Put the API key for the provider named in `.agents/agents/github/AGENT.md` in the secret file `github.api_key`. On a GitHub-hosted runner that file is `.github/secrets/github.api_key` in the checkout (the job copies it onto `$XDG_DATA_HOME/langmesh/secrets`). On a self-hosted runner you can keep it only under XDG. The App id belongs in `.github/langmesh.yaml`.
3. The model is `provider` and `model` in that agent profile. Change the profile in git to pick a different one. `.github/langmesh.yaml` names the profile (`agent: github`).
4. Keep the workflow file on the default branch. GitHub runs comment workflows from that copy.
5. Optionally install a GitHub App so comments come from your bot and GitHub recommends that handle. Put the App id in `.github/langmesh.yaml` as `app_id`, and the PEM in `.github/secrets/github.app.private_key`. See [Install a GitHub App so GitHub can suggest the bot](#install-a-github-app-so-github-can-suggest-the-bot).

The left-hand side of the profile's `provider` is a LangMesh provider name (`anthropic`, `openai`, `openrouter`, and the rest of the catalogue). `github.api_key` is the one file for that key (or `providers.<id>.api_key`). GitHub-hosted runners only see files that are in the checkout: on a private repository you can commit them (`git add -f`); on a public repository that publishes the key.

## Install a GitHub App so GitHub can suggest the bot

You do this once, in the GitHub UI. The Action cannot register the App for you. After it is installed and has posted once, typing `@` on that thread should offer your bot.

### 1. Register the App

On your user account open [Register a new GitHub App](https://github.com/settings/apps/new). For an organization, use **Organization settings → GitHub Apps → New GitHub App**.

Fill it in as follows:

- **GitHub App name:** an available name that still contains `LangMesh`, for example `LangMesh Agent`. GitHub turns that into a slug (`langmesh-agent`) and a bot login (`langmesh-agent[bot]`). The name `LangMesh` is reserved for the [`@langmesh`](https://github.com/langmesh) user, so GitHub rejects it with *Name is reserved for the account @langmesh*. The name must be unique on GitHub. Keep `langmesh` in the slug so a comment that cites the bot is recognized even when `.github/langmesh.yaml` has no `mention` key.
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

1. Copy **App ID**. That integer is `app_id` in `.github/langmesh.yaml`.
2. Under **Private keys**, click **Generate a private key**. GitHub downloads a `.pem` file. The whole file, including the `BEGIN` and `END` lines, is `.github/secrets/github.app.private_key` (mode `0600`).
3. Note the slug in that URL. The bot login is `@<slug>[bot]`. If the slug is not `langmesh-…`, set `mention: "@<slug>[bot]"` in `.github/langmesh.yaml`.

Keep the PEM out of a public git history. On a private repository you can commit it so the hosted runner sees it. On a self-hosted runner, the XDG file `$XDG_DATA_HOME/langmesh/secrets/github.app.private_key` is enough.

### 3. Install it on this repository

On the same App page, open **Install App**, choose the account, and grant access to **only** this repository (or to every repository, if you want the same bot everywhere you run the workflow).

You should then see an installation at a URL like `https://github.com/settings/installations/…` with your App listed.

### 4. Give the Action the App PEM

- File `.github/secrets/github.app.private_key` — the PEM contents (or the same name under `$XDG_DATA_HOME/langmesh/secrets` on a self-hosted runner)
- In `.github/langmesh.yaml`, `app_id` — the numeric App ID (not a secret)
- In `.github/langmesh.yaml`, `mention` — `@<slug>[bot]`, only if the slug is not `@langmesh-…[bot]`

The next mention job mints an installation token from the PEM file, then posts and pushes as that bot. Commits use that bot as the author. The App PEM is optional. Without it the job still runs as `github-actions[bot]`, and GitHub will not suggest a bot handle.

### 5. Check that citing works

On an issue or a same-repo pull request, comment `@<slug>[bot]` (or `@langmesh[bot]`) as an owner, member, or collaborator. You should see the acknowledgement appear as your bot, with a link to the live Action. On a longer turn the agent may replace that text with a short status, then with the final reply. On the next comment, type `@` — GitHub should offer that bot.

## What it will and will not do

- Only **owners**, **members**, and **collaborators** are answered. Other comments are ignored.
- Pull requests from forks are ignored.
- Two mentions on the same thread wait their turn rather than overlapping. The later job restores the saved session and appends the new comment as the next turn, so the provider cache from the previous job is reused.
- The job posts an acknowledgement after checkout, from the mention prompt files, before installing the venv — and only when the comment is a mention or a reply to the bot. The acknowledgement says it will update as there is progress, and links the Action so the log can be watched live. The agent **edits that comment** through `submit_github_comment` when the direction of the work changes, then overwrites it with the finished reply or a short failure note. It does not add a second comment for the result.
- `@langmesh[bot]` on an **issue** does the work. If files will change, the agent looks at existing branches and open pull requests first and reuses one that already is this issue's work. Only when nothing fits does it create `langmesh/<slug>-<four-hex-digits>` itself: at most three content words, then four hexadecimal digits. The agent commits and pushes on that branch, and opens a **draft** pull request if one is not already open. A later mention or reply on that issue continues that draft. The Action never marks it ready — a person does that.
- `@langmesh[bot]` on a **pull request** does the work on that PR's own branch, commits, and pushes. It does not open a second PR and does not change whether the PR is a draft.
- The default is to commit and push on a topic branch. The agent is told not to commit or push to `main`, `master`, or the repository default unless the person who mentioned it asked. The job can still push leftover topic-branch commits and will not push the default branch.
- Tool calls run unattended (`automatic`): a call that stays inside the box runs; a call that leaves it, or matches a destructive bash rule, is decided by the permission reviewer. Shell children have network and the job token, so ordinary `git` and `gh` on the topic branch do not raise a gate. If a call is still refused for reach, the agent re-issues it with `access_request`. The token is not written into the checkout.
- The GitHub comment is `submit_github_comment`. A `progress` note keeps the turn open. A `reply` is the answer and ends the turn. If the turn ends without that reply, the session reminds the model until it submits. An empty reply is posted as `Done.` Uncommitted file edits also keep the turn open until the agent commits. At the first model opening of each mention, and every 24 openings after that, the comment plugin appends a reminder to post `progress`. Those notes sit on the conversation tail; they do not rewrite the system prompt or the tool schema.
- The agent writes the commit subject from the request, in the style of this repository's `git log`. It must not invent a prefix such as `langmesh:`. The job does not invent a subject.
- Long threads keep the last 24 turns and drop the rest, with no summarizer call.
- The job does not paste the GitHub thread into the model context. Each turn is one JSON object. The opening turn has `thread`, `thread_url`, `kind`, `comment_url`, `head`, and `comment`. A later mention on the same thread — whether the session restored or the bot already commented — has only `comment_url` and `comment`, so those stable keys are not repeated. Earlier comments, the issue body, and review notes are read with `gh` and the job token. Reply detection follows the same pointers — the parent comment, or the two most recent comments — and does not load the thread.

A follow-up `@langmesh[bot]`, or a reply to the bot, on the same issue or pull request continues the same library session. The id is stable per thread (`github:{repository}:{issue|pull}:{number}`). After a turn, the conversation and the provider cache state are written to `.github/langmesh/session.sqlite`. The acknowledgement comment id and App slug for git author are files in that directory too (`acknowledgement.id`, `app-slug`); they are not environment variables. That directory is gitignored — it is not committed; the Action also unstages it before pushing file edits, so a workflow change under `.github/workflows/` can still land. GitHub issues a read-only Actions cache token for `issue_comment` on the default branch, so the job cannot save that sqlite through `actions/cache`. The previous turn is uploaded as an artifact named `langmesh-session-{issue|pull}-{number}` and the next job restores it. OpenCode Zen is called over plain HTTP with the same headers a working curl uses; the OpenAI-compatible SDK path has made Zen answer that the model is not supported. That HTTP call also sends the thread session id as `x-opencode-session` so Zen can keep the prompt cache on the same conversation. The job uploads that artifact even when the turn fails, so a later mention still restores the prefix. `session.ask` restores that checkpoint before the new mention. A miss still sends the slim follow-up JSON when the bot already commented on the thread. The mention system prompt is the same on every job so the instructions and tool schema stay a reusable provider-cache prefix; it does not assume a particular host repository. GitHub can expire an artifact; a miss starts a fresh conversation.
