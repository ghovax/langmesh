"""The session's goal: one contract for completion, durable across turns."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Any, ClassVar

from pydantic import BaseModel, Field, StringConstraints, model_validator


NonBlankText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class GoalReviewPhase(StrEnum):
    """The truthful phase between a working turn ending and its review message landing."""

    WAITING_FOR_BACKGROUND = "waiting_for_background"
    CHECKING = "checking"


class Goal(BaseModel):
    """The session's single contract for completion. The agent states it; the review decides where it stands."""

    #: The end state, in the agent's own words.
    text: NonBlankText
    #: What that end state is for, which is what lets a closed route be told apart from a lost goal.
    purpose: NonBlankText | None = None
    #: The minimum conditions that must hold for the goal to be met, each one checkable.
    requirements: list[NonBlankText] = Field(default_factory=list)
    status: str = "active"
    #: What is in the way, written by the review when it accepts that nothing here can pass it.
    blocker: NonBlankText | None = None
    #: What proved each requirement, written by the review when it accepts the goal as met.
    evidence: NonBlankText | None = None
    #: The review's exact message to the session, which opens the turn that follows it.
    review_message: NonBlankText | None = None
    review_id: NonBlankText | None = None
    #: How many turns have been opened since a person last spoke, and deliberately not shown to the model.
    continuations: int = 0
    #: The non-active status the agent just marked for itself, awaiting the secondary review
    #: that either confirms it or reverts it. Empty once the review has decided.
    pending_review: str | None = None

    #: Being worked, so the session keeps going on its own.
    ACTIVE: ClassVar[str] = "active"
    #: The review found an impasse the session cannot pass without the person. Nothing further is opened.
    BLOCKED: ClassVar[str] = "blocked"
    #: Set when the goal used its whole allowance, which is distinct from anyone judging it stuck.
    PARKED: ClassVar[str] = "parked"
    #: Reached, and kept rather than dropped so the person can see what was reached and take it up again.
    SATISFIED: ClassVar[str] = "satisfied"
    #: No longer what the person wants, kept for the same reason. Only a person sets this.
    CLEARED: ClassVar[str] = "cleared"

    @model_validator(mode="after")
    def _link_review_message_to_its_transcript(self):
        if (self.review_message is None) != (self.review_id is None):
            raise ValueError(
                "A review continuation and its transcript id must be present together."
            )
        return self

    @property
    def is_open(self) -> bool:
        """Whether this goal is still being worked, as opposed to waiting on a person."""
        return self.status == self.ACTIVE

    @property
    def is_parked(self) -> bool:
        """Whether this goal is set aside waiting for its person, which is what a resume lifts."""
        return self.status == self.PARKED

    def updated(self, **changes: Any) -> Goal:
        """Make a validated replacement so linked goal fields cannot diverge."""
        return type(self).model_validate({**self.model_dump(), **changes})

    def for_model(self) -> dict:
        """What the agent is shown: the goal itself, never the bookkeeping around it."""
        picture: dict[str, Any] = {"goal": self.text}
        if self.purpose:
            picture["purpose"] = self.purpose
        if self.requirements:
            picture["requirements"] = list(self.requirements)
        if self.status != self.ACTIVE:
            picture["status"] = self.status
        if self.blocker:
            picture["blocker"] = self.blocker
        return picture

    def public(self) -> dict:
        """What the interface shows: the goal, its purpose, its minimum conditions, and its standing."""
        return {
            "text": self.text,
            "purpose": self.purpose,
            "requirements": list(self.requirements),
            "status": self.status,
            "blocker": self.blocker,
            "evidence": self.evidence,
            "pending_review": self.pending_review,
        }
