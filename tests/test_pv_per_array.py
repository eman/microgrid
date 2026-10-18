"""PV per array.

A plant with AC- and DC-coupled arrays takes its PV per array, each in its own terms;
a plant whose arrays share one coupling takes either form. Each PV scenario takes
every array at the same percentile: one sky over the site.
"""

from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from engine_fixtures import (
    EXAMPLE_PLANT,
    NOW,
    PACIFIC,
    config_of,
    inputs,
    live_reading,
    pv,
    pv_forecast,
    received,
)
from microgrid.engine import Cause, Engine, EngineConfig, Pricing
from microgrid.engine._surplus import forecast_surplus
from microgrid.engine._value import quantile_traces
from microgrid.engine.series import SLOT, Forecast, Interval, Quantiles, Series
from microgrid.plant import BatteryPart, Conversion, Inverter, Plant, PlantConfig, PvArray

T0 = datetime(2026, 9, 25, 7, 0, tzinfo=UTC)
HOURS = SLOT / timedelta(hours=1)
CONVERSION = Conversion(eta_in=0.97, eta_out=0.97, loss_in_per_kw=0.002, loss_out_per_kw=0.002)
SITE = EXAMPLE_PLANT.battery


def _starts(count: int) -> list[datetime]:
    return [T0 + k * SLOT for k in range(count)]


def mixed() -> Plant:
    """AC PV on micro-inverters and DC strings on a hybrid, a battery folded on its bus."""
    return Plant(
        PlantConfig(
            tz=PACIFIC,
            pv=(
                PvArray("roof", "ac", ac_rating_kw=5.0),
                PvArray("strings", "dc", dc_rating_kw=4.0),
            ),
            battery=BatteryPart(SITE, "dc_bus", dc_charge_kw=7.5, dc_discharge_kw=8.5),
            hybrid=Inverter(ac_rating_kw=6.0, standby_kw=0.04),
        )
    )


def _scaled(
    forecast: Forecast[Quantiles], factor: float, polled: datetime | None = None
) -> Series[Forecast[Quantiles]]:
    return received(
        Forecast(
            tuple(
                Interval(
                    i.start, i.end, Quantiles({q: kw * factor for q, kw in i.value.kw.items()})
                )
                for i in forecast.intervals
            ),
            forecast.issued_at if polled is None else polled,
        )
    )


def test_each_array_goes_its_own_path() -> None:
    # The roof's AC serves the house (and the hybrid's 0.04 kW standby) as it is; the
    # strings' DC charges the battery on its bus first. 2 kW AC and 3 kW DC, the house
    # 1: the roof's spare 0.96 crosses to the battery, the strings' 3 go straight in.
    flows = mixed().project(_starts(1), [SLOT], {"roof": [2.0], "strings": [3.0]}, [1.0], 0.5)
    (flow,) = flows
    assert flow.pv_to_load == pytest.approx(1.04)
    assert flow.dc_pv_to_battery == pytest.approx(3.0)
    assert flow.pv_to_battery == pytest.approx(0.96)
    assert flow.pv_export == pytest.approx(0.0) and flow.pv_clipped == 0.0


@settings(max_examples=100)
@given(
    st.lists(
        st.tuples(
            st.floats(0, 7, allow_nan=False),
            st.floats(0, 6, allow_nan=False),
            st.floats(0, 5, allow_nan=False),
        ),
        min_size=1,
        max_size=48,
    ),
    st.floats(0, 1),
)
def test_a_mixed_plant_balances_every_slot(
    day: list[tuple[float, float, float]], soc: float
) -> None:
    plant = mixed()
    roof, strings, house = [d[0] for d in day], [d[1] for d in day], [d[2] for d in day]
    flows = plant.project(
        _starts(len(day)), [SLOT] * len(day), {"roof": roof, "strings": strings}, house, soc
    )
    capacity = SITE.capacity_kwh
    for flow, a, d, need in zip(flows, roof, strings, house, strict=True):
        assert flow.soc_end is not None and flow.soc_start is not None
        stored = (flow.soc_end - flow.soc_start) * capacity / HOURS
        sources = a + d + flow.grid_to_load + flow.grid_to_battery
        sinks = (
            need
            + flow.inverter_draw
            + flow.pv_export
            + flow.pv_curtail
            + flow.pv_clipped
            + flow.conversion_loss
            + stored
        )
        assert sources == pytest.approx(sinks, abs=1e-6)


