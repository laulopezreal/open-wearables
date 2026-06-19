"""Tests for the garmin-grafana to Open Wearables wellness import bridge (OW #3/#4/#5)."""

from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy.orm import Session

from app.schemas.enums import HealthScoreCategory, ProviderName
from app.services.providers.garmin.data_247 import Garmin247Data
from app.services.providers.garmin.grafana_wellness_import import (
    build_recovery_health_score,
    build_recovery_scores,
    daily_stats_row_to_garmin_daily,
    import_wellness_rows,
    parse_influx_time,
    sleep_summary_row_to_garmin_sleep,
)
from tests.factories import UserFactory

WELLNESS_IMPORT_PATH = (
    Path(__file__).resolve().parents[3] / "app" / "services" / "providers" / "garmin" / "grafana_wellness_import.py"
)


class FakeGarmin247:
    """Minimal fake exposing only the adapter boundary write method."""

    def __init__(self, saved: int | None = None) -> None:
        self.calls: list[tuple[UUID, str, list[dict[str, Any]]]] = []
        self._saved = saved

    def process_items_batch(
        self,
        db: object,
        user_id: UUID,
        summary_type: str,
        items: list[dict[str, Any]],
    ) -> int:
        self.calls.append((user_id, summary_type, items))
        return self._saved if self._saved is not None else len(items)


def _daily_row(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "time": "2024-01-15T00:00:00+00:00",
        "totalSteps": 8432,
        "totalDistanceMeters": 6240,
        "activeKilocalories": 342,
        "restingHeartRate": 52,
        "minHeartRate": 44,
        "maxHeartRate": 168,
        "floorsAscended": 12.0,
        "moderateIntensityMinutes": 30,
        "vigorousIntensityMinutes": 10,
    }
    row.update(overrides)
    return row


def _sleep_row(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "time": "2024-01-15T23:30:00+00:00",
        "sleepTimeSeconds": 27000,  # 450 minutes
        "deepSleepSeconds": 5400,
        "lightSleepSeconds": 16200,
        "remSleepSeconds": 5400,
        "awakeSleepSeconds": 600,
        "sleepScore": 82,
        "restingHeartRate": 50,
        "avgOvernightHrv": 45.2,
        "averageRespirationValue": 14.5,
        "averageSpO2Value": 96.0,
    }
    row.update(overrides)
    return row


# ---------------------------------------------------------------------------
# parse helper
# ---------------------------------------------------------------------------


def test_parse_influx_time_converts_offset_to_utc() -> None:
    parsed = parse_influx_time("2024-01-15T10:30:00+02:00")
    assert parsed is not None
    assert parsed.tzinfo == timezone.utc
    assert parsed == datetime(2024, 1, 15, 8, 30, tzinfo=timezone.utc)


def test_parse_influx_time_returns_none_for_garbage() -> None:
    assert parse_influx_time("not-a-date") is None
    assert parse_influx_time(None) is None


# ---------------------------------------------------------------------------
# DailyStats transform
# ---------------------------------------------------------------------------


def test_daily_stats_row_maps_steps_and_resting_hr() -> None:
    daily = daily_stats_row_to_garmin_daily(_daily_row())
    assert daily is not None
    assert daily["calendarDate"] == "2024-01-15"
    assert daily["steps"] == 8432
    assert daily["restingHeartRateInBeatsPerMinute"] == 52
    assert daily["distanceInMeters"] == 6240
    assert daily["floorsClimbed"] == 12
    assert daily["moderateIntensityDurationInSeconds"] == 30 * 60
    assert daily["vigorousIntensityDurationInSeconds"] == 10 * 60


def test_daily_stats_row_returns_none_without_time() -> None:
    assert daily_stats_row_to_garmin_daily(_daily_row(time=None)) is None


def test_daily_stats_row_tolerates_missing_steps() -> None:
    daily = daily_stats_row_to_garmin_daily(_daily_row(totalSteps=None))
    assert daily is not None
    assert daily["steps"] is None


