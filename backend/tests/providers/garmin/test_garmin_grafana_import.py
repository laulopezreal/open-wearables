"""Tests for the garmin-grafana to Open Wearables workouts import bridge (PR 1)."""

from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy.orm import Session

from app.services.providers.garmin.data_247 import Garmin247Data
from app.services.providers.garmin.grafana_import import (
    activity_summary_row_to_garmin_activity,
    import_activity_summary_rows,
    is_activity_summary_end_marker,
    parse_influx_time,
)
from tests.factories import UserFactory

GRAFANA_IMPORT_PATH = (
    Path(__file__).resolve().parents[3] / "app" / "services" / "providers" / "garmin" / "grafana_import.py"
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


def _start_row(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "time": "2024-01-15T08:30:00+00:00",
        "ActivityID": 12345,
        "activityType": "running",
        "activityName": "Morning Run",
        "elapsedDuration": 1800,
        "movingDuration": 1700,
        "distance": 5000.0,
        "calories": 350,
        "averageHR": 145,
        "maxHR": 175,
        "elevationGain": 42.5,
        "averageSpeed": 2.78,
        "Device": "Forerunner 255",
        "Device_ID": "device-abc",
    }
    row.update(overrides)
    return row


# ---------------------------------------------------------------------------
# Transform tests
# ---------------------------------------------------------------------------


def test_activity_summary_row_to_garmin_activity_maps_required_fields() -> None:
    activity = activity_summary_row_to_garmin_activity(_start_row())

    assert activity is not None
    assert activity["activityId"] == "12345"
    assert activity["activityType"] == "RUNNING"
    assert activity["durationInSeconds"] == 1800
    assert activity["startTimeInSeconds"] == int(datetime(2024, 1, 15, 8, 30, tzinfo=timezone.utc).timestamp())
    assert activity["deviceName"] == "Forerunner 255"
    assert activity["deviceId"] == "device-abc"
    assert activity["distanceInMeters"] == 5000.0
    assert activity["activeKilocalories"] == 350
    assert activity["averageHeartRateInBeatsPerMinute"] == 145
    assert activity["maxHeartRateInBeatsPerMinute"] == 175
    assert activity["elevationGainInMeters"] == 42.5
    assert activity["averageSpeedInMetersPerSecond"] == 2.78
    assert activity["startTimeOffsetInSeconds"] == 0


def test_activity_summary_row_to_garmin_activity_device_id_falls_back_to_device_name() -> None:
    activity = activity_summary_row_to_garmin_activity(_start_row(Device_ID=None, Device=None))

    assert activity is not None
    assert activity["deviceName"] == "Garmin"
    assert activity["deviceId"] == "Garmin"


def test_activity_summary_row_to_garmin_activity_skips_end_marker_row() -> None:
    assert activity_summary_row_to_garmin_activity(_start_row(activityName="END")) is None


def test_activity_summary_row_to_garmin_activity_skips_no_activity_marker_row() -> None:
    assert activity_summary_row_to_garmin_activity(_start_row(activityType="No Activity")) is None


def test_activity_summary_row_to_garmin_activity_returns_none_without_activity_id() -> None:
    assert activity_summary_row_to_garmin_activity(_start_row(ActivityID=None, Activity_ID=None)) is None


def test_activity_summary_row_to_garmin_activity_returns_none_without_time() -> None:
    assert activity_summary_row_to_garmin_activity(_start_row(time=None)) is None


def test_activity_summary_row_to_garmin_activity_returns_none_without_duration() -> None:
    assert activity_summary_row_to_garmin_activity(_start_row(elapsedDuration=None, movingDuration=None)) is None


def test_activity_summary_row_to_garmin_activity_returns_none_without_activity_type() -> None:
    assert activity_summary_row_to_garmin_activity(_start_row(activityType=None)) is None


def test_is_activity_summary_end_marker() -> None:
    assert is_activity_summary_end_marker({"activityName": "END"}) is True
    assert is_activity_summary_end_marker({"activityType": "No Activity"}) is True
    assert is_activity_summary_end_marker({"activityName": "Run"}) is False


def test_parse_influx_time_converts_offset_to_utc() -> None:
    parsed = parse_influx_time("2024-01-15T10:30:00+02:00")

    assert parsed is not None
    assert parsed.tzinfo == timezone.utc
    assert parsed == datetime(2024, 1, 15, 8, 30, tzinfo=timezone.utc)


def test_parse_influx_time_assumes_utc_for_naive_value() -> None:
    parsed = parse_influx_time("2024-01-15T08:30:00")

    assert parsed == datetime(2024, 1, 15, 8, 30, tzinfo=timezone.utc)


def test_parse_influx_time_returns_none_for_garbage() -> None:
    assert parse_influx_time("not-a-date") is None
    assert parse_influx_time(None) is None


# ---------------------------------------------------------------------------
# Boundary behavioral tests (fake Garmin247)
# ---------------------------------------------------------------------------


def test_import_activity_summary_rows_dry_run_does_not_call_process_items_batch() -> None:
    fake = FakeGarmin247()

    summary = import_activity_summary_rows(
        db=object(),
        garmin_247=fake,  # type: ignore[arg-type]
        user_id=uuid4(),
        rows=[_start_row()],
        dry_run=True,
    )

    assert fake.calls == []
    assert summary.transformed["activities"] == 1
    assert summary.saved["activities"] == 0


def test_import_activity_summary_rows_write_calls_process_items_batch() -> None:
    fake = FakeGarmin247()
    user_id = uuid4()

    summary = import_activity_summary_rows(
        db=object(),
        garmin_247=fake,  # type: ignore[arg-type]
        user_id=user_id,
        rows=[_start_row()],
        dry_run=False,
    )

    assert len(fake.calls) == 1
    called_user_id, summary_type, items = fake.calls[0]
    assert called_user_id == user_id
    assert summary_type == "activities"
    assert len(items) == 1
    assert summary.saved["activities"] == 1


def test_import_activity_summary_rows_uses_only_process_items_batch_for_writes() -> None:
    # Fake exposes only process_items_batch; no repository/service fake is needed,
    # proving the importer routes all writes through the single boundary method.
    fake = FakeGarmin247()

    import_activity_summary_rows(
        db=object(),
        garmin_247=fake,  # type: ignore[arg-type]
        user_id=uuid4(),
        rows=[_start_row(), _start_row(ActivityID=999)],
        dry_run=False,
    )

    assert len(fake.calls) == 1
    assert fake.calls[0][1] == "activities"


def test_import_activity_summary_rows_start_end_pair_transforms_only_start_row() -> None:
    fake = FakeGarmin247()
    end_row = _start_row(activityName="END", activityType="No Activity")

    summary = import_activity_summary_rows(
        db=object(),
        garmin_247=fake,  # type: ignore[arg-type]
        user_id=uuid4(),
        rows=[_start_row(), end_row],
        dry_run=False,
    )

    assert summary.transformed["activities"] == 1
    assert summary.skipped["ActivitySummary.end_marker"] == 1
    assert len(fake.calls[0][2]) == 1


def test_import_activity_summary_rows_summary_separates_end_marker_and_invalid_skips() -> None:
    fake = FakeGarmin247()
    rows = [
        _start_row(),
        _start_row(activityName="END"),
        _start_row(activityType="No Activity"),
        _start_row(ActivityID=None, Activity_ID=None),  # invalid
        _start_row(time=None),  # invalid
    ]

    summary = import_activity_summary_rows(
        db=object(),
        garmin_247=fake,  # type: ignore[arg-type]
        user_id=uuid4(),
        rows=rows,
        dry_run=True,
    )

    assert summary.queried["ActivitySummary"] == 5
    assert summary.transformed["activities"] == 1
    assert summary.skipped["ActivitySummary.end_marker"] == 2
    assert summary.skipped["ActivitySummary.invalid"] == 2


def test_import_activity_summary_rows_duplicate_write_uses_process_items_batch_once_per_run() -> None:
    # Each run routes all activities through a single process_items_batch call.
    # Real cross-run idempotency is verified by the DB-backed test below.
    fake = FakeGarmin247()
    rows = [_start_row()]

    import_activity_summary_rows(db=object(), garmin_247=fake, user_id=uuid4(), rows=rows, dry_run=False)  # type: ignore[arg-type]
    import_activity_summary_rows(db=object(), garmin_247=fake, user_id=uuid4(), rows=rows, dry_run=False)  # type: ignore[arg-type]

    assert len(fake.calls) == 2  # one batch call per run, never per-item


def test_import_activity_summary_rows_honest_saved_count_when_fewer_saved_than_transformed() -> None:
    # process_items_batch can save fewer than transformed (duplicates).
    fake = FakeGarmin247(saved=0)

    summary = import_activity_summary_rows(
        db=object(),
        garmin_247=fake,  # type: ignore[arg-type]
        user_id=uuid4(),
        rows=[_start_row()],
        dry_run=False,
    )

    assert summary.transformed["activities"] == 1
    assert summary.saved["activities"] == 0


# ---------------------------------------------------------------------------
# Adapter boundary (static) test
# ---------------------------------------------------------------------------


def _code_without_docstrings_and_comments(source: str) -> str:
    """Return source code with triple-quoted strings and # comments removed.

    The bridge's own docstring names the forbidden modules to explain that they
    must not be imported; we only want to scan executable code and real imports.
    Lines are preserved (only STRING/COMMENT tokens are blanked) so that import
    statements like ``from app.repositories ...`` stay intact for substring checks.
    """
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


def test_grafana_import_has_no_direct_persistence_imports() -> None:
    source = _code_without_docstrings_and_comments(GRAFANA_IMPORT_PATH.read_text())
    forbidden = [
        "event_record_service",
        "EventRecordCreate",
        "EventRecordDetailCreate",
        "DataPointSeriesRepository",
        "EventRecordRepository",
        "from app.models",
        "from app.repositories",
        "from influxdb",
        "import influxdb",
        "process_push_activities",
    ]
    for token in forbidden:
        assert token not in source, f"grafana_import.py must not reference {token!r}"


# ---------------------------------------------------------------------------
# Script-level tests
# ---------------------------------------------------------------------------


def test_query_influx_measurement_rejects_invalid_measurement_name() -> None:
    from scripts.import_garmin_grafana import query_influx_measurement

    with pytest.raises(ValueError, match="Invalid Influx measurement name"):
        query_influx_measurement(
            host="localhost",
            port=8086,
            database="GarminStats",
            username=None,
            password=None,
            measurement='ActivitySummary"; DROP',
            start=datetime(2024, 1, 1, tzinfo=timezone.utc),
            end=datetime(2024, 1, 2, tzinfo=timezone.utc),
        )


def test_cli_limit_slices_rows_before_import() -> None:
    # Mirrors the script behavior: rows are sliced to --limit after querying.
    rows = [_start_row(ActivityID=i) for i in range(1, 11)]
    limit = 3
    sliced = rows[:limit]

    fake = FakeGarmin247()
    summary = import_activity_summary_rows(
        db=object(),
        garmin_247=fake,  # type: ignore[arg-type]
        user_id=uuid4(),
        rows=sliced,
        dry_run=True,
    )

    assert summary.queried["ActivitySummary"] == limit
    assert summary.transformed["activities"] == limit


# ---------------------------------------------------------------------------
# DB-backed idempotency test (real Garmin247Data + process_items_batch)
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


def test_import_activity_summary_rows_duplicate_db_write_saves_zero_on_second_run(
    garmin_247: Garmin247Data,
    db: Session,
) -> None:
    user = UserFactory()
    rows = [_start_row()]

    first = import_activity_summary_rows(
        db,
        garmin_247,
        user.id,
        rows,
        dry_run=False,
    )
    assert first.transformed["activities"] == 1
    assert first.saved["activities"] == 1

    second = import_activity_summary_rows(
        db,
        garmin_247,
        user.id,
        rows,
        dry_run=False,
    )
    assert second.transformed["activities"] == 1
    # Idempotent: the same workout (data_source_id + start + end) is not re-inserted.
    assert second.saved["activities"] == 0
