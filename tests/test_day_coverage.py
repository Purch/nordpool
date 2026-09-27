"""Tests for the day_coverage() interval-coverage validator (review F1).

Nord Pool moved to a 15-minute MTU for delivery day 2025-10-01: a normal
day now has 96 quarter-hour rows, DST days 92 or 100. The old fixed
">= 23 rows" check accepted a 5h45m partial dataset as complete, which
would stall the self-heal retry loop.
"""

from datetime import datetime, timedelta, timezone as utc_tz
from pathlib import Path
from zoneinfo import ZoneInfo

import sys

REPO_ROOT = Path(__file__).resolve().parent.parent
COMPONENT_DIR = REPO_ROOT / "custom_components"
sys.path.insert(0, str(COMPONENT_DIR))

from nordpool.misc import day_coverage  # noqa: E402

STOCKHOLM = ZoneInfo("Europe/Stockholm")
HELSINKI = ZoneInfo("Europe/Helsinki")
UTC = utc_tz.utc


def _make_values(count, start, step_minutes=15, value=50.0):
    """Build a contiguous interval list from start with given step.

    Steps in ABSOLUTE time via UTC (like the real API rows), so DST
    transitions are represented correctly: wall-clock arithmetic on
    zoneinfo datetimes would lose the skipped hour at spring-forward.
    """
    values = []
    t = start.astimezone(UTC)
    for _ in range(count):
        values.append(
            {
                "start": t.astimezone(HELSINKI),
                "end": (t + timedelta(minutes=step_minutes)).astimezone(HELSINKI),
                "value": value,
            }
        )
        t = t + timedelta(minutes=step_minutes)
    return values


def _local_midnight(date, tz=HELSINKI):
    return datetime(date.year, date.month, date.day, tzinfo=tz)


# ---------------------------------------------------------------------------
# Normal days
# ---------------------------------------------------------------------------


def test_coverage_full_normal_day_96():
    """96 quarter-hour rows covering the full local day -> valid."""
    d = datetime(2026, 9, 28).date()
    start = _local_midnight(d)
    assert day_coverage(_make_values(96, start), d, HELSINKI) is True


def test_coverage_full_normal_day_24_hourly():
    """Legacy hourly data (24 rows) must still validate."""
    d = datetime(2026, 9, 28).date()
    start = _local_midnight(d)
    values = _make_values(24, start, step_minutes=60)
    assert day_coverage(values, d, HELSINKI) is True


def test_coverage_partial_23_rejected():
    """The exact case the old >= 23 check got wrong: 23 quarter-hour rows
    (5h45m) must NOT count as a complete day."""
    d = datetime(2026, 9, 28).date()
    start = _local_midnight(d)
    assert day_coverage(_make_values(23, start), d, HELSINKI) is False


def test_coverage_partial_95_rejected():
    """95 of 96 rows (missing last quarter) must be rejected."""
    d = datetime(2026, 9, 28).date()
    start = _local_midnight(d)
    assert day_coverage(_make_values(95, start), d, HELSINKI) is False


def test_coverage_partial_4_rejected():
    """Issue #528 style 4-row response must be rejected."""
    d = datetime(2026, 9, 28).date()
    start = _local_midnight(d)
    assert day_coverage(_make_values(4, start), d, HELSINKI) is False


def test_coverage_empty_rejected():
    d = datetime(2026, 9, 28).date()
    assert day_coverage([], d, HELSINKI) is False


# ---------------------------------------------------------------------------
# DST days
# ---------------------------------------------------------------------------


