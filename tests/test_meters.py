"""Meters by configuration: which flows a plant meters and where, read as a
``MeterReading`` and split between sources and sinks.

- **The configuration:** the default set (today's), the round trip, every rule.
- **Today's branch:** a sign, a consumption meter, a battery metered at its AC side.
- **A plant with a DC bus:** constructed splits, and over generated plants, the split
  of what the dispatch's own flows would meter gives back its grid, consumption,
  battery and crossing.
"""

from dataclasses import replace
from datetime import UTC, datetime, time, timedelta

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from engine_fixtures import EXAMPLE_PLANT, NOW, PACIFIC, config_of, inputs, reading, site_meters
from legacy_dispatch import consumption_kw
from microgrid.engine.series import SLOT
from microgrid.plant import (
    Battery,
    BatteryPart,
    Conversion,
    Dispatch,
    ExportWindow,
    GridLimits,
    Inverter,
    Load,
    Meter,
    Plant,
    PlantConfig,
    PlantConfigError,
    PvArray,
)
from test_paths import days, dc_plants

T0 = datetime(2026, 9, 25, 19, 0, tzinfo=UTC)
LOSSLESS = Conversion(eta_in=1.0, eta_out=1.0)
CLEAN = Battery(10.0, 20.0, 20.0, 1.0, 1.0)


# --- The configuration -------------------------------------------------------------


def test_the_default_set_is_todays() -> None:
    site = config_of(EXAMPLE_PLANT)
    assert site.meters is None
    assert site.meter_set == (Meter("pv", "pv"), Meter("grid", "grid"), Meter("battery", "battery"))
    mixed = PlantConfig(
        tz=PACIFIC,
        pv=(PvArray("roof", "ac"), PvArray("strings", "dc")),
        hybrid=Inverter(conversion=LOSSLESS),
        loads=(Load("house"), Load("ev", "dc_bus")),
    )
    assert mixed.meter_set == (
        Meter("pv:roof", "pv", array="roof"),
        Meter("pv:strings", "pv", array="strings"),
        Meter("grid", "grid"),
        Meter("dc_load", "dc_load"),
    )


def test_meters_round_trip() -> None:
    config = replace(
        config_of(EXAMPLE_PLANT),
        meters=(
            Meter("solar", "pv", array="pv"),
            Meter("utility", "grid", sign=-1.0),
            Meter("bank", "battery", at="dc"),
        ),
    )
    mapping = config.to_mapping()
    assert mapping["meters"] == [
        {"name": "solar", "measures": "pv", "array": "pv", "sign": 1.0},
        {"name": "utility", "measures": "grid", "sign": -1.0},
        {"name": "bank", "measures": "battery", "at": "dc", "sign": 1.0},
    ]
    assert PlantConfig.from_mapping(mapping, tz=PACIFIC) == config
    assert "meters" not in config_of(EXAMPLE_PLANT).to_mapping()  # the default stays unsaid


def _errors(meters: tuple[Meter, ...]) -> set[str]:
    """This site's configuration (the battery on the hybrid's DC bus) with ``meters``."""
    with pytest.raises(PlantConfigError) as raised:
        replace(config_of(EXAMPLE_PLANT, connection="dc_bus"), meters=meters)
    return set(raised.value.errors)


