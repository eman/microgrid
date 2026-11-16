"""A plant holding its battery above its configured reserve (eman/microgrid#31).

The plant can keep a floor its configuration does not have: a discharge cut-off raised
on the plant itself. The house then imports though the model's battery could serve it.
A valuation reads the meters as the live value does: where they show the plant holding
its battery and the run's own first slot does not, the measured SOC is the battery's
floor for the run, and ``battery_reserve`` is named.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from engine_fixtures import NOW as EV_NOW
from engine_fixtures import inputs as ev_inputs
from engine_fixtures import reading
from microgrid.engine import (
    Cause,
    Engine,
    EngineConfig,
    Inputs,
    LiveReading,
    Pricing,
    Served,
    Step,
    ValuationKind,
)
from microgrid.engine import series as ts
from microgrid.engine.ledger import Amount, Lots
from microgrid.plant import Dispatch, Plant
from microgrid.plant.examples import example_plant
from test_dc_loads import EV_PLANT, HOLD, _engine, _evening_ev

BERLIN = ZoneInfo("Europe/Berlin")
NOW = datetime(2026, 11, 16, 9, 3, 7, tzinfo=UTC)  # a dull November morning
HOUR = timedelta(hours=1)
MIN = timedelta(minutes=1)
#: The example plant's configured reserve is 0.1; the plant holds at 0.19.
HELD_SOC = 0.19


def _config() -> EngineConfig:
    plant = Plant(example_plant("ac_coupled_hybrid", BERLIN))
    return EngineConfig(plant=plant, pricing=Pricing(scenarios=3), horizon=6 * HOUR)


def _forecast[V](
    value: V, step: timedelta, hours: int, issued: datetime, at: datetime = NOW
) -> ts.Forecast[V]:
    first = at.replace(minute=0, second=0) - HOUR
    count = int(hours * HOUR / step)
    return ts.Forecast(
        tuple(ts.Interval(first + i * step, first + (i + 1) * step, value) for i in range(count)),
        issued,
        "test",
    )


def _issues[V](forecast: ts.Forecast[V], at: datetime = NOW) -> ts.Series[ts.Forecast[V]]:
    return ts.Series.of([(forecast.issued_at, forecast)], known_until=at + HOUR)


def _meters(
    battery_kw: float, grid_kw: float, pv_kw: float = 0.2, at: datetime = NOW
) -> dict[str, ts.Series[float]]:
    """Each meter steady over the last half hour: battery positive charging, grid
    positive importing."""
    readings = {"pv": pv_kw, "grid": grid_kw, "battery": battery_kw}
    return {
        name: ts.Series.of([(at - 30 * MIN, kw)], known_until=at) for name, kw in readings.items()
    }


def _inputs(
    meters: dict[str, ts.Series[float]] | None = None, soc: float = HELD_SOC, at: datetime = NOW
) -> Inputs:
    """PV below the house load all day (0.2 kW against 0.8 kW): nothing refills the
    battery."""
    dull = ts.Quantiles({0.1: 0.1, 0.5: 0.2, 0.9: 0.3})
    return Inputs(
        prices=_issues(_forecast(ts.Price(0.30, 0.08), HOUR, 9, at - 10 * MIN, at), at),
        pv=_issues(_forecast(dull, 30 * MIN, 9, at - HOUR, at), at),
        load=_issues(_forecast(0.8, 30 * MIN, 8, at - 5 * MIN, at), at),
        soc=ts.Series.of([(at - 2 * MIN, soc)], known_until=at),
        dispatch_as_modelled=ts.Series.of([(at - 3 * HOUR, True)], known_until=at),
        meters=meters or {},
        lots=ts.Series.of(
            [(at - 10 * MIN, Lots(at - 10 * MIN, {}, Amount(1.0, 0.1)))], known_until=at
        ),
    )


def _battery_kw(supply: tuple[Step, ...]) -> float:
    return sum(step.kw or 0.0 for step in supply if step.source is Served.BATTERY)


def test_a_battery_held_above_its_reserve_offers_nothing() -> None:
    """The issue's case: the battery idle at 19 % while the house imports 0.6 kW, its
    configured reserve 10 %. Without the meters the model offers the battery's 1 kWh
    above the reserve; with them the battery offers nothing over the horizon, every
    slot is the grid's, and ``battery_reserve`` is named."""
    engine = Engine(_config())
    blind = engine.value(_inputs(), at=NOW)
    assert blind.slots[0].regime == "battery"
    assert _battery_kw(blind.slots[0].supply) > 1.0
    held = engine.value(_inputs(_meters(battery_kw=0.0, grid_kw=0.6)), at=NOW)
    assert held.kind is ValuationKind.STORED_VALUE_V1
    assert held.degraded == (Cause.BATTERY_RESERVE,)
    for slot in held.slots:
        assert slot.regime == "import"
        assert _battery_kw(slot.supply) == 0.0
        assert slot.marginal_cost == pytest.approx(slot.p_import)


