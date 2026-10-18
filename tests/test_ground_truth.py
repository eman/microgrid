"""The plant model against the meters: ``microgrid.engine.analysis.ground_truth``, on
neutral data: the example plant, a European zone."""

from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from engine_fixtures import EXAMPLE_PLANT, config_of, plant_of
from legacy_dispatch import NativePlant, simulate
from microgrid.engine.analysis.ground_truth import (
    GAP,
    HORIZON,
    Projection,
    Projector,
    Recording,
    clock,
    dc_bus_inputs,
    meters_plant,
    net_inverter_kw,
    project_against_meters,
    routing_class,
    session_holds,
)
from microgrid.engine.analysis.scoring import soc_end
from microgrid.engine.extend import Flows, SlotFlow
from microgrid.engine.series import SLOT, slot_starts
from microgrid.plant import WORKING_KW, HoldRule, Load, Plant

TZ = ZoneInfo("Europe/Berlin")
DAY = date(2026, 6, 10)
PLANT = plant_of(EXAMPLE_PLANT)


def _project(native: NativePlant) -> Projector:
    def run(
        starts: Sequence[datetime],
        pv: Sequence[float],
        load: Sequence[float],
        soc: float,
        /,
        *,
        dc_load: Sequence[float] | None = None,
        held_at_start: bool | None = None,
    ) -> tuple[SlotFlow, ...]:
        return simulate(native, starts, pv, load, soc, step=SLOT)

    return run


def _recording(days: int = 1, soc: float = 0.5, load_kw: float = 0.7) -> Recording:
    """Meters as the example plant says they would read: what a perfect model of it
    reproduces."""
    start = datetime.combine(DAY, time(), TZ).astimezone(UTC)
    end = datetime.combine(DAY + timedelta(days=days), time(), TZ).astimezone(UTC)
    starts = list(slot_starts(start, end))

    def pv_at(at: datetime) -> float:
        local = at.astimezone(TZ)
        hour = local.hour + local.minute / 60
        return max(0.0, 6.0 * (1 - abs(hour - 13) / 6))

    pv = [pv_at(at) for at in starts]
    load = [load_kw] * len(starts)
    native = EXAMPLE_PLANT
    modelled = simulate(native, starts, pv, load, soc, step=SLOT)
    capacity = native.battery.capacity_kwh
    flows = []
    for flow, kw in zip(modelled, load, strict=True):
        moving = flow.pv_to_battery + flow.grid_to_battery + flow.battery_to_load
        draw = native.standby_kw + (native.working_kw if moving > WORKING_KW else 0.0)
        flows.append(
            Flows(
                consumed=kw + draw,
                pv_to_load=flow.pv_to_load,
                battery_to_load=flow.battery_to_load,
                grid_to_load=flow.grid_to_load,
                pv_to_battery=flow.pv_to_battery,
                grid_to_battery=flow.grid_to_battery,
                pv_export=flow.pv_export,
                battery_export=0.0,
            )
        )
    return Recording(
        starts=tuple(starts),
        flows=tuple(flows),
        stored_after=tuple(soc_end(f) * capacity for f in modelled),
        stored_before=soc * capacity,
        p_import=(0.30,) * len(starts),
        p_export=(0.08,) * len(starts),
        pv_kw=tuple(pv),
        house_kw=tuple(load),
        grid_import_kw=tuple(f.grid_to_load + f.grid_to_battery for f in modelled),
        grid_export_kw=tuple(f.pv_export for f in modelled),
    )


def _run(rec: Recording, labels: dict[date, tuple[str, ...]] | None = None) -> Projection:
    return project_against_meters(
        rec,
        pinned=PLANT,
        plant=PLANT,
        project=_project(EXAMPLE_PLANT),
        tz=TZ,
        labels=labels or {},
        dc_draws=[0.0] * len(rec.starts),
    )


def test_a_perfect_model_matches_the_meters() -> None:
    found = _run(_recording(days=3))
    assert [day.reasons for day in found.days] == [(), (), ()]
    for day in found.days:
        assert day.pairs is not None and day.soc_max_pp == pytest.approx(0.0, abs=1e-9)
        for model, meters in day.pairs.values():
            assert model == pytest.approx(meters, abs=1e-9)
    runs = found.runs
    assert len(runs) == 3 * 24  # from every hour
    assert all(run.start.minute == 0 for run in runs)
    assert runs[0].full and not runs[-1].full  # the last run reaches the window's end
    assert all(len(r.soc_errors) <= HORIZON for r in runs)
    assert all(error == pytest.approx(0.0, abs=1e-9) for r in runs for _, error in r.soc_errors)


