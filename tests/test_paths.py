"""The general dispatch: a plant with a DC bus.

- **Equivalence:** for every configuration today's arithmetic ran, and where it can run
  the same plant (AC PV through a lossless hybrid, a non-binding rating, a split battery
  behind a lossless hybrid), the general dispatch gives the legacy dispatch's flows
  (``legacy_dispatch``, the oracle) to float noise.
- **Constructed cases, by hand:** clipping a DC battery absorbs, the hybrid's rating
  netted, curtailed DC PV, a hybrid beside a battery on its own inverter.
- **Over generated plants:** the balance, the limits, the SOC band, the contract.
- **The storage's answers** under netting and for clipped DC PV, at the fitted
  battery's numbers, and a clipped fill priced at the wear.
"""

from dataclasses import replace
from datetime import UTC, datetime, time, timedelta

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from engine_fixtures import EXAMPLE_PLANT, NOW, PACIFIC, config_of, inputs
from legacy_dispatch import NativePlant, PlantStorage, assert_matches, native_of, simulate
from microgrid.engine import Engine, EngineConfig, Pricing, Served
from microgrid.engine.extend import ChargeSource, Event, Flows, SlotDispatch, SlotFlow, price_flows
from microgrid.engine.series import SLOT
from microgrid.engine.testing import assert_plant_model_contract
from microgrid.plant import (
    Battery,
    BatteryPart,
    Conversion,
    Dispatch,
    ExportWindow,
    GridCharge,
    GridLimits,
    HoldRule,
    Inverter,
    Load,
    Plant,
    PlantConfig,
    PlantConfigError,
    PvArray,
)
from microgrid.plant._battery import Grid
from microgrid.plant._config import ChargeOrder, Connection
from microgrid.plant._paths import Curve, PathStorage

T0 = datetime(2026, 9, 25, 7, 0, tzinfo=UTC)  # 00:00 PDT
HOURS = SLOT / timedelta(hours=1)
LOSSLESS = Conversion(eta_in=1.0, eta_out=1.0)
CONVERSION = Conversion(eta_in=0.97, eta_out=0.97, loss_in_per_kw=0.002, loss_out_per_kw=0.002)
#: This site's fitted battery, with the example's wear.
SITE = Battery(
    16.5,
    8.4,
    9.6,
    0.987,
    0.954,
    loss_charge_per_kw=0.0046,
    loss_discharge_per_kw=0.0015,
    meter_charge=0.987,
    meter_discharge=1.017,
    wear_charge_per_kwh=0.02,
    wear_discharge_per_kwh=0.02,
)
WINDOW = GridCharge(start=time(12), end=time(15), target_soc=0.65, timing="late")
EARLY = GridCharge(start=time(1), end=time(4), target_soc=0.5)
#: An evening battery export within every generated plant's limits.
EVENING = ExportWindow(start=time(17), end=time(20), power_kw=0.8, floor_soc=0.3)
EVENING_BATTERY_YIELDS = replace(EVENING, yields="battery")


def _starts(count: int, first: datetime = T0) -> list[datetime]:
    return [first + k * SLOT for k in range(count)]


def _storage(plant: Plant) -> PathStorage:
    storage = plant.storage
    assert isinstance(storage, PathStorage)
    return storage


# --- Equivalence with today's arithmetic -----------------------------------------

natives = st.builds(
    lambda eta, q, reserve, window: NativePlant(
        Battery(
            10.0,
            5.0,
            6.0,
            eta,
            eta,
            reserve_soc=reserve,
            saturation_soc=0.97,
            loss_charge_per_kw=q,
            loss_discharge_per_kw=q,
        ),
        PACIFIC,
        standby_kw=0.042,
        working_kw=0.049,
        grid_charge=window,
    ),
    st.floats(0.85, 1.0),
    st.sampled_from([0.0, 0.004]),
    st.floats(0, 0.2),
    st.sampled_from([None, WINDOW, GridCharge(start=time(1), end=time(5), target_soc=1.0)]),
)
days = st.lists(
    st.tuples(st.floats(0, 10, allow_nan=False), st.floats(0, 6, allow_nan=False)),
    min_size=1,
    max_size=72,
)
first_slots = st.sampled_from([T0, T0 + timedelta(hours=11), T0 + timedelta(hours=13)])


def _same(general: tuple[SlotFlow, ...], today: tuple[SlotFlow, ...]) -> None:
    assert len(general) == len(today)
    for mine, theirs in zip(general, today, strict=True):
        assert mine.soc_end == pytest.approx(theirs.soc_end, abs=1e-9)
        for name in (
            "battery_to_load",
            "grid_to_load",
            "grid_to_battery",
            "pv_export",
            "pv_curtail",
        ):
            assert getattr(mine, name) == pytest.approx(getattr(theirs, name), abs=1e-9), name
        # PV to the battery, by whichever path: AC through a lossless hybrid is DC.
        assert mine.pv_to_battery + mine.dc_pv_to_battery == pytest.approx(
            theirs.pv_to_battery, abs=1e-9
        )
        assert mine.dispatch.held is theirs.dispatch.held
        for name in ("pv_to_load", "inverter_draw", "conversion_loss"):
            assert getattr(mine, name) == pytest.approx(getattr(theirs, name), abs=1e-9), name
        if theirs.dispatch.topup_gap is None:
            assert mine.dispatch.topup_gap is None
        else:
            assert mine.dispatch.topup_gap == pytest.approx(theirs.dispatch.topup_gap, abs=1e-9)


