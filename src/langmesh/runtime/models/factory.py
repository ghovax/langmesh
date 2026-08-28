"""Build the transport model selected by a runtime profile."""

from __future__ import annotations

from collections.abc import Mapping

from langchain_core.language_models.chat_models import BaseChatModel
from models_provider import CredentialStore, ProviderAuthentication, provider_auth_profile
from pydantic import SecretStr

from langmesh.base.configuration import AgentConfiguration
from langmesh.base.content.model_routing import resolve_litellm
from langmesh.base.content.models import find_model
from langmesh.base.identity.providers import get_provider_definition, provider_env_vars


def build_chat_model(
    model_identifier: str,
    agent_configuration: AgentConfiguration,
    working_directory: str,
    session_id: str = "",
    credential_store: CredentialStore | None = None,
    provider_api_keys: Mapping[str, str] | None = None,
    provider_base_urls: Mapping[str, str] | None = None,
) -> BaseChatModel:
    """Build a model from the profile and explicit provider inputs supplied by its host."""
    provider_api_keys = dict(provider_api_keys or {})
    provider_base_urls = dict(provider_base_urls or {})
    provider_identifier, model_suffix = model_identifier.split("/", 1)
    if provider_identifier == "chatgpt":
        from langmesh.runtime.models.codex import ChatCodexModel

        catalog_entry = find_model(model_identifier)
        return ChatCodexModel(
            model=model_suffix,
            reasoning_effort=agent_configuration.reasoning_effort,
            context_length=catalog_entry.context_length if catalog_entry else 0,
            session_id=session_id,
        )
    if provider_identifier == "cursor":
        from langmesh.runtime.models.cursor import ChatCursorModel

        catalog_entry = find_model(model_identifier)
        return ChatCursorModel(
            model=model_suffix,
            workspace=working_directory,
            context_length=catalog_entry.context_length if catalog_entry else 0,
        )

    from langmesh.runtime.models.litellm import ChatLiteLLMModel

    resolved = resolve_litellm(
        model_identifier,
        provider_api_keys,
        provider_base_urls,
        credential_store=credential_store,
    )
    catalogued = find_model(model_identifier)
    definition = get_provider_definition(provider_identifier)
    profile = provider_auth_profile(
        provider_identifier,
        environment_variables=provider_env_vars(provider_identifier),
        default_base_url=definition.default_base_url if definition else "",
        headers=definition.default_headers if definition else {},
        anonymous_api_key=definition.anonymous_api_key if definition else "",
        credential_identifier=definition.credential_identifier if definition else "",
    )
    authentication = ProviderAuthentication(
        {provider_identifier: profile},
        api_keys=provider_api_keys,
        api_bases=provider_base_urls,
        store=credential_store,
    )
    model = ChatLiteLLMModel.model_validate(
        {
            "model": resolved["model"],
            "api_key": SecretStr(resolved["api_key"]) if resolved["api_key"] else None,
            "api_base": resolved["api_base"] or None,
            "default_headers": resolved["headers"],
            "session_id": session_id,
            "context_length": catalogued.context_length if catalogued else 0,
            "temperature": 0,
            "reasoning_effort": agent_configuration.reasoning_effort,
            "provider_identifier": provider_identifier,
            "provider_environment_variables": profile.environment_variables,
        }
    )
    model._authentication = authentication
    return model
