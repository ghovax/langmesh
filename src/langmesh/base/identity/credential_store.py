"""LangMesh compatibility names for models-provider credential context."""

from models_provider import (
    CredentialStore,
    MemoryCredentialStore,
    bind_credential_store,
    current_credential_store,
    reset_credential_store,
)


def credential_store() -> CredentialStore:
    return current_credential_store()


__all__ = [
    "CredentialStore", "MemoryCredentialStore", "bind_credential_store", "credential_store",
    "reset_credential_store",
]
