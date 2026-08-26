from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from langmesh.base.identity.credentials import ChatGPTTokens
from langmesh.github.hosted import HostedCredentialStore, Settings, Store


@pytest.fixture
async def store(tmp_path: Path):
    key_path = tmp_path / "encryption.key"
    key_path.write_bytes(Fernet.generate_key())
    settings = Settings(
        app_id="4690924",
        private_key_path=tmp_path / "unused.pem",
        webhook_secret="webhook-secret",
        oauth_client_id="Iv23lihDpcXFvi7LsBkw",
        oauth_client_secret="oauth-secret",
        encryption_key_path=key_path,
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'state.sqlite'}",
        queue_poll_seconds=1,
        public_url="https://langmesh-agent.example.net",
    )
    value = Store(settings)
    await value.initialize()
    try:
        yield value
    finally:
        await value.close()


@pytest.mark.asyncio
async def test_native_credentials_are_installation_scoped_and_encrypted(store: Store) -> None:
    tokens = ChatGPTTokens("access", "refresh", "id", "account", "user@example.test", 123.0)
    await store.save_native_credential(42, "chatgpt", tokens)

    assert await store.load_native_credential(42, "chatgpt") == tokens
    assert await store.load_native_credential(43, "chatgpt") is None

    credentials = HostedCredentialStore(store, 42)
    await credentials.hydrate("chatgpt")
    assert credentials.load("chatgpt") == tokens


@pytest.mark.asyncio
async def test_native_configuration_clears_an_old_api_key(store: Store) -> None:
    await store.save_installation(42, "ghovax", "User", "openrouter", "model", "sk-or-v1-old")
    await store.save_installation(42, "ghovax", "User", "chatgpt", "gpt-5.4", "")

    assert await store.configuration(42) == ("chatgpt", "gpt-5.4", "")
