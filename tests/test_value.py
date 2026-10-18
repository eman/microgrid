"""What a kWh of flexible load costs the site, and how sure that is.

The make-up rule: a kWh the battery does not get from the sun is a
kWh it buys from the grid in the cheapest hour before it is needed; a kWh that
only delays a spill costs that export.
"""

import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from datetime import time as tod
from functools import partial
from zoneinfo import ZoneInfo

import pytest

from engine_fixtures import plant_of
from legacy_dispatch import NativePlant
from microgrid.engine._value import (
    EnergyValue,
    Pricing,
    _cover_runs,
    quantile_traces,
)
from microgrid.engine._value import (
    value_energy as _value_energy,
)
from microgrid.engine.series import Quantiles
from microgrid.plant import GridCharge
from microgrid.plant._battery import Battery, Grid

#: The make-up rule's arithmetic, tested as such: what replaces the kWh
#: (``Pricing.battery_value = "replacement"``; the default prices a battery kWh at
#: its stored cost).
value_energy = partial(_value_energy, battery_value="replacement")

PACIFIC = ZoneInfo("America/Los_Angeles")
SLOT = timedelta(minutes=10)
OFF_PEAK, PART_PEAK, PEAK = 0.373, 0.430, 0.592
EXPORT = 0.09
#: A lossless battery: energy moves one for one.
IDEAL = NativePlant(Battery(10.0, 5.0, 5.0, eta_charge=1.0, eta_discharge=1.0), PACIFIC)
SITE_PLANT = NativePlant(Battery(16.5, 8.4, 9.6), PACIFIC, standby_kw=0.11)


def _day(
    pv_peak: float, *, hours: int = 24, start_hour: float = 7.0
) -> tuple[list[float], list[float]]:
    """Slot hours of day from ``start_hour``, and a PV triangle peaking at 13:00."""
    clock = [(start_hour + i / 6) % 24 for i in range(hours * 6)]
    return clock, [max(0.0, pv_peak * (1 - abs(h - 13) / 6)) for h in clock]


def _tou(clock: list[float]) -> list[float]:
    return [
        PEAK if 16 <= h < 21 else PART_PEAK if 15 <= h < 16 or h >= 21 else OFF_PEAK for h in clock
    ]


def _starts(clock: list[float]) -> list[datetime]:
    first = datetime(2026, 9, 24, 0, tzinfo=PACIFIC) + timedelta(hours=clock[0])
    return [(first + i * SLOT).astimezone(UTC) for i in range(len(clock))]


def _value(
    plant: NativePlant,
    scenarios: list[list[float]],
    soc: float,
    clock: list[float],
    *,
    load: float = 0.5,
    imports: list[float] | None = None,
    exports: list[float] | None = None,
    first_remaining: timedelta | None = None,
    pessimistic: list[float] | None = None,
    weights: list[float] | None = None,
) -> EnergyValue:
    count = len(clock)
    return value_energy(
        plant_of(plant),
        _starts(clock),
        scenarios,
        [load] * count,
        soc,
        imports if imports is not None else _tou(clock),
        exports if exports is not None else [EXPORT] * count,
        step=SLOT,
        first_remaining=first_remaining,
        pessimistic=pessimistic,
        weights=weights,
    )


def _site_day(pv_peak: float, soc: float, hours: int = 24) -> EnergyValue:
    clock, pv = _day(pv_peak, hours=hours)
    return _value(
        SITE_PLANT,
        quantile_traces([Quantiles({0.1: p * 0.6, 0.5: p, 0.9: p * 1.2}) for p in pv]),
        soc,
        clock,
        load=0.7,
        pessimistic=[p * 0.6 for p in pv],
    )


# --- the rule -----------------------------------------------------------------------


def test_pv_that_spills_now_costs_its_export_price_for_certain() -> None:
    clock, pv = _day(8.0, hours=6, start_hour=11)
    found = _value(IDEAL, [pv], 1.0, clock)
    assert found.price[6] == pytest.approx(EXPORT)  # 12:00, full battery, exporting
    assert found.certainty_spill[6] == 1.0


def test_a_charging_battery_that_surely_fills_costs_the_export_it_delays() -> None:
    clock, pv = _day(8.0)
    found = _value(IDEAL, [pv, [p * 1.2 for p in pv]], 0.1, clock)
    nine = 12  # 09:00, charging, not yet exporting
    assert found.price[nine] == pytest.approx(EXPORT)
    assert found.certainty_spill[nine] == 1.0
    assert found.certainty_covered[nine] == 1.0


