"""garmin-grafana to Open Wearables import bridge (PR 1: workouts only).

This module is a source adapter, not a persistence layer. It transforms
garmin-grafana InfluxDB ``ActivitySummary`` rows into Garmin-shaped activity
dicts and hands them to the existing Garmin 24/7 batch path:

    garmin-grafana ActivitySummary rows
      -> adapter-only Garmin-shaped activity dicts
      -> Garmin247Data.process_items_batch(db, user_id, "activities", activities)

Adapter boundary rules (enforced by tests):

- The only write path is ``garmin_247.process_items_batch(...)``.
- This module must not import repositories, ORM models, event_record_service,
  EventRecordCreate, EventRecordDetailCreate, or DataPointSeriesRepository.
- This module must not instantiate database objects directly.
- This module must not import influxdb; InfluxDB access stays script-only.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from app.database import DbSession
from app.services.providers.garmin.data_247 import Garmin247Data


@dataclass
class GarminGrafanaImportSummary:
    """Counts and samples for a single import run.

    ``transformed`` and ``saved`` are tracked separately on purpose:
    process_items_batch may save fewer records than were transformed
    (for example duplicates that hit the idempotency conflict path).
    """

    queried: dict[str, int] = field(default_factory=dict)
    transformed: dict[str, int] = field(default_factory=dict)
    saved: dict[str, int] = field(default_factory=dict)
    skipped: dict[str, int] = field(default_factory=dict)
    samples: dict[str, list[dict[str, Any]]] = field(default_factory=dict)


def parse_influx_time(value: Any) -> datetime | None:
    """Parse an InfluxDB timestamp into a UTC-aware datetime.

    Timezone-aware inputs are converted with ``.astimezone(timezone.utc)``;
    naive inputs are assumed to already be UTC.
    """
    if not value:
        return None
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _to_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError, OverflowError):
        return None


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError, OverflowError):
        return None


def is_activity_summary_end_marker(row: dict[str, Any]) -> bool:
    """garmin-grafana writes a START row and an END row per activity.

    END rows must never become workouts; they are identified by
    ``activityName == "END"`` and/or ``activityType == "No Activity"``.
    """
    return row.get("activityName") == "END" or row.get("activityType") == "No Activity"


def activity_summary_row_to_garmin_activity(
    row: dict[str, Any],
    default_device_name: str = "Garmin",
) -> dict[str, Any] | None:
    """Transform one ActivitySummary START row into a Garmin-shaped activity dict.

    Returns None for END markers and for rows missing required fields
    (time, ActivityID, duration, activityType).
    """
    if is_activity_summary_end_marker(row):
        return None

    start = parse_influx_time(row.get("time"))
    activity_id = row.get("ActivityID") or row.get("Activity_ID")
    duration = _to_int(row.get("elapsedDuration") or row.get("movingDuration"))
    activity_type = row.get("activityType")

    if start is None or not activity_id or duration is None or not activity_type:
        return None

    device_name = row.get("Device") or default_device_name
    device_id = row.get("Device_ID") or device_name

    return {
        "activityId": str(activity_id),
        "activityType": str(activity_type).upper(),
        "startTimeInSeconds": int(start.timestamp()),
        "durationInSeconds": duration,
        "startTimeOffsetInSeconds": 0,
        "deviceName": device_name,
        "deviceId": device_id,
        "distanceInMeters": _to_float(row.get("distance")),
        "activeKilocalories": _to_int(row.get("calories")),
        "averageHeartRateInBeatsPerMinute": _to_int(row.get("averageHR")),
        "maxHeartRateInBeatsPerMinute": _to_int(row.get("maxHR")),
        "elevationGainInMeters": _to_float(row.get("elevationGain")),
        "averageSpeedInMetersPerSecond": _to_float(row.get("averageSpeed")),
    }


def import_activity_summary_rows(
    db: DbSession,
    garmin_247: Garmin247Data,
    user_id: UUID,
    rows: list[dict[str, Any]],
    *,
    dry_run: bool,
    default_device_name: str = "Garmin",
    sample_limit: int = 3,
) -> GarminGrafanaImportSummary:
    """Transform ActivitySummary rows and (unless dry_run) persist them.

    The only write path is garmin_247.process_items_batch(..., "activities", ...).
    On dry_run, or when there is nothing to write, no write call is made and
    saved["activities"] is 0.
    """
    summary = GarminGrafanaImportSummary()
    summary.queried["ActivitySummary"] = len(rows)

    activities: list[dict[str, Any]] = []
    skipped_end_marker = 0
    skipped_invalid = 0

    for row in rows:
        if is_activity_summary_end_marker(row):
            skipped_end_marker += 1
            continue
        activity = activity_summary_row_to_garmin_activity(row, default_device_name)
        if activity is None:
            skipped_invalid += 1
            continue
        activities.append(activity)

    summary.transformed["activities"] = len(activities)
    summary.skipped["ActivitySummary.end_marker"] = skipped_end_marker
    summary.skipped["ActivitySummary.invalid"] = skipped_invalid
    summary.samples["activities"] = activities[:sample_limit]

    if dry_run or not activities:
        summary.saved["activities"] = 0
        return summary

    summary.saved["activities"] = garmin_247.process_items_batch(db, user_id, "activities", activities)
    return summary
