"""A plant's configuration, as data.

- **The variations validate:** PV of any count (none included), AC- or DC-coupled;
  a battery or none, on its own inverter or on a hybrid's DC bus, folded or split;
  a hybrid beside a battery on its own inverter; limits; loads by connection
  point; the dispatch's parts.
- **Every field's and part's problem is named at once,** each with its path; the
  rules across parts follow.
- **Plain data round-trips exactly,** and the zone is compared by key, never
  looked up.
- **Today's model runs only what it can,** and refuses the rest with its step.
"""

from collections.abc import Callable
from dataclasses import fields, replace
from datetime import UTC, time, timedelta, timezone
from functools import partial
from typing import Literal, cast
from zoneinfo import ZoneInfo

import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st

from legacy_dispatch import NativePlant, native_of
from microgrid.plant import (
    BATTERY_FIELDS,
    PLANT_SCHEMA,
    WORKING_KW,
    Battery,
    BatteryPart,
    Conversion,
    Dispatch,
    ExportWindow,
    GridCharge,
    GridLimits,
    Inverter,
    Load,
    Plant,
    PlantConfig,
    PlantConfigError,
    PvArray,
)
from microgrid.plant.examples import EXAMPLE_NAMES, example_mapping, example_plant

PACIFIC = ZoneInfo("America/Los_Angeles")
BATTERY = Battery(12.0, 6.0, 6.0, eta_charge=0.96, eta_discharge=0.96, reserve_soc=0.1)
WINDOW = GridCharge(start=time(12), end=time(15), target_soc=0.65, timing="late")
DRAWS = Inverter(standby_kw=0.03, working_kw=0.06)
CONVERSION = Conversion(0.97, 0.97, 0.002, 0.002)


def this_site() -> PlantConfig:
    """AC-coupled PV, the battery folded on the hybrid's DC bus (the reference plant's
    topology), one late grid-charge window."""
    return PlantConfig(
        tz=PACIFIC,
        pv=(PvArray("pv", "ac"),),
        battery=BatteryPart(BATTERY, connection="dc_bus"),
        hybrid=DRAWS,
        dispatch=Dispatch(grid_charge=(WINDOW,)),
    )


def dc_hybrid() -> PlantConfig:
    """DC PV and a split battery on a hybrid, with an import limit and a DC-bus load."""
    return PlantConfig(
        tz=PACIFIC,
        pv=(PvArray("south", "dc", dc_rating_kw=8.0),),
        battery=BatteryPart(BATTERY, connection="dc_bus", form="split"),
        hybrid=Inverter(ac_rating_kw=6.0, conversion=CONVERSION),
        grid=GridLimits(import_limit_kw=12.0, export_limit_kw=5.0),
        loads=(Load("house"), Load("ev", "dc_bus")),
        dispatch=Dispatch(
            export_windows=(ExportWindow(time(17), time(19), power_kw=3.0, floor_soc=0.3),),
            charge_order="target_first",
        ),
    )


def mixed_folded() -> PlantConfig:
    """AC and DC arrays with a folded battery: its DC limits and the hybrid's rating."""
    return PlantConfig(
        tz=PACIFIC,
        pv=(PvArray("roof", "ac", ac_rating_kw=5.0), PvArray("shed", "dc", dc_rating_kw=3.0)),
        battery=BatteryPart(BATTERY, connection="dc_bus", dc_charge_kw=7.5, dc_discharge_kw=8.5),
        hybrid=Inverter(ac_rating_kw=10.0),
    )


def hybrid_beside_ac_battery() -> PlantConfig:
    """DC strings on a hybrid, and a battery on its own inverter on the AC bus."""
    return PlantConfig(
        tz=PACIFIC,
        pv=(PvArray("strings", "dc"),),
        battery=BatteryPart(BATTERY, connection="ac", inverter=Inverter(ac_rating_kw=5.0)),
        hybrid=Inverter(ac_rating_kw=8.0, conversion=CONVERSION),
    )