def test_each_meter_meters_something_the_plant_has_once() -> None:
    grid, battery = Meter("grid", "grid"), Meter("battery", "battery")
    assert _errors(meters=(Meter("pv", "pv", array="roof"), grid, battery)) == {
        "meters[0].array: 'roof' is not an array",
        "meters: every array's PV must be metered, each or together",
    }
    assert _errors(meters=(Meter("pv", "pv"), Meter("pv", "grid"), battery)) == {
        "meters[1].name: 'pv' is used twice"
    }
    assert _errors(meters=(Meter("pv", "pv"), grid, Meter("g2", "grid"), battery)) == {
        "meters[2]: a second grid meter"
    }
    assert _errors(meters=(Meter("pv", "pv"), Meter("again", "pv", array="pv"), grid, battery)) == {
        "meters[1]: meters PV another meter meters"
    }
    assert _errors(meters=(Meter("pv", "pv"), grid, Meter("b", "battery", at="ac"))) == {
        "meters[2].at: a battery on a DC bus is metered at its DC side"
    }
    assert _errors(meters=(Meter("pv", "pv", at="dc"), grid, battery)) == {
        "meters[0].at: only a battery meter has a side"
    }
    assert _errors(meters=(Meter("pv", "pv"), Meter("grid", "grid", array="pv"), battery)) == {
        "meters[1].array: only a PV meter meters an array"
    }
    assert _errors(meters=(Meter("pv", "pv"), Meter("grid", "grid", sign=2.0), battery)) == {
        "meters[1].sign: must be 1 or -1"
    }
    assert _errors(meters=(Meter("pv", "pv"), battery)) == {
        "meters: the grid or the consumption must be metered"
    }
    assert _errors(meters=(Meter("pv", "pv"), grid, battery, Meter("house", "consumption"))) == {
        "meters: the grid or the consumption, not both (the balance gives the other)"
    }
    assert _errors(meters=(Meter("pv", "pv"), grid)) == {"meters: the battery must be metered"}
    assert _errors(meters=(grid, battery)) == {
        "meters: every array's PV must be metered, each or together"
    }
    assert _errors(meters=(Meter("pv", "pv"), grid, battery, Meter("ev", "dc_load"))) == {
        "meters[3]: a DC-bus load meter needs a DC-bus load"
    }
    no_battery = PlantConfig(tz=PACIFIC, pv=(PvArray("roof", "ac"),))
    with pytest.raises(PlantConfigError, match="a battery meter needs a battery"):
        replace(no_battery, meters=(Meter("pv", "pv"), Meter("grid", "grid"), battery))
    mixed = PlantConfig(
        tz=PACIFIC,
        pv=(PvArray("roof", "ac"), PvArray("strings", "dc")),
        hybrid=Inverter(conversion=LOSSLESS),
    )
    with pytest.raises(PlantConfigError, match="PV of both couplings is metered per array"):
        replace(mixed, meters=(Meter("pv", "pv"), Meter("grid", "grid")))
    bad = {**config_of(EXAMPLE_PLANT).to_mapping(), "meters": [{"name": "x"}]}
    with pytest.raises(PlantConfigError, match=r"meters\[0\]\.measures: is required"):
        PlantConfig.from_mapping(bad, tz=PACIFIC)


# --- Today's branch ------------------------------------------------------------------


def test_a_meters_sign_is_its_convention() -> None:
    # A battery meter positive discharging reads as today's, negated.
    site = Plant(config_of(EXAMPLE_PLANT))
    flipped = Plant(
        replace(
            config_of(EXAMPLE_PLANT),
            meters=(
                Meter("pv", "pv"),
                Meter("grid", "grid"),
                Meter("battery", "battery", sign=-1.0),
            ),
        )
    )
    for pv, grid, battery in ((3.0, -1.0, 1.5), (0.0, 2.0, -2.2), (5.0, 0.0, 0.0)):
        assert flipped.read_meters({"pv": pv, "grid": grid, "battery": -battery}) == (
            site.read_meters(site_meters(site, pv, grid, battery))
        )


def test_consumption_metered_in_the_grids_place_gives_the_grid() -> None:
    # One closes the balance and gives the other: metering the consumption reads as
    # metering the grid that balance gives.
    config = config_of(EXAMPLE_PLANT)
    site = Plant(config)
    house = Plant(
        replace(
            config,
            meters=(Meter("pv", "pv"), Meter("battery", "battery"), Meter("house", "consumption")),
        )
    )
    battery = EXAMPLE_PLANT.battery
    for pv, battery_kw, consumed in ((3.0, 1.0, 2.2), (0.0, -2.0, 1.5), (5.0, 0.0, 0.4)):
        charged = battery.charge_ac(max(battery_kw, 0.0))
        delivered = max(battery.discharge_ac(max(-battery_kw, 0.0)), 0.0)
        grid = consumed - pv + charged - delivered
        mine = house.read_meters({"pv": pv, "battery": battery_kw, "house": consumed})
        theirs = site.read_meters({"pv": pv, "grid": grid, "battery": battery_kw})
        assert mine is not None and theirs is not None
        for name in ("consumed", "pv_to_load", "grid_to_load", "pv_export", "battery_export"):
            assert getattr(mine, name) == pytest.approx(getattr(theirs, name)), name