def test_the_callers_labels_pass_through_and_a_silent_stretch_adds_the_gap() -> None:
    """The labels are the caller's, opaque; the function adds only :data:`GAP`, where the
    telemetry was silent, and a day with it does not run."""
    rec = _recording(days=2)
    silent = list(rec.flows)
    silent[30] = None  # the first day's 05:00 local
    rec = replace(rec, flows=tuple(silent))
    second = DAY + timedelta(days=1)
    found = _run(rec, {DAY: ("a label",), second: ("another",)})
    first, other = found.days
    assert first.reasons == (GAP, "a label") and first.pairs is None
    assert other.reasons == ("another",) and other.pairs is not None
    of_day = {DAY: (GAP, "a label"), second: ("another",)}
    assert found.runs
    for run in found.runs:
        assert run.start.astimezone(TZ).date() in run.touched
        assert run.reasons == tuple(sorted({r for d in run.touched for r in of_day[d]}))


def test_a_dc_bus_plant_model_needs_the_dc_bus_meter() -> None:
    bus = config_of(EXAMPLE_PLANT, connection="dc_bus")
    assert bus.battery is not None and bus.hybrid is not None
    plant = Plant(
        replace(
            bus,
            loads=(*bus.loads, Load("dc_load", "dc_bus")),
            battery=replace(bus.battery, dc_charge_kw=5.0, dc_discharge_kw=5.0),
            hybrid=replace(bus.hybrid, ac_rating_kw=10.0),
        )
    )
    rec = _recording()
    with pytest.raises(ValueError, match="DC-bus load's meter"):
        project_against_meters(
            rec,
            pinned=PLANT,
            plant=plant,
            project=_project(EXAMPLE_PLANT),
            tz=TZ,
            labels={},
            dc_draws=[0.0] * len(rec.starts),
        )


def test_times_read_as_the_local_wall_clock_across_a_dst_change() -> None:
    change = date(2026, 10, 25)  # Europe's fall-back
    assert clock(change, 60, TZ) == "01:00"
    assert clock(change, 180, TZ) == "02:00"  # 02:00-03:00 happens twice


# -- the DC bus, the inverter and the routing --------------------------------------------


def _dc_plant(*, hold_below: float = 0.9) -> Plant:
    """The example plant folded on a hybrid's DC bus with a DC-bus load beside the
    battery: the battery's DC limits, the hybrid's AC rating, and a hold rule."""
    config = config_of(EXAMPLE_PLANT, connection="dc_bus")
    part, hybrid = config.battery, config.hybrid
    assert part is not None and hybrid is not None
    return Plant(
        replace(
            config,
            loads=(*config.loads, Load("dc_load", "dc_bus")),
            battery=replace(part, dc_charge_kw=6.3, dc_discharge_kw=6.3),
            hybrid=replace(hybrid, ac_rating_kw=14.4),
            dispatch=replace(config.dispatch, hold_rules=(HoldRule(1.0, hold_below),)),
        )
    )


AT = datetime(2026, 6, 10, 19, 0, tzinfo=UTC)


def test_the_models_inverter_power_is_out_positive_with_its_own_draw() -> None:
    # Hand-worked.
    charging = SlotFlow(start=AT, soc_start=0.5, soc_end=0.6, pv_to_battery=3.0)
    serving = SlotFlow(start=AT, soc_start=0.5, soc_end=0.4, battery_to_load=2.0)
    idle = SlotFlow(start=AT, soc_start=0.5, soc_end=0.5)
    working = EXAMPLE_PLANT.standby_kw + EXAMPLE_PLANT.working_kw
    assert net_inverter_kw(PLANT, charging, 0.0) == pytest.approx(-3.0 - working)  # in
    assert net_inverter_kw(PLANT, serving, 0.0) == pytest.approx(2.0 - working)  # out
    assert net_inverter_kw(PLANT, idle, 0.0) == pytest.approx(-EXAMPLE_PLANT.standby_kw)


def test_the_models_inverter_power_counts_the_ac_it_takes_for_the_dc_bus() -> None:
    # Hand-worked: the house takes 1 kW; the grid gives 4.1, of which 0.05 is the draw
    # and 3.05 the AC the hybrid takes for the DC-bus load: 3.1 in.
    flow = SlotFlow(
        start=AT,
        soc_start=0.5,
        soc_end=0.5,
        grid_to_load=4.1,
        ac_to_dc_load=3.0,
        inverter_draw=0.05,
    )
    assert net_inverter_kw(_dc_plant(), flow, 1.0) == pytest.approx(-3.1)


