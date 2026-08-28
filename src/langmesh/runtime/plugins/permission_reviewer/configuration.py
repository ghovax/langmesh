"""Configuration owned by the permission-review plugin."""

from pydantic import Field

from langmesh.base.configuration.configuration import Section


class PermissionReviewConfiguration(Section):
    """The retry budget for an automatic permission decision."""

    attempts: int = Field(default=4, ge=1)


__all__ = ["PermissionReviewConfiguration"]
