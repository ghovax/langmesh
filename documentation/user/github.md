# Universal GitHub App

LangMesh Agent is an installation-level GitHub App. A repository only needs the App installed; it does not need a workflow, YAML policy, App ID, provider setting, API key, or GitHub secret.

The service receives `issue_comment` and `pull_request_review_comment` webhooks, creates a repository-scoped installation token, and runs the mention session as the installed App. The App private key belongs only to the service operator. It is never entered by a person configuring an installation and is never stored in a repository.

## Service configuration

Run the hosted service outside a repository:

```sh
langmesh github-app --configuration ~/.config/langmesh/github-app.yaml
```

The configuration file is an operator/deployment file, not a repository file. It points at the App private key, webhook secret, and encryption key. Keep those files in a secret manager or a locked service directory. A complete shape is:

```yaml
app_id: "2149876"
private_key_path: "/srv/langmesh/secrets/langmesh-agent.2026-08.pem"
webhook_secret: "langmesh-gh-9a5b7c1d4e6f8a0b2c3d"
oauth_client_id: "Iv1.8f2c1d4e6a7b9c0d"
oauth_client_secret: "9c8b7a6d5e4f3210fedcba9876543210abcd1234"
encryption_key_path: "/srv/langmesh/secrets/provider-keys.fernet"
database_path: "/srv/langmesh/data/github-app.sqlite"
workspaces_path: "/srv/langmesh/data/workspaces"
public_url: "https://agent.langmesh.dev"
```

`encryption_key_path` must contain a Fernet key. Generate one once with:

```sh
python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())' \
  > /srv/langmesh/secrets/provider-keys.fernet
chmod 600 /srv/langmesh/secrets/provider-keys.fernet
```

The service stores provider API keys encrypted in its database, keyed by GitHub installation. Different installations can choose different providers and models. For example, an installation may use provider `openrouter`, model `deepseek/deepseek-chat-v3-0324`, and an API key shaped like `sk-or-v1-01f4c8e9...`.

## GitHub App settings

Register one App for the service owner and set:

- **Setup URL:** `https://agent.langmesh.dev/github/setup`
- **Callback URL:** `https://agent.langmesh.dev/github/setup/callback`
- **Webhook URL:** `https://agent.langmesh.dev/github/webhook`
- **Webhook secret:** the same value as `webhook_secret` in the service configuration
- **Webhook events:** `Installation`, `Issue comment`, and `Pull request review comment`
- **Repository permissions:** Contents read/write, Issues read/write, Pull requests read/write, and Metadata read-only

The App owner keeps the App ID, private key, OAuth client secret, and webhook secret in the service deployment. They are not installation settings.

## Installation and configuration

1. Install the App on a personal account or organization, selecting all or only the repositories it may access.
2. GitHub opens the service setup URL.
3. Sign in with GitHub when redirected. The service verifies that this account can access the installation.
4. Enter the provider, model, and API key in the configuration form.

After that, mention the installed bot in an issue or same-repository pull request. The bot identity is the actual App login, such as `@langmesh-agent[bot]`, and its commits use that identity. A webhook is ignored until its installation has a provider/model configuration.

The setup flow verifies the installer through GitHub before accepting settings; the `installation_id` in a URL is not treated as authorization. Provider keys are encrypted at rest and never written to a checkout.

## Repository behavior

The App service keeps session checkpoints and working clones in its own data directory. It uses installation tokens limited to the installed repositories and creates or updates topic branches and draft pull requests there. No repository file is created to select a model or provider.

To change the provider, model, or API key, reopen the installation setup page and save the new values. The next mention uses the new configuration.