def _answers_agree(
    mine: PathStorage,
    theirs: PlantStorage,
    general: tuple[SlotFlow, ...],
    today: tuple[SlotFlow, ...],
) -> None:
    """The general plant's storage answers as today's, flow for flow: the values to
    float, and each yes or no at the margins the price rule asks with (``_TOLERANCE``,
    ``MODEL.need_kw``) where the quantity it compares is clear of its limit (on it, the
    two paths' last bits may fall either side)."""
    for flow, base in zip(general, today, strict=True):
        assert mine.headroom(flow, HOURS) == pytest.approx(theirs.headroom(base, HOURS), abs=1e-6)
        assert mine.divertible_kw(flow) == pytest.approx(theirs.divertible_kw(base), abs=1e-9)
        for price in (0.1, 0.4):
            for source in (ChargeSource.PV, ChargeSource.GRID):
                assert mine.refill_value(price, source, flow) == pytest.approx(
                    theirs.refill_value(price, source, base), rel=1e-9
                )
            assert mine.displaced_value(price, flow) == pytest.approx(
                theirs.displaced_value(price, base), rel=1e-9
            )
            assert mine.dead_band(price, flow) == pytest.approx(
                theirs.dead_band(price, base), rel=1e-9
            )
        battery = theirs.battery
        charging = base.pv_to_battery + base.grid_to_battery
        if abs(charging - battery.charge_kw) > 1e-6:
            for source in (ChargeSource.PV, ChargeSource.GRID):
                assert mine.can_charge_more(flow, source, margin_kw=1e-9) is (
                    theirs.can_charge_more(base, source, margin_kw=1e-9)
                )
        if abs(base.battery_to_load - battery.discharge_kw) > 1e-6:
            assert mine.at_discharge_limit(flow, margin_kw=1e-9) is (
                theirs.at_discharge_limit(base, margin_kw=1e-9)
            )
        for more in (0.5, 3.0):
            if abs(base.battery_to_load + more - battery.discharge_kw) > 1e-6:
                assert mine.can_serve(flow, more) is theirs.can_serve(base, more)


#: Every configuration today's arithmetic ran (``_todays``), as legacy parameters: its
#: battery's losses and meter factors, an export limit, each kind of grid-charge window,
#: and the inverter's draws.
todays = st.builds(
    lambda eta, q, meters, reserve, saturation, limit, window, draws: NativePlant(
        Battery(
            10.0,
            5.0,
            6.0,
            eta,
            eta,
            reserve_soc=reserve,
            saturation_soc=saturation,
            loss_charge_per_kw=q,
            loss_discharge_per_kw=q,
            meter_charge=meters[0],
            meter_discharge=meters[1],
        ),
        PACIFIC,
        grid=Grid(export_limit_kw=limit),
        standby_kw=draws[0],
        working_kw=draws[1],
        grid_charge=window,
    ),
    st.floats(0.85, 1.0),
    st.sampled_from([0.0, 0.004]),
    st.sampled_from([(1.0, 1.0), (0.987, 1.017)]),
    st.floats(0, 0.2),
    st.sampled_from([1.0, 0.97]),
    st.sampled_from([None, 0.0, 3.0]),
    st.sampled_from(
        [
            None,
            WINDOW,
            GridCharge(start=time(1), end=time(5), target_soc=1.0),
            GridCharge(start=time(10), end=time(14), cover_until=time(21)),
            GridCharge(start=time(10), end=time(14), cover_until=time(7), timing="late"),
        ]
    ),
    st.sampled_from([(0.0, 0.0), (0.042, 0.049)]),
)


@settings(max_examples=300)
@given(
    todays,
    st.sampled_from(["ac", "dc_bus"]),
    st.sampled_from([0, 1, 2]),
    days,
    st.floats(0, 1),
    first_slots,
    st.sampled_from([None, SLOT / 3]),
)
def test_every_configuration_of_todays_runs_as_the_legacy_dispatch(
    native: NativePlant,
    connection: Connection,
    arrays: int,
    day: list[tuple[float, float]],
    soc: float,
    first: datetime,
    under_way: timedelta | None,
) -> None:
    # The general dispatch runs today's configurations, and gives the
    # legacy dispatch's flows to float noise (``legacy_dispatch.TOLERANCE``): a battery
    # on its own inverter or folded on a hybrid's DC bus, with no PV, one AC array, or
    # two sharing one series.
    config = config_of(native, connection=connection)
    config = replace(config, pv=tuple(PvArray(f"pv{k}", "ac") for k in range(arrays)))
    plant = Plant(config)
    assert native_of(plant) == native  # one of today's
    starts = _starts(len(day), first)
    sun = [d[0] if arrays else 0.0 for d in day]
    house = [d[1] for d in day]
    durations = [SLOT] * len(day)
    if under_way is not None:
        durations[0] = under_way
    assert_matches(
        plant.project(starts, durations, sun, house, soc),
        simulate(native, starts, sun, house, soc, durations=durations),
    )


@settings(max_examples=100)
@given(natives, days, st.floats(0, 1), first_slots)
def test_dc_pv_through_a_lossless_hybrid_is_ac_pv(
    native: NativePlant, day: list[tuple[float, float]], soc: float, first: datetime
) -> None:
    # A battery on its own inverter beside a hybrid that converts DC PV without loss
    # or rating: today's plant, with its PV on the hybrid's DC bus.
    beside = Plant(
        PlantConfig(
            tz=PACIFIC,
            pv=(PvArray("strings", "dc"),),
            battery=BatteryPart(
                native.battery,
                "ac",
                inverter=Inverter(standby_kw=native.standby_kw, working_kw=native.working_kw),
            ),
            hybrid=Inverter(conversion=LOSSLESS),
            dispatch=config_of(native).dispatch,
        )
    )
    starts, pv_kw, house = _starts(len(day), first), [d[0] for d in day], [d[1] for d in day]
    general = beside.project(starts, [SLOT] * len(day), pv_kw, house, soc)
    _same(general, simulate(native, starts, pv_kw, house, soc, step=SLOT))


