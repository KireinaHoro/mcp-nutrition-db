import copy
from datetime import date, timedelta

import pytest

from mcp_nutrition_db.body import BodyRepository
from mcp_nutrition_db.garmin import GarminAdapter, normalize
from mcp_nutrition_db.garmin_import import GarminImporter
from mcp_nutrition_db.garmin_reconcile import apply, prepare
from mcp_nutrition_db.models import GoalInput, LogTrainingInput, TrainingChanges

VALIDATION = {
    "login_validated": True,
    "session_restart_validated": True,
    "weight_grams_epoch_ms": True,
    "activity_ids_utc_seconds": True,
    "total_minus_resting": True,
}
ACTIVITY = {
    "activityId": 101,
    "activityName": "Synthetic ride",
    "startTimeGMT": "2026-08-26 18:00:00",
    "movingDuration": 3600,
    "duration": 3700,
    "calories": 700,
    "bmrCalories": 100,
    "activityType": {"typeKey": "cycling"},
    "averagePower": 200,
    "deviceId": 42,
}
WEIGHT = {"samplePk": 201, "date": 1787745600000, "weight": 75000, "sourceType": "INDEX_S2"}


class Provider:
    account_id = "123"

    def __init__(self):
        self.activity_records = [copy.deepcopy(ACTIVITY)]
        self.weight_records = [copy.deepcopy(WEIGHT)]

    def weights(self, start, end):
        return self.weight_records

    def activities(self, start, end):
        return self.activity_records


@pytest.fixture
def importer(repository):
    importer = GarminImporter(repository, Provider())
    importer.register()
    importer.validate(VALIDATION)
    return importer


def sync(importer, **kwargs):
    return importer.sync(start=date(2026, 8, 25), end=date(2026, 8, 27), **kwargs)


def tables(repository, names):
    with repository.database.connection() as db:
        return {name: [tuple(r) for r in db.execute(f"SELECT * FROM {name}")] for name in names}


def test_weight_idempotency_corrections_multiple_readings_no_goal_mutations(importer, repository):
    repository.set_goals(
        GoalInput(
            effective_from=date(2026, 8, 1),
            base_burn_kcal=2100,
            deficit_kcal=250,
            reason="explicit goals",
        )
    )
    before = tables(repository, ["daily_goals", "goal_revisions"])
    sync(importer)
    sync(importer)
    body = BodyRepository(repository.database)
    assert body.latest()["measurement"]["weight_kg"] == 75
    importer.provider.weight_records[0]["weight"] = 74900
    importer.provider.weight_records.append(
        {**WEIGHT, "samplePk": 202, "date": WEIGHT["date"] + 1000}
    )
    sync(importer)
    result = body.list_measurements(limit=1)
    assert result["measurements"][0]["weight_kg"] == 75
    page2 = body.list_measurements(limit=1, cursor=result["next_cursor"])
    assert page2["measurements"][0]["weight_kg"] == 74.9
    assert (
        len(tables(repository, ["body_measurement_revisions"])["body_measurement_revisions"]) == 1
    )
    assert tables(repository, before) == before
    assert not tables(repository, ["trainings"])["trainings"]


def test_dry_run_and_unverified_calories(importer, repository):
    before = tables(repository, ["import_staging", "sync_coverage", "body_measurements"])
    sync(importer, dry_run=True)
    assert tables(repository, before) == before
    raw = copy.deepcopy(ACTIVITY)
    raw.pop("bmrCalories")
    assert (
        "active_calories_unverified_or_nonpositive"
        in normalize("activity", raw, VALIDATION)["issues"]
    )
    raw["bmrCalories"] = 100
    raw["activeCalories"] = 700
    assert "contradictory_calories" in normalize("activity", raw, VALIDATION)["issues"]
    facts = normalize("activity", ACTIVITY, VALIDATION)
    assert facts["confidence"] == "medium"
    assert facts["active_mkcal"] == 600000
    facts = normalize("activity", ACTIVITY, {**VALIDATION, "power_meter_device_ids": ["42"]})
    assert facts["confidence"] == "high"
    raw = {**ACTIVITY, "movingDuration": 0, "duration": 30}
    assert normalize("activity", raw, VALIDATION)["duration_ms"] == 30000