# ---------------------------------------------------------------------------
# SleepSummary transform
# ---------------------------------------------------------------------------


def test_sleep_summary_row_maps_duration_and_stages() -> None:
    sleep = sleep_summary_row_to_garmin_sleep(_sleep_row())
    assert sleep is not None
    assert sleep["durationInSeconds"] == 27000
    # Stage seconds are mapped (the summary derives duration_minutes from these).
    assert sleep["deepSleepDurationInSeconds"] == 5400
    assert sleep["lightSleepDurationInSeconds"] == 16200
    assert sleep["remSleepInSeconds"] == 5400
    assert sleep["awakeDurationInSeconds"] == 600
    assert sleep["startTimeInSeconds"] == int(datetime(2024, 1, 15, 23, 30, tzinfo=timezone.utc).timestamp())
    assert sleep["overallSleepScore"] == {"value": 82, "qualifier": None}


def test_sleep_summary_row_falls_back_to_stage_sum_when_total_missing() -> None:
    sleep = sleep_summary_row_to_garmin_sleep(_sleep_row(sleepTimeSeconds=None))
    assert sleep is not None
    assert sleep["durationInSeconds"] == 5400 + 16200 + 5400


def test_sleep_summary_row_returns_none_without_start_time() -> None:
    assert sleep_summary_row_to_garmin_sleep(_sleep_row(time=None)) is None


def test_sleep_summary_row_returns_none_without_any_duration() -> None:
    row = _sleep_row(
        sleepTimeSeconds=None,
        deepSleepSeconds=None,
        lightSleepSeconds=None,
        remSleepSeconds=None,
    )
    assert sleep_summary_row_to_garmin_sleep(row) is None


def test_sleep_summary_row_omits_score_when_absent() -> None:
    sleep = sleep_summary_row_to_garmin_sleep(_sleep_row(sleepScore=None))
    assert sleep is not None
    assert "overallSleepScore" not in sleep


# ---------------------------------------------------------------------------
# RECOVERY health score
# ---------------------------------------------------------------------------


def test_build_recovery_health_score_uses_consumer_component_keys() -> None:
    user_id = uuid4()
    score = build_recovery_health_score(
        user_id,
        "2024-01-15",
        resting_heart_rate=52,
        hrv_milli=45.2,
    )
    assert score is not None
    assert score.category == HealthScoreCategory.RECOVERY
    assert score.provider == ProviderName.GARMIN
    assert score.user_id == user_id
    assert score.components is not None
    # Consumer reads components["resting_heart_rate"].value and
    # components["hrv_rmssd_milli"].value, so the keys must be exactly these.
    assert score.components["resting_heart_rate"].value == 52
    assert score.components["hrv_rmssd_milli"].value == 45.2
    # Deterministic recorded_at (noon UTC on the calendar date) so a re-run
    # collides on the unique constraint instead of inserting a duplicate.
    assert score.recorded_at == datetime(2024, 1, 15, 12, 0, tzinfo=timezone.utc)
    assert score.recorded_at.date().isoformat() == "2024-01-15"


def test_build_recovery_health_score_returns_none_when_no_metrics() -> None:
    assert build_recovery_health_score(uuid4(), "2024-01-15", resting_heart_rate=None, hrv_milli=None) is None


def test_build_recovery_scores_joins_rhr_and_hrv_per_day() -> None:
    user_id = uuid4()
    scores = build_recovery_scores(
        user_id,
        daily_rows=[_daily_row(restingHeartRate=52)],
        sleep_rows=[_sleep_row(avgOvernightHrv=45.2)],
    )
    assert len(scores) == 1
    components = scores[0].components or {}
    assert components["resting_heart_rate"].value == 52
    assert components["hrv_rmssd_milli"].value == 45.2


