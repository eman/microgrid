"""What a kWh realised: the rule over metered flows, ``microgrid.engine.analysis.scoring``."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from hypothesis import given
from hypothesis import strategies as st

from engine_fixtures import EXAMPLE_PLANT, config_of, plant_of
from legacy_dispatch import NativePlant, PlantStorage
from microgrid.engine import Regime
from microgrid.engine.analysis.scoring import (
    BUCKETS,
    battery_exports,
    bucket_label,
    hours_ahead_bucket,
    inverter_kw,
    observed_flows,
    observed_regime,
    price_hour,
    realise,
    route_hour,
    served_load,
    storage_of,
)
from microgrid.engine.extend import (
    MODEL,
    OBSERVED,
    Event,
    FlowPrices,
    Flows,
    Noise,
    SlotFlow,
    importing,
    price_flows,
    spilling,
)
from microgrid.plant import Battery, HoldRule, Load, Plant

T0 = datetime(2026, 9, 25, 7, 0, tzinfo=UTC)


SLOT = timedelta(minutes=10)


PLANT = NativePlant(
    Battery(10.0, 5.0, 5.0, eta_charge=1.0, eta_discharge=1.0, reserve_soc=0.1),
    ZoneInfo("America/Los_Angeles"),
)


IMPORT, EXPORT = 0.40, 0.08
AC = plant_of(PLANT)  # the meters, read as the example plant reads them


#: Hand-built flows carry no port flows: the rule over today's storage, the oracle.
STORAGE = PlantStorage(PLANT.battery)


def _flow(i: int, soc_start: float, soc_end: float, **kw: Any) -> SlotFlow:
    return SlotFlow(start=T0 + i * SLOT, soc_start=soc_start, soc_end=soc_end, **kw)


def _price(flows: list[SlotFlow], noise: Noise = MODEL) -> tuple[list[float], list[Event]]:
    count = len(flows)
    priced = price_flows(STORAGE, flows, [IMPORT] * count, [EXPORT] * count, 144, noise=noise)
    return list(priced.prices), list(priced.events)


flow_strategy = st.builds(
    lambda soc, pv_b, pv_x, b_l, g_l, g_b: (soc, pv_b, pv_x, b_l, g_l, g_b),
    st.sampled_from([0.1, 0.5, 1.0]),
    st.sampled_from([0.0, 2.0]),
    st.sampled_from([0.0, 0.05, 1.0]),
    st.sampled_from([0.0, 1.0]),
    st.sampled_from([0.0, 0.2, 1.0]),
    st.sampled_from([0.0, 0.3, 3.0]),
)


def _ledger_flows(**kw: float) -> Flows:
    base = dict.fromkeys(
        (
            "consumed",
            "pv_to_load",
            "battery_to_load",
            "grid_to_load",
            "pv_to_battery",
            "grid_to_battery",
            "pv_export",
            "battery_export",
        ),
        0.0,
    )
    return Flows(**(base | kw))


def test_a_kwh_before_the_battery_fills_costs_the_export_it_displaced() -> None:
    flows = [
        _flow(0, 0.90, 0.95, pv_to_battery=3.0),
        _flow(1, 0.95, 1.00, pv_to_battery=3.0),
        _flow(2, 1.00, 1.00, pv_export=3.0),
        _flow(3, 1.00, 1.00, pv_export=2.0),
    ]
    priced = price_flows(STORAGE, flows, [IMPORT] * 4, [EXPORT] * 4, 144, noise=OBSERVED)
    assert priced.events == (Event.FILL, Event.FILL, Event.SPILL, Event.SPILL)
    assert priced.prices == (EXPORT,) * 4
    assert priced.event_at == (2, 2, 2, 3)
    # The fill and the afternoon's spill after it are one outcome.
    assert priced.outcome_at == (2, 2, 2, 2)


def test_the_top_up_and_the_slot_it_happened_in() -> None:
    flows = [
        _flow(0, 0.50, 0.45, battery_to_load=3.0),
        _flow(1, 0.45, 0.60, grid_to_battery=4.0),
        _flow(2, 0.60, 0.75, grid_to_battery=4.0),
    ]
    priced = price_flows(STORAGE, flows, [IMPORT, 0.30, 0.30], [EXPORT] * 3, 144)
    assert priced.events[0] is Event.TOPUP and priced.event_at[0] == 1
    assert priced.prices[0] == 0.30  # bought back in the window, at its price
    assert priced.outcome_at[0] == 1


def test_importing_now_and_running_empty() -> None:
    flows = [
        _flow(0, 0.30, 0.20, battery_to_load=3.0),
        _flow(1, 0.20, 0.10, battery_to_load=3.0, grid_to_load=1.0),
        _flow(2, 0.10, 0.10, grid_to_load=1.0),
    ]
    priced = price_flows(STORAGE, flows, [0.50, 0.30, 0.45], [EXPORT] * 3, 144)
    assert priced.events == (Event.EMPTY, Event.IMPORT, Event.IMPORT)
    assert priced.event_at == (1, 1, 2)
    assert priced.prices == (0.30, 0.30, 0.45)  # the cheapest import before empty
    assert priced.outcome_at == (1, 1, 1)  # one run of importing


def test_no_event_in_the_flows_is_unresolved() -> None:
    flows = [_flow(i, 0.5, 0.5, battery_to_load=0.5) for i in range(3)]
    priced = price_flows(STORAGE, flows, [IMPORT] * 3, [EXPORT] * 3, 144)
    assert priced.events == (Event.HORIZON,) * 3
    assert priced.resolved == (False, False, False)
    assert priced.event_at == priced.outcome_at == (3, 3, 3)


def test_no_flows_no_prices_and_lengths_must_match() -> None:
    assert price_flows(STORAGE, [], [], [], 144).prices == ()
    with pytest.raises(ValueError, match="price pair"):
        price_flows(STORAGE, [_flow(0, 0.5, 0.5)], [IMPORT, IMPORT], [EXPORT], 144)


def test_the_meters_noise_is_not_an_event() -> None:
    """Sensor timing: up to 0.13 kW of export far from full (95 %), 0.42 kW of grid charging."""
    exports = [_flow(0, 0.50, 0.52, pv_to_battery=2.0, pv_export=0.07)]
    assert _price(exports)[1] == [Event.SPILL]  # the model's floor is 1 W
    assert _price(exports, noise=OBSERVED)[1] == [Event.HORIZON]
    flows = [
        _flow(0, 0.50, 0.50, battery_to_load=0.5),
        _flow(1, 0.50, 0.52, grid_to_battery=0.42),
        _flow(2, 0.52, 1.00, pv_to_battery=3.0),
        _flow(3, 1.00, 1.00, pv_export=2.0),
    ]
    assert _price(flows)[1][0] is Event.TOPUP
    assert _price(flows, noise=OBSERVED)[1][0] is Event.FILL
    real = [_flow(0, 0.50, 0.50, battery_to_load=0.5), _flow(1, 0.50, 0.60, grid_to_battery=3.0)]
    assert _price(real, noise=OBSERVED)[1][0] is Event.TOPUP


def test_a_spill_is_net_of_what_the_battery_and_grid_supplied() -> None:
    """09-09 15:10: 0.19 kW out while the battery gave the house 0.10 kW."""
    both = _flow(0, 1.0, 1.0, pv_export=0.19, battery_to_load=0.10)
    assert not spilling(both, OBSERVED)
    assert spilling(_flow(0, 1.0, 1.0, pv_export=0.19), OBSERVED)
    # The model never exports while it discharges or imports: net is gross there.
    assert spilling(_flow(0, 1.0, 1.0, pv_export=0.01), MODEL)


def test_an_soc_within_half_a_point_of_saturation_is_full_on_the_meters() -> None:
    flows = [_flow(0, 0.99, 0.996, pv_export=1.0)]
    assert _price([*flows, _flow(1, 0.996, 0.996)], noise=OBSERVED)[1][0] is Event.SPILL
    before = [_flow(0, 0.95, 0.996, pv_to_battery=2.0), _flow(1, 0.996, 0.996, pv_export=1.0)]
    assert _price(before, noise=OBSERVED)[1][0] is Event.FILL
    assert _price(before)[1][0] is Event.HORIZON  # the model's full is exact


def test_regulation_is_the_batterys_and_a_hold_is_the_grids() -> None:
    """While the battery served, the grid gave up to 0.14 kW (99.5 %); at dusk up to
    0.2 kW with it idle; holds imported the house load, 0.4 kW and up."""
    serving = _flow(0, 0.5, 0.49, battery_to_load=0.3, grid_to_load=0.14)
    assert importing(STORAGE, serving, MODEL)
    assert not importing(STORAGE, serving, OBSERVED)
    dusk = _flow(0, 0.77, 0.77, battery_to_load=0.08, grid_to_load=0.2)
    assert not importing(STORAGE, dusk, OBSERVED)
    held = _flow(0, 0.5, 0.5, grid_to_load=0.46)
    assert importing(STORAGE, held, OBSERVED)
    step = _flow(0, 0.5, 0.49, battery_to_load=2.0, grid_to_load=0.53)  # over the 0.3 kW cap
    assert importing(STORAGE, step, OBSERVED)
    at_the_limit = _flow(0, 0.5, 0.4, battery_to_load=4.8, grid_to_load=0.2)  # 5.0: headroom
    assert not importing(STORAGE, at_the_limit, OBSERVED)
    past_it = _flow(0, 0.5, 0.4, battery_to_load=4.9, grid_to_load=0.2)
    assert importing(STORAGE, past_it, OBSERVED)
    at_reserve = _flow(0, 0.11, 0.104, battery_to_load=0.3, grid_to_load=0.2)
    assert importing(STORAGE, at_reserve, OBSERVED)
    trickle = _flow(0, 0.5, 0.5, grid_to_load=0.1)  # under 0.12 kW on the meters: noise
    assert not importing(STORAGE, trickle, OBSERVED)
    # The model has no sensor noise: 0.1 kW short is the house importing (else a dawn
    # empty under 20 Wh is missed); float residue is not.
    assert importing(STORAGE, trickle, MODEL)
    assert not importing(STORAGE, _flow(0, 0.5, 0.5, grid_to_load=1e-6), MODEL)


def test_covered_waits_for_a_day_of_prices() -> None:
    """The expensive run is read against the cheapest import of the day
    ahead, so a run near the data's end is not settled."""
    flows = [
        _flow(0, 0.5, 0.5, battery_to_load=1.0),
        _flow(1, 0.5, 0.5, battery_to_load=1.0),
        _flow(2, 0.5, 0.5, battery_to_load=1.0),
        _flow(3, 0.5, 0.5, battery_to_load=1.0),
        _flow(4, 0.5, 0.5, grid_to_load=1.0),
        _flow(5, 0.5, 0.5, battery_to_load=1.0),
    ]
    prices = [0.45, 0.45, 0.60, 0.60, 0.45, 0.45]
    short = price_flows(STORAGE, flows, prices, [EXPORT] * 6, 8)
    assert short.covered_resolved == (False,) * 6
    longer = [*flows, *(_flow(6 + i, 0.5, 0.5, battery_to_load=1.0) for i in range(3))]
    full = price_flows(STORAGE, longer, [*prices, 0.30, 0.30, 0.30], [EXPORT] * 9, 8)
    assert full.covered_resolved[0] and not full.covered[0]  # the 0.45 hours count too
    assert full.cover_at[0] == 0


