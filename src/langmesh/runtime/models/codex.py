"""LangMesh's cache-aware wrapper around the Models Provider ChatGPT client."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, AsyncIterator, Callable, cast

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage
from langchain_core.messages.ai import add_ai_message_chunks
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from langchain_core.runnables import Runnable
from langchain_core.tools import BaseTool
from langchain_core.utils.function_calling import convert_to_openai_tool
from pydantic import PrivateAttr

from models_provider import ChatGPTResponsesModel, ContextWindowError

from langmesh.base.content.model_errors import ContextWindowExceeded
from langmesh.runtime.cache_trace import (
    INSTRUCTIONS,
    ITEM,
    SETTINGS,
    TOOLS,
    Piece,
    RequestTrace,
    active_cache_lane,
    diagnose,
    remember_cache_lane,
    reconcile,
    restore_request_traces,
    trace,
)


@dataclass(frozen=True)
class CodexCacheState:
    """The LangMesh cache-diagnostic baselines retained for one ChatGPT route."""

    model: str
    traces: Mapping[str, RequestTrace]


class ChatCodexModel(BaseChatModel):
    """A LangMesh cache-aware facade over Models Provider's ChatGPT Responses model."""

    model: str
    reasoning_effort: str | None = None
    temperature: float = 0.0
    context_length: int = 0
    session_id: str = ""
    timeout: float | None = 300.0
    credential_store: Any = None

    _provider_model: ChatGPTResponsesModel = PrivateAttr()
    _previous_traces: dict[str, RequestTrace] = PrivateAttr(default_factory=dict)

    def model_post_init(self, __context: Any) -> None:
        self._provider_model = ChatGPTResponsesModel(
            model=self.model,
            reasoning_effort=self.reasoning_effort,
            temperature=self.temperature,
            context_length=self.context_length,
            session_id=self.session_id,
            timeout=self.timeout,
            credential_store=self.credential_store,
        )

    @property
    def _llm_type(self) -> str:
        return "codex"

    def context_window(self) -> int:
        return self._provider_model.context_window()

    @property
    def _identifying_params(self) -> dict[str, Any]:
        return {"model": self.model, "reasoning_effort": self.reasoning_effort}

    def model_cache_snapshot(self) -> CodexCacheState:
        return CodexCacheState(self.model, dict(self._previous_traces))

    def restore_model_cache(self, snapshot: object) -> None:
        if isinstance(snapshot, CodexCacheState):
            if snapshot.model == self.model:
                self._previous_traces = dict(snapshot.traces)
            return
        if isinstance(snapshot, Mapping) and snapshot.get("model") == self.model:
            self._previous_traces = restore_request_traces(snapshot.get("traces"))

    def bind_tools(
        self,
        tools: Sequence[dict[str, Any] | type | Callable[..., Any] | BaseTool],
        *,
        tool_choice: str | None = None,
        parallel_tool_calls: bool | None = None,
        **kwargs: Any,
    ) -> Runnable:
        return self.bind(
            tools=[
                ChatGPTResponsesModel.responses_tool(convert_to_openai_tool(tool))
                for tool in tools
            ],
            **{
                key: value
                for key, value in {
                    "tool_choice": tool_choice,
                    "parallel_tool_calls": parallel_tool_calls,
                    **kwargs,
                }.items()
                if value is not None
            },
        )

    def _trace_payload(self, payload: dict[str, Any]) -> RequestTrace:
        pieces = [
            Piece(kind=INSTRUCTIONS, text=payload.get("instructions") or ""),
            Piece(kind=TOOLS, text=_compact(payload.get("tools") or [])),
            Piece(
                kind=SETTINGS,
                text=_compact(
                    {
                        "reasoning": payload.get("reasoning"),
                        "tool_choice": payload.get("tool_choice"),
                    }
                ),
            ),
        ]
        for position, item in enumerate(payload.get("input") or []):
            pieces.append(
                Piece(
                    kind=ITEM,
                    text=_compact(item),
                    position=position,
                    role=str(item.get("role") or item.get("type") or ""),
                )
            )
        return trace(pieces)

    def _cache_diagnosis(self, current: RequestTrace) -> dict[str, object]:
        lane = active_cache_lane()
        previous = self._previous_traces.get(lane)
        if previous is None and lane != "conversation":
            previous = self._previous_traces.get("conversation")
        diagnosis = diagnose(current, previous)
        remember_cache_lane(self._previous_traces, lane, current)
        return diagnosis

    @staticmethod
    def _translate_context_error(error: ContextWindowError) -> ContextWindowExceeded:
        return ContextWindowExceeded(
            str(error),
            model=error.model,
            context_window=error.context_window,
        )

    async def _astream(
        self,
        messages: Sequence[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> AsyncIterator[ChatGenerationChunk]:
        payload = self._provider_model.build_payload(messages, stream=True, **kwargs)
        diagnosis = self._cache_diagnosis(self._trace_payload(payload))
        reported = False
        try:
            async for chunk in self._provider_model.stream_generations(
                messages, stop=stop, **kwargs
            ):
                usage = getattr(chunk.message, "usage_metadata", None)
                if usage and not reported:
                    reported = True
                    reconcile(
                        diagnosis,
                        int((usage.get("input_token_details") or {}).get("cache_read", 0) or 0),
                    )
                    chunk.message.additional_kwargs["cache_trace"] = diagnosis
                yield chunk
        except ContextWindowError as error:
            raise self._translate_context_error(error) from error

    @classmethod
    def _chunks_to_result(cls, chunks: list[AIMessageChunk]) -> ChatResult:
        aggregate = add_ai_message_chunks(chunks[0], *chunks[1:]) if chunks else None
        if aggregate is None:
            return ChatResult(generations=[])
        return ChatResult(
            generations=[
                ChatGeneration(
                    message=AIMessage(
                        content=aggregate.content,
                        tool_calls=list(aggregate.tool_calls or []),
                        additional_kwargs=aggregate.additional_kwargs,
                        usage_metadata=aggregate.usage_metadata,
                    )
                )
            ]
        )

    async def _agenerate(
        self,
        messages: Sequence[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        chunks: list[AIMessageChunk] = []
        async for chunk in self._astream(messages, stop=stop, run_manager=run_manager, **kwargs):
            chunks.append(cast(AIMessageChunk, chunk.message))
        return self._chunks_to_result(chunks)

    def _generate(
        self,
        messages: Sequence[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        payload = self._provider_model.build_payload(messages, stream=True, **kwargs)
        diagnosis = self._cache_diagnosis(self._trace_payload(payload))
        try:
            result = self._provider_model.generate_result(messages, stop=stop, **kwargs)
        except ContextWindowError as error:
            raise self._translate_context_error(error) from error
        if result.generations:
            message = result.generations[0].message
            usage = getattr(message, "usage_metadata", None)
            if usage:
                reconcile(
                    diagnosis,
                    int((usage.get("input_token_details") or {}).get("cache_read", 0) or 0),
                )
                message.additional_kwargs["cache_trace"] = diagnosis
        return result


def _compact(value: Any) -> str:
    """Keep cache diagnostics independent from the provider package's JSON helper."""
    import json

    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


__all__ = ["ChatCodexModel", "CodexCacheState"]
