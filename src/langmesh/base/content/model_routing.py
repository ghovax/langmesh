"""Translate a selected model into the transport call parameters."""

from __future__ import annotations

from typing import TypedDict

from models_provider import CredentialStore

from langmesh.base.identity.providers import (
    ProviderDefinition,
    get_provider_definition,
    resolve_base_url,
    resolve_provider_credentials,
)
from langmesh.base.content.models import find_model, split_model_identifier


class ResolvedLiteLLM(TypedDict):
    """LiteLLM parameters for one selected model."""

    model: str
    api_key: str
    api_base: str
    headers: dict[str, str]
    environment: dict[str, str]


def _gateway_api_base(provider_base_url: str, litellm_prefix: str) -> str:
    """Add the protocol path required by a multi-protocol gateway."""
    if litellm_prefix == "anthropic":
        return f"{provider_base_url.rstrip('/')}/messages"
    return provider_base_url


def resolve_litellm(
    model_identifier: str,
    configured_keys: dict[str, str],
    configured_bases: dict[str, str],
    *,
    credential_store: CredentialStore | None = None,
) -> ResolvedLiteLLM:
    """Translate a provider-qualified model into LiteLLM call parameters."""
    split = split_model_identifier(model_identifier)
    if split is None:
        raise ValueError(f"Model id has no provider prefix: {model_identifier!r}")
    provider_identifier, suffix = split
    catalog_model = find_model(model_identifier)
    definition: ProviderDefinition | None = get_provider_definition(provider_identifier)
    if definition is None:
        raise ValueError(f"Unknown provider in model id: {model_identifier!r}")
    litellm_prefix = (
        catalog_model.litellm_prefix if catalog_model else ""
    ) or definition.litellm_prefix
    provider_base_url = (
        resolve_base_url(provider_identifier, configured_bases)
        if definition.uses_custom_base_url or definition.openai_compatible
        else ""
    )
    credentials = resolve_provider_credentials(
        provider_identifier, configured_keys, credential_store=credential_store
    )
    return {
        "model": f"{litellm_prefix}/{suffix}",
        "api_key": credentials.api_key,
        "api_base": (
            _gateway_api_base(provider_base_url, litellm_prefix)
            if definition.uses_custom_base_url
            else provider_base_url
        ),
        "headers": definition.default_headers,
        "environment": dict(credentials.environment),
    }