def approve_all(importer, tmp_path):
    report = importer.report()
    for item in report["activities"]:
        item["action"] = "add"
    for identity in report["local_decisions"]:
        report["local_decisions"][identity] = "preserve"
    plan = prepare(importer, report)
    return apply(importer, plan["plan_id"], approved=True, backup=tmp_path / "backup.sqlite3")


def test_reconcile_refresh_overrides_and_deletion(importer, repository, tmp_path):
    sync(importer)
    result = approve_all(importer, tmp_path)
    training_id = result["changes"][0]["training_id"]
    assert result["changes"][0]["reported_burn_kcal"] == 600
    assert result["changes"][0]["credited_burn_kcal"] == 480
    sync(importer)
    assert repository.get_training(training_id)["revision"] == 1
    current = repository.update_training(
        training_id, 1, "my title", TrainingChanges(activity="Local title")
    )
    importer.provider.activity_records[0]["calories"] = 710
    sync(importer)
    current = repository.get_training(training_id)
    assert current["reported_burn_kcal"] == 610 and current["activity"] == "Local title"
    current = repository.update_training(
        training_id,
        current["revision"],
        "local correction",
        TrainingChanges(reported_burn_kcal=580),
    )
    importer.provider.activity_records[0]["calories"] = 720
    sync(importer)
    assert repository.get_training(training_id)["reported_burn_kcal"] == 580
    assert (
        BodyRepository(repository.database).sync_status()["pending"][0]["status"]
        == "local_override_conflict"
    )
    repository.delete_training(training_id, current["revision"], "exclude this imported workout")
    sync(importer)
    with repository.database.connection() as db:
        assert db.execute("SELECT suppressed FROM external_records").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM trainings").fetchone()[0] == 1


def test_every_manual_record_and_revision_checked(importer, repository, tmp_path):
    local = repository.create_training(
        LogTrainingInput.model_validate(
            {
                "occurred_at": "2026-08-26T08:00:00+02:00",
                "activity": "Morning ride?",
                "duration_minutes": 50,
                "reported_burn_kcal": 550,
                "confidence": "high",
                "measurement_method": "power_meter",
                "source": {"type": "user_provided"},
                "notes": "Keep this note",
            }
        )
    )
    sync(importer)
    report = importer.report()
    assert report["activities"][0]["candidates"][0]["training_id"] == local["training_id"]
    report["activities"][0].update(action="link", training_id=local["training_id"])
    report["local_decisions"][local["training_id"]] = "link"
    plan = prepare(importer, report)
    repository.update_training(
        local["training_id"], 1, "new note", TrainingChanges(notes="New note")
    )
    with pytest.raises(ValueError, match="stale"):
        apply(importer, plan["plan_id"], approved=True, backup=tmp_path / "backup.sqlite3")
    assert repository.get_training(local["training_id"])["reported_burn_kcal"] == 550


def test_pagination_failure_and_disconnect_hint(importer, repository, clock):
    class Broken:
        garmin_connect_activities = "/activities"

        def connectapi(self, *args, **kwargs):
            return [ACTIVITY]

        def get_activity(self, identity):
            return ACTIVITY

    with pytest.raises(ValueError, match="repeated"):
        GarminAdapter(Broken(), "123").activities(date(2026, 8, 1), date(2026, 8, 31))
    sync(importer)
    clock.value += timedelta(hours=3)
    assert (
        BodyRepository(repository.database).sync_status()["connection_hint"]["state"]
        == "sync_stale"
    )
    with repository.database.connection(write=True) as db:
        db.execute("UPDATE garmin_accounts SET auth_state='reauth_required'")
    assert repository.get_goals()["garmin_connection"]["state"] == "reauth_required"


def test_timezone_dst_and_multisport():
    facts = normalize("activity", {**ACTIVITY, "startTimeGMT": "2026-10-25 01:30:00"}, VALIDATION)
    assert GarminImporter.owned(facts)["occurred_at"] == "2026-10-25T02:30:00+01:00"
    facts = normalize("activity", {**ACTIVITY, "parentId": 999}, VALIDATION)
    assert facts["issues"] == ["multisport_selection_required"]


