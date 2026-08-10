"""Structural and value-level coverage for the YH550 parser.

Do not delete this file: it pins the rule that undocumented bytes are a *warning*,
not an error. The upstream reference implementation raises on them and consequently
fails on its own sample data — if this test goes away, someone will reintroduce that
behaviour and imports will start rejecting valid files.
"""

from __future__ import annotations

import struct
from datetime import datetime

import pytest

from app.parsers import yh550
from app.parsers.base import InvalidPAPFile
from app.parsers.registry import parse as registry_parse
from tests.conftest import TEST_SERIAL, build_bys, build_record


class TestHeader:
    def test_reads_every_documented_header_field(self):
        data = build_bys(
            [build_record()],
            start=datetime(2025, 8, 21, 22, 30, 15),
            serial=TEST_SERIAL,
            mode=1,
            ramp_minutes=10,
            initial_pressure=4.0,
            min_pressure=5.0,
            max_pressure=15.0,
            humidity=3,
            device_avg_leak=0.9,
            device_avg_pressure=5.8,
        )

        session = yh550.parse(data)

        assert session.model == "YH550"
        assert session.serial == TEST_SERIAL
        assert session.start == datetime(2025, 8, 21, 22, 30, 15)
        assert session.end == datetime(2025, 8, 21, 22, 31, 15)
        assert session.mode == 1
        assert session.mode_name == "apap"
        assert session.ramp_minutes == 10
        assert session.initial_pressure == 4.0
        assert session.min_pressure == 5.0
        assert session.max_pressure == 15.0
        assert session.humidity == 3
        assert session.device_avg_leak == 0.9
        assert session.device_avg_pressure == 5.8

    def test_mode_zero_is_cpap(self):
        session = yh550.parse(build_bys([build_record()], mode=0))
        assert session.mode_name == "cpap"

    def test_serial_is_stripped_of_padding(self):
        data = bytearray(build_bys([build_record()]))
        data[30:46] = b"YH550A-1\x00\x00\x00\x00\x00\x00\x00\x00"
        assert yh550.parse(bytes(data)).serial == "YH550A-1"

    def test_empty_serial_warns_rather_than_failing(self):
        data = bytearray(build_bys([build_record()]))
        data[30:46] = b"\x00" * 16
        session = yh550.parse(bytes(data))
        assert session.serial == "unknown"
        assert any("serial" in w for w in session.warnings), session.warnings


class TestRecords:
    def test_scales_pressure_and_leak_by_ten(self):
        """Guards the x10 decimal encoding: a raw byte of 52 means 5.2 cmH2O."""
        records = [
            build_record(pressure=5.2, leak=4.1),
            build_record(pressure=12.0, leak=0.0),
        ]

        session = yh550.parse(build_bys(records))

        assert len(session.records) == 2, "parser dropped records; check the size maths"
        assert session.records[0].pressure == 5.2
        assert session.records[0].leak == 4.1
        assert session.records[1].pressure == 12.0
        assert session.records[1].leak == 0.0

    def test_event_counts_land_in_the_right_fields(self):
        """oai/hi/cai sit at bytes 3/4/5 — swapping them silently skews every index."""
        session = yh550.parse(build_bys([build_record(oai=1, hi=2, cai=3)]))

        assert len(session.records) == 1, "no record parsed; the rest asserts nothing"
        record = session.records[0]
        assert (record.oai, record.hi, record.cai) == (1, 2, 3)

    def test_minute_offsets_are_sequential(self):
        session = yh550.parse(build_bys([build_record() for _ in range(5)]))

        assert len(session.records) == 5
        assert [r.minute for r in session.records] == [0, 1, 2, 3, 4]

    def test_records_are_not_truncated_for_a_long_session(self):
        """A 483-minute session is normal; anything that reads only one byte of the
        count would silently keep 227."""
        count = 483
        session = yh550.parse(build_bys([build_record() for _ in range(count)]))

        assert len(session.records) == count


