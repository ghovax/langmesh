from __future__ import annotations

from dataclasses import dataclass, field, replace


@dataclass(frozen=True)
class ProviderDefinition:
    """One routable provider, with the default LiteLLM prefix a catalog model may override."""

    identifier: str
    name: str
    litellm_prefix: str
    env_vars: tuple[str, ...] = ()
    default_base_url: str = ""
    openai_compatible: bool = False
    uses_custom_base_url: bool = False
    credential_identifier: str = ""
    # Whether this provider is surfaced as a pickable source of models.
    selectable: bool = True
    # A native provider is not routed through LiteLLM: it has its own client and its own non-key auth.
    native: bool = False
    # Headers sent on every request to the provider, for the gateway's own client gate.
    default_headers: dict[str, str] = field(default_factory=dict)
    # The credential to present when none is configured. Anonymous gateways use a
    # sentinel key (OpenCode Zen reads `public`) to let free-tier calls through without billing.
    anonymous_api_key: str = ""


# The order here is the order models are grouped in the picker.
PROVIDERS: dict[str, ProviderDefinition] = {
    provider.identifier: provider
    for provider in [
        ProviderDefinition(
            identifier="opencode",
            name="OpenCode Zen",
            litellm_prefix="openai",
            env_vars=("OPENCODE_API_KEY",),
            default_base_url="https://opencode.ai/zen/v1",
            uses_custom_base_url=True,
            # Zen serves its free tier anonymously: the client sends the sentinel `public` key and
            # an opencode User-Agent, and paid calls require a real OPENCODE_API_KEY instead.
            default_headers={
                "User-Agent": "opencode/0.0.0",
                "x-opencode-client": "langmesh",
            },
            anonymous_api_key="public",
        ),
        ProviderDefinition(
            # Command Code's Provider API: OpenAI-compatible, same key that signs into the CLI.
            identifier="commandcode",
            name="Command Code",
            litellm_prefix="openai",
            env_vars=("COMMAND_CODE_API_KEY",),
            default_base_url="https://api.commandcode.ai/provider/v1",
            openai_compatible=True,
            uses_custom_base_url=True,
        ),
        ProviderDefinition(
            identifier="anthropic",
            name="Anthropic",
            litellm_prefix="anthropic",
            env_vars=("ANTHROPIC_API_KEY",),
        ),
        # The three big clouds' own resale of the frontier models, billed through an existing cloud account.
        ProviderDefinition(
            identifier="azure",
            name="Azure OpenAI",
            litellm_prefix="azure",
            env_vars=("AZURE_API_KEY",),
            # Every Azure account has its own resource host, so there is no default worth registering.
            uses_custom_base_url=True,
        ),
        ProviderDefinition(
            identifier="alibaba",
            name="Alibaba Model Studio",
            litellm_prefix="dashscope",
            env_vars=("DASHSCOPE_API_KEY",),
        ),
        ProviderDefinition(
            identifier="vercel",
            name="Vercel AI Gateway",
            litellm_prefix="vercel_ai_gateway",
            env_vars=("VERCEL_AI_GATEWAY_API_KEY", "AI_GATEWAY_API_KEY"),
        ),
        ProviderDefinition(
            identifier="openai",
            name="OpenAI",
            litellm_prefix="openai",
            env_vars=("OPENAI_API_KEY",),
        ),
        ProviderDefinition(
            # Experimental: pay for model calls with a ChatGPT subscription, unlocked by signing in rather than a key.
            identifier="chatgpt",
            name="ChatGPT Subscription Plan",
            litellm_prefix="",
            env_vars=(),
            native=True,
        ),
        ProviderDefinition(
            # Experimental: pay for model calls with a Cursor subscription, unlocked by signing in rather than a key.
            identifier="cursor",
            name="Cursor Subscription Plan",
            litellm_prefix="",
            env_vars=(),
            native=True,
        ),
        ProviderDefinition(
            identifier="google",
            name="Google Gemini",
            litellm_prefix="gemini",
            env_vars=("GOOGLE_GENERATIVE_AI_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY"),
        ),
        ProviderDefinition(
            identifier="openrouter",
            name="OpenRouter",
            litellm_prefix="openrouter",
            env_vars=("OPENROUTER_API_KEY",),
        ),
        ProviderDefinition(
            identifier="xai",
            name="xAI",
            litellm_prefix="xai",
            env_vars=("XAI_API_KEY",),
        ),
        ProviderDefinition(
            identifier="zai",
            name="Zhipu AI",
            litellm_prefix="zai",
            env_vars=("ZAI_API_KEY", "ZHIPU_API_KEY"),
        ),
        ProviderDefinition(
            identifier="deepseek",
            name="DeepSeek",
            litellm_prefix="deepseek",
            env_vars=("DEEPSEEK_API_KEY",),
        ),
        ProviderDefinition(
            identifier="groq",
            name="Groq",
            litellm_prefix="groq",
            env_vars=("GROQ_API_KEY",),
        ),
        ProviderDefinition(
            identifier="mistral",
            name="Mistral",
            litellm_prefix="mistral",
            env_vars=("MISTRAL_API_KEY",),
        ),
        ProviderDefinition(
            identifier="meta_llama",
            name="Meta Llama",
            litellm_prefix="meta_llama",
            env_vars=("LLAMA_API_KEY", "META_LLAMA_API_KEY"),
        ),
        ProviderDefinition(
            identifier="ai21",
            name="AI21",
            litellm_prefix="ai21",
            env_vars=("AI21_API_KEY",),
        ),
        ProviderDefinition(
            identifier="cerebras",
            name="Cerebras",
            litellm_prefix="cerebras",
            env_vars=("CEREBRAS_API_KEY",),
        ),
        ProviderDefinition(
            identifier="cohere",
            name="Cohere",
            litellm_prefix="cohere",
            env_vars=("COHERE_API_KEY",),
        ),
        ProviderDefinition(
            identifier="databricks",
            name="Databricks",
            litellm_prefix="databricks",
            env_vars=("DATABRICKS_API_KEY", "DATABRICKS_TOKEN"),
        ),
        ProviderDefinition(
            identifier="deepinfra",
            name="DeepInfra",
            litellm_prefix="deepinfra",
            env_vars=("DEEPINFRA_API_KEY",),
        ),
        ProviderDefinition(
            identifier="hyperbolic",
            name="Hyperbolic",
            litellm_prefix="hyperbolic",
            env_vars=("HYPERBOLIC_API_KEY",),
        ),
        ProviderDefinition(
            identifier="lambda_ai",
            name="Lambda AI",
            litellm_prefix="lambda_ai",
            env_vars=("LAMBDA_API_KEY",),
        ),
        ProviderDefinition(
            identifier="minimax",
            name="MiniMax",
            litellm_prefix="minimax",
            env_vars=("MINIMAX_API_KEY",),
        ),
        ProviderDefinition(
            identifier="perplexity",
            name="Perplexity AI",
            litellm_prefix="perplexity",
            env_vars=("PERPLEXITY_API_KEY", "PERPLEXITYAI_API_KEY"),
        ),
        ProviderDefinition(
            identifier="sambanova",
            name="SambaNova",
            litellm_prefix="sambanova",
            env_vars=("SAMBANOVA_API_KEY", "SAMBA_NOVA_API_KEY"),
        ),
        ProviderDefinition(
            identifier="oci",
            name="OCI",
            litellm_prefix="oci",
            env_vars=("OCI_API_KEY",),
        ),
        ProviderDefinition(
            identifier="nebius",
            name="Nebius AI Studio",
            litellm_prefix="nebius",
            env_vars=("NEBIUS_API_KEY",),
        ),
        ProviderDefinition(
            identifier="nscale",
            name="Nscale",
            litellm_prefix="nscale",
            env_vars=("NSCALE_API_KEY",),
        ),
        ProviderDefinition(
            identifier="ovhcloud",
            name="OVHcloud",
            litellm_prefix="ovhcloud",
            env_vars=("OVHCLOUD_API_KEY",),
        ),
        ProviderDefinition(
            identifier="hetzner",
            name="Hetzner Inference",
            litellm_prefix="openai",
            env_vars=("HETZNER_API_KEY",),
            default_base_url="https://inference.hetzner.com/api/v1",
            openai_compatible=True,
        ),
        ProviderDefinition(
            identifier="scaleway",
            name="Scaleway",
            litellm_prefix="openai",
            env_vars=("SCALEWAY_API_KEY",),
            default_base_url="https://api.scaleway.ai/v1",
            openai_compatible=True,
        ),
        ProviderDefinition(
            identifier="volcengine",
            name="Volcengine",
            litellm_prefix="volcengine",
            env_vars=("VOLCENGINE_API_KEY",),
        ),
        ProviderDefinition(
            identifier="featherless_ai",
            name="Featherless AI",
            litellm_prefix="featherless_ai",
            env_vars=("FEATHERLESS_API_KEY",),
        ),
        ProviderDefinition(
            identifier="inception",
            name="Inception",
            litellm_prefix="inception",
            env_vars=("INCEPTION_API_KEY",),
        ),
        ProviderDefinition(
            identifier="maritalk",
            name="MariTalk",
            litellm_prefix="maritalk",
            env_vars=("MARITALK_API_KEY",),
        ),
        ProviderDefinition(
            identifier="morph",
            name="Morph",
            litellm_prefix="morph",
            env_vars=("MORPH_API_KEY",),
        ),
        ProviderDefinition(
            identifier="wandb",
            name="Weights & Biases",
            litellm_prefix="wandb",
            env_vars=("WANDB_API_KEY",),
        ),
        ProviderDefinition(
            identifier="custom",
            name="Custom (OpenAI-compatible)",
            litellm_prefix="openai",
            env_vars=(),
            openai_compatible=True,
        ),
    ]
}


