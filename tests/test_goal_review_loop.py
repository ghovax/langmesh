"""The goal-review loop must end when a review lands no verdict.

A failed or cancelled review leaves the goal undecided. If the goal stays open, the next
completed turn re-arms another review, so a session whose goal cannot be decided burns
tokens forever (seen in the wild as a 12-deep chain of peer sessions). The contract under
test: a review that lands nothing must park the goal, and a parked goal must not continue.
"""

from types import SimpleNamespace

from langmesh.runtime.features.plugins.goal_review import Goal, GoalReviewFeature
from langmesh.runtime.features.plugins.continuation.policy import TuningContinuationPolicy


def _feature() -> GoalReviewFeature:
    host = SimpleNamespace(bookkeeping=SimpleNamespace(note_state_changed=lambda: None))
    feature = GoalReviewFeature()
    feature.attach(
        SimpleNamespace(prompts=lambda _namespace: SimpleNamespace(load=lambda *a, **k: "")),
        host,
    )
    return feature


def test_a_review_that_lands_nothing_parks_the_goal():
    feature = _feature()
    feature.write(Goal(text="x", purpose="y", requirements=["z"], continuations=0))
    # The worker's contract on `verdict is None` is to park; this is the feature's park.
    feature.park()
    parked = feature.goal
    assert parked is not None
    assert parked.status == Goal.PARKED
    assert not parked.is_open


def test_a_parked_goal_does_not_continue():
    policy = TuningContinuationPolicy()
    parked = Goal(text="x", purpose="y", requirements=["z"], status=Goal.PARKED, continuations=0)
    assert not policy.continue_goal(parked, 0)
    # And even a fresh open goal respects the allowance: continuation is bounded.
    open_goal = Goal(text="x", purpose="y", requirements=["z"], continuations=0)
    assert policy.continue_goal(open_goal, 0)


def test_parking_clears_the_review_message_so_no_continuation_turn_opens():
    feature = _feature()
    feature.write(
        Goal(
            text="x",
            purpose="y",
            requirements=["z"],
            continuations=0,
            review_message="keep going",
            review_id="review-1",
        )
    )
    feature.park()
    parked = feature.goal
    assert parked is not None
    assert parked.review_message is None
    assert parked.review_id is None
