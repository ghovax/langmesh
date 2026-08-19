from __future__ import annotations

import logging
import re
import threading
from dataclasses import dataclass
from typing import TypedDict

import httpx

from langmesh.base.identity.providers import (
    PROVIDERS,
    ProviderDefinition,
    extend_provider_env_vars,
    get_provider_definition,
    register_models_dev_provider,
    resolve_api_key,
    resolve_base_url,
)


@dataclass(frozen=True)
class ModelDefinition:
    """A pickable model: its canonical provider-namespaced id, and the name a person reads."""

    identifier: str
    name: str
    provider: str
    # Capabilities from the catalog, used to gate and annotate the interface.
    attachment: bool = False
    vision: bool = False
    input_modalities: tuple[str, ...] = ()
    # Maximum input context in tokens from the catalog, with 0 meaning unknown.
    context_length: int = 0
    # Release date from the catalog, on which the picker sorts newest-first rather than alphabetically.
    release_date: str = ""
    # A per-model override for gateways that expose several wire protocols.
    litellm_prefix: str = ""


class ResolvedLiteLLM(TypedDict):
    """The LiteLLM call parameters for one model: routed id, credential, endpoint, and headers."""

    model: str
    api_key: str
    api_base: str
    headers: dict[str, str]


# Wire-protocol names from models.dev, mapped to the LiteLLM prefix that speaks them.
_GATEWAY_LITELLM_PREFIXES = {
    "@ai-sdk/openai-compatible": "openai",
    "@ai-sdk/openai": "openai/responses",
    "@ai-sdk/anthropic": "anthropic",
    "@ai-sdk/google": "gemini",
    "@ai-sdk/azure": "azure",
    "@ai-sdk/cohere": "cohere",
    "@ai-sdk/togetherai": "together_ai",
    "@ai-sdk/google-vertex": "vertex_ai",
    "@ai-sdk/google-vertex/anthropic": "vertex_ai",
    "@ai-sdk/xai": "xai",
    "@ai-sdk/mistral": "mistral",
    "@ai-sdk/amazon-bedrock": "bedrock",
    "@ai-sdk/groq": "groq",
    "@ai-sdk/deepinfra": "deepinfra",
    "@ai-sdk/cerebras": "cerebras",
    "@ai-sdk/perplexity": "perplexity",
    "@openrouter/ai-sdk-provider": "openrouter",
}

# The prefix for an auto-registered provider: the SDK's own protocol, with everything unknown falling back to the OpenAI-compatible wire since that is what most expose.
_AUTO_PROVIDER_PREFIXES = {
    **_GATEWAY_LITELLM_PREFIXES,
    # The Responses protocol is openai's own; third-party endpoints almost always serve the chat-completions wire instead, so auto providers use the plain openai prefix.
    "@ai-sdk/openai": "openai",
}