EXAMPLES: dict[str, Callable[[], PlantConfig]] = {
    "this site": this_site,
    "a DC-coupled hybrid": dc_hybrid,
    "mixed, folded": mixed_folded,
    "a hybrid beside an AC battery": hybrid_beside_ac_battery,
    "PV only": lambda: PlantConfig(tz=PACIFIC, pv=(PvArray("roof", "ac"),)),
    "battery only": lambda: PlantConfig(
        tz=PACIFIC,
        battery=BatteryPart(BATTERY, connection="ac", inverter=DRAWS),
        dispatch=Dispatch(grid_charge=(WINDOW,)),
    ),
    "grid only": lambda: PlantConfig(tz=PACIFIC),
    "may not export": lambda: PlantConfig(
        tz=PACIFIC, pv=(PvArray("roof", "ac"),), grid=GridLimits(export_limit_kw=0.0)
    ),
}
# The engine's own example plants, beside the variations above.
EXAMPLES.update(
    {f"example {name}": partial(example_plant, name, PACIFIC) for name in EXAMPLE_NAMES}
)


@pytest.mark.parametrize("name", EXAMPLES)
def test_each_variation_is_valid_and_round_trips(name: str) -> None:
    config = EXAMPLES[name]()
    data = config.to_mapping()
    assert data["schema"] == PLANT_SCHEMA
    assert data["timezone"] == "America/Los_Angeles"
    assert PlantConfig.from_mapping(data, tz=PACIFIC) == config


def test_the_mapping_is_plain_data() -> None:
    def plain(value: object) -> bool:
        if isinstance(value, dict):
            return all(isinstance(k, str) and plain(v) for k, v in value.items())
        if isinstance(value, list):
            return all(plain(v) for v in value)
        return isinstance(value, str | int | float | bool)

    for make in EXAMPLES.values():
        assert plain(make().to_mapping())


def test_the_battery_keys_are_batterys_fields() -> None:
    # A new Battery field changes the schema only on purpose.
    assert tuple(f.name for f in fields(Battery)) == BATTERY_FIELDS


def test_toml_local_times_are_read_as_well_as_strings() -> None:
    data = this_site().to_mapping()
    dispatch = cast("dict[str, list[dict[str, object]]]", data["dispatch"])
    dispatch["grid_charge"][0]["start"] = time(12)  # tomllib gives a time
    assert PlantConfig.from_mapping(data, tz=PACIFIC) == this_site()


def test_a_null_is_a_key_left_out() -> None:
    data = this_site().to_mapping()
    data |= {"hybrid": None, "grid": None, "loads": None, "dispatch": None}
    battery = cast("dict[str, object]", data["battery"])
    battery |= {"connection": "ac", "inverter": None}
    with pytest.raises(PlantConfigError) as raised:
        PlantConfig.from_mapping(data, tz=PACIFIC)
    # Read as left out: the house is the load, and the AC battery lacks its inverter.
    assert raised.value.errors == ("battery.inverter: a battery on the AC bus needs its own",)


# Every problem, at once ----------------------------------------------------------------


def test_every_field_and_part_problem_is_named_at_once() -> None:
    data = dc_hybrid().to_mapping()
    battery = cast("dict[str, object]", data["battery"])
    battery |= {
        "capacity_kwh": True,  # a bool is not a number
        "charge_kw": -1.0,
        "eta_charge": float("nan"),
        "colour": "red",
        "connection": "dcc",
    }
    pv = cast("list[dict[str, object]]", data["pv"])
    pv[0] |= {"name": " ", "dc_rating_kw": 0.0}
    cast("dict[str, object]", data["hybrid"])["standby_kw"] = -0.1
    data["grid"] = {"import_limit_kw": -5.0}
    dispatch = cast("dict[str, list[dict[str, object]]]", data["dispatch"])
    dispatch["export_windows"][0] |= {"start": "20:00:00", "power_kw": 0.0}
    data["schema"] = "plant/0"
    with pytest.raises(PlantConfigError) as raised:
        PlantConfig.from_mapping(data, tz=PACIFIC)
    assert set(raised.value.errors) == {
        "schema: must be 'plant/2'",
        "battery.capacity_kwh: must be a finite number",
        "battery.eta_charge: must be a finite number",
        "battery.colour: is not a field",
        "battery.charge_kw: must be positive",
        "battery.connection: must be one of 'ac', 'dc_bus'",
        "pv[0].name: must be a name, not empty or padded with spaces",
        "pv[0].dc_rating_kw: must be positive",
        "hybrid.standby_kw: must not be negative",
        "grid.import_limit_kw: must be positive",
        "dispatch.export_windows[0]: start must be before end (a window lies within one local day)",
        "dispatch.export_windows[0].power_kw: must be positive",
    }