def test_a_battery_the_sun_leaves_short_costs_the_days_import_price() -> None:
    """Every kWh the battery doesn't get from the sun because
    something else used it is a kWh it buys from the grid, at the import price for
    the day, up until the price gets more expensive."""
    clock, pv = _day(1.0)  # 1.5 kWh to store, 4 kWh of evening to cover
    found = _value(IDEAL, [pv], 0.0, clock)
    noon = 30
    assert found.price[noon] == pytest.approx(OFF_PEAK)
    assert found.certainty_spill[noon] == 0.0
    assert found.certainty_covered[noon] == 0.0


def test_inside_the_peak_with_no_cheaper_hour_left_a_kwh_costs_the_peak() -> None:
    """A battery that runs empty during the peak cannot buy the kWh back before it."""
    clock = [(16 + i / 6) % 24 for i in range(24)]  # 16:00-20:00, all peak
    found = _value(IDEAL, [[0.0] * 24], 0.1, clock)  # 1 kWh: empty by 18:00
    assert found.price[0] == pytest.approx(PEAK)


def test_a_battery_that_lasts_the_evening_is_bought_back_off_peak() -> None:
    clock = [(16 + i / 6) % 24 for i in range(96)]  # 16:00-08:00
    found = _value(IDEAL, [[0.0] * 96], 0.5, clock, load=0.4)  # 5 kWh lasts to 04:30
    assert found.price[0] == pytest.approx(OFF_PEAK)
    assert found.certainty_covered[0] == 1.0  # the sun already did its part


def test_uncertainty_mixes_the_two() -> None:
    """Half the scenarios fill and spill, half fall short: the price is the mixture."""
    clock, sunny = _day(8.0)
    _, dim = _day(1.0)
    found = _value(IDEAL, [sunny, dim], 0.0, clock)
    ten = 18
    assert found.price[ten] == pytest.approx(0.5 * EXPORT + 0.5 * OFF_PEAK)
    assert found.certainty_spill[ten] == pytest.approx(0.5)
    assert found.certainty_covered[ten] == pytest.approx(0.5)