@settings(max_examples=100)
@given(natives, days, st.floats(0, 1), first_slots)
def test_a_folded_battery_with_a_rating_that_never_binds_is_todays(
    native: NativePlant, day: list[tuple[float, float]], soc: float, first: datetime
) -> None:
    # This site's topology with an AC rating on the hybrid: the general dispatch, its
    # limits the folded path's AC-side ones through its curve.
    site = config_of(native, connection="dc_bus")
    assert site.hybrid is not None
    rated = Plant(replace(site, hybrid=replace(site.hybrid, ac_rating_kw=1000.0)))
    assert isinstance(rated.storage, PathStorage)
    starts, pv_kw, house = _starts(len(day), first), [d[0] for d in day], [d[1] for d in day]
    general = rated.project(starts, [SLOT] * len(day), pv_kw, house, soc)
    today = simulate(native, starts, pv_kw, house, soc, step=SLOT)
    _same(general, today)
    # Its storage answers as today's does, flow for flow: with nothing else on the bus,
    # each answer under netting is today's expression.
    _answers_agree(_storage(rated), PlantStorage(native.battery), general, today)


@settings(max_examples=100)
@given(natives, days, st.floats(0, 1), first_slots)
def test_a_split_battery_behind_a_lossless_hybrid_is_todays(
    native: NativePlant, day: list[tuple[float, float]], soc: float, first: datetime
) -> None:
    # The split form: the battery holds the DC bus to stored; a lossless hybrid makes
    # its DC bus the AC bus.
    split = Plant(
        PlantConfig(
            tz=PACIFIC,
            pv=(PvArray("roof", "ac"),),
            battery=BatteryPart(native.battery, "dc_bus", form="split"),
            hybrid=Inverter(
                standby_kw=native.standby_kw, working_kw=native.working_kw, conversion=LOSSLESS
            ),
            dispatch=config_of(native).dispatch,
        )
    )
    starts, pv_kw, house = _starts(len(day), first), [d[0] for d in day], [d[1] for d in day]
    general = split.project(starts, [SLOT] * len(day), pv_kw, house, soc)
    today = simulate(native, starts, pv_kw, house, soc, step=SLOT)
    _same(general, today)
    _answers_agree(_storage(split), PlantStorage(native.battery), general, today)


# --- Constructed cases, by hand ----------------------------------------------------


def _lossless_folded(*, rating: float, dc_charge: float, export: float | None = None) -> Plant:
    """A folded battery with no loss anywhere, DC PV on its bus: the cases' arithmetic
    is then kW in and out."""
    battery = Battery(10.0, 20.0, 20.0, 1.0, 1.0)
    return Plant(
        PlantConfig(
            tz=PACIFIC,
            pv=(PvArray("strings", "dc"),),
            battery=BatteryPart(battery, "dc_bus", dc_charge_kw=dc_charge, dc_discharge_kw=10.0),
            hybrid=Inverter(ac_rating_kw=rating),
            grid=GridLimits(export_limit_kw=export),
        )
    )


def test_clipping_a_dc_battery_absorbs() -> None:
    # 8 kW DC, the house 1 kW, a 5 kW rating, the battery taking 2 kW: the house 1, the
    # battery 2, export 4 (the rating less the house), and 1 clipped.
    plant = _lossless_folded(rating=5.0, dc_charge=2.0)
    (flow,) = plant.project(_starts(1), [SLOT], [8.0], [1.0], 0.5)
    assert flow.pv_to_load == pytest.approx(1.0)
    assert flow.dc_pv_to_battery == pytest.approx(2.0)
    assert flow.pv_export == pytest.approx(4.0)
    assert flow.pv_clipped == pytest.approx(1.0)
    assert flow.dispatch.crossing == pytest.approx(5.0)
    # Full, the battery takes none and more is clipped: the rating binds.
    (full,) = plant.project(_starts(1), [SLOT], [8.0], [1.0], 1.0)
    assert full.dc_pv_to_battery == 0.0 and full.pv_clipped == pytest.approx(3.0)


def test_the_rating_binds_on_the_net() -> None:
    # In the window the grid tops up the DC battery while DC PV serves the house, and
    # the hybrid nets the two, within its 0.5 kW rating. DC PV 3, the house 2: the
    # rating lets DC PV give the house 0.5, the battery takes the 2.5 left, and the
    # grid's top-up crosses back net of those 0.5, to the rating rectifying: 1 more.
    battery = Battery(10.0, 20.0, 20.0, 1.0, 1.0)
    window = GridCharge(start=time(0), end=time(1), target_soc=1.0)
    config = PlantConfig(
        tz=PACIFIC,
        pv=(PvArray("strings", "dc"),),
        battery=BatteryPart(battery, "dc_bus", dc_charge_kw=4.0, dc_discharge_kw=10.0),
        hybrid=Inverter(ac_rating_kw=0.5),
        dispatch=replace(config_of(EXAMPLE_PLANT).dispatch, grid_charge=(window,)),
    )
    (flow,) = Plant(config).project(_starts(1), [SLOT], [3.0], [2.0], 0.1)
    assert flow.pv_to_load == pytest.approx(0.5)
    assert flow.dc_pv_to_battery == pytest.approx(2.5)
    assert flow.grid_to_battery == pytest.approx(1.0)
    assert flow.grid_to_load == pytest.approx(1.5)
    assert flow.dispatch.crossing == pytest.approx(-0.5)  # the rating, rectifying
    assert flow.dispatch.battery_port == pytest.approx(3.5)
    # The meter: the grid gives 2.5, the house's 2 and the 0.5 crossing to the battery.
    assert flow.grid_to_load + flow.grid_to_battery == pytest.approx(2.5)


