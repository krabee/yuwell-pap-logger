"""Writing sessions into InfluxDB.

Two measurements:

  pap_minute   one point per minute of therapy
  pap_session  one point per session, at the session start

Re-importing a file is harmless: InfluxDB overwrites a point that has the same
measurement, tag set and timestamp, so the same session written twice collapses onto
itself. The ingest ledger exists for the user interface and for skipping work, not to
protect correctness here.

Timestamps arrive as naive device-local wall-clock and are converted to UTC here,
once, using DEVICE_TZ. Nothing downstream of this module deals in local time.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Iterator

from influxdb_client import InfluxDBClient, Point, WritePrecision
from influxdb_client.client.write_api import SYNCHRONOUS

from app.config import Settings
from app.metrics import SessionSummary
from app.parsers.base import SessionFile

log = logging.getLogger(__name__)

MEASUREMENT_MINUTE = "pap_minute"
MEASUREMENT_SESSION = "pap_session"

# Points per write request. The whole point of batching here is to keep a 200k-point
# first import down to a few dozen round trips instead of one per minute of sleep.
BATCH_SIZE = 5000


class InfluxUnavailable(Exception):
    """Could not reach or write to the configured InfluxDB."""


def session_id(serial: str, start: datetime) -> str:
    """Stable identifier for one night on one machine.

    Built from the device serial and the session start rather than the filename: the
    machine restarts its filenames at 00100001.BYS after a reset, so filenames repeat.
    """
    return f"{serial}:{start.strftime('%Y%m%dT%H%M%S')}"


def _to_utc(naive_local: datetime, settings: Settings) -> datetime:
    """Attach the device's timezone to a naive wall-clock reading, then convert."""
    return naive_local.replace(tzinfo=settings.device_zoneinfo).astimezone(timezone.utc)


def build_points(
    session: SessionFile,
    summary: SessionSummary,
    username: str,
    settings: Settings,
    source_file: str,
    file_sha256: str,
) -> Iterator[Point]:
    """Yield every point for one session, session summary first."""
    sid = session_id(session.serial, session.start)
    start_utc = _to_utc(session.start, settings)

    yield (
        Point(MEASUREMENT_SESSION)
        .tag("user", username)
        .tag("serial", session.serial)
        .tag("session_id", sid)
        .tag("mode", session.mode_name)
        .tag("night_date", summary.night.isoformat())
        .field("duration_min", summary.duration_minutes)
        .field("oai_total", summary.oai_total)
        .field("hi_total", summary.hi_total)
        .field("cai_total", summary.cai_total)
        .field("ahi", float(summary.ahi))
        .field("oai_index", float(summary.oai_index))
        .field("hi_index", float(summary.hi_index))
        .field("cai_index", float(summary.cai_index))
        .field("avg_pressure", float(summary.avg_pressure))
        .field("p90_pressure", float(summary.p90_pressure))
        .field("p95_pressure", float(summary.p95_pressure))
        .field("max_pressure", float(summary.max_pressure))
        .field("avg_leak", float(summary.avg_leak))
        .field("max_leak", float(summary.max_leak))
        .field("set_initial_pressure", float(session.initial_pressure))
        .field("set_min_pressure", float(session.min_pressure))
        .field("set_max_pressure", float(session.max_pressure))
        .field("ramp_min", session.ramp_minutes)
        .field("humidity", session.humidity)
        # What the machine itself reported, kept so a scaling error in our maths
        # can be spotted after the fact without re-reading the SD card.
        .field("device_avg_pressure", float(session.device_avg_pressure))
        .field("device_avg_leak", float(session.device_avg_leak))
        .field("model", session.model)
        .field("source_file", source_file)
        .field("file_sha256", file_sha256)
        .time(start_utc, WritePrecision.S)
    )

    for record in session.records:
        yield (
            Point(MEASUREMENT_MINUTE)
            .tag("user", username)
            .tag("serial", session.serial)
            .tag("session_id", sid)
            .tag("mode", session.mode_name)
            .field("pressure", float(record.pressure))
            .field("leak", float(record.leak))
            .field("oai", record.oai)
            .field("hi", record.hi)
            .field("cai", record.cai)
            # Undocumented bytes are stored rather than dropped: if their meaning is
            # worked out later, it can be applied to data already collected.
            .field("u1", record.u1)
            .field("u2", record.u2)
            .field("u6", record.u6)
            .field("u7", record.u7)
            .field("u8", record.u8)
            .time(start_utc + timedelta(minutes=record.minute), WritePrecision.S)
        )


class InfluxWriter:
    """Thin wrapper over the Influx client, batching writes.

    Held open for the lifetime of the app; the client keeps its own connection pool.
    """

    def __init__(self, settings: Settings):
        self._settings = settings
        self._client = InfluxDBClient(
            url=settings.influx_url,
            token=settings.influx_token,
            org=settings.influx_org,
        )
        self._write_api = self._client.write_api(write_options=SYNCHRONOUS)

    def close(self) -> None:
        self._client.close()

    def write_session(
        self,
        session: SessionFile,
        summary: SessionSummary,
        username: str,
        source_file: str,
        file_sha256: str,
    ) -> int:
        """Write one session. Returns the number of points written."""
        batch: list[Point] = []
        written = 0

        for point in build_points(
            session, summary, username, self._settings, source_file, file_sha256
        ):
            batch.append(point)
            if len(batch) >= BATCH_SIZE:
                self._write(batch)
                written += len(batch)
                batch = []

        if batch:
            self._write(batch)
            written += len(batch)

        return written

    def _write(self, points: list[Point]) -> None:
        try:
            self._write_api.write(
                bucket=self._settings.influx_bucket,
                org=self._settings.influx_org,
                record=points,
            )
        except Exception as exc:  # the client raises a wide range of transport errors
            raise InfluxUnavailable(
                f"could not write to InfluxDB at {self._settings.influx_url}: {exc}"
            ) from exc

    def health(self) -> dict:
        """Report what the app is actually connected to, for /health.

        Checks the bucket too: a token that can ping but not write is a common
        misconfiguration that would otherwise only surface mid-upload.
        """
        info = {
            "url": self._settings.influx_url,
            "org": self._settings.influx_org,
            "bucket": self._settings.influx_bucket,
            "reachable": False,
            "bucket_found": False,
            "retention": None,
            "error": None,
        }
        try:
            ready = self._client.ping()
            info["reachable"] = bool(ready)
        except Exception as exc:
            info["error"] = str(exc)
            return info

        try:
            bucket = self._client.buckets_api().find_bucket_by_name(
                self._settings.influx_bucket
            )
            if bucket is not None:
                info["bucket_found"] = True
                rules = bucket.retention_rules or []
                seconds = rules[0].every_seconds if rules else 0
                info["retention"] = "infinite" if not seconds else f"{seconds}s"
        except Exception as exc:
            info["error"] = str(exc)

        return info