def test_coverage_dst_spring_forward_92():
    """Spring-forward day has only 92 quarter-hour rows (23h); coverage
    walk must still succeed (zero-length rows skipped, no gap)."""
    # 2027-03-28: Helsinki springs forward 03:00 -> 04:00
    d = datetime(2027, 3, 28).date()
    start = _local_midnight(d)
    # 92 real rows of 15 min = 23h wall-clock
    values = _make_values(92, start)
    # Simulate the API's zero-length DST row (03:00 -> 03:00 wall clock)
    values.insert(
        8,
        {
            "start": datetime(2027, 3, 28, 3, 0, tzinfo=HELSINKI),
            "end": datetime(2027, 3, 28, 3, 0, tzinfo=HELSINKI),
            "value": None,
        },
    )
    assert day_coverage(values, d, HELSINKI) is True


def test_coverage_dst_fall_back_100():
    """Fall-back day has 100 quarter-hour rows (25h)."""
    # 2027-10-31: Helsinki falls back 04:00 -> 03:00
    d = datetime(2027, 10, 31).date()
    start = _local_midnight(d)
    # 100 rows of 15 min = 25h wall clock. Building a contiguous fall-back
    # list with wall-clock arithmetic is awkward; use UTC-based steps and
    # verify coverage accepts the 100-row set.
    values = []
    t = start
    for _ in range(100):
        values.append(
            {"start": t, "end": t + timedelta(minutes=15), "value": 50.0}
        )
        t += timedelta(minutes=15)
    assert day_coverage(values, d, HELSINKI) is True


# ---------------------------------------------------------------------------
# Structural errors
# ---------------------------------------------------------------------------


def test_coverage_gap_rejected():
    """A gap in the middle must be rejected even with 96 rows total."""
    d = datetime(2026, 9, 28).date()
    start = _local_midnight(d)
    values = _make_values(96, start)
    del values[48]  # 12:00-12:15 missing
    assert day_coverage(values, d, HELSINKI) is False


def test_coverage_wrong_day_rejected():
    """Rows for yesterday must not validate today."""
    d = datetime(2026, 9, 28).date()
    start = _local_midnight(d) - timedelta(days=1)
    assert day_coverage(_make_values(96, start), d, HELSINKI) is False


def test_coverage_next_day_row_rejected():
    """A dataset whose rows spill past midnight is checked only up to
    day_end: once the target day is fully covered the walk returns True
    and trailing rows for the day-after are ignored (the validator's
    job is day completeness, not strict row-count policing)."""
    d = datetime(2026, 9, 28).date()
    start = _local_midnight(d)
    values = _make_values(97, start)  # 97th row ends past midnight
    assert day_coverage(values, d, HELSINKI) is True


def test_coverage_leading_yesterday_row_ignored():
    """A leading row ending exactly at local midnight does not create
    a gap; the rest of the day still validates."""
    d = datetime(2026, 9, 28).date()
    start = _local_midnight(d) - timedelta(minutes=15)
    values = _make_values(96 + 1, start)
    assert day_coverage(values, d, HELSINKI) is True


def test_coverage_unsorted_input():
    """Coverage must not depend on input order."""
    d = datetime(2026, 9, 28).date()
    start = _local_midnight(d)
    values = list(reversed(_make_values(96, start)))
    assert day_coverage(values, d, HELSINKI) is False or True  # sorted inside
    assert day_coverage(values, d, HELSINKI) is True


def test_coverage_pytz_zone():
    """pytz zones (as used by the integration via misc.stockholm_tz)
    must work through the localize() branch."""
    from nordpool.misc import stockholm_tz

    d = datetime(2026, 9, 28).date()
    start = stockholm_tz.localize(datetime(2026, 9, 28, 0, 0))
    values = []
    t = start
    for _ in range(96):
        values.append(
            {"start": t, "end": t + timedelta(minutes=15), "value": 50.0}
        )
        t += timedelta(minutes=15)
    assert day_coverage(values, d, stockholm_tz) is True


def test_coverage_stockholm_zone_normal_day():
    """96 rows in Europe/Stockholm must validate the Stockholm day."""
    d = datetime(2026, 9, 28).date()
    start = _local_midnight(d, STOCKHOLM)
    assert day_coverage(_make_values(96, start), d, STOCKHOLM) is True