def test_dynamic_prices_buy_back_at_the_cheapest_hour_before_the_need() -> None:
    """No tiers assumed: the shortfall is bought at the cheapest slot before the battery
    runs empty."""
    clock = [(i / 6) % 24 for i in range(144)]
    imports = [0.20 + 0.01 * ((i // 6) % 5) for i in range(144)]
    imports[60:66] = [0.05] * 6  # a cheap hour at 10:00
    imports[108:120] = [0.80] * 12  # an expensive evening at 18:00-20:00
    found = _value(IDEAL, [[0.0] * 144], 0.3, clock, imports=imports)  # empty at 06:00
    assert found.price[0] == pytest.approx(min(imports[:37]))
    assert found.price[40] == pytest.approx(imports[40])  # empty: importing now
    assert found.price[62] == pytest.approx(0.05)  # importing now, at the cheap hour
    assert all(found.price[i] <= imports[i] + 1e-9 for i in range(144))


def test_past_the_horizon_a_kwh_is_bought_back_at_the_days_import_price() -> None:
    """A horizon ending in the peak must not price its last hours as if no cheaper hour
    followed: the kWh is bought back at the day's import price."""
    clock = [(17 + i / 6) % 24 for i in range(144)]  # 17:00 to 17:00, ending in the peak
    found = _value(IDEAL, [[0.0] * 144], 1.0, clock, load=0.1)  # never empties
    assert found.price[-1] == pytest.approx(OFF_PEAK)


def test_export_above_import_costs_that_export() -> None:
    """On evenings where export pays more than import, PV that spills is worth that
    export to a load that takes it."""
    clock, pv = _day(8.0, hours=6, start_hour=11)
    found = _value(IDEAL, [pv], 1.0, clock, exports=[0.9] * len(clock))
    assert found.price[6] == pytest.approx(0.9) and found.price[6] > PEAK


def test_curtailed_pv_is_worth_nothing() -> None:
    capped = NativePlant(IDEAL.battery, PACIFIC, grid=Grid(export_limit_kw=0.0))
    clock, pv = _day(8.0, hours=6, start_hour=11)
    found = _value(capped, [pv], 1.0, clock)
    assert found.price[6] == pytest.approx(0.0, abs=1e-9)
    assert found.certainty_spill[6] == 1.0


def test_a_battery_at_its_reserve_imports_now() -> None:
    reserved = NativePlant(
        Battery(10.0, 5.0, 5.0, eta_charge=1.0, eta_discharge=1.0, reserve_soc=0.5), PACIFIC
    )
    clock = [(19 + i / 6) % 24 for i in range(12)]
    found = _value(reserved, [[0.0] * 12], 0.5, clock)
    assert found.price[0] == pytest.approx(PEAK)


def test_the_plants_top_up_is_the_make_up_when_the_site_has_a_window() -> None:
    """The plant closes a forecast gap from the grid before the peak, so the battery
    covers the peak rather than paying peak import. With the window in the site config
    the purchase is the event that prices a kWh taken from the battery; without it, the
    empty event does."""
    window = NativePlant(IDEAL.battery, PACIFIC, grid_charge=GridCharge(tod(12), tod(15)))
    clock, pv = _day(1.0, hours=48)  # two short days: the battery never fills from the sun
    with_window = _value(window, [pv], 0.0, clock)
    bare = _value(IDEAL, [pv], 0.0, clock)
    morning, evening = 12, 66  # 09:00 and 18:00 of the first day
    assert with_window.price[morning] == pytest.approx(OFF_PEAK)
    assert bare.price[morning] == pytest.approx(OFF_PEAK)  # the same purchase, priced either way
    # The evening: the topped-up battery carries the house, and the kWh is bought back in
    # tomorrow's window; the bare plant runs empty in the peak and imports.
    assert with_window.price[evening] == pytest.approx(OFF_PEAK)
    assert bare.price[evening] == pytest.approx(PEAK)
    assert with_window.certainty_covered[morning] == 1.0  # covered, by the top-up
    assert bare.certainty_covered[morning] == 0.0


def test_a_window_that_never_needs_to_buy_changes_nothing() -> None:
    window = NativePlant(IDEAL.battery, PACIFIC, grid_charge=GridCharge(tod(12), tod(15)))
    clock, pv = _day(8.0)
    with_window, bare = _value(window, [pv], 0.5, clock), _value(IDEAL, [pv], 0.5, clock)
    assert replace(with_window, model=None) == replace(bare, model=None)


# --- the expensive hours and certainty ---------------------------------------------------


def test_the_expensive_hours_come_after_pv_stops_covering_the_house() -> None:
    """The battery must cover the hours after the sun, priced above
    the day's cheapest import. No fixed tiers: read from TariffKit's curve."""
    prices = [0.1] * 3 + [0.5] * 2 + [0.1] * 3
    after_sun = [False] * 3 + [True] * 5
    assert _cover_runs(prices, after_sun, day=4)[0] == (3, 5)
    assert _cover_runs(prices, after_sun, day=4)[3] == (3, 5)
    assert _cover_runs([0.3] * 5, [True] * 5, day=4)[0] == (5, 5)  # flat: nothing to cover
    # A sunny part-peak hour is not one the battery must cover.
    sunny_part_peak = [0.1, 0.4, 0.4, 0.5, 0.5, 0.1]
    sun = [False, False, False, True, True, True]
    assert _cover_runs(sunny_part_peak, sun, day=6)[0] == (3, 5)


def _three(p10: float, p50: float, p90: float) -> list[Quantiles | None]:
    return [Quantiles({0.1: p10, 0.5: p50, 0.9: p90})]


def test_ten_quantile_traces_from_solcasts_three() -> None:
    traces = quantile_traces(_three(0.6, 1.0, 1.2))
    values = [trace[0] for trace in traces]
    assert len(values) == 10 and values == sorted(values)
    assert values[0] == pytest.approx(0.6 - 0.05 * 0.4 / 0.4)  # 5th: below p10, extended
    assert values[4] == pytest.approx(0.6 + 0.35 * 0.4 / 0.4)  # 45th
    assert values[9] == pytest.approx(1.2 + 0.05 * 0.2 / 0.4)  # 95th
    assert quantile_traces(_three(0.0, 0.0, 0.1))[0] == [0.0]  # never negative
    assert Pricing(scenarios=4).quantiles == (0.125, 0.375, 0.625, 0.875)


def test_crossing_percentiles_are_sorted() -> None:
    """Solcast's p10 can exceed its p50 in a slot; the traces stay ordered."""
    traces = quantile_traces(_three(1.1, 1.0, 0.9))
    assert traces == quantile_traces(_three(0.9, 1.0, 1.1))


def test_any_levels_cut_the_scenarios_and_a_missing_side_holds_the_median() -> None:
    """The cut reads the levels given (deciles here, or the median alone), not Solcast's
    three; with no level below the median, the lower scenarios stay at it (nothing is
    known of the downside: the valuation names ``pv_spread``)."""
    deciles = Quantiles({q / 10: q / 10 for q in range(1, 10)})
    traces = quantile_traces([deciles])
    assert [t[0] for t in traces] == pytest.approx([(k + 0.5) / 10 for k in range(10)])
    assert quantile_traces([Quantiles.point(2.0)]) == [[2.0]] * 10
    upper = quantile_traces([Quantiles({0.5: 1.0, 0.9: 1.4})])
    assert [t[0] for t in upper[:5]] == [1.0] * 5 and upper[9][0] == pytest.approx(1.45)
    assert quantile_traces([None]) == [[0.0]] * 10


def test_a_fuller_battery_later_in_the_day_is_cheaper_and_surer() -> None:
    """A low battery in the morning is less sure to fill. Each issue
    sees the measured SOC, so the price falls from issue to issue as it fills."""
    low, high = _site_day(3.5, 0.2), _site_day(3.5, 0.7)
    noon = 30
    assert high.price[noon] < low.price[noon]
    assert high.certainty_spill[noon] > low.certainty_spill[noon]
    assert high.certainty_covered[noon] >= low.certainty_covered[noon]


def test_a_short_winter_day_stays_at_import_and_is_not_covered() -> None:
    winter = _site_day(1.2, 0.1)
    battery = SITE_PLANT.battery
    # 07:00-15:00: never spill-backed. PV charging a battery that runs empty is bought
    # back at the import, less the round trip it no longer makes.
    diverted = battery.dead_band(battery.displaced_value(OFF_PEAK))[0]
    assert diverted < OFF_PEAK
    assert all(diverted - 1e-9 <= price <= OFF_PEAK + 1e-9 for price in winter.price[:48])
    assert min(winter.price[:48]) < OFF_PEAK  # where some scenarios charge from PV
    assert max(winter.certainty_spill[:48]) == 0.0
    assert max(winter.certainty_covered[:48]) == 0.0


def test_equal_weights_make_certainty_exactly_one() -> None:
    """Ten weights of 0.1 must sum to 1.0, not 0.9999999999999999."""
    clock, pv = _day(8.0)
    found = _value(IDEAL, [pv] * 10, 0.5, clock)
    assert found.certainty_spill[12] == 1.0
    assert found.certainty_covered[12] == 1.0


@pytest.mark.parametrize("pv_peak", [2.2, 3.5, 6.0])
@pytest.mark.parametrize("soc", [0.2, 0.55])
def test_nearly_equal_inputs_agree(pv_peak: float, soc: float) -> None:
    a, b = _site_day(pv_peak, soc), _site_day(pv_peak, soc + 1e-4)
    assert a.certainty_spill == b.certainty_spill
    assert a.certainty_covered == b.certainty_covered
    assert max(abs(x - y) for x, y in zip(a.price, b.price, strict=True)) < 0.01


def test_the_pessimistic_price_is_the_p10_trace() -> None:
    clock, pv = _day(6.0)
    dull = [p * 0.3 for p in pv]
    found = _value(IDEAL, [pv], 0.2, clock, pessimistic=dull)
    alone = _value(IDEAL, [dull], 0.2, clock)
    assert found.price_p10 == pytest.approx(alone.price)
    assert _value(IDEAL, [pv], 0.2, clock).price_p10 == found.price


def test_a_short_first_slot_is_priced_like_a_whole_one() -> None:
    clock, pv = _day(8.0)
    full = _value(IDEAL, [pv], 0.3, clock)
    short = _value(IDEAL, [pv], 0.3, clock, first_remaining=timedelta(seconds=5))
    assert short.price[0] == pytest.approx(full.price[0])


def test_the_price_stays_between_curtailment_and_the_dearer_of_import_and_export() -> None:
    clock, pv = _day(4.0, hours=48)
    found = _value(IDEAL, [pv, [p * 0.3 for p in pv]], 0.5, clock)
    imports = _tou(clock)
    assert all(
        -1e-9 <= price <= max(imports[i], EXPORT) + 1e-9 for i, price in enumerate(found.price)
    )
    assert all(0.0 <= c <= 1.0 for c in (*found.certainty_spill, *found.certainty_covered))


def test_bad_inputs() -> None:
    clock = [7.0]
    with pytest.raises(ValueError, match="weight"):
        _value(IDEAL, [[0.0]], 0.5, clock, weights=[0.5, 0.5])
    with pytest.raises(ValueError, match="same length"):
        _value(IDEAL, [[0.0, 0.0]], 0.5, clock)
    with pytest.raises(ValueError, match="scenarios"):
        Pricing(scenarios=0)
    assert value_energy(plant_of(IDEAL), [], [[]], [], 0.5, [], [], step=SLOT).price == ()


def test_a_48_hour_issue_is_quick() -> None:
    started = time.perf_counter()
    _site_day(6.0, 0.2, hours=48)
    assert time.perf_counter() - started < 2.0
