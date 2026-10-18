"""Smoothing and the surplus of the slot under way."""

import math
from datetime import UTC, datetime, timedelta

import pytest

from engine_fixtures import EXAMPLE_PLANT, plant_of, site_meters
from microgrid.engine import Regime
from microgrid.engine._live import (
    JUMP_CONFIRM,
    WINDOW_TAUS,
    LiveSurplus,
    Smoothed,
    importing_now,
    revalue,
    smoothed,
    window,
)
from microgrid.engine._timeline import slot_floor
from microgrid.engine._value import value_energy
from microgrid.engine.series import SLOT, Series

PLANT = EXAMPLE_PLANT
T0 = datetime(2026, 9, 23, 19, 0, tzinfo=UTC)  # 12:00 PDT
NOW = T0 + timedelta(minutes=3)
EXPORT = 0.092


# --- smoothing (a function of the series) ------------------------------------------

TAU = timedelta(minutes=5)


def _series(*points: tuple[timedelta, float | None], until: timedelta) -> Series[float]:
    return Series.of([(T0 + d, v) for d, v in points], known_until=T0 + until)


def test_the_first_sample_is_the_average() -> None:
    found = smoothed(_series((timedelta(0), 2.0), until=timedelta(0)), T0, TAU)
    assert found == Smoothed(2.0, warm=False, first=True, since=T0)


def test_a_value_moves_the_average_by_how_long_it_held() -> None:
    series = _series((timedelta(0), 0.0), (TAU, 1.0), until=2 * TAU)
    assert smoothed(series, T0 + TAU, TAU).value == 0.0  # 1.0 has held for no time yet
    assert smoothed(series, T0 + 2 * TAU, TAU).value == pytest.approx(1 - math.exp(-1))
    long = _series((timedelta(0), 0.0), (timedelta(seconds=1), 1.0), until=timedelta(hours=2))
    assert smoothed(long, T0 + timedelta(hours=2), TAU).value == pytest.approx(1.0, abs=1e-9)


def test_samples_weigh_by_time_not_count() -> None:
    one = _series((timedelta(0), 0.0), (timedelta(seconds=30), 1.0), until=TAU)
    two = _series(
        (timedelta(0), 0.0),
        (timedelta(seconds=30), 1.0),
        (timedelta(seconds=60), 1.0),
        until=TAU,
    )
    assert smoothed(one, T0 + TAU, TAU).value == pytest.approx(smoothed(two, T0 + TAU, TAU).value)


def test_the_value_at_a_moment_does_not_depend_on_when_it_was_asked_before() -> None:
    series = _series(
        (timedelta(0), 0.0),
        (timedelta(seconds=40), 3.0),
        (timedelta(seconds=95), 1.0),
        until=TAU,
    )
    assert smoothed(series, T0 + TAU, TAU) == smoothed(
        Series(series.samples, T0 + TAU), T0 + TAU, TAU
    )
    # a sample after the moment is not read
    later = Series.of(
        [*((s.at, s.value) for s in series.samples), (T0 + 2 * TAU, 9.0)], known_until=T0 + 2 * TAU
    )
    assert smoothed(later, T0 + TAU, TAU) == smoothed(series, T0 + TAU, TAU)


def test_a_gap_forgets_and_the_run_after_it_is_not_the_first() -> None:
    series = _series(
        (timedelta(0), 1.0),
        (timedelta(seconds=1), None),
        (timedelta(seconds=2), 5.0),
        until=timedelta(seconds=2),
    )
    assert smoothed(series, T0 + timedelta(seconds=2), TAU) == Smoothed(
        5.0, False, False, T0 + timedelta(seconds=2)
    )
    assert smoothed(series, T0 + timedelta(seconds=1), TAU) == Smoothed()


def test_past_known_until_there_is_no_value() -> None:
    series = _series((timedelta(0), 1.0), until=timedelta(seconds=10))
    assert smoothed(series, T0 + timedelta(seconds=11), TAU) == Smoothed()
    assert smoothed(None, T0, TAU) == Smoothed()


def test_zero_tau_is_the_value_in_force_and_warm_at_once() -> None:
    series = _series((timedelta(0), 1.0), (timedelta(seconds=1), 3.0), until=timedelta(seconds=1))
    assert smoothed(series, T0 + timedelta(seconds=1), timedelta(0)) == Smoothed(
        3.0, True, True, T0
    )


def test_the_average_is_warm_after_one_time_constant() -> None:
    series = _series((timedelta(0), 1.0), until=TAU)
    assert not smoothed(series, T0 + timedelta(minutes=4), TAU).warm
    assert smoothed(series, T0 + TAU, TAU).warm


def test_a_stray_reading_is_held_back_until_another_agrees() -> None:
    stray = _series(
        (timedelta(0), 1.0),
        (timedelta(seconds=30), 4.0),
        (timedelta(seconds=35), 1.0),
        until=TAU,
    )
    assert smoothed(stray, T0 + TAU, TAU, jump=0.5).value == pytest.approx(1.0)
    real = _series(
        (timedelta(0), 1.0),
        (timedelta(seconds=30), 4.0),
        (timedelta(seconds=35), 4.1),
        until=TAU,
    )
    held = 1 - math.exp(-((TAU - timedelta(seconds=35)) / TAU))
    assert smoothed(real, T0 + TAU, TAU, jump=0.5).value == pytest.approx(1.0 + held * 3.1)
    moved = smoothed(stray, T0 + TAU, TAU).value  # without a jump limit it moves
    assert moved is not None and moved > 1.0


