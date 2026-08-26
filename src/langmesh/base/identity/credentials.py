"""Compatibility names for the independent models-provider authentication package."""

from models_provider.auth import (
    AuthenticationError,
    ChatGPTLoginFlow,
    ChatGPTTokens,
    CredentialStore,
    current_credential_store,
    valid_chatgpt_tokens,
)

ChatGPTAuthError = AuthenticationError
PROVIDER = "chatgpt"


def load_tokens(store: CredentialStore | None = None) -> ChatGPTTokens | None:
    value = (store or current_credential_store()).load(PROVIDER)
    return value if isinstance(value, ChatGPTTokens) else None


def save_tokens(tokens: ChatGPTTokens, store: CredentialStore | None = None) -> None:
    (store or current_credential_store()).save(PROVIDER, tokens)


def clear_tokens(store: CredentialStore | None = None) -> None:
    (store or current_credential_store()).clear(PROVIDER)


def is_signed_in() -> bool:
    return load_tokens() is not None


async def valid_tokens() -> ChatGPTTokens:
    try:
        return await valid_chatgpt_tokens()
    except AuthenticationError as error:
        raise ChatGPTAuthError(str(error)) from error


__all__ = [
    "ChatGPTAuthError", "ChatGPTLoginFlow", "ChatGPTTokens", "clear_tokens", "is_signed_in",
    "load_tokens", "save_tokens", "valid_tokens",
]