def get_provider_definition(provider_identifier: str) -> ProviderDefinition | None:
    return PROVIDERS.get(provider_identifier)


def register_models_dev_provider(
    identifier: str,
    *,
    name: str,
    litellm_prefix: str,
    env_vars: tuple[str, ...],
    default_base_url: str = "",
    openai_compatible: bool = False,
    uses_custom_base_url: bool = False,
    credential_identifier: str = "",
) -> ProviderDefinition:
    """Register a provider discovered from models.dev that has no curated definition. Idempotent."""
    existing = PROVIDERS.get(identifier)
    if existing is not None:
        return existing
    definition = ProviderDefinition(
        identifier=identifier,
        name=name,
        litellm_prefix=litellm_prefix,
        env_vars=env_vars,
        default_base_url=default_base_url,
        openai_compatible=openai_compatible,
        uses_custom_base_url=uses_custom_base_url,
        credential_identifier=credential_identifier,
    )
    PROVIDERS[identifier] = definition
    return definition


def extend_provider_env_vars(provider_identifier: str, additional: tuple[str, ...]) -> None:
    """Accept a provider's additional key names without replacing the curated ones."""
    definition = PROVIDERS.get(provider_identifier)
    if definition is None or not additional:
        return
    merged = tuple(dict.fromkeys((*definition.env_vars, *additional)))
    if merged == definition.env_vars:
        return
    PROVIDERS[provider_identifier] = replace(definition, env_vars=merged)