def test_a_kw_that_is_not_a_number_is_named_as_such() -> None:
    # Not "must not be negative" or "must be positive": it is no number at all. Built
    # directly, as a client's ``replace`` builds it (the reader names it before the
    # part's own rules see it).
    site = this_site()
    hybrid = replace(DRAWS, standby_kw=cast("float", "a"))
    grid = GridLimits(import_limit_kw=cast("float", "a"), export_limit_kw=float("inf"))
    with pytest.raises(PlantConfigError) as raised:
        replace(site, hybrid=hybrid, grid=grid)
    assert set(raised.value.errors) == {
        "hybrid.standby_kw: must be a finite number",  # not negative
        "grid.import_limit_kw: must be a finite number",  # positive or none
        "grid.export_limit_kw: must be a finite number",
    }
    data = site.to_mapping()
    cast("dict[str, object]", data["hybrid"])["standby_kw"] = "a"
    with pytest.raises(PlantConfigError, match=r"hybrid\.standby_kw: must be a finite number"):
        PlantConfig.from_mapping(data, tz=PACIFIC)


def test_batterys_rules_across_fields_come_with_the_rest() -> None:
    data = this_site().to_mapping()
    battery = cast("dict[str, object]", data["battery"])
    battery |= {"capacity_kwh": -1.0, "reserve_soc": 0.9, "saturation_soc": 0.5}
    battery |= {"loss_discharge_per_kw": 100.0}
    with pytest.raises(PlantConfigError) as raised:
        PlantConfig.from_mapping(data, tz=PACIFIC)
    assert set(raised.value.errors) == {
        "battery.capacity_kwh: must be positive",
        "battery.reserve_soc, saturation_soc: need 0 <= reserve < saturation <= 1",
        "battery.loss_discharge_per_kw: cannot deliver discharge_kw with that loss growth",
    }


def test_a_conversion_that_underflows_is_refused_not_raised() -> None:
    data = this_site().to_mapping()
    data["battery"] = {
        "capacity_kwh": 16,
        "charge_kw": 8,
        "discharge_kw": 1e-300,
        "eta_discharge": 1e-320,
        "meter_discharge": 2e-19,
        "loss_discharge_per_kw": 1e-38,
        "connection": "dc_bus",
    }
    with pytest.raises(PlantConfigError, match=r"must be in \(0.5, "):
        PlantConfig.from_mapping(data, tz=PACIFIC)


def test_conversions_outside_their_plausible_range_are_refused() -> None:
    data = this_site().to_mapping()
    battery = cast("dict[str, object]", data["battery"])
    battery |= {"eta_discharge": 1e-200, "meter_charge": 1.29e-277}
    with pytest.raises(PlantConfigError) as raised:
        PlantConfig.from_mapping(data, tz=PACIFIC)
    assert set(raised.value.errors) == {
        "battery.eta_discharge: must be in (0.5, 1]",
        "battery.meter_charge: must be in (0.5, 1.5]",
    }
    with pytest.raises(ValueError, match=r"eta_discharge must be in \(0.5, 1\]"):
        Battery(16.0, 8.0, 9.0, eta_discharge=0.4)