def test_a_battery_metered_at_its_ac_side_is_not_converted() -> None:
    battery = EXAMPLE_PLANT.battery
    config = PlantConfig(
        tz=PACIFIC,
        pv=(PvArray("pv", "ac"),),
        battery=BatteryPart(battery, "ac", inverter=Inverter()),
        meters=(Meter("pv", "pv"), Meter("grid", "grid"), Meter("battery", "battery", at="ac")),
    )
    flows = Plant(config).read_meters({"pv": 3.0, "grid": 0.0, "battery": 2.0})
    assert flows is not None
    assert flows.pv_to_battery == pytest.approx(2.0)  # 2 kW AC in, as it is
    assert flows.consumed == pytest.approx(consumption_kw(3.0, 0.0, 2.0, battery=battery, at="ac"))
    assert flows.consumed == pytest.approx(1.0)


# --- A plant with a DC bus -----------------------------------------------------------


def _folded_ev(rating: float = 10.0) -> Plant:
    return Plant(
        PlantConfig(
            tz=PACIFIC,
            pv=(PvArray("strings", "dc"),),
            battery=BatteryPart(CLEAN, "dc_bus", dc_charge_kw=10.0, dc_discharge_kw=10.0),
            hybrid=Inverter(ac_rating_kw=rating),
            loads=(Load("house"), Load("ev", "dc_bus")),
        )
    )


def test_the_dc_bus_split_serves_the_ev_from_dc_pv_then_the_battery() -> None:
    # DC PV 5, the DC-bus load 7, the battery giving 4, the grid 0.5 to a 2.5 kW house:
    # the DC-bus load takes the PV and 2 of the battery; 2 of the battery cross to the
    # house.
    plant = _folded_ev()
    slot = plant.metered_flow(T0, 0.5, {"pv": 5.0, "grid": 0.5, "battery": -4.0, "dc_load": 7.0})
    assert slot is not None
    assert slot.dc_pv_to_dc_load == pytest.approx(5.0)
    assert slot.battery_to_dc_load == pytest.approx(2.0)
    assert slot.battery_to_load == pytest.approx(2.0)
    assert slot.grid_to_load == pytest.approx(0.5)
    assert slot.dispatch.crossing == pytest.approx(2.0)
    assert slot.dispatch.battery_port == pytest.approx(-4.0)
    flows = plant.read_meters({"pv": 5.0, "grid": 0.5, "battery": -4.0, "dc_load": 7.0})
    assert flows is not None and flows.consumed == pytest.approx(2.5)
    assert flows.battery_to_dc_load == pytest.approx(2.0)


def test_a_metered_slot_keeps_the_battery_feeding_the_dc_bus_load() -> None:
    # The battery giving 6 kW to the EV alone is a battery discharging, not an idle one;
    # and with DC PV and the house beside it, the slot of the means reads as the
    # reading's own slot does.
    plant = _folded_ev()
    for meters in (
        {"pv": 0.0, "grid": 1.0, "battery": -6.0, "dc_load": 6.0},
        {"pv": 5.0, "grid": 0.5, "battery": -4.0, "dc_load": 7.0},
    ):
        flows = plant.read_meters(meters)
        live = plant.metered_flow(T0, 0.5, meters)
        assert flows is not None and live is not None
        slot = plant.metered_slot(T0, 0.5, 0.45, flows)
        assert slot.battery_to_dc_load == pytest.approx(live.battery_to_dc_load)
        assert slot.battery_to_load == pytest.approx(live.battery_to_load)
        mine, theirs = slot.dispatch, live.dispatch
        assert mine.battery_port_out == pytest.approx(-meters["battery"])
        assert mine.battery_port_in == 0.0
        assert mine.battery_port == pytest.approx(theirs.battery_port)
        assert mine.crossing == pytest.approx(
            theirs.crossing - live.dc_pv_to_ac  # the DC PV crossing is not metered
        )