def conventional_api_key_env(provider_identifier: str) -> str:
    """``{IDENTIFIER}_API_KEY`` with hyphens turned into underscores."""
    return f"{provider_identifier.strip().upper().replace('-', '_')}_API_KEY"


def provider_env_vars(provider_identifier: str) -> tuple[str, ...]:
    """Catalogue key names for this provider, then the conventional ``{IDENTIFIER}_API_KEY``.

    Native providers (ChatGPT, Cursor) have no key: they sign in.
    """
    identifier = provider_identifier.strip()
    definition = PROVIDERS.get(identifier) or PROVIDERS.get(identifier.lower())
    if definition is not None and definition.native:
        return ()
    names = list(definition.env_vars) if definition is not None else []
    if identifier:
        conventional = conventional_api_key_env(identifier)
        if conventional not in names:
            names.append(conventional)
    return tuple(names)


def resolve_api_key(
    provider_identifier: str,
    configured_keys: dict[str, str],
) -> str:
    """Resolve a provider key through Models Provider's authentication boundary."""
    from langmesh.base.secrets import provider_api_key_name, read_secret
    from models_provider import ProviderAuthentication, ProviderAuthProfile

    identifier = provider_identifier.strip()
    definition = PROVIDERS.get(identifier) or PROVIDERS.get(identifier.lower())
    credential_identifier = identifier
    if definition is not None:
        credential_identifier = definition.credential_identifier or definition.identifier
    configured = configured_keys.get(credential_identifier, "") or configured_keys.get(
        identifier, ""
    )
    if configured:
        return configured
    file_key = read_secret(provider_api_key_name(credential_identifier))
    if file_key:
        return file_key
    if identifier != credential_identifier:
        file_key = read_secret(provider_api_key_name(identifier))
        if file_key:
            return file_key
    profile = ProviderAuthProfile(
        identifier=identifier,
        environment_variables=provider_env_vars(identifier),
        default_base_url=definition.default_base_url if definition is not None else "",
        headers=definition.default_headers if definition is not None else {},
        anonymous_api_key=definition.anonymous_api_key if definition is not None else "",
    )
    return ProviderAuthentication({identifier: profile}, api_keys={identifier: configured or file_key}).resolve_key(
        identifier,
        environment_variables=profile.environment_variables,
    ).api_key


def resolve_base_url(
    provider_identifier: str,
    configured_bases: dict[str, str],
) -> str:
    """Resolve a provider's explicit base URL or its registered default."""
    configured = configured_bases.get(provider_identifier, "")
    if configured:
        return configured
    definition = PROVIDERS.get(provider_identifier)
    return definition.default_base_url if definition is not None else ""
