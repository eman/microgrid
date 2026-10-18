"""Valuing series at a moment.

The moment as the limit of what is read, sources at other intervals and other slots,
point forecasts, the prices' age, and the inputs a plant does not have. The plant is a
neutral example in a European zone.
"""

import copy
import pickle
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from zoneinfo import ZoneInfo

import pytest

from microgrid.engine import (
    Cause,
    Engine,
    EngineConfig,
    Inputs,
    LiveReading,
    Pricing,
    ValuationKind,
)
from microgrid.engine import series as ts
from microgrid.engine.ledger import Amount, Lots
from microgrid.plant import Plant
from microgrid.plant.examples import example_plant
from test_config import mixed_folded

BERLIN = ZoneInfo("Europe/Berlin")
NOW = datetime(2026, 6, 21, 10, 3, 7, tzinfo=UTC)  # 12:03 CEST
HOUR = timedelta(hours=1)
MIN = timedelta(minutes=1)
HORIZON = timedelta(hours=6)


def _config(name: str = "ac_coupled_hybrid", **overrides: object) -> EngineConfig:
    config = EngineConfig(
        plant=Plant(example_plant(name, BERLIN)), pricing=Pricing(scenarios=3), horizon=HORIZON
    )
    return replace(config, **overrides)  # type: ignore[arg-type]


def _grid(first: datetime, step: timedelta, count: int) -> list[datetime]:
    return [first + i * step for i in range(count)]


def _issues[V](*forecasts: tuple[datetime, ts.Forecast[V]]) -> ts.Series[ts.Forecast[V]]:
    """Forecasts as received: ``(when received, the forecast)``."""
    return ts.Series.of(sorted(forecasts, key=lambda pair: pair[0]), known_until=NOW + HOUR)


def _prices(
    step: timedelta = HOUR, value: float = 0.30, issued: datetime = NOW - 10 * MIN
) -> ts.Forecast[ts.Price]:
    first = NOW.replace(minute=0, second=0) - HOUR
    count = int(9 * HOUR / step)
    return ts.Forecast(
        tuple(ts.Interval(s, s + step, ts.Price(value, 0.08)) for s in _grid(first, step, count)),
        issued,
        "prices",
    )


def _pv(step: timedelta = timedelta(minutes=30), point: bool = False) -> ts.Forecast[ts.Quantiles]:
    first = NOW.replace(hour=0, minute=0, second=0)
    count = int(24 * HOUR / step)

    def at(start: datetime) -> ts.Quantiles:
        hours = (start - first) / HOUR
        kw = max(0.0, 5.0 * (1 - abs(hours - 11) / 6))
        return ts.Quantiles.point(kw) if point else ts.Quantiles({0.1: kw * 0.6, 0.5: kw, 0.9: kw})

    return ts.Forecast(
        tuple(ts.Interval(s, s + step, at(s)) for s in _grid(first, step, count)),
        NOW - HOUR,
        "pv",
    )


def _load(step: timedelta = timedelta(minutes=30), kw: float = 0.6) -> ts.Forecast[float]:
    first = NOW.replace(minute=0, second=0)
    count = int(8 * HOUR / step)
    return ts.Forecast(
        tuple(ts.Interval(s, s + step, kw) for s in _grid(first, step, count)),
        NOW - 5 * MIN,
        "load",
    )


def _inputs(**overrides: object) -> Inputs:
    values: dict[str, object] = {
        "prices": _issues((NOW - 10 * MIN, _prices())),
        "pv": _issues((NOW - HOUR, _pv())),
        "load": _issues((NOW - 5 * MIN, _load())),
        "soc": ts.Series.of([(NOW - 2 * MIN, 0.5)], known_until=NOW),
        "dispatch_as_modelled": ts.Series.of([(NOW - 3 * HOUR, True)], known_until=NOW),
        "lots": ts.Series.of(
            [(NOW - 10 * MIN, Lots(NOW - 10 * MIN, {}, Amount(5.0, 0.5)))], known_until=NOW
        ),
    }
    values.update(overrides)
    return Inputs(**values)  # type: ignore[arg-type]


