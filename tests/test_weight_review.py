from datetime import date, timedelta

import pytest

from mcp_nutrition_db.body import BodyRepository
from mcp_nutrition_db.models import GoalInput, NutritionValues
from mcp_nutrition_db.repository import NutritionRepository
from mcp_nutrition_db.weight_review import ReviewProposal, WeightReviewRepository


def setup_review(repository):
    repository.set_goals(
        GoalInput(
            effective_from=date(2026, 8, 1),
            base_burn_kcal=2100,
            deficit_kcal=250,
            targets=NutritionValues(protein_g=110, fat_g=70),
            reason="initial explicit goals",
        )
    )
    return WeightReviewRepository(repository)


def proposal(review, **overrides):
    return review.propose(
        ReviewProposal.model_validate(
            {
                "outcome": "change",
                "base_burn_kcal": 2200,
                "deficit_kcal": 250,
                "measurement_start": "2026-08-01",
                "measurement_end": "2026-08-27",
                "rationale": "User-reviewed synthetic trend and uncertainty",
                **overrides,
            }
        )
    )


def goals(repository):
    with repository.database.connection() as db:
        return (
            [tuple(r) for r in db.execute("SELECT * FROM daily_goals")],
            [tuple(r) for r in db.execute("SELECT * FROM goal_revisions")],
        )


def test_approval_identity_atomicity_macros_and_retry(repository):
    review = setup_review(repository)
    before = goals(repository)
    p = proposal(review)
    assert goals(repository) == before
    assert p["effective_from"] == "2026-08-28"
    assert p["ordinary_target_kcal"] == 1950
    assert p["proposed_goals"]["targets"]["protein_g"] == 110
    with pytest.raises(ValueError, match="approval"):
        review.complete(p["proposal_id"], user_approved=False)
    assert goals(repository) == before
    result = review.complete(p["proposal_id"], user_approved=True)
    assert result["goal"]["base_burn_kcal"] == 2200
    assert result["goal"]["deficit_kcal"] == 250
    assert result["goal"]["targets"]["fat_g"] == 70
    after = goals(repository)
    assert review.complete(p["proposal_id"], user_approved=True) == result
    assert goals(repository) == after
    assert repository.get_goals(on_date=date(2026, 8, 27))["current"]["base_burn_kcal"] == 2100
    assert repository.get_goals(on_date=date(2026, 8, 28))["current"]["base_burn_kcal"] == 2200


def test_stale_and_backdated_proposals(repository):
    review = setup_review(repository)
    p = proposal(review, effective_from="2026-08-20")
    with pytest.raises(ValueError, match="backdated"):
        review.complete(p["proposal_id"], user_approved=True)
    review.complete(p["proposal_id"], user_approved=True, approved_backdate=date(2026, 8, 20))
    p = proposal(review)
    repository.set_goals(
        GoalInput(
            effective_from=date(2026, 8, 29),
            base_burn_kcal=2300,
            deficit_kcal=200,
            reason="unrelated explicit request",
        )
    )
    before = goals(repository)
    with pytest.raises(ValueError, match="stale"):
        review.complete(p["proposal_id"], user_approved=True)
    assert goals(repository) == before


def test_keep_reminders_snooze_and_restart(repository, clock):
    review = setup_review(repository)
    before = goals(repository)
    state = review.update_reminder("enable", 0)
    assert state["next_due"] == "2026-09-26" and not state["hint_eligible"]
    clock.value += timedelta(days=30)
    assert review.status()["hint_eligible"]
    review.update_reminder("acknowledge", state["revision"])
    assert not review.status()["hint_eligible"]
    review = WeightReviewRepository(NutritionRepository(repository.database.path, clock=clock))
    assert not review.status()["hint_eligible"]
    state = review.update_reminder("snooze", review.status()["revision"])
    clock.value += timedelta(days=7)
    assert review.status()["hint_eligible"]
    state = review.update_reminder("disable", state["revision"])
    assert not state["hint_eligible"]
    p = review.propose(
        ReviewProposal(
            outcome="keep",
            rationale="Keep explicit goals",
            measurement_start=date(2026, 8, 1),
            measurement_end=date(2026, 9, 26),
        )
    )
    assert review.status()["next_due"] == "2026-09-26"
    review.complete(p["proposal_id"], user_approved=True)
    assert review.status()["next_due"] == "2026-11-02"
    assert not review.status()["enabled"]
    BodyRepository(repository.database).latest()
    assert goals(repository) == before


def test_partial_macro_proposal_preserves_others(repository):
    p = proposal(setup_review(repository), targets={"protein_g": 120})
    assert p["proposed_goals"]["targets"]["protein_g"] == 120
    assert p["proposed_goals"]["targets"]["fat_g"] == 70