def test_a_part_that_is_not_a_table_is_named_once() -> None:
    data = this_site().to_mapping()
    data["battery"] = [1, 2]
    with pytest.raises(PlantConfigError) as raised:
        PlantConfig.from_mapping(data, tz=PACIFIC)
    assert raised.value.errors == ("battery: must be a table",)
    with pytest.raises(PlantConfigError, match=r"\(top level\): must be a table"):
        PlantConfig.from_mapping([], tz=PACIFIC)


def test_an_integer_too_large_for_a_float_is_refused_not_raised() -> None:
    data = this_site().to_mapping()
    cast("dict[str, object]", data["battery"])["capacity_kwh"] = 10**400  # JSON can
    with pytest.raises(PlantConfigError, match="capacity_kwh: must be a finite number"):
        PlantConfig.from_mapping(data, tz=PACIFIC)


def test_windows_with_an_offset_are_refused_not_raised() -> None:
    aware = GridCharge(start=time(1, tzinfo=UTC), end=time(3, tzinfo=UTC))
    plain = ExportWindow(time(17), time(19), power_kw=3.0, floor_soc=0.3)
    with pytest.raises(PlantConfigError) as raised:
        PlantConfig(
            tz=PACIFIC,
            battery=BatteryPart(BATTERY, "ac", inverter=DRAWS),
            dispatch=Dispatch(grid_charge=(aware,), export_windows=(plain,)),
        )
    assert raised.value.errors == (
        "dispatch.grid_charge[0].start: must be a local time without an offset (the "
        "plant's zone applies)",
        "dispatch.grid_charge[0].end: must be a local time without an offset (the "
        "plant's zone applies)",
    )


def test_the_zone_is_compared_by_key_and_never_looked_up() -> None:
    data = this_site().to_mapping()
    with pytest.raises(PlantConfigError, match="is not the given zone's key"):
        PlantConfig.from_mapping(data, tz=ZoneInfo("Europe/London"))
    data["timezone"] = 5
    with pytest.raises(PlantConfigError, match="timezone: must be a string"):
        PlantConfig.from_mapping(data, tz=PACIFIC)
    del data["timezone"]
    with pytest.raises(PlantConfigError, match="timezone: is required"):
        PlantConfig.from_mapping(data, tz=PACIFIC)
    with pytest.raises(PlantConfigError, match="tz: must be a ZoneInfo"):
        PlantConfig(tz=cast("ZoneInfo", timezone(timedelta(hours=-8))))
    with pytest.raises(PlantConfigError, match="tz: must be a ZoneInfo"):
        PlantConfig(tz=cast("ZoneInfo", UTC))