class TestUndocumentedBytes:
    """The behaviour this project deliberately differs from upstream on."""

    def test_nonzero_unknown_bytes_do_not_raise(self):
        records = [
            build_record(),
            build_record(u1=1),
            build_record(u2=1),
            build_record(u7=1),
            build_record(u8=1),
        ]

        session = yh550.parse(build_bys(records))

        assert len(session.records) == 5, (
            "parser rejected or dropped records carrying undocumented bytes — this is "
            "the upstream bug this project exists to avoid"
        )

    def test_unknown_byte_values_are_preserved_for_later_decoding(self):
        session = yh550.parse(build_bys([build_record(u1=1, u2=2, u6=3, u7=4, u8=5)]))

        assert len(session.records) == 1
        record = session.records[0]
        assert (record.u1, record.u2, record.u6, record.u7, record.u8) == (1, 2, 3, 4, 5)
        assert record.has_unknown_data

    def test_unknown_bytes_are_reported_as_a_warning(self):
        session = yh550.parse(build_bys([build_record(), build_record(u7=1)]))

        assert any("undocumented" in w for w in session.warnings), (
            f"expected a warning naming the undocumented bytes, got {session.warnings}"
        )

    def test_all_zero_records_produce_no_unknown_byte_warning(self):
        session = yh550.parse(build_bys([build_record() for _ in range(3)]))

        assert not any("undocumented" in w for w in session.warnings), session.warnings


class TestInterruptedWrites:
    """Sessions the device never finished writing.

    Seen on a real card: the header reserved 363 records, 200 were written, and the
    rest was erased flash. Read literally those bytes decode as 25.5 cmH2O and 255
    events per minute, which pushed one night's AHI to 20,611. Do not delete these
    tests — this is the failure mode that silently destroys a year of charts.
    """

    UNWRITTEN = bytes([0xFF] * 10)

    def test_trailing_erased_records_are_discarded(self):
        real = [build_record(pressure=8.0, leak=1.0) for _ in range(200)]
        data = build_bys(real + [self.UNWRITTEN] * 163, footer=0xFF)

        session = yh550.parse(data)

        assert len(session.records) == 200, (
            f"expected the 163 erased records to be dropped, kept "
            f"{len(session.records)}"
        )
        assert max(r.pressure for r in session.records) == 8.0, (
            "an erased record leaked into the data as 25.5 cmH2O"
        )
        assert all(r.oai == 0 for r in session.records)

    def test_the_discarding_is_reported(self):
        data = build_bys(
            [build_record() for _ in range(10)] + [self.UNWRITTEN] * 5, footer=0xFF
        )

        session = yh550.parse(data)

        assert any("erased flash" in w for w in session.warnings), session.warnings

    def test_a_record_torn_mid_write_is_dropped(self):
        """The device was interrupted partway through a record: real values in the
        leading bytes, erased flash in the rest."""
        torn = bytes([0x50, 0, 0, 0, 0, 0, 0, 0xFF, 0xFF, 0xFF])
        data = build_bys(
            [build_record() for _ in range(10)] + [torn] + [self.UNWRITTEN] * 4,
            footer=0xFF,
        )

        session = yh550.parse(data)

        assert len(session.records) == 10, (
            f"the torn boundary record should have been dropped, got "
            f"{len(session.records)} records"
        )

    def test_a_complete_file_is_left_alone(self):
        """Guard against over-trimming: nothing here is erased, so nothing goes."""
        data = build_bys([build_record(pressure=8.0) for _ in range(50)])

        session = yh550.parse(data)

        assert len(session.records) == 50
        assert not any("erased flash" in w for w in session.warnings), session.warnings

    def test_erased_run_in_the_middle_is_kept_and_not_trimmed(self):
        """Only a trailing run is trimmed. A gap in the middle means something other
        than an interrupted write, and hiding it would be worse than surfacing it."""
        data = build_bys(
            [build_record() for _ in range(5)]
            + [self.UNWRITTEN] * 3
            + [build_record() for _ in range(5)]
        )

        session = yh550.parse(data)

        assert len(session.records) == 13, (
            "a mid-file erased run must not be trimmed away silently"
        )
        assert any("undocumented" in w for w in session.warnings), session.warnings

    def test_a_fully_erased_body_yields_no_records(self):
        data = build_bys([self.UNWRITTEN] * 20, footer=0xFF)

        session = yh550.parse(data)

        assert session.records == []
        assert any("erased flash" in w for w in session.warnings), session.warnings


