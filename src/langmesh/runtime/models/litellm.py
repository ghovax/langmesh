from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from collections.abc import Mapping
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, AsyncIterator, Callable, ClassVar, Optional, Sequence, cast
from uuid import uuid4

import httpx
import litellm
from models_provider import ProviderAuthentication
from langchain_core.language_models.chat_models import BaseChatModel

# LiteLLM raises on a parameter a provider does not support, so unsupported ones are dropped rather than sent.
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    BaseMessage,
    ToolMessage,
)
from langchain_core.messages.ai import UsageMetadata
from langchain_core.messages.content import ContentBlock, ReasoningContentBlock
from langchain_core.messages.tool import ToolCallChunk
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from langchain_core.runnables import Runnable
from langchain_core.tools import BaseTool
from langchain_core.utils.function_calling import convert_to_openai_tool
from pydantic import Field, PrivateAttr, SecretStr

from langmesh.base.primitives.serialization import compact
from langmesh.runtime.cache_trace import (
    INSTRUCTIONS,
    ITEM,
    SETTINGS,
    TOOLS,
    Piece,
    RequestTrace,
    active_cache_lane,
    diagnose,
    provider_cache_key,
    remember_cache_lane,
    reconcile,
    restore_request_traces,
    trace,
)
from langmesh.base.content.message_content import (
    REASONING_MODEL_KEY,
    carried_reasoning_for,
    content_blocks_to_message_content,
    message_reasoning_text,
    message_text,
)

litellm.drop_params = True
logger = logging.getLogger("langmesh.runtime.litellm")

#: The one thing worth asking a Responses request for: the model's own thinking, encrypted, to hand back next call.
_ENCRYPTED_REASONING = ["reasoning.encrypted_content"]

#: Routes that want `reasoning_content` on every assistant message, present and empty rather than absent.
_ALWAYS_REASONING_ROUTES = ("deepseek",)

#: Providers whose reasoning is only recoverable through the Responses API rather than Chat Completions.
_RESPONSES_ROUTES = ("openai", "azure")


@dataclass(frozen=True)
class LiteLLMCacheState:
    """The diagnostics and explicit breakpoints retained for one LiteLLM route."""

    model: str
    api_base: str
    traces: Mapping[str, RequestTrace]
    anchors: Mapping[str, tuple[str, ...]]