def test_curtailed_dc_pv_is_spill_and_its_conversion_is_spared() -> None:
    # A hybrid without a battery, DC PV 5, the house 1, an export limit of 2: the house
    # 1, export 2, curtailed 2 (a load could take it), nothing clipped; the conversion
    # it was spared counts as a loss so the DC balances.
    plant = Plant(
        PlantConfig(
            tz=PACIFIC,
            pv=(PvArray("strings", "dc"),),
            hybrid=Inverter(conversion=CONVERSION),
            grid=GridLimits(export_limit_kw=2.0),
        )
    )
    (flow,) = plant.project(_starts(1), [SLOT], [5.0], [1.0], None)
    curve = Curve(1 / 0.97, 0.002, 0.97, 0.002)
    assert flow.pv_export == pytest.approx(2.0)
    # Curtailed: the AC the rest could have delivered, its conversion's loss and all.
    assert flow.pv_curtail == pytest.approx(curve.ac(5.0) - 1.0 - 2.0)
    assert flow.pv_clipped == 0.0
    sources = 5.0 + flow.grid_to_load
    sinks = 1.0 + flow.pv_export + flow.pv_curtail + flow.conversion_loss
    assert sources == pytest.approx(sinks)


def test_dc_pv_inverts_to_a_battery_on_its_own_inverter_within_the_rating() -> None:
    battery = Battery(10.0, 6.0, 6.0, 1.0, 1.0)
    plant = Plant(
        PlantConfig(
            tz=PACIFIC,
            pv=(PvArray("strings", "dc"),),
            battery=BatteryPart(battery, "ac", inverter=Inverter()),
            hybrid=Inverter(ac_rating_kw=4.0, conversion=LOSSLESS),
        )
    )
    (flow,) = plant.project(_starts(1), [SLOT], [7.0], [1.0], 0.5)
    # The house 1, the battery 3 (the rating's rest), and 3 clipped: the battery is on
    # the AC bus, so what the rating holds back cannot reach it.
    assert flow.pv_to_load == pytest.approx(1.0)
    assert flow.pv_to_battery == pytest.approx(3.0) and flow.dc_pv_to_battery == 0.0
    assert flow.pv_clipped == pytest.approx(3.0)
    assert not _storage(plant).takes_clipped_pv(flow)


def test_arrays_share_one_forecast_by_their_dc_ratings_each_capped() -> None:
    plant = Plant(
        PlantConfig(
            tz=PACIFIC,
            pv=(PvArray("east", "dc", dc_rating_kw=2.0), PvArray("west", "dc", dc_rating_kw=6.0)),
            hybrid=Inverter(conversion=LOSSLESS),
        )
    )
    (flow,) = plant.project(_starts(1), [SLOT], [10.0], [0.0], None)
    # 10 shared 1:3 is 2.5 and 7.5; each capped at its own rating: 2 and 6, 2 clipped.
    assert flow.pv_export == pytest.approx(8.0) and flow.pv_clipped == pytest.approx(2.0)


def test_pv_as_one_series_is_refused_where_the_arrays_cannot_share_it() -> None:
    # PV per array: a mixed plant takes it per array, and several capped
    # arrays share one series only by each one's DC rating.
    mixed = Plant(
        PlantConfig(
            tz=PACIFIC,
            pv=(PvArray("roof", "ac"), PvArray("strings", "dc")),
            hybrid=Inverter(conversion=CONVERSION),
        )
    )
    with pytest.raises(ValueError, match="AC and DC arrays takes PV per array"):
        mixed.project(_starts(1), [SLOT], [1.0], [1.0], None)
    partly = Plant(
        PlantConfig(
            tz=PACIFIC,
            pv=(PvArray("east", "dc", dc_rating_kw=3.0), PvArray("west", "dc")),
            hybrid=Inverter(conversion=CONVERSION),
        )
    )
    with pytest.raises(ValueError, match="share one PV series by their DC ratings"):
        partly.project(_starts(1), [SLOT], [1.0], [1.0], None)
    partly.project(_starts(1), [SLOT], {"east": [1.0], "west": [2.0]}, [1.0], None)
    with pytest.raises(ValueError, match="must name exactly"):
        partly.project(_starts(1), [SLOT], {"east": [1.0]}, [1.0], None)
    # Without a cap the sharing does not matter: two unrated arrays sum.
    Plant(
        PlantConfig(
            tz=PACIFIC,
            pv=(PvArray("east", "ac"), PvArray("west", "ac")),
            battery=config_of(EXAMPLE_PLANT).battery,
        )
    )


def test_a_folded_battery_that_stores_more_than_it_draws_is_refused_beside_dc_pv() -> None:
    # Netted, the dead band's width within a slot is meter_charge / meter_discharge: a
    # DC-to-stored step that returns more than it takes would invert the band.
    battery = replace(SITE, meter_charge=1.02, meter_discharge=1.0)
    with pytest.raises(PlantConfigError, match="meter_charge <= meter_discharge"):
        PlantConfig(
            tz=PACIFIC,
            pv=(PvArray("strings", "dc"),),
            battery=BatteryPart(battery, "dc_bus", dc_charge_kw=8.0, dc_discharge_kw=9.0),
            hybrid=Inverter(ac_rating_kw=5.0),
        )