def test_a_battery_without_its_lots_is_valued_and_names_them() -> None:
    """Missing lots: all the battery holds is of unknown origin, at the import
    (tariffkit#67 R-14), named ``battery_lots``; the valuation stands. Stale lots are
    missing lots."""
    given = Engine(_config()).value(_inputs(), at=NOW)
    none = Engine(_config()).value(_inputs(lots=None), at=NOW)
    stale = ts.Series.of(
        [(NOW - HOUR, Lots(NOW - HOUR, {}, Amount(5.0, 0.5)))], known_until=NOW - 30 * MIN
    )
    old = Engine(_config()).value(_inputs(lots=stale), at=NOW)
    assert given is not None and none is not None and old is not None
    assert none.kind is ValuationKind.STORED_VALUE_V1 and none.degraded == (Cause.BATTERY_LOTS,)
    assert old.degraded == (Cause.BATTERY_LOTS,)
    battery = [i for i, slot in enumerate(given.slots) if slot.regime == "battery"]
    assert battery
    # Never cheaper for not knowing (``test_stored_cost`` prices the difference).
    assert all(none.slots[i].marginal_cost >= given.slots[i].marginal_cost for i in battery)


def test_the_series_value_a_priced_horizon() -> None:
    valuation = Engine(_config()).value(_inputs(), at=NOW)
    assert valuation is not None
    assert valuation.kind is ValuationKind.STORED_VALUE_V1 and valuation.degraded == ()
    assert len(valuation.slots) == 36
    assert valuation.slots[0].start == datetime(2026, 6, 21, 10, 0, tzinfo=UTC)
    assert valuation.inputs[Cause.PRICES].as_of == NOW - 10 * MIN
    assert valuation.inputs[Cause.BATTERY_SOC].as_of == NOW - 2 * MIN


def test_inputs_are_valued_at_a_moment() -> None:
    engine = Engine(_config())
    with pytest.raises(TypeError, match="at"):
        engine.value(_inputs())  # type: ignore[call-arg]
    with pytest.raises(TypeError, match="takes Inputs"):
        engine.value(object(), at=NOW)  # type: ignore[arg-type]


def test_nothing_issued_or_measured_after_the_moment_is_read() -> None:
    engine = Engine(_config())
    plain = engine.value(_inputs(), at=NOW)
    later_prices = _issues(
        (NOW - 10 * MIN, _prices()), (NOW + MIN, _prices(value=0.9, issued=NOW + MIN))
    )
    later_soc = ts.Series.of([(NOW - 2 * MIN, 0.5), (NOW + MIN, 0.1)], known_until=NOW + HOUR)
    looked_ahead = engine.value(_inputs(prices=later_prices, soc=later_soc), at=NOW)
    assert looked_ahead == plain


def test_a_forecast_received_after_the_moment_is_not_read_even_if_issued_before() -> None:
    engine = Engine(_config())
    plain = engine.value(_inputs(), at=NOW)
    received_late = _issues(
        (NOW - 10 * MIN, _prices()), (NOW + MIN, _prices(value=0.9, issued=NOW - MIN))
    )
    assert engine.value(_inputs(prices=received_late), at=NOW) == plain


def test_five_minute_prices_tile_ten_minute_slots() -> None:
    first = NOW.replace(minute=0, second=0) - HOUR
    steps = _grid(first, 5 * MIN, 9 * 12)
    tiled = ts.Forecast(
        tuple(
            ts.Interval(s, s + 5 * MIN, ts.Price(0.2 if i % 2 else 0.4, 0.08))
            for i, s in enumerate(steps)
        ),
        NOW - 10 * MIN,
    )
    valuation = Engine(_config()).value(_inputs(prices=_issues((NOW - 10 * MIN, tiled))), at=NOW)
    assert valuation is not None
    assert [slot.p_import for slot in valuation.slots] == pytest.approx([0.3] * 36)


