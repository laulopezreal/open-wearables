#!/usr/bin/env python3
"""CLI to import garmin-grafana InfluxDB workouts into Open Wearables (PR 1).

This script owns CLI parsing and InfluxDB v1 access. The influxdb dependency is
script-only and is never imported by app service modules; run it with:

    uv run --with influxdb==5.3.2 python scripts/import_garmin_grafana.py \\
        --user-id <uuid> --start-date 2024-01-01 --end-date 2024-12-31

Default mode is dry-run; pass --write to persist. The session is rolled back on
dry-run and on any exception, and committed only on a successful --write.
"""

from __future__ import annotations

import argparse
import json
import os
import re
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from app.database import SessionLocal
from app.services.providers.factory import ProviderFactory
from app.services.providers.garmin.data_247 import Garmin247Data
from app.services.providers.garmin.grafana_import import import_activity_summary_rows


def parse_date(value: str) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def query_influx_measurement(
    *,
    host: str,
    port: int,
    database: str,
    username: str | None,
    password: str | None,
    measurement: str,
    start: datetime,
    end: datetime,
) -> list[dict[str, Any]]:
    """Query an InfluxDB v1 measurement within a time range.

    The measurement name is validated against a conservative regex before being
    interpolated into InfluxQL.
    """
    if not re.fullmatch(r"[A-Za-z0-9_]+", measurement):
        raise ValueError(f"Invalid Influx measurement name: {measurement!r}")

    # influxdb is a script-only dependency provided via `uv run --with influxdb==5.3.2`.
    from influxdb import InfluxDBClient  # ty: ignore[unresolved-import]

    client = InfluxDBClient(host=host, port=port, username=username, password=password)
    client.switch_database(database)
    query = f"SELECT * FROM \"{measurement}\" WHERE time >= '{start.isoformat()}' AND time <= '{end.isoformat()}'"
    return list(client.query(query).get_points())


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Import garmin-grafana InfluxDB data into Open Wearables",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--user-id", required=True, type=UUID)
    parser.add_argument("--start-date", required=True)
    parser.add_argument("--end-date", required=True)
    parser.add_argument("--influx-host", default="localhost")
    parser.add_argument("--influx-port", type=int, default=8086)
    parser.add_argument("--influx-db", default="GarminStats")
    parser.add_argument("--influx-username")
    parser.add_argument("--influx-password", default=os.environ.get("GARMIN_GRAFANA_INFLUX_PASSWORD"))
    parser.add_argument("--default-device-name", default="Garmin")
    parser.add_argument("--sample-limit", type=int, default=3)
    parser.add_argument("--limit", type=int, help="Maximum ActivitySummary rows to process after querying")
    parser.add_argument("--write", action="store_true", help="Persist imported records. Without this, dry-run only.")
    args = parser.parse_args()
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be a positive integer")
    return args


def main() -> None:
    args = parse_args()
    start = parse_date(args.start_date)
    end = parse_date(args.end_date)
    if start > end:
        raise SystemExit("--start-date must be before --end-date")

    mode = "WRITE" if args.write else "DRY RUN"
    print(f"Mode: {mode}")
    print(f"User ID: {args.user_id}")
    print(f"Range: {start.isoformat()} -> {end.isoformat()}")
    print(f"Influx: {args.influx_host}:{args.influx_port}/{args.influx_db}")
    if args.limit:
        print(f"Limit: {args.limit} ActivitySummary rows")

    rows = query_influx_measurement(
        host=args.influx_host,
        port=args.influx_port,
        database=args.influx_db,
        username=args.influx_username,
        password=args.influx_password,
        measurement="ActivitySummary",
        start=start,
        end=end,
    )
    if args.limit:
        rows = rows[: args.limit]

    garmin_strategy = ProviderFactory().get_provider("garmin")
    garmin_247 = garmin_strategy.data_247
    if not isinstance(garmin_247, Garmin247Data):
        raise SystemExit("Garmin 247 service unavailable")

    with SessionLocal() as db:
        try:
            summary = import_activity_summary_rows(
                db,
                garmin_247,
                args.user_id,
                rows,
                dry_run=not args.write,
                default_device_name=args.default_device_name,
                sample_limit=args.sample_limit,
            )
            if args.write:
                db.commit()
            else:
                db.rollback()
        except Exception:
            db.rollback()
            raise
        print(json.dumps(asdict(summary), default=str, indent=2))


if __name__ == "__main__":
    main()