@pytest.mark.parametrize(
    ("config", "error"),
    [
        (
            lambda: PlantConfig(tz=PACIFIC, pv=(PvArray("a", "dc"),)),
            "pv[0].coupling: a DC-coupled array needs a hybrid inverter",
        ),
        (
            lambda: PlantConfig(tz=PACIFIC, pv=(PvArray("a", "dc", ac_rating_kw=3.0),)),
            "pv[0].ac_rating_kw: a DC-coupled array has no inverter of its own",
        ),
        (
            lambda: PlantConfig(tz=PACIFIC, pv=(PvArray("house", "ac"),)),
            "pv[0].name: 'house' is also a load's name",
        ),
        (
            lambda: PlantConfig(tz=PACIFIC, loads=(Load("ev", "dc_bus"),)),
            "loads[0].connection: a DC-bus load needs a hybrid inverter",
        ),
        (
            lambda: PlantConfig(tz=PACIFIC, loads=()),
            "loads: at least one load on the AC bus (the house)",
        ),
        (
            lambda: PlantConfig(tz=PACIFIC, dispatch=Dispatch(grid_charge=(WINDOW,))),
            "dispatch.grid_charge[0]: a grid-charge window needs a battery",
        ),
        (
            lambda: PlantConfig(tz=PACIFIC, battery=BatteryPart(BATTERY, "ac")),
            "battery.inverter: a battery on the AC bus needs its own",
        ),
        (
            lambda: PlantConfig(tz=PACIFIC, battery=BatteryPart(BATTERY, "dc_bus")),
            "hybrid: a battery on a DC bus needs a hybrid inverter",
        ),
        (
            lambda: PlantConfig(
                tz=PACIFIC,
                battery=BatteryPart(BATTERY, "dc_bus", inverter=DRAWS),
                hybrid=DRAWS,
            ),
            "battery.inverter: a battery on a DC bus uses the hybrid",
        ),
        (
            lambda: PlantConfig(
                tz=PACIFIC, battery=BatteryPart(BATTERY, "ac", form="split", inverter=DRAWS)
            ),
            "battery.form: only a battery on a DC bus can be split",
        ),
        (
            lambda: PlantConfig(
                tz=PACIFIC,
                battery=BatteryPart(BATTERY, "ac", inverter=DRAWS, dc_charge_kw=5.0),
            ),
            "battery.dc_charge_kw, dc_discharge_kw: only a battery on a DC bus has them",
        ),
        (
            lambda: PlantConfig(
                tz=PACIFIC,
                battery=BatteryPart(BATTERY, "ac", inverter=Inverter(conversion=CONVERSION)),
            ),
            "battery.inverter.conversion: a battery's own inverter converts as the battery "
            "does (its conversion is the battery's)",
        ),
        (
            lambda: PlantConfig(tz=PACIFIC, hybrid=DRAWS),
            "hybrid.conversion: a hybrid without a folded battery needs one",
        ),
        (
            lambda: PlantConfig(
                tz=PACIFIC,
                battery=BatteryPart(BATTERY, "dc_bus"),
                hybrid=Inverter(conversion=CONVERSION),
            ),
            "hybrid.conversion: a folded battery holds the hybrid's conversion",
        ),
        (
            lambda: PlantConfig(
                tz=PACIFIC,
                pv=(PvArray("shed", "dc"),),
                battery=BatteryPart(BATTERY, "dc_bus"),
                hybrid=DRAWS,
            ),
            "hybrid.ac_rating_kw: a folded battery with anything else on its DC bus needs "
            "the hybrid's AC rating",
        ),
        (
            lambda: PlantConfig(
                tz=PACIFIC,
                battery=BatteryPart(BATTERY, "ac", inverter=DRAWS),
                dispatch=Dispatch(
                    grid_charge=(GridCharge(start=time(12), end=time(15), target_soc=0.05),)
                ),
            ),
            "dispatch.grid_charge[0].target_soc: must be above the battery's reserve",
        ),
        (
            lambda: PlantConfig(
                tz=PACIFIC,
                battery=BatteryPart(BATTERY, "ac", inverter=DRAWS),
                grid=GridLimits(export_limit_kw=0.0),
                dispatch=Dispatch(export_windows=(ExportWindow(time(17), time(19), 3.0, 0.3),)),
            ),
            "dispatch.export_windows[0]: the site may not export (grid.export_limit_kw = 0)",
        ),
        (
            lambda: PlantConfig(
                tz=PACIFIC,
                battery=BatteryPart(BATTERY, "ac", inverter=DRAWS),
                dispatch=Dispatch(
                    grid_charge=(WINDOW,),
                    export_windows=(ExportWindow(time(14), time(16), 3.0, 0.3),),
                ),
            ),
            "dispatch.export_windows[0]: overlaps dispatch.grid_charge[0]",
        ),
        (
            lambda: PlantConfig(
                tz=PACIFIC,
                battery=BatteryPart(BATTERY, "ac", inverter=DRAWS),
                dispatch=Dispatch(export_windows=(ExportWindow(time(17), time(19), 3.0, 0.05),)),
            ),
            "dispatch.export_windows[0].floor_soc: must be at or above the reserve, below "
            "saturation",
        ),
        (
            lambda: PlantConfig(
                tz=PACIFIC,
                battery=BatteryPart(BATTERY, "ac", inverter=DRAWS),
                dispatch=Dispatch(export_windows=(ExportWindow(time(20), time(0), 3.0, 0.3),)),
            ),
            "dispatch.export_windows[0]: start must be before end (a window lies within one "
            "local day)",
        ),
        (
            lambda: PlantConfig(tz=PACIFIC, grid=GridLimits(import_limit_kw=0.0)),
            "grid.import_limit_kw: must be positive",
        ),
        (
            lambda: PlantConfig(
                tz=PACIFIC,
                battery=BatteryPart(BATTERY, "ac", inverter=Inverter(ac_rating_kw=2.0)),
                dispatch=Dispatch(export_windows=(ExportWindow(time(17), time(19), 5.0, 0.3),)),
            ),
            "dispatch.export_windows[0].power_kw: above its inverter's AC rating",
        ),
        (
            lambda: PlantConfig(
                tz=PACIFIC,
                battery=BatteryPart(BATTERY, "dc_bus", form="split"),
                hybrid=Inverter(ac_rating_kw=2.0, conversion=CONVERSION),
                dispatch=Dispatch(export_windows=(ExportWindow(time(17), time(19), 5.0, 0.3),)),
            ),
            "dispatch.export_windows[0].power_kw: above its inverter's AC rating",
        ),
    ],
)
def test_inconsistent_combinations_are_refused_with_their_reason(
    config: Callable[[], PlantConfig], error: str
) -> None:
    with pytest.raises(PlantConfigError) as raised:
        config()
    assert error in raised.value.errors


