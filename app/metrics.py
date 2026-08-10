"""Derived numbers for one session.

These are the values a user actually reads off a chart, so the formulas live in one
place and are covered by tests with hand-computed expectations.

Nothing here interprets the results medically — it reports counts and indices only.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta

from app.parsers.base import SessionFile

# A session that starts after midnight belongs to the night that began the previous
# evening. Shifting by 12 hours puts everything from noon to noon on one date, which
# matches how a user thinks about "last night".
NIGHT_BOUNDARY_SHIFT = timedelta(hours=12)


def night_date(session_start: datetime) -> date:
    """The night a session belongs to: 00:42 on the 22nd is the night of the 21st."""
    return (session_start - NIGHT_BOUNDARY_SHIFT).date()


def percentile(values: list[float], pct: float) -> float:
    """Linear-interpolated percentile, `pct` in 0..100.

    Written out rather than pulled from numpy: it is six lines, and it keeps the
    ingest path free of a numeric stack that would otherwise only exist for this.
    """
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (pct / 100) * (len(ordered) - 1)
    low = int(rank)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (rank - low)


def _index_per_hour(count: int, minutes: int) -> float:
    """Events per hour. Zero-length sessions report 0 rather than dividing by zero."""
    if minutes <= 0:
        return 0.0
    return count / minutes * 60


@dataclass(slots=True)
class SessionSummary:
    """One night, reduced to the numbers that go on a dashboard."""

    duration_minutes: int
    oai_total: int
    hi_total: int
    cai_total: int
    ahi: float
    oai_index: float
    hi_index: float
    cai_index: float
    avg_pressure: float
    p90_pressure: float
    p95_pressure: float
    max_pressure: float
    avg_leak: float
    max_leak: float
    night: date


def summarise(session: SessionFile) -> SessionSummary:
    records = session.records
    minutes = len(records)

    pressures = [r.pressure for r in records]
    leaks = [r.leak for r in records]

    oai_total = sum(r.oai for r in records)
    hi_total = sum(r.hi for r in records)
    cai_total = sum(r.cai for r in records)

    return SessionSummary(
        duration_minutes=minutes,
        oai_total=oai_total,
        hi_total=hi_total,
        cai_total=cai_total,
        # AHI counts apnoeas and hypopnoeas together; central events are apnoeas too,
        # so they are included here and also reported separately as cai_index.
        ahi=_index_per_hour(oai_total + hi_total + cai_total, minutes),
        oai_index=_index_per_hour(oai_total, minutes),
        hi_index=_index_per_hour(hi_total, minutes),
        cai_index=_index_per_hour(cai_total, minutes),
        avg_pressure=(sum(pressures) / minutes) if minutes else 0.0,
        p90_pressure=percentile(pressures, 90),
        p95_pressure=percentile(pressures, 95),
        max_pressure=max(pressures, default=0.0),
        avg_leak=(sum(leaks) / minutes) if minutes else 0.0,
        max_leak=max(leaks, default=0.0),
        night=night_date(session.start),
    )