def test_the_dc_bus_split_serves_the_house_from_dc_pv_before_charging() -> None:
    # DC PV 3, the battery charging 5, a 1 kW house, the grid 3: as the dispatch
    # orders it, DC PV crosses to the house first, its other 2 charge the battery,
    # and the grid's 3 rectify into it.
    plant = _folded_ev()
    slot = plant.metered_flow(T0, 0.5, {"pv": 3.0, "grid": 3.0, "battery": 5.0, "dc_load": 0.0})
    assert slot is not None
    assert slot.pv_to_load == pytest.approx(1.0)
    assert slot.dc_pv_to_battery == pytest.approx(2.0)
    assert slot.grid_to_battery == pytest.approx(3.0)
    assert slot.grid_to_load == pytest.approx(0.0)
    assert slot.dispatch.crossing == pytest.approx(-2.0)


def test_a_dc_metered_battery_on_its_own_inverter_converts_by_its_law() -> None:
    battery = EXAMPLE_PLANT.battery
    plant = Plant(
        PlantConfig(
            tz=PACIFIC,
            pv=(PvArray("strings", "dc"),),
            battery=BatteryPart(battery, "ac", inverter=Inverter()),
            hybrid=Inverter(conversion=LOSSLESS),
        )
    )
    slot = plant.metered_flow(T0, 0.5, {"pv": 4.0, "grid": 0.0, "battery": 2.0})
    assert slot is not None
    assert slot.pv_to_battery == pytest.approx(battery.charge_ac(2.0))
    assert slot.dispatch.battery_port == pytest.approx(battery.charge_ac(2.0))


# --- Over generated plants: the split gives back the dispatch ------------------------


@settings(max_examples=150)
@given(dc_plants(), days, st.floats(0, 1), st.data())
def test_the_split_of_what_a_plant_would_meter_gives_its_flows_back(
    plant: Plant, day: list[tuple[float, float]], soc: float, data: st.DataObject
) -> None:
    _split_matches(plant, day, soc, data, with_ac_pv=False)


@settings(max_examples=150)
@given(dc_plants(), days, st.floats(0, 1), st.data())
def test_the_split_gives_the_flows_back_with_consumption_metered(
    plant: Plant, day: list[tuple[float, float]], soc: float, data: st.DataObject
) -> None:
    # Consumption metered in the grid's place: the balance gives the grid.
    _split_matches(plant, day, soc, data, with_ac_pv=False, by_consumption=True)


@settings(max_examples=150)
@given(dc_plants(), days, st.floats(0, 1), st.data())
def test_the_split_gives_the_flows_back_with_ac_pv_on_the_dc_bus_plant(
    plant: Plant, day: list[tuple[float, float]], soc: float, data: st.DataObject
) -> None:
    # An AC array beside the DC one, metered per array; no export limit, so no AC PV
    # is curtailed out of the meter's sight.
    config = replace(
        plant.config,
        pv=(PvArray("roof", "ac"), *plant.config.pv),
        grid=replace(plant.config.grid, export_limit_kw=None),
    )
    _split_matches(Plant(config), day, soc, data, with_ac_pv=True)


