"""Configuration owned by the permission-review plugin."""

from pydantic import Field

from langmesh.base.configuration.configuration import Section


class PermissionReviewConfiguration(Section):
    """The attempt and time budget for an automatic permission decision."""

    attempts: int = Field(default=4, ge=1)
    timeout_seconds: float = Field(default=30.0, gt=0)


__all__ = ["PermissionReviewConfiguration"]
