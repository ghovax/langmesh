"""Mailbox sessions overlay YAML provider/model onto the agent profile.

The profile still owns tools and the prompt. ``email.provider`` and ``email.model``
pick which catalogue provider the turn calls, so any ``providers.<id>`` block and
its secret file work the same way they do for a desktop session.
"""

from __future__ import annotations

from langmesh.base.configuration.configuration import AgentConfiguration
from langmeshd.commons.configuration import EmailConfiguration


def apply_email_model(
    agent: AgentConfiguration, email: EmailConfiguration
) -> AgentConfiguration:
    """Return ``agent`` with the mailbox YAML provider/model overlaid when both are set."""
    provider = email.effective_provider
    model = email.effective_model
    if not provider and not model:
        return agent
    if not provider or not model:
        raise ValueError(
            "email.provider and email.model must be set together "
            "(omit both to keep the agent profile's provider and model)."
        )
    return agent.model_copy(update={"provider": provider, "model": model})


def overlay_mailbox_agent(agent: AgentConfiguration) -> AgentConfiguration:
    """Apply the on-disk email section's provider/model overlay, if any."""
    from langmeshd.commons.configuration_file import load as load_document

    email = EmailConfiguration.model_validate((load_document() or {}).get("email") or {})
    return apply_email_model(agent, email)


__all__ = ["apply_email_model", "overlay_mailbox_agent"]