def test_build_recovery_scores_falls_back_to_sleep_resting_hr() -> None:
    scores = build_recovery_scores(
        uuid4(),
        daily_rows=[_daily_row(restingHeartRate=None)],
        sleep_rows=[_sleep_row(restingHeartRate=50, avgOvernightHrv=None)],
    )
    assert len(scores) == 1
    assert (scores[0].components or {})["resting_heart_rate"].value == 50


# ---------------------------------------------------------------------------
# Orchestration (fake Garmin247)
# ---------------------------------------------------------------------------


def test_import_wellness_rows_dry_run_does_not_write() -> None:
    fake = FakeGarmin247()
    summary = import_wellness_rows(
        db=object(),
        garmin_247=fake,  # type: ignore[arg-type]
        user_id=uuid4(),
        daily_rows=[_daily_row()],
        sleep_rows=[_sleep_row()],
        dry_run=True,
    )
    assert fake.calls == []
    assert summary.transformed["dailies"] == 1
    assert summary.transformed["sleeps"] == 1
    assert summary.transformed["recovery"] == 1
    assert summary.saved == {"dailies": 0, "sleeps": 0, "recovery": 0}


def test_import_wellness_rows_write_routes_dailies_and_sleeps_through_batch() -> None:
    fake = FakeGarmin247()
    user_id = uuid4()
    # No resting HR / HRV so no RECOVERY score is built: this test isolates the
    # process_items_batch routing, and the fake db (object()) cannot satisfy the
    # real health_score_service.bulk_create write path.
    summary = import_wellness_rows(
        db=object(),
        garmin_247=fake,  # type: ignore[arg-type]
        user_id=user_id,
        daily_rows=[_daily_row(restingHeartRate=None)],
        sleep_rows=[_sleep_row(restingHeartRate=None, avgOvernightHrv=None)],
        dry_run=False,
    )
    summary_types = {summary_type for _, summary_type, _ in fake.calls}
    assert summary_types == {"dailies", "sleeps"}
    for called_user_id, _, _ in fake.calls:
        assert called_user_id == user_id
    assert summary.saved["dailies"] == 1
    assert summary.saved["sleeps"] == 1


def test_import_wellness_rows_separates_invalid_skips() -> None:
    fake = FakeGarmin247()
    summary = import_wellness_rows(
        db=object(),
        garmin_247=fake,  # type: ignore[arg-type]
        user_id=uuid4(),
        daily_rows=[_daily_row(), _daily_row(time=None)],
        sleep_rows=[_sleep_row(), _sleep_row(time=None)],
        dry_run=True,
    )
    assert summary.queried["DailyStats"] == 2
    assert summary.queried["SleepSummary"] == 2
    assert summary.transformed["dailies"] == 1
    assert summary.transformed["sleeps"] == 1
    assert summary.skipped["DailyStats.invalid"] == 1
    assert summary.skipped["SleepSummary.invalid"] == 1


def test_import_wellness_rows_no_batch_call_for_empty_category() -> None:
    fake = FakeGarmin247()
    # restingHeartRate=None so no RECOVERY score is built (fake db cannot back
    # the health_score_service write path); this isolates batch-call routing.
    import_wellness_rows(
        db=object(),
        garmin_247=fake,  # type: ignore[arg-type]
        user_id=uuid4(),
        daily_rows=[_daily_row(restingHeartRate=None)],
        sleep_rows=[],
        dry_run=False,
    )
    assert [summary_type for _, summary_type, _ in fake.calls] == ["dailies"]


# ---------------------------------------------------------------------------
# Adapter boundary (static) test
# ---------------------------------------------------------------------------


def _code_without_docstrings_and_comments(source: str) -> str:
    """Return source code with triple-quoted strings and # comments removed."""
    import io
    import tokenize

    blanked = {tokenize.STRING, tokenize.COMMENT}
    if hasattr(tokenize, "FSTRING_START"):
        blanked |= {tokenize.FSTRING_START, tokenize.FSTRING_MIDDLE, tokenize.FSTRING_END}

    result: list[str] = []
    last_line = 1
    last_col = 0
    for tok in tokenize.generate_tokens(io.StringIO(source).readline):
        start_line, start_col = tok.start
        if start_line > last_line:
            result.append("\n" * (start_line - last_line))
            last_col = 0
        if start_col > last_col:
            result.append(" " * (start_col - last_col))
        text = "" if tok.type in blanked else tok.string
        result.append(text)
        last_line, last_col = tok.end
    return "".join(result)


