"""The titling plugin: the automatic label a session is listed under.

Given the session's first message, the model is asked, in the same shape every structured
extraction uses, to name the conversation. The host publishes the result; the prompt and the
schema of the naming call live beside this module so both are configurable.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from langmesh.base.primitives.tuning import Tunable, active_tuning
from langmesh.runtime.features import Feature, PluginContext, PluginHost
from langmesh.runtime.verdict import collect_structured_call


class SessionTitle(BaseModel):
    """The one field the model fills to name a session."""

    title: str = Field(description="The label the session is listed under.")


class TitleAssignment(Feature):
    """Give a session its automatic title from its first message."""

    def __init__(self) -> None:
        pass

    def attach(self, context: PluginContext, host: PluginHost | None = None) -> None:
        self._context = context
        self._prompts = context.prompts("titling")

    async def assign_title(self, first_message: str) -> str | None:
        """Ask the model to name the session from its first message, or ``None`` if it never does."""
        from langchain_core.messages import HumanMessage, SystemMessage

        from langmesh.runtime.internals import model_is_authorized
        from langmesh.runtime.runtime import build_chat_model

        agent_configuration = self._context.agent_configuration
        model_identifier = agent_configuration.model_identifier
        if not model_identifier or not model_is_authorized(
            model_identifier, self._context.global_configuration
        ):
            return None
        titling_configuration = agent_configuration.model_copy(update={"reasoning_effort": "low"})
        model = build_chat_model(
            model_identifier,
            self._context.global_configuration,
            titling_configuration,
            self._context.working_directory,
            session_id=self._context.session_id,
        ).bind_tools([SessionTitle], tool_choice="auto")
        request = [
            SystemMessage(content=self._prompts.load("session_title", {})),
            HumanMessage(content=first_message),
        ]
        # The tool is offered and the prompt insists on it: forcing it, a thinking model behind a gateway refuses.
        attempts = active_tuning().amount(Tunable.session_title_attempts)
        validated = await collect_structured_call(
            model,
            request,
            tool_name="SessionTitle",
            schema=SessionTitle,
            attempts=attempts,
            cache_lane_name="session-title",
            reason=f"naming session {self._context.session_id}",
            accept=lambda value: bool(value.title.strip()),
        )
        if validated is None:
            return None
        return validated.title.strip()


__all__ = ["SessionTitle", "TitleAssignment"]