def test_prices_that_cross_the_slots_are_unpriced() -> None:
    """15-minute prices on 10-minute slots price no slot, a named cause (``prices``,
    ``unpriced``), not an exception; on 15-minute slots they price every one."""
    quarter_hours = _prices(step=15 * MIN)
    issues = _issues((NOW - 10 * MIN, quarter_hours))
    crossed = Engine(_config()).value(_inputs(prices=issues), at=NOW)
    assert crossed.kind is ValuationKind.UNPRICED and crossed.degraded == ("prices",)
    tiled = Engine(_config(slot=15 * MIN)).value(_inputs(prices=issues), at=NOW)
    assert tiled.kind is not ValuationKind.UNPRICED
    assert all(slot.start.minute % 15 == 0 for slot in tiled.slots)


def test_a_pv_or_load_forecast_that_crosses_the_slots_is_its_cause() -> None:
    engine = Engine(_config())
    pv = engine.value(_inputs(pv=_issues((NOW - HOUR, _pv(step=15 * MIN)))), at=NOW)
    assert pv is not None and pv.degraded == ("pv_forecast",)
    load = engine.value(_inputs(load=_issues((NOW - 5 * MIN, _load(step=15 * MIN)))), at=NOW)
    assert load is not None and load.degraded == ("load_forecast",)


def test_a_point_forecast_stands_for_its_three_quantiles_and_names_its_spread() -> None:
    """A point forecast prices as one whose three quantiles are its median; with no
    level below the median the pessimistic case is the median, named ``pv_spread``, not
    left silent."""
    engine = Engine(_config())
    point = engine.value(_inputs(pv=_issues((NOW - HOUR, _pv(point=True)))), at=NOW)
    flat = _pv()
    same = ts.Forecast(
        tuple(
            ts.Interval(
                i.start,
                i.end,
                ts.Quantiles({0.1: i.value.median, 0.5: i.value.median, 0.9: i.value.median}),
            )
            for i in flat.intervals
        ),
        flat.issued_at,
        flat.source,
    )
    flat_three = engine.value(_inputs(pv=_issues((NOW - HOUR, same))), at=NOW)
    assert point.degraded == (Cause.PV_SPREAD,) and flat_three.degraded == ()
    assert replace(point, degraded=()) == flat_three


def test_prices_older_than_their_limit_give_no_valuation() -> None:
    old = _issues((NOW - 3 * HOUR, _prices(issued=NOW - 3 * HOUR)))
    within = Engine(_config()).value(_inputs(prices=old), at=NOW)  # the default: 26 h
    assert within.kind is not ValuationKind.UNPRICED
    limited = _config(price_max_age=2 * HOUR)
    assert Engine(limited).value(_inputs(prices=old), at=NOW).kind is ValuationKind.UNPRICED


def test_a_load_forecast_older_than_its_limit_is_its_cause() -> None:
    stale = _issues((NOW - 2 * HOUR, replace(_load(), issued_at=NOW - 2 * HOUR)))
    valuation = Engine(_config()).value(_inputs(load=stale), at=NOW)
    assert valuation is not None and valuation.degraded == ("load_forecast",)
    assert valuation.inputs[Cause.LOAD_FORECAST].as_of == NOW - 2 * HOUR


def test_a_silent_path_makes_the_soc_unknown_and_keeps_when_it_was_known() -> None:
    silent = ts.Series.of([(NOW - 2 * MIN, 0.5)], known_until=NOW - MIN)
    valuation = Engine(_config()).value(_inputs(soc=silent), at=NOW)
    assert valuation is not None and valuation.degraded == ("battery_soc",)
    assert valuation.inputs[Cause.BATTERY_SOC].as_of == NOW - 2 * MIN


def test_an_input_the_plant_does_not_have_is_a_mistake() -> None:
    with pytest.raises(ValueError, match="no PV"):
        Engine(_config("battery_only")).value(_inputs(), at=NOW)
    with pytest.raises(ValueError, match="no battery"):
        Engine(_config("pv_only")).value(_inputs(), at=NOW)
    with pytest.raises(ValueError, match="DC-bus load"):
        Engine(_config()).value(_inputs(dc_load=_issues((NOW, _load()))), at=NOW)


