"""Parser for Yuwell YH550 (BreathCare ECO) `.BYS` session files.

Written from the published binary-grammar documentation in
https://github.com/Centurix/djmed by Chris Read (clean-room reverse engineering).
That repository carries no licence, so this is an independent implementation from
the documented field tables — no code was copied.

Layout, verified against 46 real session files:

    header       51 bytes
    records      10 bytes x record_count
    footer        1 byte  (0xFA)

so `len(data) == 51 + record_count * 10 + 1` exactly. Little endian; decimal values
are stored as the value times ten; years are two digits.

Header
    0   6   start date/time (Y M D h m s, one byte each)
    6   6   end date/time
    12  1   mode: 0x00 CPAP, 0x01 APAP
    13  1   ramp up time, minutes
    14  1   initial pressure (x10)
    15  1   minimum pressure (x10)
    16  1   maximum pressure (x10)
    18  1   humidity
    26  1   average leak volume (x10), as computed by the machine
    28  1   average pressure (x10), as computed by the machine
    30  16  serial
    46  2   record count (uint16)
    50  1   0xF9 header end marker

Record (one per minute)
    0   pressure cmH2O (x10)
    3   obstructive apnea count
    4   hypopnea count
    5   central apnea count
    9   leak volume Lpm (x10)
    1, 2, 6, 7, 8  meaning unknown
"""

from __future__ import annotations

import struct
from datetime import datetime

from app.parsers.base import InvalidPAPFile, MinuteRecord, SessionFile

MODEL = "YH550"

HEADER_SIZE = 51
RECORD_SIZE = 10
FOOTER_SIZE = 1

HEADER_END_MARKER = 0xF9
FOOTER_MARKER = 0xFA

# Erased flash reads back as 0xFF. When a session ends without the device finishing
# its write — power cut, card pulled — the header still declares the record count it
# reserved space for, and everything past the last real write stays 0xFF. Those
# records must be discarded: read literally they decode as 25.5 cmH2O pressure and
# 255 events per minute, which is not merely wrong but wrong in a direction that
# wrecks every chart the data appears in.
UNWRITTEN_BYTE = 0xFF

_HEADER_MARKER_OFFSET = 50
_SERIAL_OFFSET = 30
_SERIAL_LENGTH = 16
_RECORD_COUNT_OFFSET = 46

# Sanity bounds for the two-digit year. Widened deliberately: a device with a flat
# clock battery can report a nonsense date, and we would rather import the therapy
# data with a warning than reject the file outright.
_MIN_YEAR = 2000
_MAX_YEAR = 2099


def _read_datetime(data: bytes, offset: int, label: str) -> datetime:
    year, month, day, hour, minute, second = struct.unpack_from("6B", data, offset)
    try:
        return datetime(_MIN_YEAR + year, month, day, hour, minute, second)
    except ValueError as exc:
        raise InvalidPAPFile(
            f"{label} at offset {offset} is not a valid date/time: "
            f"20{year:02d}-{month:02d}-{day:02d} {hour:02d}:{minute:02d}:{second:02d}"
        ) from exc


def _count_written_records(data: bytes, record_count: int) -> int:
    """How many of the declared records the device actually wrote.

    Walks back from the end over records that are entirely 0xFF. Only a *trailing*
    run is trimmed: a gap of erased flash in the middle would mean something stranger
    than an interrupted write, and silently dropping it would hide that.

    The record at the boundary is dropped too when it contains any 0xFF, because a
    write cut off mid-record leaves exactly that — real values in the leading bytes
    and erased flash in the rest.
    """
    written = record_count
    while written > 0:
        record = data[
            HEADER_SIZE + (written - 1) * RECORD_SIZE : HEADER_SIZE + written * RECORD_SIZE
        ]
        if not all(byte == UNWRITTEN_BYTE for byte in record):
            break
        written -= 1

    if written == record_count:
        return written  # nothing was trimmed, so there is no torn record to consider

    if written > 0:
        boundary = data[
            HEADER_SIZE + (written - 1) * RECORD_SIZE : HEADER_SIZE + written * RECORD_SIZE
        ]
        if UNWRITTEN_BYTE in boundary:
            written -= 1

    return written


def looks_like_yh550(data: bytes) -> bool:
    """Cheap structural check used by the parser registry.

    Deliberately does not look at the serial string: rebadged units from resellers
    carry other brand names while using the same layout.
    """
    if len(data) < HEADER_SIZE + FOOTER_SIZE:
        return False
    if data[_HEADER_MARKER_OFFSET] != HEADER_END_MARKER:
        return False
    (record_count,) = struct.unpack_from("<H", data, _RECORD_COUNT_OFFSET)
    return len(data) == HEADER_SIZE + record_count * RECORD_SIZE + FOOTER_SIZE