def test_cross_part_problems_are_named_at_once() -> None:
    with pytest.raises(PlantConfigError) as raised:
        PlantConfig(
            tz=PACIFIC,
            pv=(PvArray("a", "dc"),),
            loads=(Load("house"), Load("ev", "dc_bus")),
            dispatch=Dispatch(grid_charge=(WINDOW,)),
        )
    assert len(raised.value.errors) == 3


_IN_RANGE = st.fixed_dictionaries(
    {
        "capacity_kwh": st.floats(0.5, 40.0),
        "charge_kw": st.floats(0.5, 15.0),
        "discharge_kw": st.floats(0.5, 15.0),
        "eta_charge": st.floats(0.5, 1.0),
        "eta_discharge": st.floats(0.5, 1.0),
        "reserve_soc": st.floats(0.0, 0.5),
        "saturation_soc": st.floats(0.4, 1.0),
        "wear_charge_per_kwh": st.floats(0.0, 0.05),
        "wear_discharge_per_kwh": st.floats(0.0, 0.05),
        "loss_charge_per_kw": st.floats(0.0, 0.05),
        "loss_discharge_per_kw": st.floats(0.0, 0.03),
        "meter_charge": st.floats(0.8, 1.2),
        "meter_discharge": st.floats(0.8, 1.2),
    }
)
_EDGES = st.sampled_from([0.0, -0.0, -1.0, 1.5, 100.0, float("nan"), float("inf")])
#: An in-range battery with none, one or two fields broken: both sides of the rules,
#: and the rules across fields (reserve against saturation, loss growth) in range.
_BATTERY_VALUES = st.builds(
    lambda good, broken: good | dict(broken),
    _IN_RANGE,
    st.lists(st.tuples(st.sampled_from(BATTERY_FIELDS), _EDGES), max_size=2),
)
_GOOD = {
    "capacity_kwh": 16.0,
    "charge_kw": 8.0,
    "discharge_kw": 9.0,
    "eta_charge": 0.98,
    "eta_discharge": 0.95,
    "reserve_soc": 0.1,
    "saturation_soc": 1.0,
    "wear_charge_per_kwh": 0.02,
    "wear_discharge_per_kwh": 0.02,
    "loss_charge_per_kw": 0.0,
    "loss_discharge_per_kw": 0.0,
    "meter_charge": 1.0,
    "meter_discharge": 1.0,
}


