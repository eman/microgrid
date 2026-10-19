"""A battery's rating as its meters show it: a synthetic 10 kWh battery, 95 % each way,
charging at 3 kW and discharging at 2 kW."""

import math
from datetime import UTC, datetime, timedelta

import pytest

from microgrid.cli.seen import BatterySeen, battery_seen, rounded
from microgrid.engine.series import Series

T0 = datetime(2026, 10, 1, tzinfo=UTC)
HOUR = timedelta(hours=1)
CAPACITY = 10.0
ETA = 0.95


def _recorded() -> tuple[Series[float], Series[float]]:
    """A minute at a time: idle, 2 h charging at 3 kW, idle, 2 h discharging at 2 kW,
    over a day; the SOC from the energy stored."""
    power: list[tuple[datetime, float]] = []
    soc: list[tuple[datetime, float]] = []
    stored = 0.2 * CAPACITY
    for minute in range(24 * 60):
        at = T0 + timedelta(minutes=minute)
        hour = minute // 60
        kw = 3.0 if 10 <= hour < 12 else -2.0 if 18 <= hour < 20 else 0.0
        power.append((at, kw))
        soc.append((at, stored / CAPACITY))
        stored += (kw * ETA if kw > 0 else kw / ETA) / 60
    end = T0 + 24 * HOUR
    return Series.of(power, known_until=end), Series.of(soc, known_until=end)


def test_the_most_seen_and_the_capacity() -> None:
    power, soc = _recorded()
    seen = battery_seen(power, soc, T0, T0 + 24 * HOUR)
    assert seen.charge_kw == pytest.approx(3.0)
    assert seen.discharge_kw == pytest.approx(2.0)
    assert seen.capacity_kwh == pytest.approx(CAPACITY, rel=0.02)  # losses cancel
    assert rounded(seen) == BatterySeen(3.0, 2.0, round(seen.capacity_kwh or 0.0, 1))


def test_without_the_soc_or_its_travel_no_capacity() -> None:
    power, _ = _recorded()
    assert battery_seen(power, None, T0, T0 + 24 * HOUR).capacity_kwh is None
    still = Series.of([(T0, 0.5)], known_until=T0 + 24 * HOUR)
    assert battery_seen(power, still, T0, T0 + 24 * HOUR).capacity_kwh is None


def test_a_coarse_soc_on_a_slow_large_battery() -> None:
    """A 40 kWh battery whose SOC reports in 1 % steps, discharging then charging at
    0.3 kW for a day each: most hours the SOC does not step, and their energy counts."""
    capacity = 40.0
    power: list[tuple[datetime, float]] = []
    soc: list[tuple[datetime, float]] = []
    stored = 0.9 * capacity
    for minute in range(48 * 60):
        at = T0 + timedelta(minutes=minute)
        kw = -0.3 if minute < 24 * 60 else 0.3
        power.append((at, kw))
        soc.append((at, math.floor(stored / capacity * 100) / 100))
        stored += kw / 60
    end = T0 + 48 * HOUR
    seen = battery_seen(Series.of(power, known_until=end), Series.of(soc, known_until=end), T0, end)
    assert seen.capacity_kwh == pytest.approx(capacity, rel=0.05)


@pytest.mark.parametrize("tops_out", [1.0, 0.99])
def test_hours_held_full_say_nothing(tops_out: float) -> None:
    """A 10 kWh battery charged to full, then held there 6 hours while its meter shows
    0.2 kW still going in (balancing): those hours count no energy, whether its SOC
    sensor reads full as 100 % or tops out at 99 %."""
    power: list[tuple[datetime, float]] = []
    soc: list[tuple[datetime, float]] = []
    stored = 0.2 * CAPACITY
    for minute in range(24 * 60):
        at = T0 + timedelta(minutes=minute)
        hour = minute // 60
        kw = 2.0 if 8 <= hour < 12 else 0.2 if 12 <= hour < 18 else -2.0 if 18 <= hour < 22 else 0.0
        power.append((at, kw))
        soc.append((at, min(tops_out, math.floor(stored / CAPACITY * 100) / 100)))
        stored = min(CAPACITY, stored + kw / 60)
    end = T0 + 24 * HOUR
    seen = battery_seen(Series.of(power, known_until=end), Series.of(soc, known_until=end), T0, end)
    assert seen.capacity_kwh == pytest.approx(CAPACITY, rel=0.03)


def test_limits_round_up() -> None:
    assert rounded(BatterySeen(4.91, 5.0, 13.46)) == BatterySeen(5.0, 5.0, 13.5)
    assert rounded(BatterySeen(None, None, None)) == BatterySeen(None, None, None)