# --- Over generated plants -----------------------------------------------------------


@st.composite
def dc_plants(draw: st.DrawFn) -> Plant:
    shape = draw(st.sampled_from(["folded", "split", "beside", "hybrid only"]))
    rating = draw(st.one_of(st.none(), st.floats(1.0, 10.0)))
    export = draw(st.one_of(st.none(), st.floats(0.0, 6.0)))
    import_limit = draw(st.one_of(st.none(), st.floats(0.5, 8.0)))
    window = draw(st.sampled_from([(), (WINDOW,), (EARLY, WINDOW)]))
    order: ChargeOrder = draw(st.sampled_from(["pv_first", "target_first"]))
    exports = draw(st.sampled_from([(), (EVENING,), (EVENING_BATTERY_YIELDS,)]))
    loads = (Load("house"), Load("ev", "dc_bus")) if draw(st.booleans()) else (Load("house"),)
    holds: tuple[HoldRule, ...] = ()
    if len(loads) > 1 and shape != "hybrid only" and draw(st.booleans()):
        holds = (HoldRule(dc_load_above_kw=1.0, start_below_soc=0.65),)
    if shape == "hybrid only" or (export is not None and export < EVENING.power_kw):
        exports = ()  # validation: a window needs a battery, and fits the export limit
    conversion = CONVERSION if draw(st.booleans()) else LOSSLESS
    working = draw(st.sampled_from([0.0, 0.05]))
    grid = GridLimits(export_limit_kw=export, import_limit_kw=import_limit)
    dispatch = replace(
        config_of(EXAMPLE_PLANT).dispatch,
        grid_charge=window,
        charge_order=order,
        export_windows=exports,
        hold_rules=holds,
    )
    pv_arrays = (PvArray("strings", "dc", dc_rating_kw=draw(st.sampled_from([None, 7.0]))),)
    if shape == "folded":
        config = PlantConfig(
            tz=PACIFIC,
            pv=pv_arrays,
            battery=BatteryPart(SITE, "dc_bus", dc_charge_kw=7.5, dc_discharge_kw=8.5),
            hybrid=Inverter(ac_rating_kw=rating or 6.0, standby_kw=0.04, working_kw=working),
            grid=grid,
            loads=loads,
            dispatch=dispatch,
        )
    elif shape == "split":
        config = PlantConfig(
            tz=PACIFIC,
            pv=pv_arrays,
            battery=BatteryPart(Battery(12.0, 6.0, 6.0, 0.96, 0.96), "dc_bus", form="split"),
            hybrid=Inverter(
                ac_rating_kw=rating, standby_kw=0.04, working_kw=working, conversion=conversion
            ),
            grid=grid,
            loads=loads,
            dispatch=dispatch,
        )
    elif shape == "beside":
        config = PlantConfig(
            tz=PACIFIC,
            pv=pv_arrays,
            battery=BatteryPart(
                SITE,
                "ac",
                inverter=Inverter(ac_rating_kw=5.0, standby_kw=0.04, working_kw=working),
            ),
            hybrid=Inverter(ac_rating_kw=rating, conversion=conversion),
            grid=grid,
            loads=loads,
            dispatch=dispatch,
        )
    else:
        config = PlantConfig(
            tz=PACIFIC,
            pv=pv_arrays,
            hybrid=Inverter(ac_rating_kw=rating, conversion=conversion),
            grid=grid,
            loads=loads,
        )
    return Plant(config)