def test_a_battery_serving_the_house_is_not_held() -> None:
    """The battery discharging to the house: the meters agree with the model, and the
    configured reserve stands."""
    engine = Engine(_config())
    blind = engine.value(_inputs(), at=NOW)
    serving = engine.value(_inputs(_meters(battery_kw=-0.6, grid_kw=0.0)), at=NOW)
    assert serving.degraded == ()
    assert [s.marginal_cost for s in serving.slots] == [s.marginal_cost for s in blind.slots]
    assert [s.supply for s in serving.slots] == [s.supply for s in blind.slots]


def test_regulation_is_not_a_hold() -> None:
    """An import within the plant's regulation (``observed.regulation_kw``) with the
    battery able to serve it is not a hold."""
    engine = Engine(_config())
    regulating = engine.value(_inputs(_meters(battery_kw=-0.5, grid_kw=0.1)), at=NOW)
    assert Cause.BATTERY_RESERVE not in regulating.degraded


def test_meters_not_yet_settled_say_nothing() -> None:
    """Meters read for less than the smoothing's time constant (5 minutes) are
    unknown: no hold."""
    engine = Engine(_config())
    fresh = {
        name: ts.Series.of([(NOW - 2 * MIN, kw)], known_until=NOW)
        for name, kw in {"pv": 0.2, "grid": 0.6, "battery": 0.0}.items()
    }
    assert Cause.BATTERY_RESERVE not in engine.value(_inputs(fresh), at=NOW).degraded


def test_a_hold_the_model_projects_is_its_own() -> None:
    """In the grid-charge window (01:00-05:00 local, to 60 %) the model's own first
    slot holds the battery: the meters showing the house importing raise no floor."""
    night = datetime(2026, 11, 16, 3, 3, 7, tzinfo=UTC)  # 04:03 CET
    inputs = _inputs(_meters(battery_kw=0.0, grid_kw=0.8, pv_kw=0.0, at=night), at=night)
    valuation = Engine(_config()).value(inputs, at=night)
    assert valuation.model is not None
    assert valuation.model.flows[0][0].dispatch.topup_gap is not None
    assert Cause.BATTERY_RESERVE not in valuation.degraded


def test_with_reserve_raises_the_floor_only() -> None:
    plant = Plant(example_plant("ac_coupled_hybrid", BERLIN))
    raised = plant.with_reserve(0.3)
    assert raised.storage is not None and raised.storage.reserve_soc == 0.3
    assert plant.storage is not None and plant.storage.reserve_soc == 0.1
    with pytest.raises(ValueError, match="reserve"):
        plant.with_reserve(0.05)
    with pytest.raises(ValueError, match="reserve"):
        plant.with_reserve(1.0)
    with pytest.raises(ValueError, match="no battery"):
        Plant(example_plant("pv_only", BERLIN)).with_reserve(0.3)