@settings(max_examples=500, deadline=None)
@given(_BATTERY_VALUES)
@example(_GOOD)  # builds
@example(_GOOD | {"loss_discharge_per_kw": 0.2, "discharge_kw": 15.0})  # cannot deliver
@example(_GOOD | {"reserve_soc": 0.5, "saturation_soc": 0.4})
def test_the_battery_rules_agree_with_battery(values: dict[str, float]) -> None:
    # The reader names Battery's rules by field; it accepts exactly what Battery does.
    try:
        Battery(**values)
        builds = True
    except ValueError:
        builds = False
    data = this_site().to_mapping()
    data["battery"] = {**values, "connection": "dc_bus"}
    try:
        PlantConfig.from_mapping(data, tz=PACIFIC)
        reads = True
    except PlantConfigError as err:
        reads = not any(e.startswith("battery.") for e in err.errors)
    assert reads == builds


# Today's model -------------------------------------------------------------------------


def test_this_sites_configuration_is_todays_plant() -> None:
    native = native_of(Plant(this_site()))  # the legacy dispatch's parameters
    assert native == NativePlant(
        battery=BATTERY,
        timezone=PACIFIC,
        standby_kw=0.03,
        working_kw=0.06,
        grid_charge=WINDOW,
    )
    on_its_own = PlantConfig(
        tz=PACIFIC,
        pv=(PvArray("pv", "ac"),),
        battery=BatteryPart(BATTERY, connection="ac", inverter=DRAWS),
        dispatch=Dispatch(grid_charge=(WINDOW,)),
    )
    assert native_of(Plant(on_its_own)) == native


def test_the_working_threshold_is_the_configurations() -> None:
    hybrid = replace(DRAWS, working_threshold_kw=0.2)
    assert _native(Plant(replace(this_site(), hybrid=hybrid))).working_threshold_kw == 0.2
    assert _native(Plant(this_site())).working_threshold_kw == WORKING_KW


def _native(plant: Plant) -> NativePlant:
    """The legacy dispatch's parameters for a plant with a battery."""
    native = native_of(plant)
    assert native is not None
    return native


def test_every_example_runs() -> None:
    # With DC-bus loads, every configuration plant/2 accepts runs.
    for make in EXAMPLES.values():
        Plant(make())


@pytest.mark.parametrize(
    ("name", "has_pv", "has_battery"),
    [
        ("PV only", True, False),
        ("a hybrid beside an AC battery", True, True),
        ("mixed, folded", True, True),
        ("battery only", False, True),
        ("grid only", False, False),
        ("may not export", True, False),
    ],
)
def test_a_plant_without_pv_or_a_battery_runs(name: str, has_pv: bool, has_battery: bool) -> None:
    # No battery and no PV are configurations, not refusals.
    plant = Plant(EXAMPLES[name]())
    assert plant.has_pv is has_pv
    assert (plant.storage is not None) is has_battery


def test_a_hybrid_without_a_battery_runs() -> None:
    # A hybrid with DC PV and no battery runs the general dispatch.
    config = PlantConfig(
        tz=PACIFIC, pv=(PvArray("roof", "dc"),), hybrid=Inverter(conversion=CONVERSION)
    )
    plant = Plant(config)
    assert plant.storage is None and plant.has_pv


def test_several_ac_loads_run_as_todays_on_their_one_series() -> None:
    # The load forecast owns every load (R-9): AC loads arrive as one AC series.
    config = PlantConfig(
        tz=PACIFIC,
        pv=(PvArray("pv", "ac"),),
        battery=BatteryPart(BATTERY, "ac", inverter=DRAWS),
        loads=(Load("house"), Load("garage")),
    )
    assert native_of(Plant(config)) is not None


# Round trip over many configurations ---------------------------------------------------

_KW = st.floats(0.5, 20.0)