@settings(max_examples=200)
@given(dc_plants(), days, st.floats(0, 1), first_slots, st.data())
def test_every_dc_plant_balances_and_keeps_its_limits(
    plant: Plant, day: list[tuple[float, float]], soc: float, first: datetime, data: st.DataObject
) -> None:
    storage = plant.storage
    start_soc = soc if storage is not None else None
    starts, pv_kw, house = _starts(len(day), first), [d[0] for d in day], [d[1] for d in day]
    ev = [0.0] * len(day)
    if plant.has_dc_loads:
        ev = data.draw(
            st.lists(
                st.sampled_from([0.0, 0.5, 3.0, 7.0, 11.0]), min_size=len(day), max_size=len(day)
            )
        )
    flows = plant.project(starts, [SLOT] * len(day), pv_kw, house, start_soc, dc_load_kw=ev)
    assert len(flows) == len(day)
    paths = plant._paths
    assert paths is not None
    curve = paths.curve
    capacity = storage.capacity_kwh if storage is not None else 0.0
    limit = plant.config.grid.export_limit_kw
    cap = plant.config.grid.import_limit_kw
    for flow, sun, need, dc_need in zip(flows, pv_kw, house, ev, strict=True):
        stored = 0.0
        if flow.soc_end is not None and flow.soc_start is not None:
            stored = (flow.soc_end - flow.soc_start) * capacity / HOURS
        sources = sun + flow.grid_to_load + flow.grid_to_battery
        sinks = (
            need
            + dc_need
            - flow.unserved
            + flow.inverter_draw
            + flow.pv_export
            + flow.battery_export
            + flow.pv_curtail
            + flow.pv_clipped
            + flow.conversion_loss
            + stored
        )
        assert sources == pytest.approx(sinks, abs=1e-6)
        for name in (
            "pv_to_load",
            "pv_to_battery",
            "dc_pv_to_battery",
            "dc_pv_to_ac",
            "pv_export",
            "pv_curtail",
            "pv_clipped",
            "battery_to_load",
            "grid_to_load",
            "grid_to_battery",
            "conversion_loss",
            "dc_pv_to_dc_load",
            "battery_to_dc_load",
            "ac_to_dc_load",
            "unserved",
        ):
            assert getattr(flow, name) >= -1e-9, name
        dc_served = flow.dc_pv_to_dc_load + flow.battery_to_dc_load + flow.ac_to_dc_load
        assert dc_served <= dc_need + 1e-9
        assert -1e-9 <= flow.dispatch.unserved_unreachable <= flow.unserved + 1e-9
        if cap is None:
            # Without an import limit only the hybrid's rating leaves a load unserved;
            # a battery on the bus serves it first, so only at its reserve or limit.
            assert flow.unserved == pytest.approx(dc_need - dc_served, abs=1e-6)
            port = paths.port
            if flow.unserved > 1e-6 and port is not None and paths.on_bus:
                assert flow.soc_end is not None
                at_reserve = flow.soc_end <= port.battery.reserve_soc + 1e-6
                assert at_reserve or -flow.dispatch.battery_port >= port.discharge_max - 1e-6
        if curve is not None:
            assert abs(curve.ac(flow.dispatch.crossing)) <= paths.rating + 1e-9
        if limit is not None:
            assert flow.pv_export <= limit + 1e-9
        if cap is not None:
            assert flow.grid_to_load + flow.grid_to_battery <= cap + 1e-9
        if paths.port is not None:
            port = paths.port
            assert flow.dispatch.battery_port <= port.charge_max + 1e-9
            assert -flow.dispatch.battery_port <= port.discharge_max + 1e-9
            assert flow.soc_start is not None and flow.soc_end is not None
            battery = port.battery
            # The SOC never moves further outside its band.
            if flow.soc_end > battery.saturation_soc + 1e-9:
                assert flow.soc_end <= flow.soc_start + 1e-9
            if flow.soc_end < battery.reserve_soc - 1e-9:
                assert flow.soc_end >= flow.soc_start - 1e-9
        else:
            assert flow.soc_end is None and flow.moving == 0.0


def test_every_dc_plant_keeps_the_port_contract() -> None:
    for plant in (
        _lossless_folded(rating=5.0, dc_charge=2.0),
        Plant(
            PlantConfig(
                tz=PACIFIC,
                pv=(PvArray("strings", "dc"),),
                battery=BatteryPart(Battery(12.0, 6.0, 6.0, 0.96, 0.96), "dc_bus", form="split"),
                hybrid=Inverter(ac_rating_kw=6.0, conversion=CONVERSION),
            )
        ),
        Plant(
            PlantConfig(
                tz=PACIFIC,
                pv=(PvArray("strings", "dc"),),
                hybrid=Inverter(ac_rating_kw=4.0, conversion=CONVERSION),
            )
        ),
        # A DC-bus load, held for.
        Plant(
            PlantConfig(
                tz=PACIFIC,
                pv=(PvArray("roof", "ac"),),
                battery=BatteryPart(SITE, "dc_bus", dc_charge_kw=7.5, dc_discharge_kw=8.5),
                hybrid=Inverter(ac_rating_kw=6.0, standby_kw=0.04, working_kw=0.05),
                loads=(Load("house"), Load("ev", "dc_bus")),
                dispatch=Dispatch(hold_rules=(HoldRule(1.0, 0.65),)),
            )
        ),
    ):
        assert_plant_model_contract(plant)


# --- The storage's answers, at the fitted battery's numbers ------------------------


def _site_on_the_bus() -> PathStorage:
    """This site's fitted battery, folded on a hybrid with DC PV beside it."""
    return _storage(
        Plant(
            PlantConfig(
                tz=PACIFIC,
                pv=(PvArray("strings", "dc"),),
                battery=BatteryPart(SITE, "dc_bus", dc_charge_kw=8.0, dc_discharge_kw=9.0),
                hybrid=Inverter(ac_rating_kw=10.0),
            )
        )
    )


def _flow(
    *,
    crossing: float = 0.0,
    battery_port: float = 0.0,
    pv_clipped: float = 0.0,
    dc_pv_to_ac: float = 0.0,
) -> SlotFlow:
    return SlotFlow(
        start=T0,
        soc_start=0.5,
        soc_end=0.5,
        pv_clipped=pv_clipped,
        dc_pv_to_ac=dc_pv_to_ac,
        dispatch=SlotDispatch(crossing=crossing, battery_port=battery_port),
    )


def test_the_margins_under_netting_are_one_sided() -> None:
    storage = _site_on_the_bus()
    curve = Curve.folded(SITE)
    # At a crossing of 1 kW DC inverting, a charge stores 1.0205 per AC kWh, and
    # at 1 kW rectifying a discharge gives 0.9923 AC per stored kWh.
    inverting = _flow(crossing=1.0)
    assert storage.refill_value(0.0, ChargeSource.GRID, inverting) == pytest.approx(
        SITE.wear_charge_per_kwh / 1.0205, abs=1e-4
    )
    rectifying = _flow(crossing=-1.0)
    assert storage.displaced_value(1.0, rectifying) == pytest.approx(0.9923 * 0.98, abs=1e-4)
    # From rest, the low-power sides: today's.
    assert storage.displaced_value(1.0) == pytest.approx(SITE.eta_discharge * 0.98)
    # The kink: from 0 a charge rectifies, a hair above 0 it replaces inverting PV.
    assert curve.slope_down(0.0) == pytest.approx(1.0)
    assert SITE.meter_charge / curve.slope_down(1e-12) == pytest.approx(1.0173, abs=1e-4)
    # Each side without the battery's own flow the other way: a charging slot's
    # discharge side reads the crossing with the charge taken out.
    charging = _flow(crossing=-3.0, battery_port=3.0)
    low, high = storage.dead_band(0.1, charging)
    assert high == pytest.approx(0.1 / SITE.eta_discharge + 0.02)
    assert low < high


