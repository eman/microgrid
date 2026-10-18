"""The plant model behind the port, and this site's plant behind it.

Two kinds of test:

- **The contract** any plant model keeps (``microgrid.engine.testing``'s
  :func:`assert_plant_model_contract`), for a second topology to run as it is.
- **Equivalence:** every answer the AC-coupled plant gives is today's own expression
  (kept here as the oracle, copied from ``_value``, ``_supply`` and ``_live``), equal
  to the bit, so routing the price model through the port changes nothing.
"""

from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st

from engine_fixtures import (
    ENGINE,
    EXAMPLE_PLANT,
    PLANT,
    Connection,
    engine_imports,
    module_source,
    plant_of,
    reach,
    site_meters,
    soc,
    storage_of,
)
from legacy_dispatch import (
    UNREPORTED,
    NativePlant,
    PlantStorage,
    allocate,
    assert_matches,
    consumption_kw,
    flat,
    simulate,
)
from microgrid.engine.extend import (
    MODEL,
    OBSERVED,
    ChargeSource,
    Flows,
    PlantModel,
    SlotFlow,
    Storage,
)
from microgrid.engine.testing import assert_plant_model_contract
from microgrid.plant import WORKING_KW, Battery, GridCharge, Plant
from microgrid.plant._paths import PathStorage
from test_paths import _answers_agree, todays

T0 = datetime(2026, 9, 25, 7, 0, tzinfo=UTC)  # 00:00 PDT
SLOT = timedelta(minutes=10)
HOURS = SLOT / timedelta(hours=1)
PACIFIC = ZoneInfo("America/Los_Angeles")
#: ``_value._TOLERANCE``: the rule's float tolerance in its comparisons.
TOLERANCE = 1e-9
#: The charge paths today's branch has: PV on the AC bus, and the grid.
AC_BUS = (ChargeSource.PV, ChargeSource.GRID)
#: The paths beyond today's branch, each 0 on a plant without it.
PATHS = (
    "dc_pv_to_battery",
    "dc_pv_to_ac",
    "dc_pv_to_dc_load",
    "battery_to_dc_load",
    "battery_export",
    "ac_to_dc_load",
    "unserved",
)


def _is_plant_model(plant: Plant) -> PlantModel:
    return plant  # mypy: Plant satisfies the port


def _is_storage(storage: PathStorage) -> Storage:
    return storage  # mypy: and its storage, Storage


# --- Who knows whom ----------------------------------------------------------------

#: The port reaches only the flow types and the shared checks: it is the plant model's,
#: and the plant model imports nothing of the engine.
PORT_REACHES = {
    "microgrid.plant.flows",
    "microgrid._checks",
}
#: The battery's accounting is neutral too (``_stored``: the lots, and their cost run
#: forward through the port): the ledger fills it and the price model reads it, and
#: neither knows the other.
STORED = "microgrid.engine._stored"
#: A plant model reaches the neutral modules and the ``plant`` subpackage's own,
#: never the price model (``_value``, ``_supply``, ``_live``, ``_api``, ...): one half
#: of the two-way rule.
#: A plant model no longer reaches the lots: the price runs their cost forward through the
#: port's conversion (``Storage.stored_per_kwh``), so ``_paths`` does not import the ledger
#: (no ``_paths -> ledger -> _battery`` cycle).
PLANT_REACHES = {
    *PORT_REACHES,
    "microgrid.plant.port",
}


def test_the_port_knows_neither_a_plant_model_nor_the_price_model() -> None:
    path = module_source("microgrid.plant.port")
    assert path is not None
    assert engine_imports(path.read_text(encoding="utf-8")) == {"microgrid.plant.flows"}
    reached = reach("microgrid.plant.port")
    assert reached <= PORT_REACHES, reached - PORT_REACHES


def _plant_modules(root: Path) -> list[str]:
    """Every module of the plant package (``microgrid.plant``, beside the engine; a test's
    ``plant`` directory under its own ``root``), imported or not."""
    base = PLANT if root == ENGINE else root / "plant"
    return [
        ("microgrid.plant." + ".".join(path.relative_to(base).with_suffix("").parts)).removesuffix(
            ".__init__"
        )
        for path in sorted(base.rglob("*.py"))
    ]


