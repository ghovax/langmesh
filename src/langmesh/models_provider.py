"""LangMesh's implementation of the independent models-provider contract."""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from langchain_core.language_models import BaseChatModel
from models_provider import ModelConfiguration

__all__ = ["LangMeshProvider"]


class LangMeshProvider:
    """Expose LangMesh model implementations through models-provider."""

    def __init__(
        self,
        *,
        configuration: Any | None = None,
        providers: Mapping[str, str | Mapping[str, str]] | None = None,
        credential_store: Any | None = None,
        workspace: Path | None = None,
    ) -> None:
        from langmesh.base.configuration import Configuration

        self._configuration = configuration or Configuration()
        self._providers = dict(providers or {})
        self._credential_store = credential_store
        self._workspace = workspace or Path.cwd()

    def create(self, configuration: ModelConfiguration) -> BaseChatModel:
        """Create one LangMesh chat model from the neutral model choice."""
        from langmesh.base.configuration import AgentConfiguration, ProviderCredential
        from langmesh.runtime.runtime import build_chat_model

        langmesh_configuration = self._configuration.model_copy(deep=True)
        for provider, value in self._providers.items():
            credential = langmesh_configuration.providers.get(provider) or ProviderCredential()
            update = {"api_key": value} if isinstance(value, str) else dict(value)
            langmesh_configuration.providers[provider] = credential.model_copy(update=update)

        agent = AgentConfiguration(
            provider=configuration.provider,
            model=configuration.model,
            reasoning_effort=configuration.reasoning_effort or "high",
        )
        model = build_chat_model(
            configuration.identifier,
            langmesh_configuration,
            agent,
            str(self._workspace.resolve()),
        )
        updates = {
            key: value
            for key, value in {
                "temperature": configuration.temperature,
                "timeout": configuration.timeout_seconds,
                "context_length": configuration.context_length,
                **dict(configuration.extra),
            }.items()
            if key in model.model_fields
        }
        return model.model_copy(update=updates) if updates else model

    @contextmanager
    def scope(self) -> Iterator[None]:
        """Bind a caller-owned LangMesh credential store for native providers."""
        if self._credential_store is None:
            yield
            return
        from langmesh.base.identity.credential_store import (
            bind_credential_store,
            reset_credential_store,
        )

        token = bind_credential_store(self._credential_store)
        try:
            yield
        finally:
            reset_credential_store(token)
