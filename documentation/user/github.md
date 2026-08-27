# Universal GitHub App

LangMesh Agent is an installation-level GitHub App. A repository only needs the App
installed; it does not need a workflow, YAML policy, App ID, provider setting, API key,
or GitHub secret.

The service receives `issue_comment` and `pull_request_review_comment` webhooks, creates
a repository-scoped installation token, and runs the mention session as the installed
App. The App private key belongs only to the service operator. It is never entered by a
person configuring an installation and is never stored in a repository.

## Service configuration

Run the hosted service outside a repository:

```sh
langmesh github --configuration ~/.config/langmesh/github.yaml
```

The configuration file is an operator/deployment file, not a repository file. It points
at the App private key, webhook secret, and encryption key. Keep those files in a secret
manager or a locked service directory. A complete shape is:

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
  api_url: "https://api.github.com"
server:
  public_url: "https://github-agent.example.net"
storage:
  database:
    url: "postgresql+asyncpg://postgres:...@db.qxwzjvkrmno.supabase.co:5432/postgres?ssl=require"
  encryption:
    key_path: "/srv/langmesh/secrets/provider-keys.fernet"
  queue:
    poll_seconds: 5
```

`storage.encryption.key_path` must contain a Fernet key. Generate one once with:

```sh
python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())' \
  > /srv/langmesh/secrets/provider-keys.fernet
chmod 600 /srv/langmesh/secrets/provider-keys.fernet
```

The service stores provider API keys encrypted in the external database, keyed by GitHub
installation. The GitHub worker uses the compaction plugin's configured threshold with a
direct preparation port. Compaction intentionally invalidates the conversation portion
of the provider cache; the stable instructions and tool definitions remain reusable. The
delivery queue and session checkpoints use that same database, so another worker can
continue after the original worker disappears. Different installations can choose
different providers and models. For example, an installation may use provider
`openrouter`, model `deepseek/deepseek-chat-v3-0324`, and an API key shaped like
`sk-or-v1-...`.

GitHub mention sessions have a private Nix package profile. The service image already
contains Nix, Git, `gh`, the Render CLI, `curl`, `jq`, `ripgrep`, `fd`, archive tools,
the Python/uv runtime, Ruff, GCC/G++, Clang/LLVM, Make, CMake, Ninja, pkg-config, Rust,
Node.js, and Bun. The agent can install another package into its private profile with
`nix profile add nixpkgs#<package>`. The LangMesh checkout also contains the reproducible
Render CLI package, available as `nix profile add github:ghovax/langmesh#render-cli`.
The GitHub service supplies `GH_TOKEN` for repository operations. Render commands require
an explicitly configured `RENDER_API_KEY`; the agent must never fabricate or print it.

## GitHub App settings

Register one App for the service owner and set:

- **Setup URL:** `https://github-agent.example.net/github/setup`
- **Callback URL:** `https://github-agent.example.net/github/setup/callback`
- **Webhook URL:** `https://github-agent.example.net/github/webhook`
- **Webhook secret:** the same value as `webhook_secret` in the service configuration
- **Webhook events:** `Installation`, `Issue comment`, and `Pull request review comment`
- **Repository permissions:** Contents read/write, Issues read/write, Pull requests
  read/write, and Metadata read-only

The App owner keeps the App ID, private key, OAuth client secret, and webhook secret in
the service deployment. They are not installation settings.

## Installation and configuration

1. Install the App on a personal account or organization, selecting all or only the
   repositories it may access.
1. GitHub opens the service setup URL.
1. Sign in with GitHub when redirected. The service verifies that this account can
   access the installation.
1. The callback returns a JSON object containing a short-lived setup token, for example:

```json
{
  "installation_id": 184736295,
  "setup_token": "7kQ2mN...vR8pL4",
  "expires_in": 600,
  "configuration_url": "https://github-agent.example.net/github/configuration"
}
```

The token above is shortened for readability. Copy the complete value returned by your
callback when calling the JSON configuration endpoint:

```sh
curl --fail-with-body --request PUT \
  --url https://langmesh-agent.onrender.com/github/configuration \
  --header 'Authorization: Bearer 7kQ2mN...vR8pL4' \
  --header 'Content-Type: application/json' \
  --data '{"provider":"openrouter","model":"deepseek/deepseek-chat-v3-0324","api_key":"sk-or-v1-01f4c8e9..."}'
```

Read the saved state with the same token:

```sh
curl --fail-with-body \
  --url https://langmesh-agent.onrender.com/github/configuration \
  --header 'Authorization: Bearer 7kQ2mN...vR8pL4'
```

The response never includes the API key.

After that, mention the installed bot in an issue or same-repository pull request. The
bot identity is the actual App login, such as `@langmesh-agent[bot]`, and its commits
use that identity. A webhook is ignored until its installation has a provider/model
configuration.

The setup flow verifies the installer through GitHub before accepting settings; the
`installation_id` in a URL is not treated as authorization. Provider keys are encrypted
at rest and never written to a checkout.

Each turn creates one acknowledgement comment and updates that same comment with useful
status and the final response. A failed update never creates a replacement comment, and
the service ignores edited or deleted comment events. The final response addresses the
known author of the triggering comment with a GitHub `@username` mention when
appropriate. Usernames are never guessed, altered, or copied from untrusted prose.

## Repository behavior

The App service keeps its delivery queue, encrypted installation settings, and session
checkpoints in the external database. Each delivery gets a temporary checkout on the
execution machine, and that checkout is deleted when processing ends; GitHub branches
and pull requests remain the durable source for repository changes. It uses installation
tokens limited to the installed repositories and creates or updates topic branches and
draft pull requests there. No repository file is created to select a model or provider.

To change the provider, model, or API key, start the setup flow again and send another
JSON `PUT` request. The next mention uses the new configuration.