def test_the_live_value_carries_the_cause_through_its_warm_up() -> None:
    """A live session's first warm-up takes the valuation's price, and names its
    ``battery_reserve`` with it."""
    config = _config()
    engine = Engine(config)
    valuation = engine.value(_inputs(_meters(battery_kw=0.0, grid_kw=0.6)), at=NOW)
    live = engine.live_session().update(
        LiveReading(
            at=NOW + MIN,
            load_kw=0.8,
            soc=HELD_SOC,
            dispatch_as_modelled=True,
            meters={"pv": 0.2, "grid": 0.6, "battery": 0.0},
        ),
        valuation,
        NOW + HOUR,
    )
    assert Cause.BATTERY_RESERVE in live.degraded


@pytest.mark.parametrize(
    ("battery_kw", "grid_kw"),
    [
        (-6.0, 2.0),  # at its 6 kW discharge limit, the grid serving the rest
        (3.0, 3.8),  # charging from the grid
    ],
)
def test_a_battery_that_moves_is_not_held(battery_kw: float, grid_kw: float) -> None:
    """The house importing while the battery gives all it can, or takes from the grid:
    the plant is not holding it."""
    engine = Engine(_config())
    moving = engine.value(_inputs(_meters(battery_kw=battery_kw, grid_kw=grid_kw)), at=NOW)
    assert Cause.BATTERY_RESERVE not in moving.degraded


def _steps(before: float, after: float, change: datetime, at: datetime) -> ts.Series[float]:
    return ts.Series.of([(change - 30 * MIN, before), (change, after)], known_until=at)


#: 05:00 CET: the example plant's grid-charge window (01:00-05:00) ends.
WINDOW_END = datetime(2026, 11, 16, 4, 0, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("grid", "battery", "soc", "seconds"),
    [
        # Held at the target through the window, then released to serve the house.
        ((0.8, 0.0), (0.0, -0.8), 0.6, (5, 15, 30, 45, 60, 120)),
        # A late top-up charging to the window's end, then serving the house.
        ((3.8, 0.0), (3.0, -0.8), 0.55, (400, 430, 460, 490, 520, 560)),
    ],
)
def test_a_battery_starting_to_serve_is_not_held_while_the_averages_lag(
    grid: tuple[float, float], battery: tuple[float, float], soc: float, seconds: tuple[int, ...]
) -> None:
    """As the battery starts discharging and the grid falls, the smoothed battery passes
    through idle while the smoothed grid still imports: the readings in force show no
    hold, so none is told."""
    engine = Engine(_config())
    for second in seconds:
        at = WINDOW_END + timedelta(seconds=second)
        meters = {
            "pv": _steps(0.0, 0.0, WINDOW_END, at),
            "grid": _steps(*grid, WINDOW_END, at),
            "battery": _steps(*battery, WINDOW_END, at),
        }
        valuation = engine.value(_inputs(meters, soc=soc, at=at), at=at)
        assert Cause.BATTERY_RESERVE not in valuation.degraded, second


def test_a_hybrid_at_its_rating_cannot_serve_so_is_not_held() -> None:
    """DC PV fills the hybrid's 6 kW AC rating: the battery behind it cannot reach the
    house, so its standing idle while the grid imports is no hold."""
    plant = Plant(example_plant("dc_coupled_hybrid", BERLIN))
    config = EngineConfig(plant=plant, pricing=Pricing(scenarios=3), horizon=6 * HOUR)
    noon = datetime(2026, 6, 16, 11, 3, 7, tzinfo=UTC)
    for pv_kw in (6.0, 6.3, 6.6):
        meters = _meters(battery_kw=0.0, grid_kw=3.0, pv_kw=pv_kw, at=noon)
        valuation = Engine(config).value(_inputs(meters, soc=0.9, at=noon), at=noon)
        assert Cause.BATTERY_RESERVE not in valuation.degraded, pv_kw