def test_without_prices_there_is_no_valuation() -> None:
    assert Engine(_config()).value(_inputs(prices=None), at=NOW).kind is ValuationKind.UNPRICED
    nothing_yet = _issues((NOW + MIN, _prices(issued=NOW + MIN)))
    assert (
        Engine(_config()).value(_inputs(prices=nothing_yet), at=NOW).kind is ValuationKind.UNPRICED
    )


# -- edge cases -------------------------------------------------------------------------


def test_an_older_price_issue_never_fills_the_newest_ones_gaps() -> None:
    """A newer tariff replaces the one before it: a month-old issue at 0.99 must not
    price what a fresh one with a gap leaves out."""
    month_old = _prices(value=0.99, issued=NOW - timedelta(days=30))
    fresh = _prices(issued=NOW - 10 * MIN)
    gappy = replace(fresh, intervals=fresh.intervals[:3] + fresh.intervals[4:])  # 12-13 UTC out
    valuation = Engine(_config()).value(
        _inputs(prices=_issues((NOW - timedelta(days=30), month_old), (NOW - 10 * MIN, gappy))),
        at=NOW,
    )
    assert valuation is not None
    assert {slot.p_import for slot in valuation.slots} == {0.30}
    assert valuation.slots[-1].start == datetime(2026, 6, 21, 11, 50, tzinfo=UTC)  # to the gap


def test_prices_are_old_by_their_issue_not_their_first_interval() -> None:
    reaching_back = _prices(issued=NOW - 10 * MIN)  # its first interval starts at 09:00 UTC
    limited = Engine(_config(price_max_age=30 * MIN))
    reaching = _inputs(prices=_issues((NOW - 10 * MIN, reaching_back)))
    assert limited.value(reaching, at=NOW).kind is not ValuationKind.UNPRICED
    no_age = Engine(_config(price_max_age=timedelta(0)))
    assert (
        no_age.value(_inputs(prices=_issues((NOW - 10 * MIN, reaching_back))), at=NOW).kind
        is ValuationKind.UNPRICED
    )


def test_a_forecast_sample_with_no_value_does_not_end_the_forecast_read_before_it() -> None:
    """A source that answered nothing later leaves the forecast it gave in force, until
    it is too old: as the service keeps the last value it read."""
    engine = Engine(_config())
    plain = engine.value(_inputs(), at=NOW)
    pv = _pv()
    failed_since = ts.Series.of([(NOW - HOUR, pv), (NOW - 30 * MIN, None)], known_until=NOW)
    assert engine.value(_inputs(pv=failed_since), at=NOW) == plain


def test_quantiles_cannot_change_once_made_and_pv_cannot_be_negative() -> None:
    raw = {0.1: 1.0, 0.5: 2.0, 0.9: 3.0}
    quantiles = ts.Quantiles(raw)
    raw[0.5] = float("nan")
    assert quantiles.median == 2.0
    assert hash(quantiles) == hash(ts.Quantiles({0.9: 3.0, 0.5: 2.0, 0.1: 1.0}))
    negative = replace(_pv(), intervals=(ts.Interval(NOW, NOW + HOUR, ts.Quantiles.point(-1.0)),))
    with pytest.raises(ValueError, match="negative PV"):
        Engine(_config()).value(_inputs(pv=_issues((NOW - HOUR, negative))), at=NOW)


def test_an_older_misaligned_load_issue_is_left_out_and_a_misaligned_newest_keeps_its_time() -> (
    None
):
    engine = Engine(_config())
    plain = engine.value(_inputs(), at=NOW)
    quarter = replace(_load(step=15 * MIN), issued_at=NOW - 20 * MIN)
    older_crossing = _issues((NOW - 20 * MIN, quarter), (NOW - 5 * MIN, _load()))
    assert engine.value(_inputs(load=older_crossing), at=NOW) == plain
    newest_crossing = _issues(
        (NOW - 5 * MIN, replace(_load(step=15 * MIN), issued_at=NOW - 5 * MIN))
    )
    valuation = engine.value(_inputs(load=newest_crossing), at=NOW)
    assert valuation is not None and valuation.degraded == ("load_forecast",)
    assert valuation.inputs[Cause.LOAD_FORECAST].as_of == NOW - 5 * MIN