def _known_by_plant_models(root: Path) -> dict[str, set[str]]:
    allowed = PLANT_REACHES | set(_plant_modules(root))
    found = {module: reach(module, root) - allowed for module in _plant_modules(root)}
    return {module: names for module, names in found.items() if names}


def test_a_plant_model_does_not_know_the_price_model() -> None:
    assert "microgrid.plant._plant" in _plant_modules(ENGINE)
    assert _known_by_plant_models(ENGINE) == {}


def test_the_plant_rule_sees_a_module_nothing_imports(tmp_path: Path) -> None:
    (tmp_path / "plant").mkdir()
    (tmp_path / "plant" / "__init__.py").write_text("")
    (tmp_path / "plant" / "dc_coupled.py").write_text(
        "from microgrid.engine._value import importing\n"
    )
    (tmp_path / "_value.py").write_text("")
    assert module_source("microgrid.plant.dc_coupled", tmp_path) is not None
    assert _known_by_plant_models(tmp_path) == {
        "microgrid.plant.dc_coupled": {"microgrid.engine._value"}
    }


def _price_model(root: Path) -> set[str]:
    """The price model: every top-level engine module that is not neutral (the plant is
    the ``plant`` subpackage), so a module added later is held to the rule without being
    listed."""
    return {
        f"microgrid.engine.{path.stem}" for path in root.glob("_*.py") if path.name != "__init__.py"
    } - PRICE_MODEL_ALLOWED


#: It may reach its own modules and the neutral ones, and a plant only through the
#: port: the other half of the two-way rule. The series toolkit is neutral too: the
#: form inputs arrive in, knowing neither half.
PRICE_MODEL_ALLOWED = {
    *PORT_REACHES,
    "microgrid.plant.port",
    "microgrid.engine.series",
    "microgrid.engine._timeline",
    STORED,
}


def test_the_series_toolkit_knows_neither_the_price_model_nor_a_plant() -> None:
    # The slot grid it re-exports (``_timeline``) is neutral too.
    assert reach("microgrid.engine.series") <= {
        "microgrid._checks",
        "microgrid.engine._timeline",
    }


def _analysis_offences(root: Path) -> set[str]:
    """Each module under ``root`` that imports ``analysis`` from outside it (the package's
    own ``__init__`` included, and a relative import, which hides what it names), and
    each private name ``analysis`` imports."""
    found: set[str] = set()
    for path in sorted(root.rglob("*.py")):
        names = engine_imports(path.read_text())
        where = str(path.relative_to(root))
        if "analysis" in path.relative_to(root).parts:
            found |= {
                f"{where}: {name}"
                for name in names
                if name == "<relative>" or any(p.startswith("_") for p in name.split(".")[2:])
            }
        else:
            found |= {
                f"{where}: {name}"
                for name in names
                if name == "<relative>" or name.startswith("microgrid.engine.analysis")
            }
    return found


def test_the_checks_are_a_client_of_the_models() -> None:
    """Nothing in the models imports ``analysis`` (the checks run the
    models; the models never run the checks), and ``analysis`` takes the models through
    the library's public names only, as any client does."""
    assert (ENGINE / "analysis" / "scoring.py").exists()
    assert _analysis_offences(ENGINE) == set()


def test_the_analysis_rule_sees_each_kind_of_offence(tmp_path: Path) -> None:
    (tmp_path / "analysis").mkdir()
    (tmp_path / "__init__.py").write_text("from microgrid.engine.analysis import scoring\n")
    (tmp_path / "_value.py").write_text("from microgrid.engine import analysis\n")
    (tmp_path / "_api.py").write_text("from .analysis import scoring\n")
    (tmp_path / "analysis" / "scoring.py").write_text("from microgrid.engine._value import x\n")
    assert _analysis_offences(tmp_path) == {
        "__init__.py: microgrid.engine.analysis",
        "_value.py: microgrid.engine.analysis",
        "_api.py: <relative>",
        "analysis/scoring.py: microgrid.engine._value",
    }


def test_the_ledger_knows_the_battery_but_not_the_price_model() -> None:
    """The lots know the battery's conversion (``plant._battery``) and
    what the port, the series and the neutral accounting (``_stored``) know, never the
    price model, which reads the lots through ``_stored`` and the port."""
    plant = set(_plant_modules(ENGINE))
    assert reach("microgrid.engine.ledger") - plant <= PRICE_MODEL_ALLOWED