class ChatLiteLLMModel(BaseChatModel):
    """A `BaseChatModel` backed by LiteLLM, the single route to every provider this harness can reach."""

    model: str
    api_key: Optional[SecretStr] = None
    api_base: Optional[str] = None
    #: The conversation this model serves, sent as the provider's prompt cache key.
    session_id: str = ""
    #: The window models.dev advertises, for models LiteLLM's own map has never heard of.
    context_length: int = 0
    temperature: float = 0.0
    reasoning_effort: Optional[str] = None
    maximum_tokens: Optional[int] = None
    # A bounded request timeout, since the streaming loop only checks for aborts between chunks.
    timeout: Optional[float] = 300.0
    default_headers: dict[str, str] = Field(default_factory=dict)

    provider_identifier: str = ""
    provider_environment_variables: tuple[str, ...] = ()

    #: The last request's segment trace, declared rather than merely assigned so Pydantic will hold it.
    _previous_traces: dict[str, RequestTrace] = PrivateAttr(default_factory=dict)

    #: The latest attempted prefix and its fallback, retained independently for each lane.
    _cache_anchors: dict[str, tuple[str, ...]] = PrivateAttr(default_factory=dict)

    _authentication: ProviderAuthentication | None = PrivateAttr(default=None)

    @property
    def _llm_type(self) -> str:
        return "litellm"

    def context_window(self) -> int:
        """The model's maximum input context in tokens, from LiteLLM's map or the catalogue, and zero when genuinely unknown."""
        catalogued = max(0, int(self.context_length or 0))
        try:
            info = litellm.get_model_info(self.model)
            live = int(info.get("max_input_tokens") or info.get("max_tokens") or 0)
        except Exception:  # noqa: BLE001 — an unknown id is a custom endpoint, not a failure
            live = 0
        if live and catalogued:
            return min(live, catalogued)
        return live or catalogued

    @property
    def _identifying_params(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "api_base": self.api_base,
            "temperature": self.temperature,
            "reasoning_effort": self.reasoning_effort,
        }

    def _custom_chat_endpoint(self) -> bool:
        """An openai-prefixed host that is not api.openai.com, so it speaks chat completions."""
        if self._route() != "openai":
            return False
        base = (self.api_base or "").lower()
        return bool(base) and "api.openai.com" not in base

    def _opencode_host(self) -> bool:
        return "opencode.ai" in (self.api_base or "").lower()

    def _speaks_responses(self) -> bool:
        """Whether this model's reasoning only round-trips over the Responses API, asked of LiteLLM's own model map."""
        if self._custom_chat_endpoint():
            return False
        route, _, remainder = self.model.partition("/")
        if remainder.startswith("responses/"):
            return True
        if route.lower() not in _RESPONSES_ROUTES:
            return False
        try:
            info = litellm.get_model_info(self.model)
        except Exception:  # noqa: BLE001 — an unknown id is a custom endpoint; leave it alone
            return False
        return info.get("mode") == "responses" or bool(info.get("supports_reasoning"))

    def _request_model(self) -> str:
        """The model id to send, which is the Responses one when reasoning depends on it."""
        route, separator, remainder = self.model.partition("/")
        if separator and not remainder.strip():
            raise ValueError(f"empty model suffix: {self.model!r}")
        if not self._speaks_responses() or not separator or remainder.startswith("responses/"):
            return self.model
        return f"{route}/responses/{remainder}"

    # Tool binding.

    def bind_tools(
        self,
        tools: Sequence[dict[str, Any] | type | Callable[..., Any] | BaseTool],
        *,
        tool_choice: Optional[str] = None,
        parallel_tool_calls: Optional[bool] = None,
        **kwargs: Any,
    ) -> Runnable:
        formatted_tools = [convert_to_openai_tool(tool) for tool in tools]
        bound: dict[str, Any] = {"tools": formatted_tools}
        if tool_choice is not None:
            bound["tool_choice"] = tool_choice
        if parallel_tool_calls is not None:
            bound["parallel_tool_calls"] = parallel_tool_calls
        return self.bind(**bound, **kwargs)

    # Message translation between LangChain messages and LiteLLM request dicts.

    def _messages_to_dicts(self, messages: Sequence[BaseMessage]) -> list[dict[str, Any]]:
        dicts: list[dict[str, Any]] = []
        for message in messages:
            role = ChatLiteLLMModel._role_for(message)
            entry: dict[str, Any] = {
                "role": role,
                "content": message_text(message)
                if isinstance(message, AIMessage)
                else message.content,
            }
            if isinstance(message, AIMessage):
                tool_calls = ChatLiteLLMModel._tool_calls_to_openai(message.tool_calls)
                if tool_calls:
                    entry["tool_calls"] = tool_calls
                # Reasoning goes back in exactly one form, preferring the provider-native block that carries its own signature.
                carried = carried_reasoning_for(message, self.model)
                entry.update(carried)
                reasoning = message_reasoning_text(message)
                if not carried and (reasoning or self._route() in _ALWAYS_REASONING_ROUTES):
                    entry["reasoning_content"] = reasoning
            elif isinstance(message, ToolMessage):
                entry["tool_call_id"] = message.tool_call_id
            dicts.append(entry)
        return dicts

    def _reasoning_to_carry(self, source: Any) -> dict[str, Any]:
        """Whatever of the provider's own reasoning this message can hand back, taking the signed block over the deltas."""
        carried: dict[str, Any] = {}
        blocks = [
            block
            for block in ChatLiteLLMModel._plain(getattr(source, "thinking_blocks", None))
            if block.get("type") == "redacted_thinking" or block.get("signature")
        ]
        if blocks:
            carried["thinking_blocks"] = blocks
        items = ChatLiteLLMModel._plain(getattr(source, "reasoning_items", None))
        if items:
            carried["reasoning_items"] = items
        if carried:
            carried[REASONING_MODEL_KEY] = self.model
        return carried

    @staticmethod
    def _plain(value: Any) -> list[dict[str, Any]]:
        """A list of provider objects as plain dicts, so it survives being checkpointed to JSON."""
        plain: list[dict[str, Any]] = []
        for entry in value or []:
            if isinstance(entry, dict):
                plain.append(dict(entry))
            elif hasattr(entry, "model_dump"):
                plain.append(entry.model_dump(exclude_none=True))
        return plain

    #: What makes a model want explicit cache breakpoints is being a Claude, wherever it is served from.
    _CACHE_BREAKPOINT_MARKERS = ("claude", "anthropic")

    #: Routes whose models take the same breakpoints regardless of family, like Model Studio's Qwen.
    _CACHE_BREAKPOINT_ROUTES = ("dashscope",)

    #: The one route that must never be marked, because a gateway rewrites the request it is caching.
    _GATEWAY_ROUTE = "vercel_ai_gateway"

    #: Copilot resells Claude but reads a differently named marker, so the key is named per route rather than assumed.
    _CACHE_CONTROL_KEYS: ClassVar[dict[str, str]] = {"github_copilot": "copilot_cache_control"}
    _DEFAULT_CACHE_CONTROL_KEY = "cache_control"

    def _route(self) -> str:
        """The LiteLLM provider this model is addressed through — the first path segment."""
        return self.model.split("/", 1)[0].lower()

    def _cache_control_key(self) -> str:
        return self._CACHE_CONTROL_KEYS.get(self._route(), self._DEFAULT_CACHE_CONTROL_KEY)

    def _apply_cache_breakpoints(
        self,
        dicts: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], tuple[str, str] | None]:
        """Keep recent prefix candidates reachable and mark the new tail for the next request."""
        route = self._route()
        if route == self._GATEWAY_ROUTE:
            return dicts, None
        model = self.model.lower()
        if not (
            any(marker in model for marker in self._CACHE_BREAKPOINT_MARKERS)
            or route in self._CACHE_BREAKPOINT_ROUTES
        ):
            return dicts, None
        for entry in dicts:
            self._normalize_cacheable_content(entry)
        cacheable = [entry for entry in dicts if self._cacheable(entry)]
        if not cacheable:
            return dicts, None
        lane = active_cache_lane()
        previous = self._cache_anchors.get(lane)
        if previous is None and lane != "conversation":
            previous = self._cache_anchors.get("conversation")
        identities = [(entry, self._cache_identity(entry)) for entry in cacheable]
        for anchor in previous or ():
            for entry, identity in reversed(identities):
                if identity == anchor:
                    self._mark_cached(entry, self._cache_control_key())
                    break
        tail, identity = identities[-1]
        self._mark_cached(tail, self._cache_control_key())
        return dicts, (lane, identity)

    @staticmethod
    def _normalize_cacheable_content(entry: dict[str, Any]) -> None:
        """Give text one stable wire shape whether or not this request marks its block."""
        content = entry.get("content")
        if isinstance(content, str) and content:
            entry["content"] = [{"type": "text", "text": content}]

    @staticmethod
    def _cacheable(entry: dict[str, Any]) -> bool:
        """Whether the entry has a provider block that can carry an explicit breakpoint."""
        if entry.get("role") == "tool":
            return True
        content = entry.get("content")
        return bool(isinstance(content, list) and content and isinstance(content[-1], dict))

    @staticmethod
    def _cache_identity(entry: dict[str, Any]) -> str:
        """Identify one stable message independently of temporary cache-control metadata."""
        return hashlib.blake2b(compact(entry).encode(), digest_size=16).hexdigest()

    def _remember_cache_candidate(self, candidate: tuple[str, str] | None) -> None:
        """Retain the attempted tail and its fallback so a dropped request loses neither lookup."""
        if candidate is not None:
            lane, identity = candidate
            prior = self._cache_anchors.get(lane, ())
            remember_cache_lane(
                self._cache_anchors,
                lane,
                tuple(dict.fromkeys((identity, *prior)))[:2],
            )

    def model_cache_snapshot(self) -> LiteLLMCacheState:
        """Return the bounded diagnostics and explicit breakpoints owned by this session."""
        return LiteLLMCacheState(
            model=self.model,
            api_base=self.api_base or "",
            traces=dict(self._previous_traces),
            anchors=dict(self._cache_anchors),
        )

    def restore_model_cache(self, snapshot: object) -> None:
        """Restore validated diagnostics and breakpoint anchors from durable session state."""
        if isinstance(snapshot, LiteLLMCacheState):
            if snapshot.model != self.model or snapshot.api_base != (self.api_base or ""):
                return
            self._previous_traces = dict(snapshot.traces)
            self._cache_anchors = dict(snapshot.anchors)
            return
        if not isinstance(snapshot, Mapping):
            return
        if snapshot.get("model") != self.model or snapshot.get("api_base", "") != (
            self.api_base or ""
        ):
            return
        self._previous_traces = restore_request_traces(snapshot.get("traces"))
        self._cache_anchors = {}
        raw_anchors = snapshot.get("anchors")
        if not isinstance(raw_anchors, dict):
            return
        for raw_lane, raw_values in list(raw_anchors.items())[-16:]:
            lane = str(raw_lane).strip()
            if not lane or not isinstance(raw_values, list):
                continue
            anchors = tuple(
                value for raw in raw_values[:2] if (value := str(raw).strip()) and len(value) <= 128
            )
            if anchors:
                remember_cache_lane(self._cache_anchors, lane, anchors)

    @staticmethod
    def _mark_cached(entry: dict[str, Any], key: str = "cache_control") -> bool:
        """Put the breakpoint where LiteLLM reads it for this role, and say whether it went."""
        if entry["role"] == "tool":
            entry[key] = {"type": "ephemeral"}
            return True
        content = entry.get("content")
        if not isinstance(content, list) or not content:
            return False
        blocks: list[Any] = entry["content"]
        last = blocks[-1]
        if not isinstance(last, dict):
            return False
        last[key] = {"type": "ephemeral"}
        return True

    @staticmethod
    def _role_for(message: BaseMessage) -> str:
        # LangChain's message types map onto the OpenAI role names LiteLLM expects.
        if message.additional_kwargs.get("reminder"):
            return "system"
        name = message.__class__.__name__
        if isinstance(message, ToolMessage):
            return "tool"
        return {
            "SystemMessage": "system",
            "HumanMessage": "user",
            "AIMessage": "assistant",
            "AIMessageChunk": "assistant",
            "ToolMessage": "tool",
        }.get(name, "user")

    @staticmethod
    def _tool_calls_to_openai(tool_calls: Sequence[Any]) -> list[dict[str, Any]]:
        rendered: list[dict[str, Any]] = []
        for call in tool_calls:
            # LangChain stores parsed arguments under `args`, while the wire format we serialize back to uses `arguments`.
            arguments = call.get("args")
            serialized = arguments if isinstance(arguments, str) else compact(arguments)
            rendered.append(
                {
                    "id": call.get("id"),
                    "type": "function",
                    "function": {"name": call.get("name"), "arguments": serialized},
                }
            )
        return rendered

    # Shared kwargs assembled for every LiteLLM completion call.

    def _completion_kwargs(self, **kwargs: Any) -> dict[str, Any]:
        params: dict[str, Any] = {
            "model": self._request_model(),
            "temperature": self.temperature,
        }
        resolved = None
        if self._authentication is not None and self.provider_identifier:
            resolved = self._authentication.resolve(
                self.provider_identifier,
                environment_variables=self.provider_environment_variables,
            )
        if resolved is not None and resolved.api_key:
            params["api_key"] = resolved.api_key
        elif self.api_key is not None:
            unsealed = self.api_key.get_secret_value()
            if unsealed:
                params["api_key"] = unsealed
        if resolved is not None and resolved.api_base:
            params["api_base"] = resolved.api_base
        elif self.api_base:
            params["api_base"] = self.api_base
        if self.reasoning_effort:
            params["reasoning_effort"] = self.reasoning_effort
        if self.maximum_tokens is not None:
            params["max_tokens"] = self.maximum_tokens  # litellm/OpenAI API param name
        if self.timeout is not None:
            params["timeout"] = self.timeout
        headers = dict(self.default_headers)
        if resolved is not None:
            headers = {**resolved.headers, **headers}
        if headers and not self._opencode_host():
            params["extra_headers"] = headers
        if self._route() == self._GATEWAY_ROUTE:
            # A gateway rewrites the request for whichever provider it routes to, so it is the only thing that can place breakpoints.
            params["extra_body"] = {**params.get("extra_body", {}), "gateway": {"caching": "auto"}}
        if self._speaks_responses():
            # Ask for the encrypted reasoning and decline to have the conversation kept, which only make sense together.
            params["extra_body"] = {
                **params.get("extra_body", {}),
                "include": _ENCRYPTED_REASONING,
                "store": False,
            }
        # Caller-supplied arguments override the model defaults, so `bind_tools` bindings reach LiteLLM.
        params.update({key: value for key, value in kwargs.items() if value is not None})
        return params

    def _attach_prompt_cache_key(self, params: dict[str, Any], sent: list[dict[str, Any]]) -> None:
        """OpenAI's routing hint. Custom openai-compatible hosts do not take it."""
        if self._custom_chat_endpoint():
            return
        params["prompt_cache_key"] = self._provider_cache_key(params, sent)

    @staticmethod
    def _empty_model_refusal(error: BaseException | str) -> bool:
        """OpenCode answers some refusals as 401 `Model  is not supported` with an empty id."""
        text = str(error)
        return "Model  is not supported" in text or "Model is not supported" in text

    def _wire_model(self) -> str:
        suffix = self.model.split("/", 1)[-1].strip()
        if not suffix:
            raise ValueError(f"empty model suffix: {self.model!r}")
        return suffix

    def _opencode_headers(self, *, anonymous: bool = False) -> dict[str, str]:
        """The few headers a curl that works against Zen actually sends.

        The OpenAI Python client adds ``x-stainless-*`` fields. Zen has been
        answering those requests as an empty-model 401 even when ``model`` is set.
        ``x-opencode-session`` is the conversation's session id so Zen can keep
        the prompt cache on the same route.
        """
        headers = {
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            "User-Agent": "opencode/0.0.0",
        }
        if self.session_id.strip():
            headers["x-opencode-session"] = self.session_id.strip()
        client = self.default_headers.get("x-opencode-client", "").strip()
        if client:
            headers["x-opencode-client"] = client
        key = ""
        if self.api_key is not None:
            key = self.api_key.get_secret_value().strip()
        if not anonymous and key and key != "public":
            headers["Authorization"] = f"Bearer {key}"
        return headers

    def _opencode_payload(
        self, sent: list[dict[str, Any]], params: dict[str, Any], *, stream: bool
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self._wire_model(),
            "messages": sent,
            "stream": stream,
        }
        if params.get("temperature") is not None:
            payload["temperature"] = params["temperature"]
        if params.get("tools"):
            payload["tools"] = params["tools"]
        if params.get("tool_choice"):
            payload["tool_choice"] = params["tool_choice"]
        if params.get("stop"):
            payload["stop"] = params["stop"]
        if params.get("max_tokens"):
            payload["max_tokens"] = params["max_tokens"]
        if stream:
            payload["stream_options"] = {"include_usage": True}
        return payload

    @staticmethod
    def _namespace(value: Any) -> Any:
        if isinstance(value, dict):
            return SimpleNamespace(
                **{key: ChatLiteLLMModel._namespace(item) for key, item in value.items()}
            )
        if isinstance(value, list):
            return [ChatLiteLLMModel._namespace(item) for item in value]
        return value

    async def _opencode_events(
        self, payload: dict[str, Any], headers: dict[str, str]
    ) -> AsyncIterator[dict[str, Any]]:
        url = f"{(self.api_base or '').rstrip('/')}/chat/completions"
        timeout = self.timeout if self.timeout is not None else 300.0
        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream("POST", url, headers=headers, json=payload) as response:
                if response.status_code >= 400:
                    text = (await response.aread()).decode("utf-8", "replace")[:800]
                    raise RuntimeError(f"OpenCode {response.status_code}: {text}")
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        return
                    if not data:
                        continue
                    yield json.loads(data)

    async def _opencode_stream_events(
        self, payload: dict[str, Any]
    ) -> AsyncIterator[dict[str, Any]]:
        last_error: Exception | None = None
        for anonymous in (False, True):
            headers = self._opencode_headers(anonymous=anonymous)
            try:
                async for event in self._opencode_events(payload, headers):
                    yield event
                return
            except Exception as error:
                last_error = error
                if not ChatLiteLLMModel._empty_model_refusal(error):
                    raise
                logger.warning(
                    "OpenCode refused the model id; retrying after a pause (anonymous=%s)",
                    anonymous,
                )
                await asyncio.sleep(8)
        assert last_error is not None
        raise last_error

    async def _opencode_completion(self, payload: dict[str, Any]) -> dict[str, Any]:
        url = f"{(self.api_base or '').rstrip('/')}/chat/completions"
        timeout = self.timeout if self.timeout is not None else 300.0
        last_error: Exception | None = None
        for anonymous in (False, True):
            headers = self._opencode_headers(anonymous=anonymous)
            if payload.get("stream"):
                headers["Accept"] = "text/event-stream"
            else:
                headers["Accept"] = "application/json"
            try:
                async with httpx.AsyncClient(timeout=timeout) as client:
                    response = await client.post(url, headers=headers, json=payload)
                    if response.status_code >= 400:
                        raise RuntimeError(
                            f"OpenCode {response.status_code}: {response.text[:800]}"
                        )
                    return response.json()
            except Exception as error:
                last_error = error
                if not ChatLiteLLMModel._empty_model_refusal(error):
                    raise
                logger.warning(
                    "OpenCode refused the model id; retrying after a pause (anonymous=%s)",
                    anonymous,
                )
                await asyncio.sleep(8)
        assert last_error is not None
        raise last_error

    def _opencode_completion_sync(self, payload: dict[str, Any]) -> dict[str, Any]:
        url = f"{(self.api_base or '').rstrip('/')}/chat/completions"
        timeout = self.timeout if self.timeout is not None else 300.0
        last_error: Exception | None = None
        for anonymous in (False, True):
            headers = self._opencode_headers(anonymous=anonymous)
            headers["Accept"] = "application/json"
            try:
                with httpx.Client(timeout=timeout) as client:
                    response = client.post(url, headers=headers, json=payload)
                    if response.status_code >= 400:
                        raise RuntimeError(
                            f"OpenCode {response.status_code}: {response.text[:800]}"
                        )
                    return response.json()
            except Exception as error:
                last_error = error
                if not ChatLiteLLMModel._empty_model_refusal(error):
                    raise
                logger.warning(
                    "OpenCode refused the model id; retrying after a pause (anonymous=%s)",
                    anonymous,
                )
                time.sleep(8)
        assert last_error is not None
        raise last_error

    def _trace_request(self, params: dict[str, Any], sent: list[dict[str, Any]]) -> RequestTrace:
        """Cut the outgoing request into the pieces a prompt cache matches on, in wire order."""
        pieces = [Piece(kind=TOOLS, text=compact(params.get("tools") or []))]
        item_start = 0
        if sent and sent[0].get("role") == "system":
            pieces.append(Piece(kind=INSTRUCTIONS, text=compact(sent[0])))
            item_start = 1
        pieces.append(
            Piece(
                kind=SETTINGS,
                text=compact(
                    {
                        "reasoning_effort": params.get("reasoning_effort"),
                        "tool_choice": params.get("tool_choice"),
                    }
                ),
            )
        )
        for position, message in enumerate(sent[item_start:], start=item_start):
            pieces.append(
                Piece(
                    kind=ITEM,
                    text=compact(message),
                    position=position,
                    role=str(message.get("role") or ""),
                )
            )
        return trace(pieces)

    def _provider_cache_key(self, params: dict[str, Any], sent: list[dict[str, Any]]) -> str:
        # Stable prefix captured once at first call — only ITEMs should append thereafter
        if not hasattr(self, "_stable_provider_key"):
            instructions = compact(sent[0]) if sent and sent[0].get("role") == "system" else ""
            self._stable_provider_key = provider_cache_key(
                str(params.get("model") or ""),
                compact(params.get("tools") or []),
                instructions,
                compact(
                    {
                        "reasoning_effort": params.get("reasoning_effort"),
                        "tool_choice": params.get("tool_choice"),
                    }
                ),
            )
        return self._stable_provider_key

    def _cache_diagnosis(self, current: RequestTrace) -> dict[str, object]:
        """What this request kept from the previous request in its cache lane."""
        lane = active_cache_lane()
        previous = self._previous_traces.get(lane)
        if previous is None and lane != "conversation":
            previous = self._previous_traces.get("conversation")
        diagnosis = diagnose(current, previous)
        remember_cache_lane(self._previous_traces, lane, current)
        return diagnosis

    # Streaming generation.

    async def _astream(
        self,
        messages: Sequence[BaseMessage],
        stop: Optional[list[str]] = None,
        run_manager=None,
        **kwargs: Any,
    ) -> AsyncIterator[ChatGenerationChunk]:
        # `include_usage` asks for a trailing usage chunk, so a streamed turn still reports real token counts.
        params = self._completion_kwargs(
            stop=stop,
            stream=True,
            stream_options={"include_usage": True},
            **kwargs,
        )
        # One name for the prose this call produces, minted here because nothing LiteLLM streams identifies the block.
        block = f"litellm-{uuid4().hex}-"
        translated = self._messages_to_dicts(messages)
        # Taken before cache-control metadata is added, because marker placement is not model-visible prompt content.
        current_trace = self._trace_request(params, translated)
        self._attach_prompt_cache_key(params, translated)
        sent, cache_candidate = self._apply_cache_breakpoints(translated)
        self._remember_cache_candidate(cache_candidate)
        # The baseline advances when the request is sent, so a usage-less or interrupted response still leaves the next request a true comparison.
        diagnosis = self._cache_diagnosis(current_trace)
        reported = False
        if self._opencode_host():
            payload = self._opencode_payload(sent, params, stream=True)
            async for event in self._opencode_stream_events(payload):
                generation_chunk = self._litellm_chunk_to_generation_chunk(
                    self._namespace(event), block
                )
                if generation_chunk is None:
                    continue
                usage = getattr(generation_chunk.message, "usage_metadata", None)
                if usage and not reported:
                    reported = True
                    reconcile(
                        diagnosis,
                        int((usage.get("input_token_details") or {}).get("cache_read", 0) or 0),
                    )
                    generation_chunk.message.additional_kwargs["cache_trace"] = diagnosis
                yield generation_chunk
            return
        stream = cast(AsyncIterator[Any], await litellm.acompletion(messages=sent, **params))
        async for chunk in stream:
            generation_chunk = self._litellm_chunk_to_generation_chunk(chunk, block)
            if generation_chunk is not None:
                usage = getattr(generation_chunk.message, "usage_metadata", None)
                # Attached to the chunk carrying usage, so the diagnosis travels with the figure it explains.
                if usage and not reported:
                    reported = True
                    # The byte verdict was made before the call; the response's cache figure corrects it.
                    reconcile(
                        diagnosis,
                        int((usage.get("input_token_details") or {}).get("cache_read", 0) or 0),
                    )
                    generation_chunk.message.additional_kwargs["cache_trace"] = diagnosis
                yield generation_chunk

    @staticmethod
    def _usage_metadata(usage: Any) -> Optional[UsageMetadata]:
        """Normalize a LiteLLM usage object into LangChain's, or `None` when the response carries none."""
        if usage is None:
            return None

        def _value(source: Any, key: str) -> int:
            if source is None:
                return 0
            if isinstance(source, dict):
                return int(source.get(key) or 0)
            return int(getattr(source, key, 0) or 0)

        input_tokens = _value(usage, "prompt_tokens")
        output_tokens = _value(usage, "completion_tokens")
        total_tokens = _value(usage, "total_tokens") or (input_tokens + output_tokens)
        if not (input_tokens or output_tokens or total_tokens):
            return None
        metadata: dict[str, Any] = {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": total_tokens,
        }
        prompt_details = (
            usage.get("prompt_tokens_details")
            if isinstance(usage, dict)
            else getattr(usage, "prompt_tokens_details", None)
        )
        cache_read = (
            _value(prompt_details, "cached_tokens")
            or _value(usage, "cache_read_input_tokens")
            or _value(usage, "cached_tokens")
            or _value(usage, "prompt_cache_hit_tokens")
        )
        cache_write = (
            _value(prompt_details, "cache_creation_tokens")
            or _value(prompt_details, "cache_write_tokens")
            or _value(usage, "cache_creation_input_tokens")
        )
        if cache_read or cache_write:
            metadata["input_token_details"] = {
                "cache_read": cache_read,
                "cache_creation": cache_write,
            }
        completion_details = (
            usage.get("completion_tokens_details")
            if isinstance(usage, dict)
            else getattr(usage, "completion_tokens_details", None)
        )
        reasoning = _value(completion_details, "reasoning_tokens")
        if reasoning:
            metadata["output_token_details"] = {"reasoning": reasoning}
        return cast(UsageMetadata, metadata)

    def _litellm_chunk_to_generation_chunk(
        self, chunk: Any, block: str = ""
    ) -> Optional[ChatGenerationChunk]:
        usage_metadata = ChatLiteLLMModel._usage_metadata(getattr(chunk, "usage", None))
        choices = getattr(chunk, "choices", None) or []
        if not choices:
            # A chunk with usage but no choices surfaces an empty message, so the merged response still accumulates the counts.
            if usage_metadata is not None:
                return ChatGenerationChunk(
                    message=AIMessageChunk(content="", usage_metadata=usage_metadata)
                )
            return None
        choice = choices[0]
        delta = getattr(choice, "delta", None)
        if delta is None:
            if usage_metadata is not None:
                return ChatGenerationChunk(
                    message=AIMessageChunk(content="", usage_metadata=usage_metadata)
                )
            return None
        content = getattr(delta, "content", None) or ""
        tool_call_chunks: list[ToolCallChunk] = []
        for call in getattr(delta, "tool_calls", None) or []:
            function = getattr(call, "function", None)
            tool_call_chunks.append(
                cast(
                    ToolCallChunk,
                    {
                        "index": getattr(call, "index", 0) or 0,
                        "id": getattr(call, "id", None),
                        "name": getattr(function, "name", None) if function else None,
                        # The streaming delta exposes partial JSON as `function.arguments`, which LangChain stores under `args`.
                        "args": getattr(function, "arguments", None) if function else None,
                        "type": "tool_call_chunk",
                    },
                )
            )
        reasoning = getattr(delta, "reasoning_content", None)
        if not reasoning:
            # Some providers nest reasoning under a different attribute.
            reasoning = getattr(delta, "reasoning", None)
        content_blocks = ChatLiteLLMModel._standard_content_blocks(content, reasoning, block)
        message = AIMessageChunk(
            content=content_blocks_to_message_content(content_blocks),
            tool_call_chunks=tool_call_chunks,
            usage_metadata=usage_metadata,
            # Chunk merging concatenates these lists, so the finished message holds every signed block in order.
            additional_kwargs=self._reasoning_to_carry(delta),
        )
        finish_reason = getattr(choice, "finish_reason", None)
        generation_info = {"finish_reason": finish_reason} if finish_reason else None
        return ChatGenerationChunk(message=message, generation_info=generation_info)

    @staticmethod
    def _standard_content_blocks(
        content: Any, reasoning: Any, block: str = ""
    ) -> list[ContentBlock]:
        """Name the blocks in one streamed chunk, keyed by the call so two calls' prose never merges into one block."""
        normalized_blocks: list[ContentBlock] = []
        if reasoning:
            normalized_blocks.append(
                ReasoningContentBlock(
                    type="reasoning",
                    reasoning=str(reasoning),
                    id=f"{block}reasoning",
                    index=0,
                )
            )
        if content:
            source_blocks = AIMessageChunk(content=content).content_blocks
            for position, source_block in enumerate(source_blocks):
                normalized_block: dict[str, Any] = dict(source_block)
                normalized_block["id"] = f"{block}content-{position}"
                normalized_block["index"] = position + 1
                normalized_blocks.append(cast(ContentBlock, normalized_block))
        return normalized_blocks

    # Non-streaming generation.

    async def _agenerate(
        self,
        messages: Sequence[BaseMessage],
        stop: Optional[list[str]] = None,
        run_manager=None,
        **kwargs: Any,
    ) -> ChatResult:
        params = self._completion_kwargs(stop=stop, **kwargs)
        translated = self._messages_to_dicts(messages)
        current_trace = self._trace_request(params, translated)
        self._attach_prompt_cache_key(params, translated)
        sent, cache_candidate = self._apply_cache_breakpoints(translated)
        self._remember_cache_candidate(cache_candidate)
        # Same outgoing-boundary advance as the streaming path: the comparison chain moves with the request.
        diagnosis = self._cache_diagnosis(current_trace)
        if self._opencode_host():
            payload = self._opencode_payload(sent, params, stream=False)
            response = self._namespace(await self._opencode_completion(payload))
        else:
            response = await litellm.acompletion(
                messages=sent,
                **params,
            )
        # The byte verdict was made before the call; the response's cache figure corrects it.
        reported_usage = self._usage_metadata(getattr(response, "usage", None)) or {}
        reconcile(
            diagnosis,
            int((reported_usage.get("input_token_details") or {}).get("cache_read", 0) or 0),
        )
        result = self._response_to_result(response)
        if result.generations:
            result.generations[0].message.additional_kwargs["cache_trace"] = diagnosis
        return result

    def _generate(
        self,
        messages: Sequence[BaseMessage],
        stop: Optional[list[str]] = None,
        run_manager=None,
        **kwargs: Any,
    ) -> ChatResult:
        params = self._completion_kwargs(stop=stop, **kwargs)
        translated = self._messages_to_dicts(messages)
        current_trace = self._trace_request(params, translated)
        self._attach_prompt_cache_key(params, translated)
        sent, cache_candidate = self._apply_cache_breakpoints(translated)
        self._remember_cache_candidate(cache_candidate)
        diagnosis = self._cache_diagnosis(current_trace)
        if self._opencode_host():
            payload = self._opencode_payload(sent, params, stream=False)
            response = self._namespace(self._opencode_completion_sync(payload))
        else:
            response = litellm.completion(messages=sent, **params)
        reported_usage = self._usage_metadata(getattr(response, "usage", None)) or {}
        reconcile(
            diagnosis,
            int((reported_usage.get("input_token_details") or {}).get("cache_read", 0) or 0),
        )
        result = self._response_to_result(response)
        if result.generations:
            result.generations[0].message.additional_kwargs["cache_trace"] = diagnosis
        return result

    def _response_to_result(self, response: Any) -> ChatResult:
        import json as _json

        choices = getattr(response, "choices", None) or []
        if not choices:
            return ChatResult(generations=[])
        message_obj = getattr(choices[0], "message", None)
        content = getattr(message_obj, "content", None) or ""
        reasoning = getattr(message_obj, "reasoning_content", None)
        tool_calls: list[dict[str, Any]] = []
        for call in getattr(message_obj, "tool_calls", None) or []:
            function = getattr(call, "function", None)
            raw_arguments = getattr(function, "arguments", None) if function else None
            try:
                parsed_arguments = _json.loads(raw_arguments) if raw_arguments else {}
            except (TypeError, ValueError):
                parsed_arguments = raw_arguments
            tool_calls.append(
                {
                    "name": getattr(function, "name", None) if function else None,
                    "args": parsed_arguments,
                    "id": getattr(call, "id", None),
                }
            )
        message = AIMessage(
            content_blocks=ChatLiteLLMModel._standard_content_blocks(content, reasoning),
            # An empty list rather than `None`, because a default applies to an omitted key and an explicit `None` fails validation.
            tool_calls=list(tool_calls or []),
            usage_metadata=ChatLiteLLMModel._usage_metadata(getattr(response, "usage", None)),
            additional_kwargs=self._reasoning_to_carry(message_obj),
        )
        return ChatResult(generations=[ChatGeneration(message=message)])