def _split_matches(
    plant: Plant,
    day: list[tuple[float, float]],
    soc: float,
    data: st.DataObject,
    *,
    with_ac_pv: bool,
    by_consumption: bool = False,
) -> None:
    """Meter what ``plant``'s dispatch projects, split it, and find the dispatch's
    flows again."""
    config = plant.config
    part = config.battery
    if part is not None and part.connection == "ac":
        # A battery on its own inverter, metered at its AC side: its port, as it is.
        meters = tuple(
            replace(m, at="ac") if m.measures == "battery" else m for m in config.meter_set
        )
        plant = Plant(replace(config, meters=meters))
    if by_consumption:
        meters = tuple(
            Meter("house", "consumption") if m.measures == "grid" else m
            for m in plant.config.meter_set
        )
        plant = Plant(replace(plant.config, meters=meters))
    storage = plant.storage
    start_soc = soc if storage is not None else None
    count = len(day)
    starts = [T0 + k * SLOT for k in range(count)]
    ev = [0.0] * count
    if plant.has_dc_loads:
        ev = data.draw(st.lists(st.sampled_from([0.0, 3.0, 7.0]), min_size=count, max_size=count))
    sun = [d[0] for d in day]
    roof = [data.draw(st.sampled_from([0.0, 1.5, 4.0])) for _ in range(count)] if with_ac_pv else []
    pv_in: dict[str, list[float]] | list[float] = (
        {"roof": roof, "strings": sun} if with_ac_pv else sun
    )
    flows = plant.project(
        starts, [SLOT] * count, pv_in, [d[1] for d in day], start_soc, dc_load_kw=ev
    )
    for index, flow in enumerate(flows):
        if flow.dispatch.run_end is not None:
            continue  # a slot in two parts: its mean crossing is neither part's
        imported = flow.grid_to_load + flow.grid_to_battery
        exported = flow.pv_export + flow.battery_export
        dc_pv = flow.dc_pv_to_battery + flow.dc_pv_to_ac + flow.dc_pv_to_dc_load
        reading: dict[str, float | None] = {"grid": imported - exported}
        if by_consumption:
            if flow.unserved > 0:
                continue  # what the AC side drew is then not its load
            del reading["grid"]
            reading["house"] = day[index][1] + flow.inverter_draw
        if with_ac_pv:
            reading["pv:roof"], reading["pv:strings"] = roof[index], dc_pv
        else:
            reading["pv"] = dc_pv
        if storage is not None:
            reading["battery"] = flow.dispatch.battery_port
        if plant.has_dc_loads:
            reading["dc_load"] = (
                flow.dc_pv_to_dc_load + flow.battery_to_dc_load + flow.ac_to_dc_load
            )
        split = plant.metered_flow(flow.start, flow.soc_start, reading)
        flows_read = plant.read_meters(reading)
        assert split is not None and flows_read is not None
        served = flow.pv_to_load + flow.battery_to_load + flow.grid_to_load
        assert flows_read.consumed == pytest.approx(served, abs=1e-6)
        assert flows_read.grid_to_load + flows_read.grid_to_battery == pytest.approx(
            max(imported - exported, 0.0), abs=1e-6
        )
        assert flows_read.pv_export + flows_read.battery_export == pytest.approx(
            max(exported - imported, 0.0), abs=1e-6
        )
        assert split.dispatch.battery_port == pytest.approx(flow.dispatch.battery_port, abs=1e-9)
        assert split.dispatch.crossing == pytest.approx(flow.dispatch.crossing, abs=1e-6)
        # And each source to each sink, as the dispatch booked it.
        for name in (
            "pv_to_load",
            "pv_to_battery",
            "grid_to_load",
            "grid_to_battery",
            "battery_to_load",
            "dc_pv_to_battery",
            "battery_to_dc_load",
            "ac_to_dc_load",
        ):
            assert getattr(split, name) == pytest.approx(getattr(flow, name), abs=1e-6), name


def test_no_meter_the_plant_lacks() -> None:
    plant = Plant(PlantConfig(tz=PACIFIC, grid=GridLimits()))
    assert dict(plant.meters) == {"grid": "grid"}
    flows = plant.read_meters({"grid": 1.5})
    assert flows is not None and flows.grid_to_load == 1.5