def test_the_accounting_is_neutral() -> None:
    """``_stored`` reaches the port and the projection only: neither a plant model nor the
    price model, so both may read it."""
    assert reach(STORED) <= {*PORT_REACHES, "microgrid.plant.port"}
    assert STORED not in reach("microgrid.plant._paths")


def _knowing_a_plant(root: Path) -> dict[str, set[str]]:
    price_model = _price_model(root)
    found = {
        module: reach(module, root) - price_model - PRICE_MODEL_ALLOWED for module in price_model
    }
    return {module: names for module, names in found.items() if names}


def test_the_price_model_knows_no_plant_but_through_the_port() -> None:
    names = {f"microgrid.engine.{n}" for n in ("_value", "_supply", "_live", "_api", "_surplus")}
    assert names <= _price_model(ENGINE)
    assert _knowing_a_plant(ENGINE) == {}


def test_the_price_model_rule_sees_a_plant_reached_through_another_module(
    tmp_path: Path,
) -> None:
    (tmp_path / "_value.py").write_text("from microgrid.engine._supply import Step\n")
    (tmp_path / "_supply.py").write_text("from microgrid.plant._dispatch import simulate\n")
    (tmp_path / "plant").mkdir()
    (tmp_path / "plant" / "__init__.py").write_text("")
    (tmp_path / "plant" / "_dispatch.py").write_text("")
    dispatch = {"microgrid.plant._dispatch"}
    assert _knowing_a_plant(tmp_path) == {
        "microgrid.engine._value": dispatch,
        "microgrid.engine._supply": dispatch,
    }


def test_the_price_model_rule_holds_a_new_module_to_it(tmp_path: Path) -> None:
    (tmp_path / "_new.py").write_text("from microgrid.plant import Plant\n")
    (tmp_path / "plant").mkdir()
    (tmp_path / "plant" / "__init__.py").write_text("")
    assert _knowing_a_plant(tmp_path) == {"microgrid.engine._new": {"microgrid.plant"}}


# --- The contract ----------------------------------------------------------------


def _day(pv_peak: float, load: float, slots: int = 144) -> tuple[list[float], list[float]]:
    pv = [max(0.0, pv_peak * (1 - abs(k - 78) / 36)) for k in range(slots)]  # 13:00 PDT
    return pv, [load] * slots


def _imbalance(flow: SlotFlow, pv: float, load: float, capacity: float, hours: float) -> float:
    """Sources less sinks in ``flow``'s slot, kW, from its fields and the slot's inputs,
    ``pv`` and ``load`` (``SlotFlow``'s *Balance*): 0 where the
    energy balances."""
    stored = 0.0  # without a battery nothing is stored
    if flow.soc_end is not None and flow.soc_start is not None:
        stored = (flow.soc_end - flow.soc_start) * capacity / hours
    sources = pv + flow.grid_to_load + flow.grid_to_battery
    sinks = (
        load
        - flow.unserved
        + flow.inverter_draw
        + flow.pv_export
        + flow.battery_export
        + flow.pv_curtail
        + flow.pv_clipped
        + flow.conversion_loss
        + stored
    )
    return sources - sinks


def _windowed(charge: GridCharge) -> NativePlant:
    return NativePlant(
        EXAMPLE_PLANT.battery, PACIFIC, standby_kw=0.042, working_kw=0.049, grid_charge=charge
    )


#: A site's window, and the plant's dynamic target (``cover_until``), where more load
#: raises the top-up.
WINDOWS = (
    GridCharge(start=time(12), end=time(15), target_soc=0.65, timing="late"),
    GridCharge(start=time(12), end=time(15), target_soc=1.0, cover_until=time(0), timing="late"),
)


def test_the_plant_keeps_the_contract() -> None:
    assert_plant_model_contract(plant_of(EXAMPLE_PLANT))
    for charge in WINDOWS:
        assert_plant_model_contract(plant_of(_windowed(charge)))


def test_ac_coupled_pv_is_all_ac() -> None:
    # Beyond the contract: AC-coupled PV loses nothing on its way to a load, so every
    # kW of it is routed somewhere, and none is clipped.
    for native in (EXAMPLE_PLANT, *(_windowed(charge) for charge in WINDOWS)):
        pv, house = _day(9.0, 0.7)
        starts = [T0 + k * SLOT for k in range(len(pv))]
        flows = plant_of(native).project(starts, [SLOT] * len(pv), pv, house, 0.2)
        for flow, sun in zip(flows, pv, strict=True):
            assert flow.pv_clipped == 0.0
            assert flow.pv_to_load + flow.pv_spare == pytest.approx(sun, abs=1e-9)


