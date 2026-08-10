"""Shared test helpers.

`build_bys` is the reason the suite runs anywhere: real `.BYS` files carry a device
serial and a person's health data, so they are never committed. Tests that want a
real file are marked `realdata` and skip themselves when tests/fixtures/ is empty.
Everything else builds the bytes it needs here. Do not remove this helper — without
it the suite can only run on the one machine that owns the data.
"""

from __future__ import annotations

import struct
from datetime import datetime, timedelta
from pathlib import Path

import pytest

FIXTURE_DIR = Path(__file__).parent / "fixtures"

# Never a real device serial. Reviewers grep for this.
TEST_SERIAL = "YH550A-XXXXXXXXX"


def build_record(
    pressure: float = 5.0,
    leak: float = 1.0,
    oai: int = 0,
    hi: int = 0,
    cai: int = 0,
    u1: int = 0,
    u2: int = 0,
    u6: int = 0,
    u7: int = 0,
    u8: int = 0,
) -> bytes:
    """One 10-byte YH550 minute record. Pressure and leak are given in real units."""
    return bytes(
        [
            round(pressure * 10),
            u1,
            u2,
            oai,
            hi,
            cai,
            u6,
            u7,
            u8,
            round(leak * 10),
        ]
    )


def build_bys(
    records: list[bytes],
    start: datetime = datetime(2025, 8, 21, 22, 30, 0),
    end: datetime | None = None,
    serial: str = TEST_SERIAL,
    mode: int = 1,
    ramp_minutes: int = 10,
    initial_pressure: float = 4.0,
    min_pressure: float = 5.0,
    max_pressure: float = 15.0,
    humidity: int = 0,
    device_avg_leak: float = 0.9,
    device_avg_pressure: float = 5.8,
    header_marker: int = 0xF9,
    footer: int = 0xFA,
    record_count: int | None = None,
) -> bytes:
    """Assemble a syntactically valid YH550 `.BYS` file.

    `end` defaults to start + len(records) minutes, which is what the device writes.
    `record_count`, `header_marker` and `footer` can be overridden to build the
    malformed files that the structural tests need.
    """
    if end is None:
        end = start + timedelta(minutes=len(records))
    if record_count is None:
        record_count = len(records)

    header = bytearray(51)

    def put_dt(offset: int, dt: datetime) -> None:
        header[offset : offset + 6] = bytes(
            [dt.year - 2000, dt.month, dt.day, dt.hour, dt.minute, dt.second]
        )

    put_dt(0, start)
    put_dt(6, end)
    header[12] = mode
    header[13] = ramp_minutes
    header[14] = round(initial_pressure * 10)
    header[15] = round(min_pressure * 10)
    header[16] = round(max_pressure * 10)
    header[18] = humidity
    header[26] = round(device_avg_leak * 10)
    header[28] = round(device_avg_pressure * 10)
    header[30:46] = serial.encode("ascii").ljust(16, b"\x00")[:16]
    struct.pack_into("<H", header, 46, record_count)
    header[50] = header_marker

    return bytes(header) + b"".join(records) + bytes([footer])


def real_fixture_files() -> list[Path]:
    """Real `.BYS` files the user dropped into tests/fixtures/, if any."""
    if not FIXTURE_DIR.is_dir():
        return []
    return sorted(
        p for p in FIXTURE_DIR.iterdir() if p.suffix.lower() == ".bys" and p.is_file()
    )


@pytest.fixture
def real_files() -> list[Path]:
    files = real_fixture_files()
    if not files:
        pytest.skip(
            "no real .BYS files in tests/fixtures/ — drop some in to run these checks"
        )
    return files