def test_the_checks_come_in_order() -> None:
    no_battery = Engine(_config("pv_only"))
    empty = ts.Series[float]((), known_until=NOW)
    no_soc = no_battery.value(_inputs(soc=empty, dispatch_as_modelled=None), at=NOW)
    assert no_soc.kind is not ValuationKind.UNPRICED
    no_dc = Engine(_config())
    assert (
        no_dc.value(_inputs(prices=None, dc_load=_issues((NOW, _load()))), at=NOW).kind
        is ValuationKind.UNPRICED
    )


def test_a_plant_that_needs_pv_per_array_names_a_stale_array() -> None:
    """AC and DC arrays on one plant: its PV comes per array, and a stale array is the
    ``pv_forecast`` cause."""
    config = replace(_config(), plant=Plant(replace(mixed_folded(), tz=BERLIN)))
    assert config.plant.pv_per_array
    engine = Engine(config)
    arrays = {"roof": _pv(), "shed": replace(_pv(), source="shed")}
    for stale in (None, "shed"):
        given = {
            name: replace(f, issued_at=NOW - 21 * HOUR) if name == stale else f
            for name, f in arrays.items()
        }
        inputs = _inputs(pv={name: _issues((f.issued_at, f)) for name, f in given.items()})
        valuation = engine.value(inputs, at=NOW)
        assert valuation.degraded == (() if stale is None else ("pv_forecast",))
        assert valuation.inputs[Cause.PV_FORECAST].as_of == min(f.issued_at for f in given.values())


def test_quantiles_pickle_and_deep_copy() -> None:
    quantiles = ts.Quantiles({0.1: 1.0, 0.5: 2.0, 0.9: 3.0})
    assert pickle.loads(pickle.dumps(quantiles)) == quantiles
    assert copy.deepcopy(_pv()) == _pv()


def test_the_slot_is_configuration() -> None:
    """A valuation on 15-minute slots, from the same series."""
    config = _config(slot=timedelta(minutes=15))
    engine = Engine(config)
    valuation = engine.value(_inputs(), at=NOW)
    assert valuation.kind is ValuationKind.STORED_VALUE_V1 and valuation.degraded == ()
    assert len(valuation.slots) == HORIZON / config.slot
    assert valuation.slots[0].start == datetime(2026, 6, 21, 10, 0, tzinfo=UTC)
    assert all(b - a == config.slot for a, b in pairwise(valuation.starts))
    live = engine.live_session().update(
        LiveReading(
            at=NOW + timedelta(minutes=13),
            load_kw=0.6,
            soc=0.5,
            dispatch_as_modelled=True,
            meters=dict.fromkeys(config.plant.meters, 0.0),
        ),
        valuation,
        NOW + HOUR,
    )
    assert live.kind is not ValuationKind.UNPRICED  # 10:16 is in the 10:15 slot


@pytest.mark.parametrize(("over", "stale"), [(0.5, True), (-0.5, False)])
def test_a_load_forecasts_age_is_judged_at_the_moment_itself(over: float, stale: bool) -> None:
    """0.7 s into its second, a load issue aged ``load_max_age`` + 0.5 s is stale, though
    it is fresh at the whole second the valuation is stamped with; one aged 0.5 s under
    the limit is used (the service's ``load_forecast_at`` judged at the moment)."""
    config = _config()
    at = NOW + timedelta(milliseconds=700)
    issued = at - config.load_max_age - timedelta(seconds=over)
    issue = replace(_load(), issued_at=issued)
    measured = {
        "soc": ts.Series.of([(NOW - 2 * MIN, 0.5)], known_until=at),
        "dispatch_as_modelled": ts.Series.of([(NOW - 3 * HOUR, True)], known_until=at),
        "lots": ts.Series.of(
            [(NOW - 10 * MIN, Lots(NOW - 10 * MIN, {}, Amount(5.0, 0.5)))], known_until=at
        ),
    }
    valuation = Engine(config).value(_inputs(load=_issues((issued, issue)), **measured), at=at)
    assert valuation.degraded == (("load_forecast",) if stale else ())
    assert valuation.inputs[Cause.LOAD_FORECAST].usable is not stale
