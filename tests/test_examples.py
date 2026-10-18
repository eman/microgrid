"""The example plants (``microgrid.plant.examples``): each reads, round-trips, keeps the
port's contract and values a day, and shows what its topology is for. The example of
the reference plant's topology is held apart from the reference plant in every
identifying figure, so no example carries a household's figures."""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from engine_fixtures import NOW, inputs, reading
from legacy_dispatch import native_of
from microgrid.engine import Engine, EngineConfig, Pricing, ValuationKind
from microgrid.engine.series import SLOT
from microgrid.engine.testing import assert_plant_model_contract
from microgrid.plant import Plant, PlantConfig
from microgrid.plant.examples import EXAMPLE_NAMES, example_mapping, example_plant

PACIFIC = ZoneInfo("America/Los_Angeles")
MIDNIGHT = datetime(2026, 6, 21, 7, 0, tzinfo=UTC)  # 00:00 PDT, midsummer


def _day(peak: float) -> list[float]:
    """A clear day's PV, kW per slot from midnight: a ramp from 06:00 to 20:00."""
    out = []
    for k in range(144):
        hour = k / 6
        out.append(max(0.0, peak * (1 - abs(hour - 13) / 7)))
    return out


def _project(name: str, house: float = 0.6) -> tuple[Plant, list]:  # type: ignore[type-arg]
    plant = Plant(example_plant(name, PACIFIC))
    starts = [MIDNIGHT + k * SLOT for k in range(144)]
    pv = _day(10.0) if plant.has_pv else [0.0] * 144
    soc = 0.5 if plant.storage is not None else None
    return plant, list(plant.project(starts, [SLOT] * 144, pv, [house] * 144, soc))


@pytest.mark.parametrize("name", EXAMPLE_NAMES)
def test_each_example_reads_round_trips_and_keeps_the_contract(name: str) -> None:
    config = example_plant(name, PACIFIC)
    assert PlantConfig.from_mapping(config.to_mapping(), tz=PACIFIC) == config
    assert_plant_model_contract(Plant(config))


@pytest.mark.parametrize("name", EXAMPLE_NAMES)
def test_each_example_values_a_day(name: str) -> None:
    plant = Plant(example_plant(name, PACIFIC))
    engine = Engine(EngineConfig(plant=plant, pricing=Pricing(scenarios=3)))
    overrides: dict[str, object] = {}
    if not plant.has_pv:
        overrides["pv"] = None
    if plant.storage is None:
        overrides["soc"] = reading(None)
        overrides["dispatch_as_modelled"] = reading(None)
    valuation = engine.value(inputs(**overrides), at=NOW)
    assert valuation is not None
    assert valuation.kind is ValuationKind.STORED_VALUE_V1 and valuation.degraded == ()


def test_the_ac_coupled_hybrid_is_todays_and_tops_up_overnight() -> None:
    plant, flows = _project("ac_coupled_hybrid")
    assert native_of(plant) is not None  # one of today's configurations
    night = [f for f in flows if f.start < MIDNIGHT + timedelta(hours=5)]
    assert sum(f.grid_to_battery for f in night) > 0


def test_the_dc_coupled_hybrid_clips_at_its_rating_and_curtails_at_the_limit() -> None:
    # 10 kW of DC at noon, the battery full: the strings' 8 kW MPPT limit clips 2, the
    # hybrid's 6 kW AC rating clips what it cannot pass on its bus, and the 5 kW export
    # limit curtails what crosses beyond the house.
    plant = Plant(example_plant("dc_coupled_hybrid", PACIFIC))
    noon = MIDNIGHT + timedelta(hours=13)
    (flow,) = plant.project([noon], [SLOT], [10.0], [0.5], 1.0)
    assert flow.pv_clipped - flow.dispatch.bus_clipped == pytest.approx(2.0)  # the array's own
    assert flow.dispatch.bus_clipped > 1.0  # the hybrid's rating, on its bus
    assert flow.pv_export == pytest.approx(5.0)
    # 6 kW AC, less the house's 0.5 and the hybrid's 0.03 standby, less the 5 exported.
    assert flow.pv_curtail == pytest.approx(6.0 - 0.53 - 5.0)


def test_pv_only_caps_each_array_and_stores_nothing() -> None:
    plant = Plant(example_plant("pv_only", PACIFIC))
    noon = MIDNIGHT + timedelta(hours=13)
    (flow,) = plant.project([noon], [SLOT], {"east": [3.5], "west": [3.5]}, [0.5], None)
    assert flow.pv_clipped == pytest.approx(1.0)  # 0.5 kW over each inverter's 3 kW
    assert flow.soc_end is None and flow.moving == 0.0


def test_battery_only_buys_overnight_and_exports_in_the_evening() -> None:
    _, flows = _project("battery_only", house=0.4)
    night = [f for f in flows if f.start < MIDNIGHT + timedelta(hours=5)]
    evening = [
        f
        for f in flows
        if MIDNIGHT + timedelta(hours=17) <= f.start < MIDNIGHT + timedelta(hours=20)
    ]
    assert sum(f.grid_to_battery for f in night) > 0
    assert sum(f.battery_export for f in evening) > 0


def test_an_example_is_a_fresh_copy_and_named() -> None:
    # A client changing its copy, nested parts included, changes no one else's.
    mapping = example_mapping("battery_only", PACIFIC)
    battery = mapping["battery"]
    assert isinstance(battery, dict)
    battery["capacity_kwh"] = 1.0
    dispatch = mapping["dispatch"]
    assert isinstance(dispatch, dict)
    dispatch["export_windows"].append({"start": "21:00:00"})
    again = example_mapping("battery_only", PACIFIC)
    assert again["battery"]["capacity_kwh"] == 13.5  # type: ignore[index]
    assert len(again["dispatch"]["export_windows"]) == 1  # type: ignore[index]
    with pytest.raises(KeyError, match="no example plant"):
        example_plant("nowhere", PACIFIC)


#: The reference plant, as fitted: the sizes, losses and draws that identify it.
#: Efficiencies, wear and meter factors are near any battery's, so they say nothing.
_SITE = {
    "capacity_kwh": (16.0, 16.12, 16.5),
    "charge_kw": (8.0, 8.4),
    "discharge_kw": (9.0, 9.6),
    "loss_charge_per_kw": (0.0046,),
    "loss_discharge_per_kw": (0.0015,),
    "standby_kw": (0.042,),
    "working_kw": (0.049,),
}


def test_the_reference_topology_is_not_the_reference_plant() -> None:
    # The example of its topology differs from the plant by more than 10 % in
    # every figure that identifies it: a rounded figure would pass an exact check.
    mapping = example_mapping("ac_coupled_hybrid", PACIFIC)
    battery, hybrid = mapping["battery"], mapping["hybrid"]
    assert isinstance(battery, dict) and isinstance(hybrid, dict)
    figures = {**battery, **hybrid}
    for field, site in _SITE.items():
        mine = figures[field]
        assert all(abs(mine - kw) > 0.1 * kw for kw in site), field
