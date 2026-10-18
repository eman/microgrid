"""Properties of the slot timeline, in a northern zone and a southern one, each with
DST."""

from datetime import UTC, date, datetime, time, timedelta
from itertools import pairwise
from zoneinfo import ZoneInfo

from hypothesis import given
from hypothesis import strategies as st

from microgrid.engine.series import SLOT, horizon_slots, slot_floor

PACIFIC = ZoneInfo("America/Los_Angeles")
#: The southern hemisphere: DST from October to April.
SYDNEY = ZoneInfo("Australia/Sydney")
moments = st.datetimes(
    min_value=datetime(2020, 1, 1), max_value=datetime(2040, 1, 1), timezones=st.just(UTC)
)
horizons = st.integers(min_value=1, max_value=167).map(lambda h: timedelta(hours=h))


@given(moments)
def test_floor_contains_moment(now: datetime) -> None:
    first = slot_floor(now)
    assert first <= now < first + SLOT
    assert first.tzinfo is UTC
    assert first.minute % 10 == 0 and first.second == 0 and first.microsecond == 0


@given(moments, horizons)
def test_horizon_is_exact_and_contiguous(now: datetime, horizon: timedelta) -> None:
    starts = horizon_slots(now, horizon)
    assert len(starts) == horizon // SLOT
    assert starts[0] == slot_floor(now)
    assert all(b - a == SLOT for a, b in pairwise(starts))


@given(
    st.dates(min_value=date(2020, 1, 1), max_value=date(2040, 1, 1)),
    st.sampled_from([PACIFIC, SYDNEY]),
)
def test_a_local_day_has_its_true_length_in_slots(day: date, zone: ZoneInfo) -> None:
    """Local midnights converted at the edge: DST days get 23 or 25 hours of slots."""
    start = datetime.combine(day, time(), tzinfo=zone).astimezone(UTC)
    end = datetime.combine(day + timedelta(days=1), time(), tzinfo=zone).astimezone(UTC)
    slots = horizon_slots(start, end - start)
    assert len(slots) in (23 * 6, 24 * 6, 25 * 6)
    assert len(slots) == (end - start) // SLOT
    assert slots[-1] + SLOT == end


def test_dst_days_2026() -> None:
    def count(day: date, zone: ZoneInfo = PACIFIC) -> int:
        start = datetime.combine(day, time(), tzinfo=zone).astimezone(UTC)
        end = datetime.combine(day + timedelta(days=1), time(), tzinfo=zone).astimezone(UTC)
        return len(horizon_slots(start, end - start))

    assert count(date(2026, 3, 8)) == 23 * 6
    assert count(date(2026, 11, 1)) == 25 * 6
    # Sydney: the clocks go back on the first Sunday of April, forward on October's.
    assert count(date(2026, 4, 5), SYDNEY) == 25 * 6
    assert count(date(2026, 10, 4), SYDNEY) == 23 * 6
    assert count(date(2026, 7, 1), SYDNEY) == 24 * 6