def test_the_storage_is_built_once_and_follows_the_battery() -> None:
    plant = plant_of(EXAMPLE_PLANT)
    assert plant.storage is plant.storage
    assert storage_of(plant).battery is EXAMPLE_PLANT.battery
    assert plant == plant_of(EXAMPLE_PLANT)  # equal by configuration
    assert plant != plant_of(EXAMPLE_PLANT, connection="dc_bus")


def test_the_battery_on_a_hybrids_dc_bus_keeps_the_contract_and_runs_alike() -> None:
    # This site's battery is folded on the hybrid's DC bus; today's
    # arithmetic runs it as it runs a battery on its own inverter.
    for native in (EXAMPLE_PLANT, *(_windowed(charge) for charge in WINDOWS)):
        on_the_bus = plant_of(native, connection="dc_bus")
        assert_plant_model_contract(on_the_bus)
        pv, house = _day(9.0, 0.7)
        starts = [T0 + k * SLOT for k in range(len(pv))]
        durations = [SLOT] * len(pv)
        assert_matches(
            on_the_bus.project(starts, durations, pv, house, 0.2),
            plant_of(native).project(starts, durations, pv, house, 0.2),
        )


def test_a_plant_takes_a_configuration_and_names_its_inverter() -> None:
    with pytest.raises(TypeError, match="Plant takes a PlantConfig, not NativePlant"):
        Plant(EXAMPLE_PLANT)  # type: ignore[arg-type]  # engine/0's AcCoupledPlant(native)
    on_its_own, on_the_bus = plant_of(EXAMPLE_PLANT), plant_of(EXAMPLE_PLANT, connection="dc_bus")
    assert on_its_own.config.battery is not None and on_the_bus.config.hybrid is not None
    assert on_its_own.inverter is on_its_own.config.battery.inverter
    assert on_the_bus.inverter is on_the_bus.config.hybrid


def test_the_working_threshold_is_the_configurations() -> None:
    # Today's WORKING_KW by default, or the inverter's own. The battery
    # charging about 0.26 kW works over 0.05 kW, not over 0.5 kW.
    own = replace(EXAMPLE_PLANT, working_threshold_kw=0.5)
    starts = [T0 + k * SLOT for k in range(3)]
    pv, house = [1.0] * 3, [0.7] * 3
    for native in (EXAMPLE_PLANT, own):
        flows = plant_of(native).project(starts, [SLOT] * 3, pv, house, 0.5)
        assert_matches(flows, simulate(native, starts, pv, house, 0.5, step=SLOT))
    working = plant_of(EXAMPLE_PLANT).project(starts, [SLOT] * 3, pv, house, 0.5)[0]
    idle = plant_of(own).project(starts, [SLOT] * 3, pv, house, 0.5)[0]
    spare = 1.0 - 0.7 - EXAMPLE_PLANT.standby_kw
    assert idle.pv_to_battery == pytest.approx(spare)
    assert working.pv_to_battery == pytest.approx(spare - EXAMPLE_PLANT.working_kw)
    metered = Flows(
        consumed=1.0,
        pv_to_load=0.7,
        battery_to_load=0.0,
        grid_to_load=0.0,
        pv_to_battery=0.26,
        grid_to_battery=0.0,
        pv_export=0.0,
        battery_export=0.0,
    )
    draws = EXAMPLE_PLANT.standby_kw + EXAMPLE_PLANT.working_kw
    assert plant_of(EXAMPLE_PLANT).own_draw_kw(metered) == draws
    # The projected slot reports the draw its settle served.
    assert working.inverter_draw == draws
    assert idle.inverter_draw == EXAMPLE_PLANT.standby_kw
    assert plant_of(own).own_draw_kw(metered) == EXAMPLE_PLANT.standby_kw


@pytest.mark.parametrize("field", ["standby_kw", "working_kw", "working_threshold_kw"])
@pytest.mark.parametrize("value", [-0.1, float("nan"), float("inf")])
def test_todays_dispatch_refuses_a_draw_or_threshold_that_is_not_a_kw(
    field: str, value: float
) -> None:
    with pytest.raises(ValueError, match=rf"{field} must (not be negative|be finite)"):
        replace(EXAMPLE_PLANT, **{field: value})  # type: ignore[arg-type]