def _catalog() -> list[ModelDefinition]:
    """The model catalog from models.dev, best effort so the harness still starts without one."""
    MODELS_DEV_URL = "https://models.dev/api.json"
    try:
        response = httpx.get(MODELS_DEV_URL, timeout=5)
        response.raise_for_status()
        raw = response.json()
    except Exception:
        logging.getLogger(__name__).warning(
            "Could not fetch model catalog from %s — no models available",
            MODELS_DEV_URL,
        )
        return []

    models: dict[str, ModelDefinition] = {}
    for models_dev_id, provider_info in raw.items():
        local_id = models_dev_id
        definition = get_provider_definition(local_id)
        env_vars = tuple(str(value) for value in (provider_info.get("env") or ()) if value)
        if definition is None:
            # Every models.dev provider is routable: register it from the catalogue's own metadata (declared env var, endpoint, wire protocol) when nothing is curated.
            npm = str(provider_info.get("npm") or "")
            litellm_prefix = _AUTO_PROVIDER_PREFIXES.get(npm, "openai")
            api = str(provider_info.get("api") or "")
            definition = register_models_dev_provider(
                local_id,
                name=str(provider_info.get("name") or models_dev_id).strip(),
                litellm_prefix=litellm_prefix,
                env_vars=env_vars,
                default_base_url=api,
                openai_compatible=litellm_prefix == "openai",
                # The base URL is resolved from the catalogue or configuration and passed through the gateway suffix logic, since a model can override the provider's own protocol.
                uses_custom_base_url=True,
            )
        else:
            # The curated key names stay authoritative; models.dev's own names join as aliases.
            extend_provider_env_vars(local_id, env_vars)
        for model_id, model_info in provider_info.get("models", {}).items():
            # Stripped, because the catalogue is community-maintained and some names carry stray whitespace.
            name = (model_info.get("name", "") or model_id).strip() or model_id
            identifier = f"{local_id}/{model_id}"
            modalities = model_info.get("modalities") or {}
            input_modalities = tuple(
                str(modality) for modality in (modalities.get("input") or []) if modality
            )
            litellm_prefix = ""
            if local_id in {"opencode", "opencode-go"}:
                model_provider = model_info.get("provider") or {}
                sdk_package = model_provider.get("npm") or provider_info.get("npm") or ""
                litellm_prefix = _GATEWAY_LITELLM_PREFIXES.get(str(sdk_package), "")
                if not litellm_prefix:
                    logging.getLogger(__name__).warning(
                        "skipping %s because its models.dev protocol %r is unsupported",
                        identifier,
                        sdk_package,
                    )
                    continue
            models.setdefault(
                identifier,
                ModelDefinition(
                    identifier=identifier,
                    name=name,
                    provider=local_id,
                    attachment=bool(model_info.get("attachment")),
                    vision="image" in input_modalities,
                    input_modalities=input_modalities,
                    context_length=int((model_info.get("limit") or {}).get("context") or 0),
                    release_date=str(model_info.get("release_date") or "").strip(),
                    litellm_prefix=litellm_prefix,
                ),
            )
    return list(models.values())


# Which OpenAI models the Codex endpoint serves, as an allow and deny set plus a version rule.
_CODEX_ALLOWED_MODELS = frozenset({"gpt-5.5", "gpt-5.3-codex-spark", "gpt-5.4", "gpt-5.4-mini"})
_CODEX_DISALLOWED_MODELS = frozenset({"gpt-5.5-pro"})


def _codex_eligible(model_suffix: str) -> bool:
    if model_suffix in _CODEX_ALLOWED_MODELS:
        return True
    if model_suffix in _CODEX_DISALLOWED_MODELS:
        return False
    match = re.match(r"^gpt-(\d+\.\d+)", model_suffix)
    return float(match.group(1)) > 5.4 if match else False


def _chatgpt_models(base: list[ModelDefinition]) -> list[ModelDefinition]:
    """The `chatgpt` subscription models, filtered from the catalog's OpenAI entries so new ones appear automatically."""
    chatgpt: list[ModelDefinition] = []
    for model in base:
        if model.provider != "openai":
            continue
        suffix = model.identifier.split("/", 1)[1]
        if not _codex_eligible(suffix):
            continue
        chatgpt.append(
            ModelDefinition(
                identifier=f"chatgpt/{suffix}",
                name=model.name,
                provider="chatgpt",
                attachment=model.attachment,
                vision=model.vision,
                input_modalities=model.input_modalities,
                context_length=model.context_length,
                release_date=model.release_date,
            )
        )
    return chatgpt


# The `cursor` provider contributes nothing here by design, since its models are an account fact.
_catalogue_cache: list[ModelDefinition] | None = None
_catalogue_lock = threading.Lock()


# Command Code's Provider API serves a live catalog at /provider/v1/models (no auth needed).
# The endpoint carries id/name/context only; the wire protocol is derived from the model id,
# since Anthropic models must be called over /messages and everything else over /chat/completions.
_COMMANDCODE_MODELS_URL = "https://api.commandcode.ai/provider/v1/models"


