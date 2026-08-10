"""Coverage for the derived numbers a user reads off the dashboard.

Do not delete: these are the figures someone might show a doctor. Expected values
here are computed by hand on purpose — deriving them from the code under test would
make the suite follow a bug instead of catching it.
"""

from __future__ import annotations

from datetime import date, datetime

import pytest

from app.metrics import night_date, percentile, summarise
from app.parsers import yh550
from tests.conftest import build_bys, build_record


def parse_session(records, **kwargs):
    return yh550.parse(build_bys(records, **kwargs))


class TestNightDate:
    """A night is noon-to-noon, so a session starting after midnight belongs to the
    previous calendar day. Getting this wrong shifts an entire year of nightly
    charts by one day without any visible error."""

    def test_evening_session_belongs_to_that_day(self):
        assert night_date(datetime(2025, 8, 21, 22, 30)) == date(2025, 8, 21)

    def test_after_midnight_belongs_to_the_previous_day(self):
        assert night_date(datetime(2025, 8, 22, 0, 42)) == date(2025, 8, 21)

    def test_early_morning_still_belongs_to_the_previous_day(self):
        assert night_date(datetime(2025, 8, 22, 5, 48)) == date(2025, 8, 21)

    def test_noon_is_the_boundary(self):
        assert night_date(datetime(2025, 8, 22, 11, 59)) == date(2025, 8, 21)
        assert night_date(datetime(2025, 8, 22, 12, 0)) == date(2025, 8, 22)


class TestPercentile:
    def test_known_values(self):
        values = [1.0, 2.0, 3.0, 4.0, 5.0]
        assert percentile(values, 0) == 1.0
        assert percentile(values, 50) == 3.0
        assert percentile(values, 100) == 5.0

    def test_interpolates_between_samples(self):
        # rank = 0.9 * (10-1) = 8.1 -> between the 9th and 10th values
        values = [float(v) for v in range(1, 11)]
        assert percentile(values, 90) == pytest.approx(9.1)

    def test_unsorted_input_is_handled(self):
        assert percentile([5.0, 1.0, 3.0], 50) == 3.0

    def test_empty_and_single(self):
        assert percentile([], 90) == 0.0
        assert percentile([7.5], 90) == 7.5


class TestSummarise:
    def test_ahi_counts_all_three_event_types(self):
        """AHI = (OAI + HI + CAI) / minutes * 60.

        60 minutes with 1 obstructive, 2 hypopnoea and 1 central event is 4 events
        in one hour, so AHI is exactly 4.0.
        """
        records = [build_record() for _ in range(60)]
        records[10] = build_record(oai=1)
        records[20] = build_record(hi=2)
        records[30] = build_record(cai=1)

        summary = summarise(parse_session(records))

        assert summary.duration_minutes == 60, "wrong record count invalidates every index below"
        assert summary.oai_total == 1
        assert summary.hi_total == 2
        assert summary.cai_total == 1
        assert summary.ahi == pytest.approx(4.0)

    def test_per_type_indices_are_reported_separately(self):
        """120 minutes with 3 obstructive events is 1.5/hour."""
        records = [build_record() for _ in range(120)]
        records[5] = build_record(oai=3)

        summary = summarise(parse_session(records))

        assert summary.duration_minutes == 120
        assert summary.oai_index == pytest.approx(1.5)
        assert summary.hi_index == 0.0
        assert summary.cai_index == 0.0

    def test_pressure_statistics(self):
        pressures = [4.0, 5.0, 6.0, 7.0, 8.0]
        records = [build_record(pressure=p) for p in pressures]

        summary = summarise(parse_session(records))

        assert summary.duration_minutes == 5
        assert summary.avg_pressure == pytest.approx(6.0)
        assert summary.max_pressure == 8.0
        # rank = 0.9 * 4 = 3.6 -> 7.0 + 0.6*(8.0-7.0)
        assert summary.p90_pressure == pytest.approx(7.6)

    def test_leak_statistics(self):
        records = [build_record(leak=leak) for leak in (0.0, 2.0, 10.0)]

        summary = summarise(parse_session(records))

        assert summary.duration_minutes == 3
        assert summary.avg_leak == pytest.approx(4.0)
        assert summary.max_leak == 10.0

    def test_night_comes_from_the_session_start(self):
        records = [build_record() for _ in range(30)]
        summary = summarise(
            parse_session(records, start=datetime(2025, 8, 22, 0, 42, 0))
        )
        assert summary.night == date(2025, 8, 21)

    def test_zero_length_session_does_not_divide_by_zero(self):
        """The device can write a header with no records if therapy stops instantly."""
        summary = summarise(parse_session([]))

        assert summary.duration_minutes == 0
        assert summary.ahi == 0.0
        assert summary.avg_pressure == 0.0
        assert summary.max_leak == 0.0
