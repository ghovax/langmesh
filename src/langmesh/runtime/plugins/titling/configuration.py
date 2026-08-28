"""Configuration owned by the titling plugin."""

from pydantic import Field

from langmesh.base.configuration.configuration import Section


class TitlingConfiguration(Section):
    """The retry budget for naming a session."""

    attempts: int = Field(default=4, ge=1)


__all__ = ["TitlingConfiguration"]
