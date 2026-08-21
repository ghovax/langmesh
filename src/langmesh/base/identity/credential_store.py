"""Task-local access to the caller-owned credential store."""

from __future__ import annotations

import contextvars

from langmesh.base.contracts.ports import CredentialStore, MemoryCredentialStore


_default_store = MemoryCredentialStore()
_store: contextvars.ContextVar[CredentialStore] = contextvars.ContextVar(
    "langmesh_credential_store", default=_default_store
)


def credential_store() -> CredentialStore:
    """Return the credential store bound to this runtime task."""
    return _store.get()


def bind_credential_store(store: CredentialStore) -> contextvars.Token[CredentialStore]:
    """Bind a credential store until its token is reset."""
    return _store.set(store)


def reset_credential_store(token: contextvars.Token[CredentialStore]) -> None:
    """Restore the credential store that preceded a binding."""
    _store.reset(token)


__all__ = ["bind_credential_store", "credential_store", "reset_credential_store"]