def parse(data: bytes) -> SessionFile:
    """Parse one YH550 `.BYS` file.

    Raises InvalidPAPFile when the structure is wrong. Values that are merely
    unexpected are collected into `SessionFile.warnings` — see the note on unknown
    bytes below.
    """
    if len(data) < HEADER_SIZE + FOOTER_SIZE:
        raise InvalidPAPFile(
            f"file is {len(data)} bytes, too short for a {HEADER_SIZE}-byte header plus footer"
        )

    marker = data[_HEADER_MARKER_OFFSET]
    if marker != HEADER_END_MARKER:
        raise InvalidPAPFile(
            f"expected header end marker 0x{HEADER_END_MARKER:02X} at offset "
            f"{_HEADER_MARKER_OFFSET}, found 0x{marker:02X}"
        )

    (record_count,) = struct.unpack_from("<H", data, _RECORD_COUNT_OFFSET)
    expected_size = HEADER_SIZE + record_count * RECORD_SIZE + FOOTER_SIZE
    if len(data) != expected_size:
        raise InvalidPAPFile(
            f"header declares {record_count} records so the file should be "
            f"{expected_size} bytes, but it is {len(data)}"
        )

    warnings: list[str] = []

    footer = data[-1]
    if footer != FOOTER_MARKER:
        # Not fatal: the therapy data has already been validated by the size check.
        warnings.append(
            f"footer is 0x{footer:02X}, expected 0x{FOOTER_MARKER:02X}"
        )

    start = _read_datetime(data, 0, "session start")
    end = _read_datetime(data, 6, "session end")
    if end < start:
        raise InvalidPAPFile(f"session end {end} is before session start {start}")
    if start.year > _MAX_YEAR:
        raise InvalidPAPFile(f"session start year {start.year} is out of range")

    mode = data[12]
    if mode not in (0, 1):
        warnings.append(f"unknown therapy mode byte 0x{mode:02X}")

    serial_raw = data[_SERIAL_OFFSET : _SERIAL_OFFSET + _SERIAL_LENGTH]
    serial = serial_raw.decode("ascii", errors="replace").strip("\x00 \xff").strip()
    if not serial:
        serial = "unknown"
        warnings.append("serial number field is empty")

    written_count = _count_written_records(data, record_count)
    if written_count < record_count:
        warnings.append(
            f"header declares {record_count} records but only {written_count} were "
            f"written; the remainder is erased flash and has been discarded "
            f"(session ended without the device finishing its write)"
        )

    records: list[MinuteRecord] = []
    unknown_minutes: list[int] = []

    for minute in range(written_count):
        offset = HEADER_SIZE + minute * RECORD_SIZE
        pressure, u1, u2, oai, hi, cai, u6, u7, u8, leak = struct.unpack_from(
            "10B", data, offset
        )
        # Bytes 1, 2, 7 and 8 are usually zero but genuinely are not always: real
        # files carry a 1 in each of them across the last few records of a session.
        # The upstream reference implementation raises here, which makes it fail on
        # its own sample data. Keep the values, note them once, and carry on.
        if u1 or u2 or u6 or u7 or u8:
            unknown_minutes.append(minute)

        records.append(
            MinuteRecord(
                minute=minute,
                pressure=pressure / 10,
                leak=leak / 10,
                oai=oai,
                hi=hi,
                cai=cai,
                u1=u1,
                u2=u2,
                u6=u6,
                u7=u7,
                u8=u8,
            )
        )

    if unknown_minutes:
        shown = ", ".join(str(m) for m in unknown_minutes[:10])
        more = f" (+{len(unknown_minutes) - 10} more)" if len(unknown_minutes) > 10 else ""
        warnings.append(
            f"{len(unknown_minutes)} record(s) carry data in undocumented bytes "
            f"at minute {shown}{more}"
        )

    # The device writes end = start + record_count minutes. A mismatch means our
    # understanding of the layout has drifted, so it is worth surfacing.
    expected_minutes = round((end - start).total_seconds() / 60)
    if expected_minutes != record_count:
        warnings.append(
            f"header timespan is {expected_minutes} minutes but the file holds "
            f"{record_count} records"
        )

    return SessionFile(
        model=MODEL,
        serial=serial,
        start=start,
        end=end,
        mode=mode,
        ramp_minutes=data[13],
        initial_pressure=data[14] / 10,
        min_pressure=data[15] / 10,
        max_pressure=data[16] / 10,
        humidity=data[18],
        device_avg_leak=data[26] / 10,
        device_avg_pressure=data[28] / 10,
        records=records,
        warnings=warnings,
    )