@given(
    st.lists(flow_strategy, min_size=1, max_size=14),
    st.lists(flow_strategy, min_size=1, max_size=8),
    st.lists(st.sampled_from([0.3, 0.45, 0.6]), min_size=22, max_size=22),
    st.sampled_from([MODEL, OBSERVED]),
)
def test_what_is_resolved_stays_resolved_when_more_data_comes(
    head: list[tuple[float, ...]],
    tail: list[tuple[float, ...]],
    prices: list[float],
    noise: Noise,
) -> None:
    """Resolved prices, events and covers do not change when flows are appended."""

    def flows(spec: list[tuple[float, ...]]) -> list[SlotFlow]:
        return [
            _flow(
                i,
                soc,
                soc,
                pv_to_battery=pb,
                pv_export=px,
                battery_to_load=bl,
                grid_to_load=gl,
                grid_to_battery=gb,
            )
            for i, (soc, pb, px, bl, gl, gb) in enumerate(spec)
        ]

    def run(spec: list[tuple[float, ...]]) -> FlowPrices:
        count = len(spec)
        return price_flows(STORAGE, flows(spec), prices[:count], [EXPORT] * count, 6, noise=noise)

    short, long = run(head), run(head + tail)
    for t in range(len(head)):
        if short.resolved[t]:
            assert short.prices[t] == long.prices[t]
            assert short.events[t] == long.events[t]
            assert short.event_at[t] == long.event_at[t]
            assert short.spilled[t] == long.spilled[t]
        if short.covered_resolved[t]:
            assert short.covered[t] == long.covered[t]