def test_a_hold_rules_session_the_forecast_does_not_hold_is_the_models() -> None:
    """A client without a DC-bus load forecast values on 0 kW, so no session opens in
    the run; the EV drawing 7 kW at 60 % (the rule holds below 65 %) is the plant's own
    hold rule, judged at the measured draw: no floor is raised."""
    plant = Plant(replace(EV_PLANT.config, dispatch=Dispatch(hold_rules=(HOLD,))))
    readings = {"grid": 8.0, "battery": 0.0, "pv": 0.0, "dc_load": 7.0}
    meters = {
        name: ts.Series.of([(EV_NOW - 30 * MIN, readings[kind])], known_until=EV_NOW)
        for name, kind in plant.meters.items()
    }
    valuation = _engine(plant).value(
        ev_inputs(soc=reading(0.6, as_of=EV_NOW), dc_load=_evening_ev(0.0), meters=meters),
        at=EV_NOW,
    )
    assert Cause.BATTERY_RESERVE not in valuation.degraded


def _live_inputs(at: datetime, battery_kw: float, grid_kw: float) -> Inputs:
    readings = {"pv": 0.2, "grid": grid_kw, "battery": battery_kw}
    return Inputs(
        prices=None,
        soc=ts.Series.of([(NOW - 30 * MIN, HELD_SOC)], known_until=at),
        dispatch_as_modelled=ts.Series.of([(NOW - 3 * HOUR, True)], known_until=at),
        meters={
            n: ts.Series.of([(NOW - 30 * MIN, kw)], known_until=at) for n, kw in readings.items()
        },
        measured_load=ts.Series.of([(NOW - 30 * MIN, 0.8)], known_until=at),
    )


def test_the_warm_live_value_follows_the_hold() -> None:
    """Still held: the house imports, and the live value prices the import. Released
    (the battery serving the house): the live value runs on the configured plant, not
    the valuation's floor, so the battery is the house's again (on the floor, it would
    read the battery as empty and price the import)."""
    engine = Engine(_config())
    valuation = engine.value(_inputs(_meters(battery_kw=0.0, grid_kw=0.6)), at=NOW)
    assert Cause.BATTERY_RESERVE in valuation.degraded
    at, until = NOW + MIN, NOW + 30 * MIN
    held = engine.live(_live_inputs(at, 0.0, 0.6), at=at, valuation=valuation, valid_until=until)
    assert held.regime == "import"
    assert held.marginal_cost == pytest.approx(0.30)
    released = engine.live(
        _live_inputs(at, -0.6, 0.0), at=at, valuation=valuation, valid_until=until
    )
    assert Cause.BATTERY_RESERVE not in released.degraded
    assert released.regime == "battery"
    assert released.marginal_cost is not None and released.marginal_cost < 0.30


@pytest.mark.parametrize("grid_kw", [0.6, 3.0, 5.5, 6.5, 8.0])
def test_a_hold_is_told_however_much_the_house_draws(grid_kw: float) -> None:
    """The battery held while the house draws more than its 6 kW limit is still held:
    able to serve is room to give more than the plant's regulation, not all of it."""
    valuation = Engine(_config()).value(_inputs(_meters(battery_kw=0.0, grid_kw=grid_kw)), at=NOW)
    assert Cause.BATTERY_RESERVE in valuation.degraded


def test_meters_not_yet_reporting_a_release_are_not_a_hold() -> None:
    """05:00:05, the grid-charge window just ended: the meters have not yet reported the
    battery starting to serve, and still read it held. The model held it a moment ago,
    so nothing is raised; a reading that still shows the hold past ``JUMP_CONFIRM`` is
    one."""
    engine = Engine(_config())
    for seconds, told in ((5, False), (25, False), (60, True)):
        at = WINDOW_END + timedelta(seconds=seconds)
        meters = _meters(battery_kw=0.0, grid_kw=0.8, pv_kw=0.0, at=at)
        valuation = engine.value(_inputs(meters, soc=0.6, at=at), at=at)
        assert (Cause.BATTERY_RESERVE in valuation.degraded) is told, seconds