def test_the_battery_serving_the_dc_bus_is_serving() -> None:
    flow = SlotFlow(start=AT, soc_start=0.5, soc_end=0.4, battery_to_dc_load=3.0)
    assert routing_class(_dc_plant(), flow) == "battery serving"


def test_the_meters_are_read_without_the_dc_bus_load() -> None:
    assert meters_plant(PLANT) is PLANT
    assert meters_plant(_dc_plant()).config == plant_of(EXAMPLE_PLANT, connection="dc_bus").config


def test_holds_by_rule_and_an_unknown_start() -> None:
    # Two rules, one session each way; a session whose start SOC is unknown leaves the
    # engine to decide (None).
    two = _dc_plant()
    rules = (HoldRule(1.0, 0.5), HoldRule(5.0, 0.9))
    two = Plant(replace(two.config, dispatch=replace(two.config.dispatch, hold_rules=rules)))
    dc = [0.0, 3.0, 6.0, 6.0, 3.0, 0.0]
    soc: list[float | None] = [0.6, 0.6, 0.6, 0.6, 0.6, 0.6]
    # The 5 kW rule's session (slots 2-3) holds; the 1 kW rule's, from 60 %, does not.
    assert session_holds(two, dc, soc) == [None, False, True, True, False, None]
    soc[1] = None
    assert session_holds(_dc_plant(), dc, soc) == [None, None, None, None, None, None]


def test_dc_bus_inputs_split_a_session() -> None:
    # The house is the load served less the DC-bus load's draw; the DC bus takes the
    # meter where the load drew (unknown where it drew and the meter is), and the hold
    # its start's.
    plant = _dc_plant(hold_below=0.6)
    served = [1.0, 4.0, 4.2, 1.1]
    draws = [0.0, 3.0, 3.1, 0.0]
    dc = [0.2, 2.9, None, 0.3]
    soc: list[float | None] = [0.5, 0.5, 0.52, 0.53]
    house, bus, held = dc_bus_inputs(plant, served, draws, dc, soc)
    assert house == pytest.approx([1.0, 1.0, 1.1, 1.1])
    assert bus == [0.0, 2.9, None, 0.0]
    assert held == [None, True, None, None]


def test_a_dc_bus_plant_model_is_fed_the_session_on_its_bus() -> None:
    """A run inside a session is fed the DC-bus meter and the hold its start says."""
    rec = _recording()
    slot = 60  # an hour's start
    flows = list(rec.flows)
    flow = flows[slot]
    assert flow is not None
    flows[slot] = replace(flow, consumed=flow.consumed + 3.0, grid_to_load=flow.grid_to_load + 3.0)
    dc = [0.1] * len(rec.starts)
    dc[slot] = 2.9
    draws = [0.0] * len(rec.starts)
    draws[slot] = 3.0
    rec = replace(rec, flows=tuple(flows), dc_load_kw=tuple(dc))
    plant = _dc_plant(hold_below=1.0)  # any session holds
    fed: dict[datetime, tuple[list[float] | None, bool | None]] = {}

    def feeding(
        starts: Sequence[datetime],
        pv: Sequence[float],
        load: Sequence[float],
        soc: float,
        /,
        *,
        dc_load: Sequence[float] | None = None,
        held_at_start: bool | None = None,
    ) -> tuple[SlotFlow, ...]:
        fed.setdefault(starts[0], (list(dc_load) if dc_load is not None else None, held_at_start))
        return plant.project(
            starts,
            [SLOT] * len(starts),
            pv,
            load,
            soc,
            dc_load_kw=dc_load,
            held_at_start=held_at_start,
        )

    project_against_meters(
        rec, pinned=PLANT, plant=plant, project=feeding, tz=TZ, labels={}, dc_draws=draws
    )
    bus, held = fed[rec.starts[slot]]
    assert bus is not None and bus[0] == 2.9 and held is True
    before, _ = fed[rec.starts[slot - 6]]
    assert before is not None and before[6] == 2.9 and before[0] == 0.0  # noise is not a draw


def test_a_perfect_model_agrees_on_events_and_routing() -> None:
    found = _run(_recording(days=2))
    events = [run.event for run in found.runs if run.event is not None]
    assert events and all(model == meters for model, meters in events)
    assert found.routing and all(
        key.split(" -> ")[0] == key.split(" -> ")[1] for key in found.routing
    )


def test_a_dc_bus_plant_model_needs_the_draws_outside_the_house_meter() -> None:
    rec = replace(_recording(), dc_load_kw=(0.0,) * 144)
    with pytest.raises(ValueError, match="DC-bus load's draws"):
        project_against_meters(
            rec, pinned=PLANT, plant=_dc_plant(), project=_project(EXAMPLE_PLANT), tz=TZ, labels={}
        )