def test_metered_flows_take_their_soc_from_the_stored_energy() -> None:
    starts = [T0, T0 + SLOT, T0 + 2 * SLOT]
    flows = [_ledger_flows(pv_to_battery=3.0), None, _ledger_flows(battery_export=2.0)]
    result = observed_flows(starts, flows, [5.5, 6.0, 5.6], 5.0, plant=AC)
    first, gap, last = result
    assert first is not None and (first.soc_start, first.soc_end) == (0.5, 0.55)
    assert first.pv_to_battery == 3.0
    assert gap is None
    assert last is not None and (last.soc_start, last.soc_end) == pytest.approx((0.6, 0.56))
    assert observed_flows(starts[:1], flows[:1], [None], 5.0, plant=AC) == [None]
    with pytest.raises(ValueError, match="one flow"):
        observed_flows(starts, flows[:2], [5.5, 6.0, 5.6], 5.0, plant=AC)


def test_a_dc_bus_draw_is_load_like_any_other() -> None:
    # The house load is the plant's metered consumption, as is: nothing is taken out
    # of the meters for it.
    flows = [_ledger_flows(consumed=9.0, grid_to_load=6.0, battery_to_load=2.0, pv_to_load=1.0)]
    (seen,) = observed_flows([T0], flows, [5.0], 5.5, plant=AC)
    assert seen is not None
    assert (seen.grid_to_load, seen.battery_to_load, seen.pv_to_load) == (6.0, 2.0, 1.0)


