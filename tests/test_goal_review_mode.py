"""The self-managed goal mode: an open goal re-prompts the agent, never a reviewer."""

from langmesh.base.configuration import Configuration
from langmesh.runtime.features.plugins.goal_review import GoalReviewFeature
from langmesh.runtime.features.plugins.goal_review.models import GoalReview


def test_goal_review_mode_defaults_to_review():
    configuration = Configuration()
    assert configuration.goal_review.mode == "review"


def test_goal_review_mode_accepts_self_managed():
    configuration = Configuration.model_validate({"goal_review": {"mode": "self_managed"}})
    assert configuration.goal_review.mode == "self_managed"


def test_goal_review_mode_rejects_unknown():
    from pydantic import ValidationError

    try:
        Configuration.model_validate({"goal_review": {"mode": "sideways"}})
    except ValidationError:
        return
    raise AssertionError("an unknown goal_review.mode must be rejected")


def test_feature_reads_the_mode_from_configuration():
    configuration = Configuration.model_validate({"goal_review": {"mode": "self_managed"}})
    context = type("Context", (), {})()
    context.global_configuration = configuration
    context.prompts = lambda _namespace: None
    feature = GoalReviewFeature()
    feature.attach(context, None)
    assert feature.review_mode == "self_managed"