def test_a_dc_pv_refill_costs_the_wear_when_clipped_and_the_export_when_crossing() -> None:
    storage = _site_on_the_bus()
    # Clipped DC PV, at a price of 0: the wear through the DC path, 0.0203.
    clipped = _flow(pv_clipped=0.8, crossing=5.0)
    assert storage.refill_value(0.0, ChargeSource.DC_PV, clipped) == pytest.approx(0.0203, abs=1e-4)
    # DC PV crossing at 3 kW, exported at 0.10: 0.1176.
    crossing = _flow(dc_pv_to_ac=3.0, crossing=3.0)
    assert storage.refill_value(0.10, ChargeSource.DC_PV, crossing) == pytest.approx(
        0.1176, abs=1e-4
    )
    assert storage.takes_clipped_pv(clipped)


def test_the_rating_bounds_what_the_battery_can_give() -> None:
    storage = _site_on_the_bus()
    # The hybrid already gives 9.9 of its 10 kW AC: the battery has 0.1 left to give.
    near = _flow(crossing=Curve.folded(SITE).dc(9.9))
    assert storage.at_discharge_limit(near, margin_kw=0.2)
    assert not storage.at_discharge_limit(near, margin_kw=0.05)
    assert storage.can_serve(near, 0.05) and not storage.can_serve(near, 0.2)
    assert storage.headroom(near, HOURS) <= 0.1 + 1e-9


def test_dc_pv_does_not_reach_a_battery_on_its_own_inverter() -> None:
    battery = Battery(10.0, 6.0, 6.0, 0.95, 0.95)
    plant = Plant(
        PlantConfig(
            tz=PACIFIC,
            pv=(PvArray("strings", "dc"),),
            battery=BatteryPart(battery, "ac", inverter=Inverter()),
            hybrid=Inverter(conversion=CONVERSION),
        )
    )
    storage = _storage(plant)
    with pytest.raises(ValueError, match="not on the DC bus"):
        storage.charge_wear_rate(ChargeSource.DC_PV)
    assert not storage.takes_clipped_pv(_flow())


# --- The price rule: a clipped fill -----------------------------------------------


def test_a_kwh_before_a_clipping_fill_costs_the_wear() -> None:
    # A morning discharging, then the battery full at noon with DC PV clipped: a kWh
    # used in the morning is refilled from clipped PV, at the wear through the DC path.
    storage = _site_on_the_bus()
    flows = [
        SlotFlow(
            start=T0,
            soc_start=0.6,
            soc_end=0.59,
            battery_to_load=0.5,
            dispatch=SlotDispatch(crossing=0.5, battery_port=-0.5),
        ),
        SlotFlow(
            start=T0 + SLOT,
            soc_start=1.0,
            soc_end=1.0,
            pv_to_load=1.0,
            pv_export=6.0,
            pv_clipped=2.0,
            dc_pv_to_ac=7.5,
            dispatch=SlotDispatch(bus_clipped=2.0, crossing=7.5),
        ),
    ]
    priced = price_flows(storage, flows, [0.4, 0.4], [0.1, 0.1], day=2)
    assert priced.make_up_event[0] is Event.FILL
    assert priced.make_up[0] == 0.0
    assert priced.stored_value[0] == pytest.approx(0.0203, abs=1e-4)
    assert priced.spilled[0]


def test_the_engine_values_a_dc_coupled_hybrid() -> None:
    plant = Plant(
        PlantConfig(
            tz=PACIFIC,
            pv=(PvArray("strings", "dc", dc_rating_kw=9.0),),
            battery=BatteryPart(SITE, "dc_bus", dc_charge_kw=8.0, dc_discharge_kw=9.0),
            hybrid=Inverter(ac_rating_kw=5.0, standby_kw=0.04, working_kw=0.05),
            dispatch=replace(config_of(EXAMPLE_PLANT).dispatch, grid_charge=(WINDOW,)),
        )
    )
    engine = Engine(EngineConfig(plant=plant, pricing=Pricing(scenarios=3)))
    valuation = engine.value(inputs(), at=NOW)
    assert valuation is not None and valuation.degraded == ()
    # Valued end to end: every price between nothing and the import through the
    # battery's losses and wear, and the battery valued where it has a kWh to give.
    for slot in valuation.slots:
        assert 0.0 <= slot.marginal_cost <= slot.p_import / 0.9 + 0.05
        assert 0.0 <= slot.certainty_spill <= 1.0
    assert any(slot.battery_value is not None for slot in valuation.slots)
    # At noon the DC PV fills the battery and spills: a kWh there costs the export.
    assert valuation.slots[0].certainty_spill == pytest.approx(1.0)