def test_at_the_limit_the_dc_load_receives_what_the_grid_sends() -> None:
    # A 1 kW limit, the DC-bus load 3 kW through a lossless hybrid: it receives 1, and 2 is
    # unserved (not all 3 delivered, the crossing at -3, from 1 kW of AC).
    plant = Plant(
        PlantConfig(
            tz=PACIFIC,
            hybrid=Inverter(conversion=LOSSLESS),
            grid=GridLimits(import_limit_kw=1.0),
            loads=(Load("house"), Load("ev", "dc_bus")),
        )
    )
    (flow,) = plant.project([T0], [SLOT], [0.0], [0.0], None, dc_load_kw=[3.0])
    assert flow.grid_to_load == pytest.approx(1.0)
    assert flow.ac_to_dc_load == pytest.approx(1.0)
    assert flow.dispatch.crossing == pytest.approx(-1.0)
    assert flow.unserved == pytest.approx(2.0)


def test_the_live_value_reads_a_dc_bus_plant_importing() -> None:
    # A DC-bus plant read live: the grid serving the house beyond
    # regulation, with the battery idle, is an import, and a kWh now pays the import.
    from engine_fixtures import live_reading, load
    from microgrid.engine import Engine, EngineConfig, Pricing, Regime

    plant = _folded_ev()
    engine = Engine(
        EngineConfig(
            plant=plant,
            pricing=Pricing(scenarios=3),
            smoothing=timedelta(0),
        )
    )
    valuation = engine.value(inputs(soc=reading(0.5, as_of=NOW), dc_load=load()), at=NOW)
    assert valuation is not None
    until = NOW + timedelta(hours=1)
    meters = {"pv": 0.0, "grid": 3.0, "battery": 0.0, "dc_load": 0.0}
    value = engine.live_session().update(
        live_reading(pv_kw=None, soc=0.5, meters=meters), valuation, until
    )
    assert value is not None and value.degraded == ()
    assert value.regime is Regime.IMPORT and value.marginal_cost == value.p_import


def test_in_an_export_window_the_battery_serves_its_bus_first() -> None:
    # A property test's counterexample: at the battery's DC limit in an
    # export window, the dispatch sent its export out through the hybrid and rectified
    # it back for the DC-bus load on the same bus. On its bus the battery serves the DC-bus load
    # first, as the meters' split books it, and as it does outside a window.
    battery = Battery(
        12.0,
        6.0,
        6.0,
        0.96,
        0.96,
        loss_charge_per_kw=0.003,
        loss_discharge_per_kw=0.003,
        meter_charge=0.99,
        meter_discharge=1.0,
    )
    plant = Plant(
        PlantConfig(
            tz=PACIFIC,
            pv=(PvArray("strings", "dc"),),
            battery=BatteryPart(
                battery, connection="dc_bus", form="folded", dc_charge_kw=7.5, dc_discharge_kw=8.5
            ),
            hybrid=Inverter(ac_rating_kw=6.0, standby_kw=0.04),
            grid=GridLimits(),
            loads=(Load("house"), Load("ev", "dc_bus")),
            dispatch=Dispatch(export_windows=(ExportWindow(time(17), time(20), 0.8, 0.3),)),
        )
    )
    at = datetime(2026, 9, 26, 2, 0, tzinfo=UTC)  # 19:00 PDT, in the window
    (flow,) = plant.project([at], [SLOT], [0.0], [1.0], 0.88, dc_load_kw=[7.0])
    assert flow.dispatch.battery_port == pytest.approx(-8.5)  # at its DC limit
    assert flow.battery_to_dc_load == pytest.approx(7.0) and flow.ac_to_dc_load == 0.0
    reading: dict[str, float | None] = {
        "grid": flow.grid_to_load + flow.grid_to_battery - flow.pv_export - flow.battery_export,
        "pv": 0.0,
        "battery": flow.dispatch.battery_port,
        "dc_load": flow.battery_to_dc_load + flow.ac_to_dc_load + flow.dc_pv_to_dc_load,
    }
    read = plant.read_meters(reading)
    assert read is not None
    served = flow.pv_to_load + flow.battery_to_load + flow.grid_to_load
    assert read.consumed == pytest.approx(served, abs=1e-6)
    assert read.battery_to_dc_load == pytest.approx(flow.battery_to_dc_load, abs=1e-6)
