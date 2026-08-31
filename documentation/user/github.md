# Universal GitHub App

LangMesh Agent is an installation-level GitHub App. [Install it from GitHub](https://github.com/apps/langmesh-agent) on a personal account or organization, choosing the repositories it may access. A repository only needs the App installed; it does not need a workflow, YAML policy, App ID, provider setting, API key, or GitHub secret.

The service receives issue, pull request, issue-comment, and pull-request-review-comment webhooks, creates a repository-scoped installation token, and runs the session as the installed App. New issues and pull requests receive an automatic first response; later human comments start turns. The App private key belongs only to the service operator. It is never entered by a person configuring an installation and is never stored in a repository.

## Service configuration

Run the hosted service outside a repository:

```sh
langmesh github --configuration ~/.config/langmesh/github.yaml
```

The configuration file is an operator/deployment file, not a repository file. It points at the App private key, webhook secret, and encryption key. Keep those files in a secret manager or a locked service directory. A complete shape is:

```yaml
github:
  app:
    id: "2149876"
    private_key_path: "/srv/langmesh/secrets/langmesh-agent.2026-08.pem"
  webhook:
    secret: "langmesh-gh-..."
  oauth:
    client_id: "Iv1...."
    client_secret: "..."
    provider_application_ids:
      chatgpt: "app_4f8c2d1e7a9b"
      cursor: "cursor_oauth_8a2f6c1d"
  api_url: "https://api.github.com"
server:
  public_url: "https://github-agent.example.net"
  shutdown_grace_seconds: 25.0
compaction:
  automatic: true
  reclaim_at_fraction: 0.9
  output_reserve_fraction: 0.1
  recent_working_set_fraction: 0.15
storage:
  database:
    url: "postgresql+asyncpg://postgres:...@db.qxwzjvkrmno.supabase.co:5432/postgres?ssl=require"
  encryption:
    key_path: "/srv/langmesh/secrets/provider-keys.fernet"
  queue:
    poll_seconds: 5
    maximum_delivery_attempts: 5
```

Each delivery is attempted at most `maximum_delivery_attempts` times. After the last failure, the service stores the delivery as failed and stops scheduling it; the existing acknowledgement comment is updated instead of creating another comment. A delivery that cannot yet load its installation configuration remains retryable and is never marked completed as if it had been handled. The default is five attempts when this value is omitted.

The hosted processor does not impose a wall-clock deadline on a whole turn. Model verdict calls and protocol loops carry their own attempt and time budgets, command tools enforce their own limits, and a worker restart recovers a failed delivery from its durable checkpoint. This lets long but productive work finish without permitting an unbounded model retry loop.

GitHub sessions use `allow` permission mode: the bot does not stop for approval prompts. Render's operating-system confinement still limits what each tool can reach, so `allow` removes the approval gate but does not remove the sandbox.

Compaction uses the selected model's advertised context window from models.dev. For an OAuth-backed model, the live provider catalogue takes precedence when available; `maximum_context_tokens` is left unset so the service does not impose an unrelated context ceiling.

`storage.encryption.key_path` must contain a Fernet key. Generate one once with:

```sh
python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())' \
  > /srv/langmesh/secrets/provider-keys.fernet
chmod 600 /srv/langmesh/secrets/provider-keys.fernet
```

The service stores provider API keys and OAuth tokens encrypted in the external database, keyed by GitHub installation. The GitHub worker uses the compaction settings from this operator file with a direct preparation port. For a ChatGPT OAuth installation, it refreshes the authenticated live model catalogue before building the session, so the model's reported context window is used automatically; when that catalogue is unavailable, the models.dev value remains the fallback. Compaction intentionally invalidates the conversation portion of the provider cache; the stable instructions and tool definitions remain reusable. The delivery queue and session checkpoints use that same database, so another worker can continue after the original worker disappears. Different installations can choose different providers and models. For example, an installation may use provider `openrouter`, model `deepseek/deepseek-chat-v3-0324`, and an API key shaped like `sk-or-v1-...`.

On a paid Render service, set `maxShutdownDelaySeconds` to `300` to give active deliveries the full shutdown window. Free services do not support that setting, so the application uses its 25-second grace period: the worker stops claiming new deliveries, lets the active delivery finish when possible, and returns unfinished work to the database queue immediately if the window expires. The delivery then resumes from its durable checkpoint after restart.

For a Free Render web service, the repository's `Keep Render awake` workflow sends an inbound request every ten minutes. That prevents ordinary idle suspension and wakes the service when it is asleep; the queue worker then resumes from the external database. GitHub-hosted standard runners are free for this public repository. The watchdog is best effort because Render can still restart a Free service without notice.

GitHub mention sessions have a private Nix package profile. The service image already contains Nix, Git, `gh`, the Render CLI, `curl`, `jq`, `ripgrep`, `fd`, archive tools, the Python/uv runtime, Ruff, GCC/G++, Clang/LLVM, Make, CMake, Ninja, pkg-config, Rust, Node.js, and Bun. The agent can install another package into its private profile with `nix profile add nixpkgs#<package>`. The LangMesh checkout also contains the reproducible Render CLI package, available as `nix profile add github:ghovax/langmesh#render-cli`. The GitHub service supplies `GH_TOKEN` for repository operations. Render commands require an explicitly configured `RENDER_API_KEY`; the agent must never fabricate or print it.

## GitHub App settings

Register one App for the service owner and set:

- **Setup URL:** `https://github-agent.example.net/github/setup`
- **Callback URL:** `https://github-agent.example.net/github/setup/callback`
- **Webhook URL:** `https://github-agent.example.net/github/webhook`
- **Webhook secret:** the same value as `webhook_secret` in the service configuration
- **Webhook events:** `Installation`, `Issues`, `Pull requests`, `Issue comment`, and `Pull request review comment`
- **Repository permissions:** Contents read/write, Issues read/write, Pull requests read/write, and Metadata read-only

The App owner keeps the App ID, private key, OAuth client secret, and webhook secret in the service deployment. They are not installation settings.

## Installation and configuration

1. [Install the App from GitHub](https://github.com/apps/langmesh-agent) on a personal account or organization, selecting all or only the repositories it may access.
1. GitHub opens the service setup URL.
1. Sign in with GitHub when redirected. The service verifies that this account can access the installation.
1. The callback returns a JSON object containing a short-lived setup token, for example:

```json
{
  "installation_id": 184736295,
  "setup_token": "7kQ2mN...vR8pL4",
  "expires_in": 600,
  "configuration_url": "https://github-agent.example.net/github/configuration"
}
```

The token above is shortened for readability. Copy the complete value returned by your callback when calling the JSON configuration endpoint:

```sh
curl --fail-with-body --request PUT \
  --url https://langmesh-agent.onrender.com/github/configuration \
  --header 'Authorization: Bearer 7kQ2mN...vR8pL4' \
  --header 'Content-Type: application/json' \
  --data '{
    "provider": "openrouter",
    "model": "deepseek/deepseek-chat-v3-0324",
    "api_key": "sk-or-v1-01f4c8e9..."
  }'
```

Read the saved state with the same token:

```sh
curl --fail-with-body \
  --url https://langmesh-agent.onrender.com/github/configuration \
  --header 'Authorization: Bearer 7kQ2mN...vR8pL4'
```

The response never includes the API key.

### Provider OAuth

The hosted service can keep an OAuth session for any registered OAuth provider. Start the flow with the setup token returned by the GitHub callback, replacing `chatgpt` with the provider identifier:

```sh
curl --fail-with-body --request POST \
  --url https://langmesh-agent.onrender.com/github/auth/chatgpt/start \
  --header 'Authorization: Bearer 7kQ2mN...vR8pL4' \
  --header 'Content-Type: application/json' \
  --data '{"model":"gpt-5.6-luna"}'
```

The response contains the selected `model`, the provider's `authorize_url`, and the `completion_url`. Open `authorize_url` in a browser. The URL uses the redirect URI registered for that provider. For ChatGPT, this is `http://localhost:1455/auth/callback`; after authorization, copy the complete localhost URL from the browser address bar and submit its `code` and `state` to the returned `completion_url`:

```sh
curl --fail-with-body --get \
  --url https://langmesh-agent.onrender.com/github/auth/chatgpt/complete \
  --data-urlencode 'code=4/0AeaK7...nQ2' \
  --data-urlencode 'state=J8m2...pL7'
```

Replace `4/0AeaK7...nQ2` with the one-time `code` value from the copied localhost URL, and replace `J8m2...pL7` with its `state` value. The abbreviated values above are only examples.

Cursor uses its own browser and polling flow; after authorization, call the returned `completion_url` with its `state` and without a `code`. For a provider with a registered public callback, the provider redirects directly to `/github/auth/{provider}/callback`. The service validates the one-time state, exchanges the code with PKCE, stores the encrypted provider token, and stores the requested model with it. The initial OAuth flow therefore does not require a second configuration call. To change the model later, update the provider configuration without an API key:

```sh
curl --fail-with-body --request PUT \
  --url https://langmesh-agent.onrender.com/github/configuration \
  --header 'Authorization: Bearer 7kQ2mN...vR8pL4' \
  --header 'Content-Type: application/json' \
  --data '{"provider":"chatgpt","model":"gpt-5.4"}'
```

If a provider needs a deployment-specific client identifier, set it under `github.oauth.provider_application_ids`. The flow does not reuse GitHub OAuth, expose provider tokens to GitHub, or store them in a repository. Each provider controls its endpoints, token shape, refresh behavior, and request headers in models-provider.

After that, opening an issue or same-repository pull request starts an automatic first response. Every later human comment triggers a response. Bot-authored comments and edited or deleted comment events are ignored. Its commits use the App identity. A webhook is ignored until its installation has a provider/model configuration.

The setup flow verifies the installer through GitHub before accepting settings; the `installation_id` in a URL is not treated as authorization. Provider keys and OAuth tokens are encrypted at rest and never written to a checkout.

Each turn creates one acknowledgement comment and updates that same comment with useful status and the final response. While the turn is active, the acknowledgement and each status update begin with a GitHub note saying that work is still in progress; the final response removes that note. Each status and final reply also links to the read-only live session viewer, which streams checkpoint changes, disables input and exposes only a Stop button for the active turn. The link is an opaque bearer link: anyone who has it can read that session's transcript, but cannot send input or operate any other control. A failed update never creates a replacement comment, and the service ignores edited or deleted comment events. The final response addresses the known author of the triggering comment with a GitHub `@username` mention when appropriate. Usernames are never guessed, altered, or copied from untrusted prose.

## Repository behavior

The App service keeps its delivery queue, encrypted installation settings, and session checkpoints in the external database. Each delivery gets a temporary checkout on the execution machine, and that checkout is deleted when processing ends; GitHub branches and pull requests remain the durable source for repository changes. It uses installation tokens limited to the installed repositories and creates or updates topic branches and draft pull requests there. No repository file is created to select a model or provider.

To change the provider, model, or API key, start the setup flow again and send another JSON `PUT` request. The next GitHub event uses the new configuration.