def test_the_inverters_draw_is_more_while_the_battery_works() -> None:
    """The inverter draws its standby always and more while the battery works."""
    plant = plant_of(replace(PLANT, standby_kw=0.04, working_kw=0.05))
    assert inverter_kw(plant, _ledger_flows(consumed=1.0)) == 0.04
    assert inverter_kw(plant, _ledger_flows(consumed=1.0, battery_to_load=0.5)) == 0.09
    assert inverter_kw(plant, _ledger_flows(consumed=1.0, pv_to_battery=0.04)) == 0.04


def test_a_battery_export_is_not_one_where_pv_could_have_been_all_of_it() -> None:
    starts = [T0, T0 + SLOT]
    real = _ledger_flows(consumed=1.0, pv_to_load=0.5, battery_to_load=0.5, battery_export=2.0)
    timing = _ledger_flows(
        consumed=1.0, pv_to_load=1.0, pv_to_battery=0.3, pv_export=0.5, battery_export=0.2
    )
    assert battery_exports(starts, [real, timing]) == {starts[0]}


def test_the_metered_regime() -> None:
    assert observed_regime(STORAGE, _flow(0, 1, 1, pv_export=1.0)) is Regime.EXPORT
    assert observed_regime(STORAGE, _flow(0, 0.5, 0.5, grid_to_load=1.0)) is Regime.IMPORT
    assert observed_regime(STORAGE, _flow(0, 0.5, 0.6, grid_to_battery=4.0)) is Regime.IMPORT
    regulating = _flow(0, 0.5, 0.49, battery_to_load=0.3, grid_to_load=0.14)
    assert observed_regime(STORAGE, regulating) is Regime.BATTERY
    assert observed_regime(STORAGE, _flow(0, 0.5, 0.6, pv_to_battery=2.0)) is Regime.BATTERY
    # Idle above its reserve (PV just covers the house): a kWh more is the battery's.
    assert observed_regime(STORAGE, _flow(0, 0.5, 0.5)) is Regime.BATTERY
    assert observed_regime(STORAGE, _flow(0, 0.1, 0.1)) is Regime.IMPORT