def test_what_came_before_the_window_weighs_almost_nothing() -> None:
    begin = window(TAU)
    old = _series((timedelta(0), 10.0), (timedelta(seconds=1), 0.0), until=begin + TAU)
    found = smoothed(old, T0 + begin + TAU, TAU)
    # Past the window: warm, and not the first warm-up, whatever came before it.
    assert found == Smoothed(0.0, warm=True, first=False, since=None)
    assert 10.0 * math.exp(-WINDOW_TAUS) < 5e-4


# --- the live surplus (revalue) ------------------------------------------------------------


def _surplus(pv_now: float, load_now: float, soc: float, *, now: datetime = NOW) -> LiveSurplus:
    """The live surplus against a one-slot issue: the window alone."""
    model = value_energy(
        plant_of(PLANT),
        [slot_floor(now)],
        [[pv_now]],
        [load_now],
        soc,
        [0.3],
        [EXPORT],
        step=SLOT,
    ).model
    assert model is not None
    return revalue(model, 0, now, measured_pv_kw=pv_now, measured_load_kw=load_now, soc=soc).surplus


def test_spare_pv_with_a_full_battery_is_surplus_at_the_export_price() -> None:
    result = _surplus(6.0, 1.0, 1.0)
    assert result.kw == pytest.approx(6.0 - 1.0 - PLANT.standby_kw)
    assert result.regime is Regime.EXPORT
    assert result.value == pytest.approx(EXPORT)


def test_a_battery_charging_with_room_is_no_surplus() -> None:
    """Surplus is PV the site cannot use: a battery with room takes all of it."""
    result = _surplus(3.0, 0.57, 0.822)
    assert result.kw == 0.0 and result.regime is Regime.BATTERY and result.value is None


def test_no_spare_pv_is_no_surplus() -> None:
    result = _surplus(0.5, 2.0, 0.5)
    assert result.kw == 0.0
    assert result.regime in {Regime.BATTERY, Regime.IMPORT}


def test_a_battery_that_fills_within_the_window_spills_the_rest() -> None:
    # 0.5 % of room is filled within the 10 minutes; the rest of the spare PV is exported.
    result = _surplus(6.0, 1.0, 0.995)
    assert 0 < result.kw < 6.0 - 1.0 - PLANT.standby_kw
    assert result.regime is Regime.EXPORT


def test_the_window_is_a_full_slot_wherever_now_falls() -> None:
    values = {
        round(_surplus(6.0, 1.0, 0.995, now=T0 + timedelta(seconds=s)).kw, 9)
        for s in (1, 150, 300, 599)
    }
    assert len(values) == 1


# --- Whether the grid serves the house now ---------------------------------------


def test_importing_now_reads_the_meters_as_the_evaluation_does() -> None:
    def importing(pv: float, grid: float, battery: float, soc: float) -> bool:
        plant = plant_of(PLANT)
        return importing_now(plant, soc, now=T0, meters=site_meters(plant, pv, grid, battery))

    assert importing(3.0, 7.0, 0.0, 0.6)  # held for the DC-bus load
    assert not importing(0.2, 0.14, -0.5, 0.8)  # regulation
    assert not importing(6.0, -3.0, 2.0, 0.8)  # exporting
    assert importing(0.0, 4.0, -9.6, 0.7)  # drained into the DC-bus load past its limit


def test_a_step_that_holds_is_taken_without_a_second_reading() -> None:
    """A source that reports changes only sends no second reading of a steady value;
    held back, it counts once it has held JUMP_CONFIRM, and weighs from then."""
    step = _series((timedelta(0), 1.0), (timedelta(seconds=30), 7.5), until=TAU)
    early = smoothed(step, T0 + timedelta(seconds=30) + JUMP_CONFIRM, TAU, jump=0.5)
    assert early.value == pytest.approx(1.0)
    at = T0 + 2 * TAU
    held = 1 - math.exp(-((at - (T0 + timedelta(seconds=30) + JUMP_CONFIRM)) / TAU))
    step = _series((timedelta(0), 1.0), (timedelta(seconds=30), 7.5), until=2 * TAU)
    assert smoothed(step, at, TAU, jump=0.5).value == pytest.approx(1.0 + held * 6.5)


def test_without_smoothing_a_stray_reading_is_still_held_back() -> None:
    """Smoothing off (``tau`` 0) keeps the load-jump rule."""
    stray = _series((timedelta(0), 1.0), (timedelta(seconds=10), 6.0), until=timedelta(seconds=15))
    found = smoothed(stray, T0 + timedelta(seconds=15), timedelta(0), jump=0.5)
    assert found.value == 1.0
    later = _series((timedelta(0), 1.0), (timedelta(seconds=10), 6.0), until=timedelta(minutes=1))
    assert smoothed(later, T0 + timedelta(minutes=1), timedelta(0), jump=0.5).value == 6.0