def test_wellness_import_has_no_direct_persistence_imports() -> None:
    # The RECOVERY path legitimately needs HealthScoreCreate / ScoreComponent /
    # health_score_service (the Garmin push path emits no RECOVERY score), so
    # unlike the workouts bridge those are NOT forbidden here. The generic
    # time-series / event-record persistence internals still must not leak in.
    source = _code_without_docstrings_and_comments(WELLNESS_IMPORT_PATH.read_text())
    forbidden = [
        "event_record_service",
        "EventRecordCreate",
        "EventRecordDetailCreate",
        "DataPointSeriesRepository",
        "EventRecordRepository",
        "from app.models",
        "from influxdb",
        "import influxdb",
    ]
    for token in forbidden:
        assert token not in source, f"grafana_wellness_import.py must not reference {token!r}"


# ---------------------------------------------------------------------------
# Script-level test
# ---------------------------------------------------------------------------


def test_query_influx_measurement_rejects_invalid_measurement_name() -> None:
    from scripts.import_garmin_grafana_wellness import query_influx_measurement

    with pytest.raises(ValueError, match="Invalid Influx measurement name"):
        query_influx_measurement(
            host="localhost",
            port=8086,
            database="GarminStats",
            username=None,
            password=None,
            measurement='DailyStats"; DROP',
            start=datetime(2024, 1, 1, tzinfo=timezone.utc),
            end=datetime(2024, 1, 2, tzinfo=timezone.utc),
        )


# ---------------------------------------------------------------------------
# DB-backed idempotency test (real Garmin247Data + health_score_service)
# ---------------------------------------------------------------------------


@pytest.fixture
def garmin_247(db: Session) -> Garmin247Data:
    from unittest.mock import MagicMock

    from app.repositories.user_connection_repository import UserConnectionRepository
    from app.services.providers.garmin.oauth import GarminOAuth

    oauth = GarminOAuth(
        user_repo=MagicMock(),
        connection_repo=UserConnectionRepository(),
        provider_name="garmin",
        api_base_url="https://apis.garmin.com",
    )
    return Garmin247Data(
        provider_name="garmin",
        api_base_url="https://apis.garmin.com",
        oauth=oauth,
    )


def test_import_wellness_rows_second_run_saves_zero_for_all_paths(
    garmin_247: Garmin247Data,
    db: Session,
) -> None:
    user = UserFactory()
    daily_rows = [_daily_row()]
    sleep_rows = [_sleep_row()]

    first = import_wellness_rows(
        db,
        garmin_247,
        user.id,
        daily_rows=daily_rows,
        sleep_rows=sleep_rows,
        dry_run=False,
    )
    assert first.saved["dailies"] > 0
    assert first.saved["sleeps"] > 0
    assert first.saved["recovery"] == 1

    # Idempotency holds across all three write paths: a re-run of the same rows
    # must not insert duplicate steps samples, sleep records, or recovery scores.
    from app.models import DataPointSeries, EventRecord, HealthScore

    steps_before = db.query(DataPointSeries).count()
    sleeps_before = db.query(EventRecord).count()
    recovery_before = db.query(HealthScore).filter(HealthScore.category == HealthScoreCategory.RECOVERY).count()

    import_wellness_rows(
        db,
        garmin_247,
        user.id,
        daily_rows=daily_rows,
        sleep_rows=sleep_rows,
        dry_run=False,
    )

    assert db.query(DataPointSeries).count() == steps_before
    assert db.query(EventRecord).count() == sleeps_before
    assert db.query(HealthScore).filter(HealthScore.category == HealthScoreCategory.RECOVERY).count() == recovery_before