# --- Equivalence with today's expressions ---------------------------------------

power = st.floats(0, 10, allow_nan=False)
price = st.floats(-0.1, 0.6, allow_nan=False)
plants = st.builds(
    lambda eta, q, reserve, wear, window, threshold: NativePlant(
        Battery(
            10.0,
            5.0,
            6.0,
            eta,
            eta,
            reserve_soc=reserve,
            saturation_soc=0.97,
            wear_charge_per_kwh=wear,
            wear_discharge_per_kwh=wear,
            loss_charge_per_kw=q,
            loss_discharge_per_kw=q,
            meter_charge=0.99,
            meter_discharge=1.01,
        ),
        PACIFIC,
        standby_kw=0.042,
        working_kw=0.049,
        grid_charge=window,
        working_threshold_kw=threshold,
    ),
    st.floats(0.85, 1.0),
    st.sampled_from([0.0, 0.004]),
    st.floats(0, 0.2),
    st.floats(0.0, 0.05),
    st.sampled_from(
        [
            None,
            GridCharge(start=time(12), end=time(15), target_soc=0.65, timing="late"),
            GridCharge(start=time(1), end=time(5), target_soc=1.0),
        ]
    ),
    # This site's measured threshold, or a configuration's own.
    st.sampled_from([WORKING_KW, 0.0, 0.3]),
)
#: Where the battery connects: on its own inverter, or folded on a hybrid's DC bus
#: (this site). Today's branch runs both.
connections = st.sampled_from(["ac", "dc_bus"])
days = st.lists(st.tuples(power, power), min_size=1, max_size=72)
#: Where a run starts, local: the night, before the 12:00-15:00 window, and in it.
first_slots = st.sampled_from([T0, T0 + timedelta(hours=11), T0 + timedelta(hours=13)])


def _flows(
    plant: NativePlant, day: Sequence[tuple[float, float]], soc: float, first: datetime
) -> list[SlotFlow]:
    starts = [first + k * SLOT for k in range(len(day))]
    return list(simulate(plant, starts, [d[0] for d in day], [d[1] for d in day], soc))


@settings(max_examples=150)
@given(plants, days, st.floats(0, 1), st.integers(0, 71), first_slots, connections)
# Found by the search: on the DC bus the general dispatch ended a slot a rounding (4e-20
# kWh) above a 6.5e-6 reserve, and the next slot's 3.6e-19 kW discharge woke the inverter
# (threshold 0) where the legacy one stayed in standby.
@example(
    native=NativePlant(
        Battery(
            10.0,
            5.0,
            6.0,
            0.875,
            0.875,
            reserve_soc=6.499041809550174e-06,
            saturation_soc=0.97,
            loss_charge_per_kw=0.004,
            loss_discharge_per_kw=0.004,
            meter_charge=0.99,
            meter_discharge=1.01,
        ),
        PACIFIC,
        standby_kw=0.042,
        working_kw=0.049,
        working_threshold_kw=0.0,
    ),
    day=[(3.125, 2.96875), (0.0, 0.0), (0.0, 0.0)],
    soc=0.0,
    stop_at=0,
    first=T0,
    connection="dc_bus",
)
# And the oracle's side: a battery holding 4e-140 of its capacity, threshold 0.
@example(
    native=NativePlant(
        Battery(
            10.0, 5.0, 6.0, 1.0, 1.0, saturation_soc=0.97, meter_charge=0.99, meter_discharge=1.01
        ),
        PACIFIC,
        standby_kw=0.042,
        working_kw=0.049,
        working_threshold_kw=0.0,
    ),
    day=[(0.0, 0.0)],
    soc=4.2095372266619e-140,
    stop_at=0,
    first=T0,
    connection="ac",
)
def test_project_is_simulate(
    native: NativePlant,
    day: list[tuple[float, float]],
    soc: float,
    stop_at: int,
    first: datetime,
    connection: Connection,
) -> None:
    starts = [first + k * SLOT for k in range(len(day))]
    pv, load = [d[0] for d in day], [d[1] for d in day]
    durations = [SLOT] * len(day)
    durations[0] = SLOT / 3  # a slot under way, as the live value's window
    plant = plant_of(native, connection=connection)
    assert_matches(
        plant.project(starts, durations, pv, load, soc),
        simulate(native, starts, pv, load, soc, durations=durations),
    )

    def stop(index: int, _: SlotFlow) -> bool:
        return index == stop_at

    assert_matches(
        plant.project(starts, durations, pv, load, soc, stop=stop),
        simulate(native, starts, pv, load, soc, durations=durations, stop=stop),
    )