def test_activation_requires_full_history_and_imports_only_after_cutoff(
    importer, repository, clock, tmp_path
):
    sync(importer)
    approve_all(importer, tmp_path)
    with pytest.raises(ValueError, match="coverage"):
        importer.activate()
    importer.sync()
    # A second full report explicitly retains the existing identity.
    report = importer.report()
    training_id = report["snapshot"]["trainings"][0]["training_id"]
    report["activities"][0].update(action="link", training_id=training_id)
    report["local_decisions"][training_id] = "link"
    plan = prepare(importer, report)
    apply(importer, plan["plan_id"], approved=True, backup=tmp_path / "full-backup.sqlite3")
    activation = importer.activate()
    assert activation["activated_at"]
    assert repository.conversation_status("Europe/Zurich")["weight_budget_review"]["enabled"]
    clock.value += timedelta(days=1)
    importer.provider.activity_records.append(
        {**ACTIVITY, "activityId": 102, "startTimeGMT": "2026-08-28 08:00:00"}
    )
    importer.sync(start=date(2026, 8, 28), end=date(2026, 8, 28))
    importer.sync(start=date(2026, 8, 28), end=date(2026, 8, 28))
    assert len(tables(repository, ["trainings"])["trainings"]) == 2


def test_failed_stream_does_not_advance_coverage(importer, repository):
    def fail(start, end):
        raise RuntimeError("synthetic interrupted page")

    importer.provider.activities = fail
    with pytest.raises(RuntimeError):
        sync(importer)
    with repository.database.connection() as db:
        assert (
            db.execute("SELECT COUNT(*) FROM sync_coverage WHERE stream='activity'").fetchone()[0]
            == 0
        )
        assert (
            db.execute("SELECT COUNT(*) FROM sync_coverage WHERE stream='weight'").fetchone()[0]
            == 1
        )
        assert db.execute("SELECT COUNT(*) FROM trainings").fetchone()[0] == 0


def test_weight_exclusion_is_audited_and_never_changes_goals(importer, repository, tmp_path):
    repository.set_goals(
        GoalInput(
            effective_from=date(2026, 8, 1),
            base_burn_kcal=2100,
            deficit_kcal=250,
            reason="explicit goals",
        )
    )
    before = tables(repository, ["daily_goals", "goal_revisions"])
    sync(importer)
    report = importer.report()
    report["activities"][0]["action"] = "exclude"
    report["weight_decisions"]["201"] = "exclude"
    plan = prepare(importer, report)
    apply(importer, plan["plan_id"], approved=True, backup=tmp_path / "backup.sqlite3")
    sync(importer)
    assert BodyRepository(repository.database).latest()["measurement"] is None
    assert (
        len(tables(repository, ["body_measurement_revisions"])["body_measurement_revisions"]) == 1
    )
    assert tables(repository, before) == before


def test_mapped_identity_cannot_be_added_twice(importer, tmp_path):
    sync(importer)
    approve_all(importer, tmp_path)
    report = importer.report()
    report["activities"][0]["action"] = "add"
    for identity in report["local_decisions"]:
        report["local_decisions"][identity] = "preserve"
    with pytest.raises(ValueError, match="retain"):
        prepare(importer, report)


def test_remote_change_invalidates_approved_plan_before_local_write(importer, repository, tmp_path):
    sync(importer)
    report = importer.report()
    report["activities"][0]["action"] = "add"
    plan = prepare(importer, report)
    importer.provider.activity_records[0]["calories"] += 10
    before = tables(repository, ["trainings", "external_records"])
    with pytest.raises(ValueError, match="remote source"):
        apply(importer, plan["plan_id"], approved=True, backup=tmp_path / "backup.sqlite3")
    assert tables(repository, before) == before


def test_reconciliation_does_not_fetch_unreviewed_older_activity_history(
    importer, repository, tmp_path
):
    sync(importer)
    report = importer.report()
    report["activities"][0]["action"] = "add"
    plan = prepare(importer, report)
    importer.provider.activity_records.append(
        {**ACTIVITY, "activityId": 900, "startTimeGMT": "2026-07-01 08:00:00"}
    )
    apply(importer, plan["plan_id"], approved=True, backup=tmp_path / "backup.sqlite3")
    assert len(tables(repository, ["trainings"])["trainings"]) == 1
    ranges = importer.ranges("activity")
    assert min(start for start, end in ranges) == date(2026, 8, 25)