def test_realise_prices_each_run_and_follows_unscored_slots_through() -> None:
    flows: list[SlotFlow | None] = [
        _flow(0, 0.80, 0.85, pv_to_battery=3.0),
        None,  # the meters were silent: a new run
        _flow(2, 0.85, 0.90, pv_to_battery=3.0),
        _flow(3, 0.90, 0.88, battery_to_load=0.5),  # the battery also exported here
        _flow(4, 0.88, 1.00, pv_to_battery=3.0),
        _flow(5, 1.00, 1.00, pv_export=2.0),
        _flow(6, 1.00, 1.00, pv_export=1.0),
        _flow(7, 1.00, 0.95, battery_to_load=2.0),  # nothing after it: unresolved
    ]
    realised = realise(
        STORAGE, flows, [IMPORT] * 8, [EXPORT] * 8, step=SLOT, unscored={T0 + 3 * SLOT}
    )
    at = [T0 + i * SLOT for i in range(8)]
    assert at[0] not in realised.price  # its run ends before anything happens
    assert realised.price[at[2]] == EXPORT
    assert realised.event[at[2]] == (Event.FILL, at[5])
    assert realised.outcome[at[2]] == realised.outcome[at[6]] == (Event.FILL, at[5])
    assert at[3] not in realised.price and at[3] not in realised.regime
    assert realised.spilled[at[4]] and realised.regime[at[5]] is Regime.EXPORT
    assert at[7] not in realised.price and realised.regime[at[7]] is Regime.BATTERY
    with pytest.raises(ValueError, match="price pair"):
        realise(STORAGE, flows, [IMPORT], [EXPORT] * 8, step=SLOT)


def test_realise_settles_covered_and_names_its_run() -> None:
    day = int(timedelta(days=1) / SLOT)
    count = day + 12
    prices = [0.30] * 4 + [0.60] * 4 + [0.30] * (count - 8)
    flows: list[SlotFlow | None] = [
        _flow(i, 0.5, 0.5, battery_to_load=1.0 if 4 <= i < 8 else 0.0) for i in range(count)
    ]
    realised = realise(STORAGE, flows, prices, [EXPORT] * count, step=SLOT)
    assert realised.covered[T0] is True  # the battery served the expensive hours
    assert realised.cover_outcome[T0] == T0 + 4 * SLOT
    assert T0 + (count - 1) * SLOT not in realised.covered  # no day ahead of it


def test_a_dc_bus_draws_import_breaks_the_cover_as_any_import_does() -> None:
    """The DC-bus load is a load like any other: a slot
    the house imports for it is an import, scored, and it breaks the cover."""
    day = int(timedelta(days=1) / SLOT)
    count = day + 12
    starts = [T0 + i * SLOT for i in range(count)]
    prices = [0.30] * 4 + [0.60] * 4 + [0.30] * (count - 8)
    ledger = [
        _ledger_flows(consumed=9.0, battery_to_load=1.0, grid_to_load=8.0)
        if i == 5
        else _ledger_flows(consumed=1.0, battery_to_load=1.0 if 4 <= i < 8 else 0.0)
        for i in range(count)
    ]
    flows = observed_flows(starts, ledger, [5.0] * count, 5.0, plant=AC)
    realised = realise(STORAGE, flows, prices, [EXPORT] * count, step=SLOT)
    assert realised.covered[T0] is False
    assert starts[5] in realised.price


# -- the hour routed through the plant model, and the meter arithmetic --


def _metered(consumed: float = 1.0, **kw: float) -> Flows:
    values = dict.fromkeys(
        (
            "consumed",
            "pv_to_load",
            "battery_to_load",
            "grid_to_load",
            "pv_to_battery",
            "grid_to_battery",
            "pv_export",
            "battery_export",
        ),
        0.0,
    )
    values.update(kw, consumed=consumed)
    return Flows(**values)


EXAMPLE = plant_of(EXAMPLE_PLANT)


