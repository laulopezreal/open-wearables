"""garmin-grafana to Open Wearables wellness import bridge (OW #3/#4/#5).

Sibling of the workouts bridge in ``grafana_import.py`` (PR 1 / OW #9). Where
that module mapped ``ActivitySummary`` rows to workout EventRecords, this module
maps the wellness measurements garmin-grafana writes into InfluxDB onto the
records the Open Wearables summaries endpoints aggregate:

    garmin-grafana DailyStats rows
      -> Garmin-shaped "dailies" dicts
      -> Garmin247Data.process_items_batch(db, user_id, "dailies", ...)
      -> DataPointSeries(steps, resting_heart_rate, ...)  [feeds summaries/activity.steps]

    garmin-grafana SleepSummary rows
      -> Garmin-shaped "sleeps" dicts
      -> Garmin247Data.process_items_batch(db, user_id, "sleeps", ...)
      -> EventRecord(sleep_session)                        [feeds summaries/sleep.duration_minutes]

    garmin-grafana DailyStats + SleepSummary rows
      -> HealthScoreCreate(category=RECOVERY, components={resting_heart_rate, hrv_rmssd_milli})
      -> health_score_service.bulk_create(db, ...)         [feeds summaries/recovery RHR + HRV]

Why a dedicated RECOVERY HealthScore (and not just the Garmin push paths):
``summaries/recovery`` is built solely from ``HealthScore(RECOVERY)`` rows and
their ``components`` JSONB (resting_heart_rate, hrv_rmssd_milli). The Garmin
provider's webhook normalizers never emit a RECOVERY score, so routing dailies
and sleeps alone would leave the consumer's Recovery card empty. This bridge
constructs that score itself; the steps and sleep paths still reuse the tested
``process_items_batch`` boundary.

Consumer contract (getmAIlean ``web/lib/sync/adapters/ow-wellness.ts``):
    summaries/activity.steps                  -> DailyMetrics.steps
    summaries/sleep.duration_minutes / 60     -> DailyMetrics.sleepHours
    summaries/recovery.resting_heart_rate_bpm -> DailyMetrics.rhr
    summaries/recovery.avg_hrv_sdnn_ms        -> DailyMetrics.hrv

Adapter boundary rules (enforced by tests):

- The only generic write path is ``garmin_247.process_items_batch(...)``; the
  one extra write path is ``health_score_service.bulk_create(...)`` for the
  RECOVERY score, which has no equivalent on the Garmin push path.
- This module must not import repositories, ORM models, event_record_service,
  EventRecordCreate, EventRecordDetailCreate, DataPointSeriesRepository, or the
  DataPointSeries / EventRecord models directly.
- This module must not import influxdb; InfluxDB access stays script-only.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

from app.database import DbSession
from app.schemas.enums import HealthScoreCategory, ProviderName
from app.schemas.model_crud.activities import HealthScoreCreate, ScoreComponent
from app.services.health_score_service import health_score_service
from app.services.providers.garmin.data_247 import Garmin247Data


@dataclass
class GarminGrafanaWellnessSummary:
    """Counts and samples for a single wellness import run.

    ``transformed`` and ``saved`` are tracked separately on purpose: the write
    paths may persist fewer records than were transformed (idempotency conflict
    paths skip duplicates, so a re-run saves zero).
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


def _calendar_date(row: dict[str, Any]) -> str | None:
    """Return the YYYY-MM-DD calendar date for an InfluxDB wellness row.

    garmin-grafana writes the daily ``DailyStats`` point at midnight of the
    calendar day, so the row ``time`` is the source of the calendar date. We use
    the date the source assigns rather than deriving it from a sleep-start
    timestamp, which can fall on the previous evening.
    """
    parsed = parse_influx_time(row.get("time"))
    if parsed is None:
        return None
    return parsed.date().isoformat()


def _recorded_at_for_date(calendar_date: str) -> datetime | None:
    """Stable noon-UTC datetime for a calendar date.

    Recovery idempotency relies on a deterministic ``recorded_at``: the
    HealthScore unique constraint is (user_id, provider, category, recorded_at),
    so a re-run must produce the same instant. Noon UTC mirrors the dailies
    fallback in data_247 and keeps ``recorded_at.date()`` (the consumer's
    recovery_date key) on the source calendar day.
    """
    try:
        return datetime.strptime(calendar_date, "%Y-%m-%d").replace(hour=12, tzinfo=timezone.utc)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# DailyStats -> Garmin "dailies" dict (steps + resting HR time-series)
# ---------------------------------------------------------------------------