@settings(max_examples=150)
@given(
    todays,
    days,
    st.floats(0, 1),
    price,
    st.floats(-0.2, 0.8, allow_nan=False),
    first_slots,
    connections,
)
def test_every_storage_answer_is_todays_expression(
    native: NativePlant,
    day: list[tuple[float, float]],
    soc: float,
    cost: float,
    more: float,
    first: datetime,
    connection: Connection,
) -> None:
    # The library's storage (``PathStorage``) answers as today's (``PlantStorage``, the
    # oracle) over the general dispatch's flows of the same
    # slots, a battery on its own inverter or folded on a hybrid's DC bus; and the
    # oracle is today's expressions.
    battery = native.battery
    plant = plant_of(native, connection=connection)
    starts = [first + k * SLOT for k in range(len(day))]
    sun, house = [d[0] for d in day], [d[1] for d in day]
    _answers_agree(
        storage_of(plant),
        PlantStorage(battery),
        plant.project(starts, [SLOT] * len(day), sun, house, soc),
        simulate(native, starts, sun, house, soc),
    )
    storage = PlantStorage(battery)
    assert storage.capacity_kwh == battery.capacity_kwh
    assert storage.reserve_soc == battery.reserve_soc
    assert storage.saturation_soc == battery.saturation_soc
    assert storage.wear_discharge_per_kwh == battery.wear_discharge_per_kwh
    for source in AC_BUS:
        # One AC rate for both sources.
        assert storage.charge_wear_rate(source) == battery.wear_charge_per_kwh
    flows = _flows(native, day, soc, first)
    for flow in flows:
        # This branch projects no other output, so ``battery_out`` is today's term.
        assert flow.battery_out == flow.battery_to_load
    hand_built = [
        SlotFlow(start=T0, soc_start=0.5, soc_end=0.5, pv_to_battery=2.0, pv_export=1e-10),
        SlotFlow(start=T0, soc_start=0.5, soc_end=0.5, grid_to_battery=battery.charge_kw - 5e-10),
        SlotFlow(
            start=T0,
            soc_start=0.5,
            soc_end=0.5,
            battery_to_load=battery.discharge_kw - MODEL.need_kw,
        ),
        SlotFlow(
            start=T0,
            soc_start=0.5,
            soc_end=0.5,
            battery_to_load=battery.discharge_kw,
            grid_to_load=0.2,
        ),
        # The discharge answers count all of the battery's output.
        SlotFlow(
            start=T0,
            soc_start=0.6,
            soc_end=0.5,
            battery_to_load=1.0,
            battery_export=battery.discharge_kw - 1.0,
        ),
        SlotFlow(
            start=T0,
            soc_start=0.6,
            soc_end=0.5,
            battery_to_load=0.5,
            battery_to_dc_load=1.5,
            battery_export=1.0,
        ),
    ]
    for flow in [*flows, *hand_built]:
        charging = flow.pv_to_battery + flow.grid_to_battery
        # ``_value.price_flows.buys_later``, after its ``topup_gap`` test.
        expected_grid = (
            charging < battery.charge_kw - TOLERANCE
            and flow.pv_export + flow.pv_curtail <= TOLERANCE
        )
        assert storage.can_charge_more(flow, ChargeSource.GRID, margin_kw=TOLERANCE) is (
            expected_grid
        )
        assert storage.can_charge_more(flow, ChargeSource.PV, margin_kw=TOLERANCE) is (
            charging < battery.charge_kw - TOLERANCE
        )
        # ``_value.price_flows``: at its discharge limit, with ``noise.need_kw``. Each
        # discharge answer reads all of the battery's output.
        out = flow.battery_to_load + flow.battery_to_dc_load + flow.battery_export
        for noise in (MODEL, OBSERVED):
            assert storage.at_discharge_limit(flow, margin_kw=noise.need_kw) is (
                out >= battery.discharge_kw - noise.need_kw
            )
        # ``_value.importing``'s regulation test, the import as ``more_kw``.
        assert storage.can_serve(flow, flow.grid_to_load) is (
            out + flow.grid_to_load <= battery.discharge_kw
        )
        assert storage.can_serve(flow, more) is (out + more <= battery.discharge_kw)
        # ``_supply.supply_stack``'s PV step, and the top-up in the same slot.
        assert storage.divertible_kw(flow) == flow.pv_to_battery
        assert storage.grid_refills_diverted_pv(flow) is True
        # ``_value.battery_headroom``'s former body, kept as the oracle below.
        assert storage.headroom(flow, HOURS) == _todays_headroom(battery, flow, HOURS)
        assert storage.headroom(flow, HOURS / 3) == _todays_headroom(battery, flow, HOURS / 3)
        # ``_value.price_flows``: refilled, displaced, and the dead band.
        for source in AC_BUS:
            assert storage.refill_value(cost, source, flow) == battery.refill_value(
                cost, charging_kw=flow.pv_to_battery + flow.grid_to_battery
            )
        assert storage.displaced_value(cost, flow) == battery.displaced_value(
            cost, discharging_kw=out
        )
        assert storage.displaced_value(cost) == battery.displaced_value(cost)
        assert storage.dead_band(cost, flow) == battery.dead_band(
            cost,
            charging_kw=flow.pv_to_battery + flow.grid_to_battery,
            discharging_kw=out,
        )


