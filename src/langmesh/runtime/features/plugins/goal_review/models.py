"""The goal-review plugin's verdict model: the schema of one reading of an open goal.

Kept as its own module so the tool that receives the verdict and the feature that runs the
review both import it at the top, with no import cycle between them.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, PrivateAttr, model_validator

from langmesh.base.configuration import PromptLoader
from langmesh.runtime.features.plugins.goal_review.goal import NonBlankText

#: The goal-review plugin's schema descriptions, configurable beside this plugin.
_DESCRIPTIONS = PromptLoader(Path(__file__).parent / "prompts")

#: Where a goal stands after one reading of the work, which is not the same as what the session says about it.
GOAL_STANDING = Literal["unmet", "satisfied", "blocked"]
GOAL_CONTRACT = Literal["complete", "needs_revision"]


class GoalReview(BaseModel):
    """One reading of an open goal: where it stands, and what the session is told to do about it."""

    # Evidence precedes the verdict so the decision follows from the review instead of leading it.
    assessment: NonBlankText = Field(
        description=_DESCRIPTIONS.load("goal_review_assessment", {}).strip()
    )
    unmet: list[NonBlankText] = Field(
        default_factory=list,
        description=_DESCRIPTIONS.load("goal_review_unmet", {}).strip(),
    )
    evidence: NonBlankText | None = Field(
        default=None,
        description=_DESCRIPTIONS.load("goal_review_evidence", {}).strip(),
    )
    blocker: NonBlankText | None = Field(
        default=None,
        description=_DESCRIPTIONS.load("goal_review_blocker", {}).strip(),
    )
    goal_contract: GOAL_CONTRACT = Field(
        description=_DESCRIPTIONS.load("goal_review_goal_contract", {}).strip()
    )
    standing: GOAL_STANDING = Field(
        description=_DESCRIPTIONS.load("goal_review_standing", {}).strip()
    )
    message: NonBlankText | None = Field(
        default=None,
        description=_DESCRIPTIONS.load("goal_review_message", {}).strip(),
    )
    _review_id: str = PrivateAttr("")

    @model_validator(mode="after")
    def _carry_what_the_verdict_rests_on(self):
        """A verdict without what establishes it is not a verdict, so the pass is retried rather than believed."""
        if self.standing == "satisfied":
            if self.unmet:
                raise ValueError("A satisfied goal has nothing unmet.")
            if self.goal_contract != "complete":
                raise ValueError("A satisfied goal needs a complete contract.")
            if self.blocker is not None:
                raise ValueError("A satisfied goal has no blocker.")
            if self.message is not None:
                raise ValueError("A satisfied goal opens no continuation message.")
            if self.evidence is None:
                raise ValueError(
                    "A satisfied goal needs the evidence that proves each requirement."
                )
            return self
        if self.evidence is not None:
            raise ValueError("Only a satisfied goal carries completion evidence.")
        if not self.unmet and self.goal_contract == "complete":
            raise ValueError(
                "A goal that is not satisfied has an unmet requirement or needs a stronger contract."
            )
        if self.goal_contract == "needs_revision" and self.standing != "unmet":
            raise ValueError("A goal the session can revise is unmet, not satisfied or blocked.")
        if self.standing == "blocked":
            if self.blocker is None:
                raise ValueError("A blocked goal needs what is in the way and what would clear it.")
            if self.message is not None:
                raise ValueError("A blocked goal opens no continuation message.")
            return self
        if self.blocker is not None:
            raise ValueError("Only a blocked goal carries a blocker.")
        if self.message is None:
            raise ValueError("An unmet goal needs the message that opens its next turn.")
        return self


__all__ = ["GOAL_CONTRACT", "GOAL_STANDING", "GoalReview"]