def test_a_site_total_is_its_arrays_shared_by_dc_rating() -> None:
    # Two DC arrays rated 2 and 6: a site total of 8 is 2 and 6 per array.
    plant = Plant(
        PlantConfig(
            tz=PACIFIC,
            pv=(PvArray("east", "dc", dc_rating_kw=2.0), PvArray("west", "dc", dc_rating_kw=6.0)),
            battery=BatteryPart(SITE, "dc_bus", dc_charge_kw=7.5, dc_discharge_kw=8.5),
            hybrid=Inverter(ac_rating_kw=6.0),
        )
    )
    starts, house = _starts(3), [1.0, 0.5, 2.0]
    total = [8.0, 4.0, 0.0]
    per_array = {"east": [2.0, 1.0, 0.0], "west": [6.0, 3.0, 0.0]}
    shared = plant.project(starts, [SLOT] * 3, total, house, 0.4)
    assert plant.project(starts, [SLOT] * 3, per_array, house, 0.4) == shared


def test_todays_arrays_per_array_sum_to_the_site() -> None:
    # Two unrated AC arrays with the example battery: today's arithmetic on their sum.
    plant = Plant(
        replace(config_of(EXAMPLE_PLANT), pv=(PvArray("east", "ac"), PvArray("west", "ac")))
    )
    starts, house = _starts(3), [1.0, 0.5, 2.0]
    per_array = {"east": [1.5, 0.25, 0.0], "west": [2.5, 0.75, 0.0]}
    assert plant.project(starts, [SLOT] * 3, per_array, house, 0.4) == plant.project(
        starts, [SLOT] * 3, [4.0, 1.0, 0.0], house, 0.4
    )


def test_one_series_is_taken_where_the_arrays_can_share_it() -> None:
    """``pv_one_series`` says what ``_pv`` takes: one coupling and, where any array is
    capped, every array's DC rating."""
    base = config_of(EXAMPLE_PLANT)
    unrated = Plant(replace(base, pv=(PvArray("east", "ac"), PvArray("west", "ac"))))
    capped = Plant(
        replace(base, pv=(PvArray("east", "ac", ac_rating_kw=5.0), PvArray("west", "ac")))
    )
    assert Plant(base).pv_one_series and unrated.pv_one_series
    assert not capped.pv_one_series and not mixed().pv_one_series
    with pytest.raises(ValueError, match="share one PV series"):
        capped.project(_starts(1), [SLOT], [1.0], [0.5], 0.4)


# --- The engine ----------------------------------------------------------------------


def _engine(plant: Plant) -> Engine:
    return Engine(EngineConfig(plant=plant, pricing=Pricing(scenarios=3)))


def _per_array(polled: datetime | None = None) -> dict[str, Series[Forecast[Quantiles]]]:
    sky = pv_forecast()
    return {"roof": _scaled(sky, 0.5), "strings": _scaled(sky, 0.6, polled)}


def test_the_engine_values_a_mixed_site_per_array() -> None:
    valuation = _engine(mixed()).value(inputs(pv=_per_array()), at=NOW)
    assert valuation is not None and valuation.degraded == ()
    # Every scenario takes both arrays at its percentile: one sky over the site.
    model = valuation.model
    assert model is not None
    count = len(valuation.slots)
    for name, factor in (("roof", 0.5), ("strings", 0.6)):
        (sample,) = _scaled(pv_forecast(), factor).samples
        assert sample.value is not None
        slots = [
            next(i.value for i in sample.value.intervals if i.start <= t < i.end)
            for t in valuation.starts
        ]
        expected = quantile_traces(slots, Pricing(scenarios=3).quantiles)
        for k, scenario in enumerate(model.scenarios):
            assert isinstance(scenario, Mapping)
            assert list(scenario[name]) == pytest.approx(expected[k][:count])


def test_the_oldest_array_is_the_forecasts_age_and_a_stale_one_a_cause() -> None:
    fresh = _engine(mixed()).value(inputs(pv=_per_array()), at=NOW)
    assert fresh is not None
    oldest = min(s.samples[0].at for s in _per_array().values())
    assert fresh.inputs[Cause.PV_FORECAST].as_of == oldest
    stale = _engine(mixed()).value(inputs(pv=_per_array(polled=NOW - timedelta(hours=30))), at=NOW)
    assert stale is not None and stale.degraded == ("pv_forecast",)


def test_a_shorter_array_forecast_shortens_the_horizon() -> None:
    # The forecast covers the slots every array covers: as if both were the shorter.
    sky = pv()
    short = received(replace(pv_forecast(), intervals=pv_forecast().intervals[:60]))
    one_short = _engine(mixed()).value(inputs(pv={"roof": sky, "strings": short}), at=NOW)
    both_short = _engine(mixed()).value(inputs(pv={"roof": short, "strings": short}), at=NOW)
    assert one_short is not None and both_short is not None
    assert one_short.starts == both_short.starts
    assert len(one_short.starts) < 288