def _todays_headroom(battery: Battery, flow: SlotFlow, hours: float) -> float:
    """``_value.battery_headroom``'s former body, its discharge read as all of the
    battery's output."""
    out = flow.battery_to_load + flow.battery_to_dc_load + flow.battery_export
    above = (soc(flow.soc_end) - battery.reserve_soc) * battery.capacity_kwh
    above -= battery.stored_by(flow.pv_to_battery + flow.grid_to_battery) * hours
    limit = battery.discharge_kw - out
    if above <= 0 or limit <= 0:
        return 0.0
    drawn = battery.drawn_by(out) + above / hours
    return max(0.0, min(limit, battery.delivered_by(drawn) - out))


@settings(max_examples=150)
@given(plants, days, st.floats(0, 1), first_slots, connections)
def test_every_slot_balances_by_path(
    native: NativePlant,
    day: list[tuple[float, float]],
    soc: float,
    first: datetime,
    connection: Connection,
) -> None:
    # The losses and draws sit beside today's fields, the new paths are
    # 0, and the balance closes from the fields alone, in a slot under way too.
    battery = native.battery
    starts = [first + k * SLOT for k in range(len(day))]
    pv, load = [d[0] for d in day], [d[1] for d in day]
    durations = [SLOT] * len(day)
    durations[0] = SLOT / 3
    flows = plant_of(native, connection=connection).project(starts, durations, pv, load, soc)
    assert_matches(flows, simulate(native, starts, pv, load, soc, durations=durations))
    draws = (native.standby_kw, native.standby_kw + native.working_kw)
    for flow, sun, need, duration in zip(flows, pv, load, durations, strict=False):
        hours = duration / timedelta(hours=1)
        assert all(getattr(flow, name) == 0.0 for name in PATHS)
        assert flow.inverter_draw in draws
        served = flow.pv_to_load + flow.battery_to_load + flow.grid_to_load
        assert served - need == pytest.approx(flow.inverter_draw, abs=1e-9)
        charging = flow.pv_to_battery + flow.grid_to_battery
        loss = (charging - battery.stored_by(charging)) + (
            battery.drawn_by(flow.battery_to_load) - flow.battery_to_load
        )
        assert flow.conversion_loss == pytest.approx(loss, abs=1e-12)
        assert flow.conversion_loss >= -1e-12  # float residue of a lossless battery
        assert _imbalance(flow, sun, need, battery.capacity_kwh, hours) == pytest.approx(
            0.0, abs=1e-6
        )
        # ``moving`` counts every path, and is today's sum where they are 0.
        assert flow.moving == flow.pv_to_battery + flow.grid_to_battery + flow.battery_to_load