def daily_stats_row_to_garmin_daily(row: dict[str, Any]) -> dict[str, Any] | None:
    """Transform one DailyStats row into a Garmin-shaped ``dailies`` dict.

    Returns None when the row has no parsable calendar date. Only fields with a
    downstream home are mapped; ``calendarDate`` drives the time-series
    ``recorded_at``.
    """
    calendar_date = _calendar_date(row)
    if calendar_date is None:
        return None

    return {
        "calendarDate": calendar_date,
        "steps": _to_int(row.get("totalSteps")),
        "distanceInMeters": _to_int(row.get("totalDistanceMeters")),
        "activeKilocalories": _to_int(row.get("activeKilocalories")),
        "restingHeartRateInBeatsPerMinute": _to_int(row.get("restingHeartRate")),
        "minHeartRateInBeatsPerMinute": _to_int(row.get("minHeartRate")),
        "maxHeartRateInBeatsPerMinute": _to_int(row.get("maxHeartRate")),
        "floorsClimbed": _to_int(row.get("floorsAscended")),
        "moderateIntensityDurationInSeconds": (_to_int(row.get("moderateIntensityMinutes")) or 0) * 60,
        "vigorousIntensityDurationInSeconds": (_to_int(row.get("vigorousIntensityMinutes")) or 0) * 60,
    }


# ---------------------------------------------------------------------------
# SleepSummary -> Garmin "sleeps" dict (duration + stages + score)
# ---------------------------------------------------------------------------


def sleep_summary_row_to_garmin_sleep(row: dict[str, Any]) -> dict[str, Any] | None:
    """Transform one SleepSummary row into a Garmin-shaped ``sleeps`` dict.

    Returns None when the row has no parsable start time or no positive total
    sleep duration. The summaries endpoint derives ``duration_minutes`` from the
    stage seconds (deep + light + rem), so the stage durations are mapped, not
    just the total: passing only ``durationInSeconds`` would yield a zero
    duration downstream.
    """
    start = parse_influx_time(row.get("time"))
    if start is None:
        return None

    total_seconds = _to_int(row.get("sleepTimeSeconds"))
    deep = _to_int(row.get("deepSleepSeconds")) or 0
    light = _to_int(row.get("lightSleepSeconds")) or 0
    rem = _to_int(row.get("remSleepSeconds")) or 0
    awake = _to_int(row.get("awakeSleepSeconds")) or 0

    if not total_seconds:
        total_seconds = deep + light + rem
    if not total_seconds:
        return None

    sleep: dict[str, Any] = {
        "startTimeInSeconds": int(start.timestamp()),
        "startTimeOffsetInSeconds": 0,
        "durationInSeconds": total_seconds,
        "deepSleepDurationInSeconds": deep,
        "lightSleepDurationInSeconds": light,
        "remSleepInSeconds": rem,
        "awakeDurationInSeconds": awake,
        "averageHeartRate": _to_int(row.get("restingHeartRate")),
        "respirationAvg": _to_float(row.get("averageRespirationValue")),
        "avgOxygenSaturation": _to_float(row.get("averageSpO2Value")),
        "summaryId": f"grafana-sleep-{int(start.timestamp())}",
    }

    sleep_score = _to_int(row.get("sleepScore"))
    if sleep_score is not None:
        sleep["overallSleepScore"] = {"value": sleep_score, "qualifier": None}

    return sleep


# ---------------------------------------------------------------------------
# DailyStats + SleepSummary -> RECOVERY HealthScore (resting HR + HRV)
# ---------------------------------------------------------------------------


def build_recovery_health_score(
    user_id: UUID,
    calendar_date: str,
    *,
    resting_heart_rate: int | None,
    hrv_milli: float | None,
    spo2_percent: float | None = None,
) -> HealthScoreCreate | None:
    """Build a RECOVERY HealthScore for one calendar day.

    Returns None when neither resting HR nor HRV is present (an empty recovery
    row has no consumer value). The HRV component key is ``hrv_rmssd_milli``
    because the consumer reads ``components["hrv_rmssd_milli"].value`` and the
    health_score_repository maps it onto ``avg_hrv_sdnn_ms`` regardless of the
    underlying metric. garmin-grafana's ``avgOvernightHrv`` is Garmin's
    overnight HRV (SDNN-flavoured); the SDNN/RMSSD naming mismatch is inherited
    from the producer schema and noted in the PR.
    """
    if resting_heart_rate is None and hrv_milli is None:
        return None

    recorded_at = _recorded_at_for_date(calendar_date)
    if recorded_at is None:
        return None

    components: dict[str, ScoreComponent] = {}
    if resting_heart_rate is not None:
        components["resting_heart_rate"] = ScoreComponent(value=resting_heart_rate)
    if hrv_milli is not None:
        components["hrv_rmssd_milli"] = ScoreComponent(value=hrv_milli)
    if spo2_percent is not None:
        components["spo2_percentage"] = ScoreComponent(value=spo2_percent)

    return HealthScoreCreate(
        id=uuid4(),
        user_id=user_id,
        provider=ProviderName.GARMIN,
        category=HealthScoreCategory.RECOVERY,
        value=None,
        recorded_at=recorded_at,
        components=components,
    )