def _dc_plant() -> Plant:
    """The example plant's battery on a hybrid's DC bus, a DC-bus load beside it, and a
    rule that holds the battery for a session started below 65 %."""
    bus = config_of(EXAMPLE_PLANT, connection="dc_bus")
    assert bus.battery is not None and bus.hybrid is not None
    return Plant(
        replace(
            bus,
            loads=(*bus.loads, Load("dc_load", "dc_bus")),
            battery=replace(bus.battery, dc_charge_kw=5.0, dc_discharge_kw=5.0),
            hybrid=replace(bus.hybrid, ac_rating_kw=10.0),
            dispatch=replace(
                bus.dispatch,
                hold_rules=(HoldRule(dc_load_above_kw=1.0, start_below_soc=0.65),),
            ),
        )
    )


def test_the_served_load_is_consumption_less_the_inverters_draw() -> None:
    draws = EXAMPLE_PLANT
    idle, working = _metered(1.0), _metered(2.0, battery_to_load=1.5)
    loads = served_load([idle, working, None], plant=EXAMPLE)
    assert loads[0] == pytest.approx(1.0 - draws.standby_kw)
    assert loads[1] == pytest.approx(2.0 - draws.standby_kw - draws.working_kw)
    assert loads[2] is None
    # A DC-bus load's session is load the plant measured, like any other.
    charging = _metered(5.0, battery_to_load=4.0)
    assert served_load([charging], plant=EXAMPLE) == [
        pytest.approx(5.0 - draws.standby_kw - draws.working_kw)
    ]


def test_a_routed_hour_is_the_model_priced_by_the_rule() -> None:
    starts = [datetime(2026, 9, 23, 19, 0, tzinfo=UTC) + k * SLOT for k in range(12)]
    window = [
        SlotFlow(start=at, soc_start=0.6, soc_end=0.6, pv_to_load=0.7, pv_to_battery=2.0)
        for at in starts
    ]
    load = [0.7] * 12
    prices = [0.373] * 12, [0.092] * 12
    hour = route_hour(EXAMPLE, window, load, *prices, day=144)
    modelled = EXAMPLE.project(starts, [SLOT] * 12, [2.7] * 12, load, 0.6)
    priced = price_flows(storage_of(EXAMPLE), modelled, *prices, 144, noise=OBSERVED)
    assert hour.regime is observed_regime(storage_of(EXAMPLE), modelled[0])
    assert (hour.resolved, hour.price, hour.event, hour.spilled) == (
        priced.resolved[0],
        priced.prices[0],
        priced.events[0],
        priced.spilled[0],
    )
    assert hour.event_at == (starts[priced.event_at[0]] if priced.resolved[0] else None)
    # The ground truth prices its own projection the same way.
    assert price_hour(storage_of(EXAMPLE), modelled, *prices, day=144) == hour


def test_a_routed_hour_takes_a_dc_bus_load_and_the_hold() -> None:
    # A plant model with a DC-bus load is routed with it, as projected.
    plant = _dc_plant()
    starts = [datetime(2026, 9, 23, 19, 0, tzinfo=UTC) + k * SLOT for k in range(12)]
    window = [
        SlotFlow(start=at, soc_start=0.5, soc_end=0.5, pv_to_load=0.7, pv_to_battery=2.0)
        for at in starts
    ]
    load, bus = [0.7] * 12, [6.0] * 12
    prices = [0.373] * 12, [0.092] * 12
    hours = {}
    for held in (True, False):
        hour = route_hour(plant, window, load, *prices, day=144, dc_load_kw=bus, held_at_start=held)
        modelled = plant.project(
            starts, [SLOT] * 12, [2.7] * 12, load, 0.5, dc_load_kw=bus, held_at_start=held
        )
        assert hour == price_hour(storage_of(plant), modelled, *prices, day=144)
        hours[held] = hour
    assert hours[True] != hours[False]


def test_the_horizon_buckets_name_hours_ahead() -> None:
    assert bucket_label(*BUCKETS[0]) == "0-1h"
    assert hours_ahead_bucket(timedelta(minutes=59)) == "0-1h"
    assert hours_ahead_bucket(timedelta(hours=1)) == "1-3h"  # [from, to)
    assert hours_ahead_bucket(BUCKETS[-1][1]) is None  # beyond the last


def test_the_checks_take_a_plant_with_a_battery() -> None:
    without = Plant(replace(config_of(EXAMPLE_PLANT), battery=None))
    with pytest.raises(ValueError, match="a plant with a battery"):
        storage_of(without)