def test_a_hybrid_beside_a_battery_draws_both_standbys() -> None:
    battery = Battery(10.0, 6.0, 6.0, 0.95, 0.95)
    plant = Plant(
        PlantConfig(
            tz=PACIFIC,
            pv=(PvArray("strings", "dc"),),
            battery=BatteryPart(battery, "ac", inverter=Inverter(standby_kw=0.03, working_kw=0.05)),
            hybrid=Inverter(standby_kw=0.02, conversion=CONVERSION),
        )
    )
    assert plant.battery is battery
    idle = Flows(1.0, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
    working = Flows(1.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0)
    assert plant.own_draw_kw(idle) == pytest.approx(0.05)
    assert plant.own_draw_kw(working) == pytest.approx(0.10)
    (flow,) = plant.project(_starts(1), [SLOT], [0.0], [1.0], 0.5)
    assert flow.inverter_draw == pytest.approx(0.10)  # both standbys, and it works


# --- Corner cases -------------------------------------------------------------------


def test_dc_pv_charging_alone_is_priced_as_pv_diverted() -> None:
    # A folded battery charging from DC PV only: a kWh diverts that PV (the dead band's
    # lower end), not a discharge more.
    storage = _site_on_the_bus()
    charging = SlotFlow(
        start=T0,
        soc_start=0.5,
        soc_end=0.52,
        pv_to_load=1.0,
        dc_pv_to_battery=2.95,
        dc_pv_to_ac=1.03,
        dispatch=SlotDispatch(crossing=1.03, battery_port=2.95),
    )
    full = SlotFlow(
        start=T0 + SLOT,
        soc_start=1.0,
        soc_end=1.0,
        pv_to_load=1.0,
        pv_export=3.0,
        dc_pv_to_ac=4.1,
        dispatch=SlotDispatch(crossing=4.1),
    )
    priced = price_flows(storage, [charging, full], [0.4, 0.4], [0.1, 0.1], day=2)
    assert priced.served[0] is Served.PV
    low, _ = storage.dead_band(priced.stored_value[0], charging)
    assert priced.prices[0] == pytest.approx(low)


def test_undoing_the_batterys_own_charge_is_not_battery_power() -> None:
    # A lossless folded battery with a 5 kW rating, the grid topping it up at the rating
    # (crossing -5, the port taking 5): it can give at most 5 kW AC, not 10.
    battery = Battery(10.0, 20.0, 20.0, 1.0, 1.0)
    storage = _storage(
        Plant(
            PlantConfig(
                tz=PACIFIC,
                pv=(PvArray("strings", "dc"),),
                battery=BatteryPart(battery, "dc_bus", dc_charge_kw=10.0, dc_discharge_kw=10.0),
                hybrid=Inverter(ac_rating_kw=5.0),
            )
        )
    )
    topping = SlotFlow(
        start=T0,
        soc_start=0.5,
        soc_end=0.55,
        grid_to_battery=5.0,
        dispatch=SlotDispatch(crossing=-5.0, battery_port=5.0),
    )
    assert storage.headroom(topping, HOURS) == pytest.approx(5.0)
    assert storage.can_serve(topping, 5.0) and not storage.can_serve(topping, 8.0)


def test_an_arrays_own_cap_is_not_pv_the_battery_can_take() -> None:
    # A full DC battery, its array capped at 7 kW DC, PV 8: the 1 kW capped never
    # reaches the bus, so the fill is the export, not clipped PV at the wear.
    plant = Plant(
        PlantConfig(
            tz=PACIFIC,
            pv=(PvArray("strings", "dc", dc_rating_kw=7.0),),
            battery=BatteryPart(SITE, "dc_bus", dc_charge_kw=8.0, dc_discharge_kw=9.0),
            hybrid=Inverter(ac_rating_kw=10.0),
        )
    )
    (full,) = plant.project(_starts(1), [SLOT], [8.0], [1.0], 1.0)
    assert full.pv_clipped == pytest.approx(1.0) and full.dispatch.bus_clipped == 0.0
    morning = SlotFlow(
        start=T0,
        soc_start=0.6,
        soc_end=0.59,
        battery_to_load=0.5,
        dispatch=SlotDispatch(crossing=0.5, battery_port=-0.5),
    )
    priced = price_flows(_storage(plant), [morning, full], [0.4, 0.4], [0.1, 0.1], day=2)
    assert priced.make_up_event[0] is Event.FILL and priced.make_up[0] == pytest.approx(0.1)


def test_clipped_pv_beyond_reach_does_not_stop_a_top_up() -> None:
    # A battery on its own inverter beside a 4 kW hybrid, DC PV 5, the house 1: the 1
    # kW the hybrid clips cannot reach the battery, so the grid can still top it up.
    battery = Battery(16.0, 8.0, 8.0, 0.95, 0.95)
    plant = Plant(
        PlantConfig(
            tz=PACIFIC,
            pv=(PvArray("strings", "dc"),),
            battery=BatteryPart(battery, "ac", inverter=Inverter()),
            hybrid=Inverter(ac_rating_kw=4.0, conversion=LOSSLESS),
        )
    )
    flow = SlotFlow(
        start=T0,
        soc_start=0.5,
        soc_end=0.52,
        pv_to_load=1.0,
        pv_to_battery=3.0,
        grid_to_battery=2.0,
        pv_clipped=1.0,
        dc_pv_to_ac=4.0,
        dispatch=SlotDispatch(bus_clipped=1.0, crossing=4.0, battery_port=5.0),
    )
    assert _storage(plant).can_charge_more(flow, ChargeSource.GRID, margin_kw=1e-9)


def test_a_rated_ac_array_without_a_battery_is_capped() -> None:
    plant = Plant(PlantConfig(tz=PACIFIC, pv=(PvArray("roof", "ac", ac_rating_kw=3.0),)))
    (flow,) = plant.project(_starts(1), [SLOT], [5.0], [1.0], None)
    assert (flow.pv_to_load, flow.pv_export, flow.pv_clipped) == pytest.approx((1.0, 2.0, 2.0))