class TestStructuralFailures:
    def test_rejects_wrong_header_marker(self):
        with pytest.raises(InvalidPAPFile, match="header end marker"):
            yh550.parse(build_bys([build_record()], header_marker=0x00))

    def test_rejects_size_that_disagrees_with_record_count(self):
        """The size identity is the strongest signal we have that the layout is right."""
        with pytest.raises(InvalidPAPFile, match="should be"):
            yh550.parse(build_bys([build_record()], record_count=99))

    def test_rejects_file_shorter_than_the_header(self):
        with pytest.raises(InvalidPAPFile, match="too short"):
            yh550.parse(b"\x00" * 10)

    def test_rejects_impossible_date(self):
        data = bytearray(build_bys([build_record()]))
        data[1] = 13  # month 13
        with pytest.raises(InvalidPAPFile, match="session start"):
            yh550.parse(bytes(data))

    def test_rejects_end_before_start(self):
        data = build_bys(
            [build_record()],
            start=datetime(2025, 8, 21, 22, 30, 0),
            end=datetime(2025, 8, 21, 21, 0, 0),
            record_count=1,
        )
        with pytest.raises(InvalidPAPFile, match="before session start"):
            yh550.parse(data)

    def test_wrong_footer_is_a_warning_not_an_error(self):
        session = yh550.parse(build_bys([build_record()], footer=0x00))

        assert len(session.records) == 1
        assert any("footer" in w for w in session.warnings), session.warnings

    def test_timespan_mismatch_is_a_warning_not_an_error(self):
        data = build_bys(
            [build_record() for _ in range(3)],
            start=datetime(2025, 8, 21, 22, 0, 0),
            end=datetime(2025, 8, 21, 23, 0, 0),  # claims 60 minutes, holds 3 records
        )

        session = yh550.parse(data)

        assert len(session.records) == 3
        assert any("timespan" in w for w in session.warnings), session.warnings


class TestRegistry:
    def test_detects_yh550_by_layout(self):
        session = registry_parse(build_bys([build_record(pressure=6.5)]))
        assert session.model == "YH550"
        assert session.records[0].pressure == 6.5

    def test_unrecognised_layout_is_rejected(self):
        with pytest.raises(InvalidPAPFile, match="not a recognised session file"):
            registry_parse(b"PK\x03\x04this is a zip, not a session file")

    def test_detector_does_not_depend_on_the_serial_string(self):
        """Rebadged units carry a reseller name; layout is the only reliable signal."""
        data = build_bys([build_record()], serial="SOMEBRAND-000001")
        assert yh550.looks_like_yh550(data)


@pytest.mark.realdata
class TestAgainstRealFiles:
    def test_every_real_file_parses(self, real_files):
        for path in real_files:
            session = yh550.parse(path.read_bytes())
            assert session.records, f"{path.name} parsed to zero records"

    def test_computed_averages_match_what_the_machine_reported(self, real_files):
        """Cross-check: our maths against the device's own summary bytes.

        A systematic scaling or offset error shows up here even when every structural
        check passes.
        """
        from app.metrics import summarise

        checked = 0
        for path in real_files:
            session = yh550.parse(path.read_bytes())
            if len(session.records) < 30:
                continue
            summary = summarise(session)
            checked += 1
            assert abs(summary.avg_pressure - session.device_avg_pressure) < 1.0, (
                f"{path.name}: computed average pressure {summary.avg_pressure:.2f} vs "
                f"device-reported {session.device_avg_pressure:.2f}"
            )

        assert checked > 0, "no sessions long enough to cross-check; add more fixtures"

    def test_size_identity_holds_for_real_files(self, real_files):
        for path in real_files:
            data = path.read_bytes()
            (count,) = struct.unpack_from("<H", data, 46)
            assert len(data) == 51 + count * 10 + 1, (
                f"{path.name} does not match the 51 + n*10 + 1 layout — a new firmware "
                f"revision may have changed the format"
            )
