"""The LangMesh session model facade and provider selection policy."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, AsyncIterator, Callable, cast

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import BaseMessage
from langchain_core.outputs import ChatGenerationChunk, ChatResult
from langchain_core.runnables import Runnable
from langchain_core.tools import BaseTool
from models_provider import ProviderAuthentication, provider_auth_profile
from pydantic import Field, PrivateAttr

from langmesh.base.configuration import AgentConfiguration
from langmesh.base.content.model_routing import resolve_litellm
from langmesh.base.content.models import find_model
from langmesh.base.identity.providers import get_provider_definition, provider_env_vars


class SessionModel(BaseChatModel):
    """A ready-to-use session model that delegates to the selected provider implementation."""

    agent_configuration: AgentConfiguration = Field(exclude=True)
    model_identifier: str = ""
    working_directory: str
    session_id: str = ""
    credential_store: Any = Field(default=None, exclude=True)
    provider_api_keys: Mapping[str, str] = Field(default_factory=dict, exclude=True)
    provider_base_urls: Mapping[str, str] = Field(default_factory=dict, exclude=True)

    _model: BaseChatModel = PrivateAttr()

    def model_post_init(self, __context: Any) -> None:
        selected_identifier = self.model_identifier or self.agent_configuration.model_identifier
        if not selected_identifier or "/" not in selected_identifier:
            raise ValueError("model_identifier must have the form 'provider/model'")
        provider_identifier, model_suffix = selected_identifier.split("/", 1)
        if provider_identifier == "chatgpt":
            from langmesh.runtime.models.codex import ChatCodexModel

            catalog_entry = find_model(selected_identifier)
            self._model = ChatCodexModel(
                model=model_suffix,
                reasoning_effort=self.agent_configuration.reasoning_effort,
                context_length=catalog_entry.context_length if catalog_entry else 0,
                session_id=self.session_id,
                credential_store=self.credential_store,
            )
            return
        if provider_identifier == "cursor":
            from langmesh.runtime.models.cursor import ChatCursorModel

            catalog_entry = find_model(selected_identifier)
            self._model = ChatCursorModel(
                model=model_suffix,
                workspace=self.working_directory,
                context_length=catalog_entry.context_length if catalog_entry else 0,
            )
            return

        from langmesh.runtime.models.litellm import ChatLiteLLMModel

        resolved = resolve_litellm(
            selected_identifier,
            dict(self.provider_api_keys),
            dict(self.provider_base_urls),
            credential_store=self.credential_store,
        )
        catalogued = find_model(selected_identifier)
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
            api_keys=dict(self.provider_api_keys),
            api_bases=dict(self.provider_base_urls),
            store=self.credential_store,
        )
        self._model = ChatLiteLLMModel.model_validate(
            {
                "model": resolved["model"],
                "api_key": resolved["api_key"],
                "api_base": resolved["api_base"] or None,
                "default_headers": resolved["headers"],
                "session_id": self.session_id,
                "context_length": catalogued.context_length if catalogued else 0,
                "temperature": 0,
                "reasoning_effort": self.agent_configuration.reasoning_effort,
                "provider_identifier": provider_identifier,
                "provider_environment_variables": profile.environment_variables,
            }
        )
        self._model._authentication = authentication

    @property
    def _llm_type(self) -> str:
        return "langmesh-session"

    def context_window(self) -> int:
        context_window = cast(Callable[[], int] | None, getattr(self._model, "context_window", None))
        return int(context_window()) if callable(context_window) else 0

    def bind_tools(
        self,
        tools: Sequence[dict[str, Any] | type | Callable[..., Any] | BaseTool],
        *,
        tool_choice: str | None = None,
        parallel_tool_calls: bool | None = None,
        **kwargs: Any,
    ) -> Runnable:
        return self._model.bind_tools(
            tools,
            tool_choice=tool_choice,
            parallel_tool_calls=parallel_tool_calls,
            **kwargs,
        )

    def model_cache_snapshot(self) -> object | None:
        snapshot = getattr(self._model, "model_cache_snapshot", None)
        return snapshot() if callable(snapshot) else None

    def restore_model_cache(self, snapshot: object) -> None:
        restore = getattr(self._model, "restore_model_cache", None)
        if callable(restore):
            restore(snapshot)

    async def _astream(
        self,
        messages: Sequence[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> AsyncIterator[ChatGenerationChunk]:
        stream = cast(
            Callable[..., AsyncIterator[ChatGenerationChunk]] | None,
            getattr(self._model, "_astream", None),
        )
        if not callable(stream):
            raise TypeError(f"{type(self._model).__name__} does not support streaming")
        async for chunk in stream(messages, stop=stop, run_manager=run_manager, **kwargs):
            yield chunk

    async def _agenerate(
        self,
        messages: Sequence[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        generate = cast(Callable[..., Any] | None, getattr(self._model, "_agenerate", None))
        if not callable(generate):
            raise TypeError(f"{type(self._model).__name__} does not support async generation")
        return await generate(messages, stop=stop, run_manager=run_manager, **kwargs)

    def _generate(
        self,
        messages: Sequence[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        generate = cast(Callable[..., Any] | None, getattr(self._model, "_generate", None))
        if not callable(generate):
            raise TypeError(f"{type(self._model).__name__} does not support generation")
        return generate(messages, stop=stop, run_manager=run_manager, **kwargs)


__all__ = ["SessionModel"]
