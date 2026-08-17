from __future__ import annotations

import logging
import re
import threading
from dataclasses import dataclass

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


# Command Code's Provider API models, curated from its live /provider/v1/models since models.dev lacks the provider.
# The last field is the wire protocol: Anthropic models go to /messages, the rest to /chat/completions.
_COMMANDCODE_MODELS: tuple[tuple[str, str, int, bool, bool, str], ...] = (
    ("claude-sonnet-5", "Claude Sonnet 5", 1000000, True, True, "anthropic"),
    ("claude-sonnet-4-6", "Claude Sonnet 4.6", 1000000, True, True, "anthropic"),
    ("claude-fable-5", "Claude Fable 5", 1000000, True, True, "anthropic"),
    ("claude-opus-5", "Claude Opus 5", 1000000, True, True, "anthropic"),
    ("claude-opus-4-8", "Claude Opus 4.8", 1000000, True, True, "anthropic"),
    ("claude-opus-4-7", "Claude Opus 4.7", 1000000, True, True, "anthropic"),
    ("claude-haiku-4-5-20251001", "Claude Haiku 4.5", 200000, True, True, "anthropic"),
    ("gpt-5.6-sol", "GPT-5.6 Sol", 1050000, True, True, "openai"),
    ("gpt-5.6-terra", "GPT-5.6 Terra", 1050000, True, True, "openai"),
    ("gpt-5.6-luna", "GPT-5.6 Luna", 1050000, True, True, "openai"),
    ("gpt-5.5", "GPT-5.5", 200000, True, True, "openai"),
    ("gpt-5.4", "GPT-5.4", 400000, True, True, "openai"),
    ("gpt-5.3-codex", "GPT-5.3 Codex", 400000, True, True, "openai"),
    ("gpt-5.4-mini", "GPT-5.4 Mini", 400000, True, True, "openai"),
    ("deepseek/deepseek-v4-pro", "DeepSeek V4 Pro (latest)", 1000000, True, True, "openai"),
    ("deepseek/deepseek-v4-flash", "DeepSeek V4 Flash (latest)", 1000000, True, True, "openai"),
    ("moonshotai/Kimi-K3", "Kimi K3", 1000000, True, True, "openai"),
    ("moonshotai/Kimi-K2.7-Code", "Kimi K2.7 Code", 256000, True, True, "openai"),
    ("moonshotai/Kimi-K2.7-Code-Highspeed", "Kimi K2.7 Code HighSpeed", 262000, True, True, "openai"),
    ("moonshotai/Kimi-K2.6", "Kimi K2.6", 256000, True, True, "openai"),
    ("moonshotai/Kimi-K2.5", "Kimi K2.5", 256000, True, True, "openai"),
    ("zai-org/GLM-5.3", "GLM-5.3", 1000000, True, True, "openai"),
    ("zai-org/GLM-5.2", "GLM-5.2", 1000000, True, True, "openai"),
    ("zai-org/GLM-5.2-Fast", "GLM-5.2 Fast", 1000000, True, True, "openai"),
    ("zai-org/GLM-5.1", "GLM-5.1", 200000, True, True, "openai"),
    ("zai-org/GLM-5", "GLM-5", 200000, True, True, "openai"),
    ("MiniMaxAI/MiniMax-M3", "MiniMax M3", 1000000, True, True, "openai"),
    ("MiniMaxAI/MiniMax-M2.7", "MiniMax M2.7", 200000, True, True, "openai"),
    ("MiniMaxAI/MiniMax-M2.5", "MiniMax M2.5", 200000, True, True, "openai"),
    ("xiaomi/mimo-v2.5-pro", "MiMo V2.5 Pro", 1000000, True, True, "openai"),
    ("xiaomi/mimo-v2.5", "MiMo V2.5", 1000000, True, True, "openai"),
    ("Qwen/Qwen3.8-Max", "Qwen 3.8 Max", 1000000, True, True, "openai"),
    ("Qwen/Qwen3.7-Max", "Qwen 3.7 Max", 1000000, True, True, "openai"),
    ("Qwen/Qwen3.7-Plus", "Qwen 3.7 Plus", 1000000, True, True, "openai"),
    ("Qwen/Qwen3.7-Flash", "Qwen 3.7 Flash", 1000000, True, True, "openai"),
    ("Qwen/Qwen3.6-Max-Preview", "Qwen 3.6 Max Preview", 200000, True, True, "openai"),
    ("Qwen/Qwen3.6-Plus", "Qwen 3.6 Plus", 200000, True, True, "openai"),
    ("stepfun/Step-3.7-Flash", "Step 3.7 Flash", 256000, True, True, "openai"),
    ("stepfun/Step-3.5-Flash", "Step 3.5 Flash", 1000000, True, True, "openai"),
    ("tencent/hy3-paid", "Tencent Hy3", 262144, True, True, "openai"),
    ("google/gemini-3.7-flash", "Gemini 3.7 Flash", 1048576, True, True, "openai"),
    ("google/gemini-3.6-flash", "Gemini 3.6 Flash", 1000000, True, True, "openai"),
    ("google/gemini-3.5-flash", "Gemini 3.5 Flash", 1000000, True, True, "openai"),
    ("google/gemini-3.5-flash-lite", "Gemini 3.5 Flash Lite", 1000000, True, True, "openai"),
    ("google/gemini-3.1-flash-lite", "Gemini 3.1 Flash Lite", 1000000, True, True, "openai"),
    ("sakana/fugu-ultra", "Fugu Ultra", 1000000, True, True, "openai"),
    ("nvidia/nemotron-3-ultra-550b-a55b", "Nemotron 3 Ultra", 1000000, True, True, "openai"),
    ("thinkingmachines/inkling", "Inkling", 256000, True, True, "openai"),
    ("thinkingmachines/inkling-small", "Inkling Small", 1000000, True, True, "openai"),
    ("poolside/laguna-s-2.1-free", "Laguna S 2.1", 256000, True, True, "openai"),
    ("meta/muse-spark-1.1", "Muse Spark 1.1", 1048576, True, True, "openai"),
    ("meta/muse-spark-1.2", "Muse Spark 1.2", 1048576, True, True, "openai"),
    ("meta/muse-spark-1.2-contributor", "Muse Spark 1.2 Contributor", 1048576, True, True, "openai"),
    ("xai/grok-4.5", "Grok 4.5", 500000, True, True, "openai"),
    ("xai/grok-4.6", "Grok 4.6", 500000, True, True, "openai"),
)


def _commandcode_models() -> list[ModelDefinition]:
    """The Command Code Provider API models, curated since models.dev does not list the provider."""
    return [
        ModelDefinition(
            identifier=f"commandcode/{model_id}",
            name=name,
            provider="commandcode",
            attachment=attachment,
            vision=vision,
            input_modalities=("image",) if vision else (),
            context_length=context_length,
            litellm_prefix=wire,
        )
        for model_id, name, context_length, attachment, vision, wire in _COMMANDCODE_MODELS
    ]


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
) -> dict[str, str]:
    """Translate a provider-qualified model into LiteLLM call parameters."""
    split = provider_and_suffix(model_identifier)
    if split is None:
        raise ValueError(f"Model id has no provider prefix: {model_identifier!r}")
    provider_identifier, suffix = split
    # `models.dev` providers are registered while the catalogue is built. Resolve the model first so a cold daemon can run its first turn without depending on the UI having listed models beforehand; model selection and provider registration become one ordered operation.
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
    }