@st.composite
def _configs(draw: st.DrawFn) -> PlantConfig:
    reserve = draw(st.floats(0.0, 0.3))
    battery = Battery(
        draw(st.floats(1.0, 40.0)),
        draw(_KW),
        draw(st.floats(10.0, 20.0)),
        eta_charge=draw(st.floats(0.8, 1.0)),
        eta_discharge=draw(st.floats(0.8, 1.0)),
        reserve_soc=reserve,
        saturation_soc=draw(st.floats(reserve + 0.2, 1.0)),
        wear_charge_per_kwh=draw(st.floats(0.0, 0.05)),
    )
    has_battery = draw(st.booleans())
    connection = cast("Literal['ac', 'dc_bus']", draw(st.sampled_from(["ac", "dc_bus"])))
    form = cast("Literal['folded', 'split']", draw(st.sampled_from(["folded", "split"])))
    if connection == "ac":
        form = "folded"
    dc_arrays = draw(st.integers(0, 2))
    dc_loads = draw(st.integers(0, 1))
    on_the_bus = has_battery and connection == "dc_bus"
    folded = on_the_bus and form == "folded"
    attached = folded and (dc_arrays > 0 or dc_loads > 0)
    hybrid = (
        Inverter(
            ac_rating_kw=draw(_KW) if attached else draw(st.none() | _KW),
            standby_kw=draw(st.floats(0.0, 0.2)),
            working_kw=draw(st.floats(0.0, 0.2)),
            conversion=None if folded else CONVERSION,
        )
        if on_the_bus or dc_arrays or dc_loads
        else None
    )
    part = (
        BatteryPart(
            battery,
            connection=connection,
            form=form,
            inverter=Inverter(ac_rating_kw=draw(st.none() | _KW)) if connection == "ac" else None,
            dc_charge_kw=draw(_KW) if attached else None,
            dc_discharge_kw=draw(_KW) if attached else None,
        )
        if has_battery
        else None
    )
    start = draw(st.integers(0, 12))
    window = GridCharge(
        start=time(start),
        end=time(draw(st.integers(start + 1, 15))),
        target_soc=draw(st.floats(reserve + 0.1, 1.0)),
        cover_until=draw(st.none() | st.sampled_from([time(0), time(21)])),
        timing=draw(st.sampled_from(["early", "late"])),
    )
    export = ExportWindow(
        time(17),
        time(draw(st.integers(18, 23))),
        power_kw=draw(st.floats(0.1, 0.5)),  # within any drawn rating (_KW >= 0.5)
        floor_soc=reserve + 0.05,
        yields=draw(st.sampled_from(["pv", "battery"])),
    )
    return PlantConfig(
        tz=PACIFIC,
        pv=tuple(
            PvArray(f"ac{i}", "ac", ac_rating_kw=draw(st.none() | _KW))
            for i in range(draw(st.integers(0, 2)))
        )
        + tuple(PvArray(f"dc{i}", "dc", dc_rating_kw=draw(_KW)) for i in range(dc_arrays)),
        battery=part,
        hybrid=hybrid,
        grid=GridLimits(import_limit_kw=draw(st.none() | _KW)),
        loads=(Load("house"), *tuple(Load(f"bus{i}", "dc_bus") for i in range(dc_loads))),
        dispatch=Dispatch(
            grid_charge=draw(st.sampled_from([(), (window,)])) if has_battery else (),
            export_windows=draw(st.sampled_from([(), (export,)])) if has_battery else (),
            charge_order=draw(st.sampled_from(["pv_first", "target_first"])),
        ),
    )


@settings(max_examples=300, deadline=None)
@given(_configs())
def test_any_configuration_round_trips_exactly(config: PlantConfig) -> None:
    assert PlantConfig.from_mapping(config.to_mapping(), tz=PACIFIC) == config


def test_a_plant_1_file_is_told_the_wears_new_names() -> None:
    """``plant/2`` renamed the wear's keys (no currency in a name); a ``plant/1`` file is
    refused with the new names, not read."""
    data = example_mapping("ac_coupled_hybrid", PACIFIC)
    data["schema"] = "plant/1"
    with pytest.raises(PlantConfigError, match="wear_charge_per_kwh and wear_discharge_per_kwh"):
        PlantConfig.from_mapping(data, tz=PACIFIC)
