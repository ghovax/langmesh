"""Configuration owned by the goal-review plugin."""

from typing import ClassVar, Literal

from pydantic import Field

from langmesh.base.configuration.configuration import Section


class GoalReviewConfiguration(Section):
    """How a goal's terminal mark is settled by this plugin."""

    REVIEWER: ClassVar[str] = "reviewer"
    AGENT: ClassVar[str] = "agent"
    settlement: Literal["reviewer", "agent"] = "reviewer"
    review_attempts: int = Field(default=3, ge=1)
    review_timeout_seconds: float = Field(default=180.0, gt=0)


__all__ = ["GoalReviewConfiguration"]
