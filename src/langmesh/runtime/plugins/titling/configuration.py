"""Configuration owned by the titling plugin."""

from pydantic import Field

from langmesh.base.configuration.configuration import Section


class TitlingConfiguration(Section):
    """The attempt and time budget for naming a session."""

    attempts: int = Field(default=4, ge=1)
    timeout_seconds: float = Field(default=30.0, gt=0)


__all__ = ["TitlingConfiguration"]