def build_recovery_scores(
    user_id: UUID,
    daily_rows: list[dict[str, Any]],
    sleep_rows: list[dict[str, Any]],
) -> list[HealthScoreCreate]:
    """Join DailyStats (resting HR) and SleepSummary (overnight HRV) per day.

    Resting HR is preferred from DailyStats and falls back to the sleep row's
    restingHeartRate; HRV comes from the sleep row's ``avgOvernightHrv``. One
    RECOVERY score per calendar date.
    """
    rhr_by_date: dict[str, int] = {}
    for row in daily_rows:
        date = _calendar_date(row)
        rhr = _to_int(row.get("restingHeartRate"))
        if date and rhr is not None:
            rhr_by_date[date] = rhr

    hrv_by_date: dict[str, float] = {}
    sleep_rhr_by_date: dict[str, int] = {}
    spo2_by_date: dict[str, float] = {}
    for row in sleep_rows:
        date = _calendar_date(row)
        if not date:
            continue
        hrv = _to_float(row.get("avgOvernightHrv"))
        if hrv is not None:
            hrv_by_date[date] = hrv
        rhr = _to_int(row.get("restingHeartRate"))
        if rhr is not None:
            sleep_rhr_by_date.setdefault(date, rhr)
        spo2 = _to_float(row.get("averageSpO2Value"))
        if spo2 is not None:
            spo2_by_date.setdefault(date, spo2)

    scores: list[HealthScoreCreate] = []
    for date in sorted(set(rhr_by_date) | set(hrv_by_date) | set(sleep_rhr_by_date)):
        resting_heart_rate = rhr_by_date.get(date, sleep_rhr_by_date.get(date))
        score = build_recovery_health_score(
            user_id,
            date,
            resting_heart_rate=resting_heart_rate,
            hrv_milli=hrv_by_date.get(date),
            spo2_percent=spo2_by_date.get(date),
        )
        if score is not None:
            scores.append(score)
    return scores


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def import_wellness_rows(
    db: DbSession,
    garmin_247: Garmin247Data,
    user_id: UUID,
    *,
    daily_rows: list[dict[str, Any]],
    sleep_rows: list[dict[str, Any]],
    dry_run: bool,
    sample_limit: int = 3,
) -> GarminGrafanaWellnessSummary:
    """Transform wellness rows and (unless dry_run) persist them.

    Writes go through three paths: ``process_items_batch("dailies", ...)`` for
    steps / resting-HR time-series, ``process_items_batch("sleeps", ...)`` for
    sleep EventRecords, and ``health_score_service.bulk_create`` for the per-day
    RECOVERY scores. On dry_run, or when a category is empty, no write call is
    made for it and its ``saved`` count is 0.
    """
    summary = GarminGrafanaWellnessSummary()
    summary.queried["DailyStats"] = len(daily_rows)
    summary.queried["SleepSummary"] = len(sleep_rows)

    dailies: list[dict[str, Any]] = []
    skipped_dailies = 0
    for row in daily_rows:
        daily = daily_stats_row_to_garmin_daily(row)
        if daily is None:
            skipped_dailies += 1
            continue
        dailies.append(daily)

    sleeps: list[dict[str, Any]] = []
    skipped_sleeps = 0
    for row in sleep_rows:
        sleep = sleep_summary_row_to_garmin_sleep(row)
        if sleep is None:
            skipped_sleeps += 1
            continue
        sleeps.append(sleep)

    recovery_scores = build_recovery_scores(user_id, daily_rows, sleep_rows)

    summary.transformed["dailies"] = len(dailies)
    summary.transformed["sleeps"] = len(sleeps)
    summary.transformed["recovery"] = len(recovery_scores)
    summary.skipped["DailyStats.invalid"] = skipped_dailies
    summary.skipped["SleepSummary.invalid"] = skipped_sleeps
    summary.samples["dailies"] = dailies[:sample_limit]
    summary.samples["sleeps"] = sleeps[:sample_limit]
    summary.samples["recovery"] = [
        {
            "recorded_at": score.recorded_at.isoformat(),
            "components": {key: comp.value for key, comp in (score.components or {}).items()},
        }
        for score in recovery_scores[:sample_limit]
    ]

    if dry_run:
        summary.saved["dailies"] = 0
        summary.saved["sleeps"] = 0
        summary.saved["recovery"] = 0
        return summary

    summary.saved["dailies"] = (
        garmin_247.process_items_batch(db, user_id, "dailies", dailies) if dailies else 0
    )
    summary.saved["sleeps"] = (
        garmin_247.process_items_batch(db, user_id, "sleeps", sleeps) if sleeps else 0
    )
    if recovery_scores:
        health_score_service.bulk_create(db, recovery_scores)
    summary.saved["recovery"] = len(recovery_scores)

    return summary