def _commandcode_models() -> list[ModelDefinition]:
    """The Command Code Provider API models, fetched from its live catalog and transformed."""
    try:
        response = httpx.get(_COMMANDCODE_MODELS_URL, timeout=5)
        response.raise_for_status()
        entries = response.json().get("data", [])
    except Exception:
        logging.getLogger(__name__).warning(
            "Could not fetch Command Code model catalog from %s", _COMMANDCODE_MODELS_URL
        )
        return []
    models: list[ModelDefinition] = []
    for entry in entries:
        model_id = str(entry.get("id") or "").strip()
        if not model_id:
            continue
        name = str(entry.get("name") or model_id).strip()
        context_length = int(entry.get("context_length") or 0)
        # Anthropic models go over the /messages wire, everything else over /chat/completions.
        wire = "anthropic" if model_id.startswith("claude-") else "openai"
        models.append(
            ModelDefinition(
                identifier=f"commandcode/{model_id}",
                name=name,
                provider="commandcode",
                attachment=True,
                vision=True,
                input_modalities=("image",),
                context_length=context_length,
                litellm_prefix=wire,
            )
        )
    return models


def list_models() -> list[ModelDefinition]:
    """The model catalogue, fetched on first use and cached, as a function because building it blocks on the network."""
    global _catalogue_cache
    if _catalogue_cache is not None:
        return list(_catalogue_cache)
    with _catalogue_lock:
        if _catalogue_cache is None:
            base = _catalog()
            _catalogue_cache = base + _chatgpt_models(base) + _commandcode_models()
        return list(_catalogue_cache)


def clear_catalogue_cache() -> None:
    """Drop the cached catalogue so the next listing refetches."""
    global _catalogue_cache
    _catalogue_cache = None


def find_model(model_identifier: str) -> ModelDefinition | None:
    for model in list_models():
        if model.identifier == model_identifier:
            return model
    return None


def provider_and_suffix(model_identifier: str) -> tuple[str, str] | None:
    """Split a model id into its provider and suffix, on the first slash only."""
    if "/" not in model_identifier:
        return None
    provider_identifier, suffix = model_identifier.split("/", 1)
    return provider_identifier, suffix


def available_models(configured_keys: dict[str, str]) -> list[ModelDefinition]:
    """Catalog entries whose provider has a resolvable credential, excluding the subscription providers."""
    unlocked_providers = {
        provider.identifier
        for provider in PROVIDERS.values()
        if resolve_api_key(provider.identifier, configured_keys)
        # The custom provider has no key of its own; it is selectable on demand.
        or provider.identifier == "custom"
    }
    return [model for model in list_models() if model.provider in unlocked_providers]


def _gateway_api_base(provider_base_url: str, litellm_prefix: str) -> str:
    """The base URL to hand LiteLLM for a gateway serving several wire protocols from one host."""
    if litellm_prefix == "anthropic":
        return f"{provider_base_url.rstrip('/')}/messages"
    return provider_base_url


def resolve_litellm(
    model_identifier: str,
    configured_keys: dict[str, str],
    configured_bases: dict[str, str],
) -> ResolvedLiteLLM:
    """Translate a provider-qualified model into LiteLLM call parameters."""
    split = provider_and_suffix(model_identifier)
    if split is None:
        raise ValueError(f"Model id has no provider prefix: {model_identifier!r}")
    provider_identifier, suffix = split
    # `models.dev` providers are registered while the catalogue is built. Resolve the model first so a cold runtime can run its first turn without depending on the UI having listed models beforehand; model selection and provider registration become one ordered operation.
    catalog_model = find_model(model_identifier)
    definition: ProviderDefinition | None = get_provider_definition(provider_identifier)
    if definition is None:
        raise ValueError(f"Unknown provider in model id: {model_identifier!r}")
    # The catalogue's prefix is an override set only for multi-protocol gateways, so an empty one means the provider's own.
    litellm_prefix = (
        catalog_model.litellm_prefix if catalog_model else ""
    ) or definition.litellm_prefix
    provider_base_url = (
        resolve_base_url(provider_identifier, configured_bases)
        if definition.uses_custom_base_url or definition.openai_compatible
        else ""
    )
    return {
        "model": f"{litellm_prefix}/{suffix}",
        "api_key": resolve_api_key(provider_identifier, configured_keys),
        "api_base": (
            _gateway_api_base(provider_base_url, litellm_prefix)
            if definition.uses_custom_base_url
            else provider_base_url
        ),
        "headers": definition.default_headers,
    }