def test_a_battery_on_its_own_inverter_answers_for_no_dc_pv() -> None:
    # ``ChargeSource`` names the path; a battery off the DC bus has none
    # there, so a question about one is refused, not answered as AC.
    storage = storage_of(plant_of(EXAMPLE_PLANT, connection="ac"))
    flow = SlotFlow(start=T0, soc_start=0.5, soc_end=0.5, pv_to_battery=1.0)
    for ask in (
        lambda: storage.charge_wear_rate(ChargeSource.DC_PV),
        lambda: storage.can_charge_more(flow, ChargeSource.DC_PV),
        lambda: storage.refill_value(0.1, ChargeSource.DC_PV, flow),
    ):
        with pytest.raises(ValueError, match="not on the DC bus"):
            ask()


def test_a_metered_slot_leaves_the_new_paths_out() -> None:
    # The meters see the battery export, but the slot leaves it out until the price
    # rule has the export event, so the live value does not move.
    plant = plant_of(EXAMPLE_PLANT, connection="dc_bus")
    exporting = plant.read_meters(site_meters(plant, 0.0, -2.0, -2.2))
    assert exporting is not None and exporting.battery_export > 1.5
    slot = plant.metered_flow(T0, 0.6, site_meters(plant, 0.0, -2.0, -2.2))
    assert slot is not None
    assert all(getattr(slot, name) == 0.0 for name in PATHS)
    assert (slot.conversion_loss, slot.inverter_draw) == (0.0, 0.0)


def _todays_slot(
    plant: NativePlant, now: datetime, soc: float, pv: float, grid: float, battery_kw: float
) -> SlotFlow:
    """``_live.importing_now``'s former slot."""
    battery = plant.battery
    flows = allocate(
        pv,
        grid,
        battery_kw,
        consumption_kw(pv, grid, battery_kw, battery=battery),
        battery=battery,
    )
    return SlotFlow(
        start=now,
        soc_start=soc,
        soc_end=soc,
        pv_to_load=flows.pv_to_load,
        pv_to_battery=flows.pv_to_battery,
        pv_export=flows.pv_export,
        battery_to_load=flows.battery_to_load,
        grid_to_load=flows.grid_to_load,
        grid_to_battery=flows.grid_to_battery,
    )


@settings(max_examples=300)
@given(
    plants,
    st.floats(0, 1),
    power,
    st.floats(-10, 10, allow_nan=False),
    st.floats(-9, 9, allow_nan=False),
    connections,
)
def test_the_meters_are_read_as_today(
    native: NativePlant,
    soc: float,
    pv: float,
    consumed: float,
    battery_kw: float,
    connection: Connection,
) -> None:
    # A reading that balances: the grid's meter is what the rest leaves. One that does
    # not (the grid exporting more than PV while the battery charges) the general split
    # books PV to the battery first, where today's booked it to export (an accepted
    # difference).
    plant = plant_of(native, connection=connection)
    battery = native.battery
    charge = battery.charge_ac(max(battery_kw, 0.0))
    delivered = max(battery.discharge_ac(max(-battery_kw, 0.0)), 0.0)
    grid = abs(consumed) - pv + charge - delivered
    reading = site_meters(plant, pv, grid, battery_kw)
    mine = plant.read_meters(reading)
    theirs = allocate(
        pv,
        grid,
        battery_kw,
        consumption_kw(pv, grid, battery_kw, battery=battery),
        battery=battery,
    )
    assert mine is not None
    for name in Flows.__dataclass_fields__:
        assert getattr(mine, name) == pytest.approx(getattr(theirs, name), abs=1e-9), name
    slot = plant.metered_flow(T0, soc, reading)
    assert slot is not None
    today = _todays_slot(native, T0, soc, pv, grid, battery_kw)
    got = flat(slot)
    for name, want in flat(today).items():
        if name not in UNREPORTED:
            assert got[name] == (
                pytest.approx(want, abs=1e-9) if isinstance(want, float) else want
            ), name


def test_a_plant_model_may_check_its_inputs_but_not_read_the_price_models(
    tmp_path: Path,
) -> None:
    # ``_checks`` is neutral; ``_models`` holds the price model's inputs.
    (tmp_path / "plant").mkdir()
    (tmp_path / "plant" / "__init__.py").write_text("")
    (tmp_path / "plant" / "dc_coupled.py").write_text(
        "from microgrid._checks import require_utc\n"
        "from microgrid.engine._models import RateForecast\n"
    )
    (tmp_path / "_checks.py").write_text("")
    (tmp_path / "_models.py").write_text("")
    assert _known_by_plant_models(tmp_path) == {
        "microgrid.plant.dc_coupled": {"microgrid.engine._models"}
    }
