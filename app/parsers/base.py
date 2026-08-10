"""Model-agnostic representation of one PAP therapy session.

Every parser in this package converts its device-specific binary layout into these
dataclasses, so metrics/influx/ingest never need to know which machine produced the
file. Adding a new Yuwell model means adding a parser module, not touching anything
downstream.

Timestamps here are **naive local wall-clock as recorded by the device**. The device
has no timezone concept at all. Conversion to an aware datetime happens exactly once,
in the ingest layer, using the configured DEVICE_TZ.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


class InvalidPAPFile(Exception):
    """The byte layout is structurally wrong — this is not a session file we can read.

    Raised only for structural failures (wrong size, missing markers, impossible
    dates). Unrecognised *values* inside an otherwise well-formed file are recorded
    in SessionFile.warnings instead, never raised.
    """


# Therapy mode as stored in the file's mode byte.
MODE_CPAP = 0
MODE_APAP = 1

MODE_NAMES = {MODE_CPAP: "cpap", MODE_APAP: "apap"}


@dataclass(slots=True)
class MinuteRecord:
    """One minute of therapy data.

    oai/hi/cai are event *counts within that minute*, not indices.

    u1/u2/u6/u7/u8 are bytes whose meaning is still unknown. They are carried through
    to the database on purpose: the format is reverse-engineered and roughly a fifth
    of it is undocumented, so keeping the raw values lets us decode them later from
    data we already collected instead of asking users to re-import.
    """

    minute: int  # offset from session start
    pressure: float  # cmH2O
    leak: float  # Lpm
    oai: int
    hi: int
    cai: int
    u1: int = 0
    u2: int = 0
    u6: int = 0
    u7: int = 0
    u8: int = 0

    @property
    def has_unknown_data(self) -> bool:
        return bool(self.u1 or self.u2 or self.u6 or self.u7 or self.u8)


@dataclass(slots=True)
class SessionFile:
    """One parsed session file.

    `start`/`end` are naive local wall-clock straight from the device.
    """

    model: str  # e.g. "YH550"
    serial: str  # e.g. "YH550A-XXXXXXXXX"
    start: datetime
    end: datetime
    mode: int
    ramp_minutes: int
    initial_pressure: float
    min_pressure: float
    max_pressure: float
    humidity: int
    device_avg_leak: float  # as computed by the machine, kept for cross-checking
    device_avg_pressure: float
    records: list[MinuteRecord] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def mode_name(self) -> str:
        return MODE_NAMES.get(self.mode, f"unknown-{self.mode}")

    @property
    def duration_minutes(self) -> int:
        return len(self.records)