def test_an_array_covering_nothing_keeps_the_full_horizon() -> None:
    # Its forecast unusable, the valuation falls back over the whole price horizon,
    # as a site whose one forecast covers nothing does; no other array clips it.
    sky = pv()
    forecast = pv_forecast()
    later = received(
        Forecast(
            tuple(
                Interval(
                    i.start + timedelta(days=5), i.end + timedelta(days=5), Quantiles.point(1.0)
                )
                for i in forecast.intervals
            ),
            forecast.issued_at,
        )
    )
    valuation = _engine(mixed()).value(inputs(pv={"roof": sky, "strings": later}), at=NOW)
    nothing = _engine(mixed()).value(inputs(pv={"roof": later, "strings": later}), at=NOW)
    assert valuation is not None and nothing is not None
    assert valuation.degraded == ("pv_forecast",)
    assert valuation.starts == nothing.starts and len(valuation.starts) == 288


def test_the_engine_refuses_pv_that_does_not_fit_the_arrays() -> None:
    with pytest.raises(ValueError, match="takes a PV forecast per array"):
        _engine(mixed()).value(inputs(), at=NOW)
    with pytest.raises(ValueError, match="has no PV array"):
        _engine(mixed()).value(inputs(pv={**_per_array(), "shed": pv()}), at=NOW)
    # A missing array is a missing input: the cause, not a mistake.
    missing = _engine(mixed()).value(inputs(pv={"roof": pv()}), at=NOW)
    assert missing is not None and missing.degraded == ("pv_forecast",)


def test_one_array_per_array_is_its_site_total() -> None:
    plant = Plant(config_of(EXAMPLE_PLANT))
    (name,) = plant.pv_arrays
    total = _engine(plant).value(inputs(), at=NOW)
    per_array = _engine(plant).value(inputs(pv={name: pv()}), at=NOW)
    assert total is not None and per_array is not None
    assert per_array.slots == total.slots  # to the bit


def test_prices_per_array_are_the_site_totals_shared() -> None:
    # Two DC arrays rated 2 and 6: a site total and its 1:3 share price every slot
    # alike.
    plant = Plant(
        PlantConfig(
            tz=PACIFIC,
            pv=(PvArray("east", "dc", dc_rating_kw=2.0), PvArray("west", "dc", dc_rating_kw=6.0)),
            battery=BatteryPart(SITE, "dc_bus", dc_charge_kw=7.5, dc_discharge_kw=8.5),
            hybrid=Inverter(ac_rating_kw=6.0),
        )
    )
    sky = pv_forecast()
    total = _engine(plant).value(inputs(pv=received(sky)), at=NOW)
    shared = _engine(plant).value(
        inputs(pv={"east": _scaled(sky, 0.25), "west": _scaled(sky, 0.75)}), at=NOW
    )
    assert total is not None and shared is not None
    for mine, theirs in zip(shared.slots, total.slots, strict=True):
        assert mine.marginal_cost == pytest.approx(theirs.marginal_cost, rel=1e-9, abs=1e-12)


def test_the_surplus_forecast_ends_at_the_first_gap_in_any_array() -> None:
    plant = mixed()
    starts = _starts(4)
    found = forecast_surplus(
        plant,
        starts,
        {"roof": [2.0, 2.0, 2.0, 2.0], "strings": [3.0, None, 3.0, 3.0]},
        {"roof": [1.0, 1.0, 1.0, 1.0], "strings": [1.0, 1.0, 1.0, 1.0]},
        [1.0] * 4,
        0.5,
    )
    assert found.surplus_kw[0] is not None and found.surplus_kw[1:] == (None, None, None)


def test_live_per_array_reads_pv_per_meter() -> None:
    # A plant with both couplings meters each array, and the live value
    # runs on what each makes.
    plant = mixed()
    assert {name: kind for name, kind in plant.meters.items() if kind == "pv"} == {
        "pv:roof": "pv",
        "pv:strings": "pv",
    }
    engine = Engine(EngineConfig(plant=plant, pricing=Pricing(scenarios=3), smoothing=timedelta(0)))
    valuation = engine.value(inputs(pv=_per_array()), at=NOW)
    assert valuation is not None
    until = NOW + timedelta(hours=1)
    with pytest.raises(ValueError, match="not the plant's meters"):
        engine.live_session().update(live_reading(), valuation, until)
    read = engine.live_session().update(
        live_reading(pv_kw=None, meters={"pv:roof": 3.0, "pv:strings": 3.6}), valuation, until
    )
    assert read is not None and read.degraded == ()
    # One array unread: ``pv_power``, and the fallback.
    unread = engine.live_session().update(
        live_reading(pv_kw=None, meters={"pv:roof": 3.0}), valuation, until
    )
    assert unread is not None and unread.degraded == ("pv_power",)
    assert unread.marginal_cost == unread.p_import
