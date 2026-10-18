"""The price rule on a plant model that is not this site's.

The AC-coupled plant gives some port questions one answer whatever is asked: one
refill path for PV and the grid, and PV always charging before the grid. So a rule
that asked the wrong question there would price this site the same, and only a
second topology would show it. A stub ``Storage`` with no battery behind it answers
each differently, and these tests hold the rule to asking the right one.
"""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, time, timedelta
from functools import partial
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from engine_fixtures import plant_of, rerun_cost, site_meters
from legacy_dispatch import NativePlant, PlantStorage
from microgrid.engine import Served
from microgrid.engine._live import importing_now
from microgrid.engine._supply import supply_stack
from microgrid.engine.extend import (
    OBSERVED,
    ChargeSource,
    Event,
    Flows,
    PlantModel,
    SlotDispatch,
    SlotFlow,
    Storage,
    forecast_surplus,
    importing,
    price_flows,
)
from microgrid.engine.extend import (
    value_energy as _value_energy,
)
from microgrid.plant import Battery, GridCharge
from microgrid.plant import Plant as SitePlant
from microgrid.plant.flows import MeterReading, PvKw

#: The make-up rule's arithmetic, tested as such: what replaces the kWh
#: (``Pricing.battery_value = "replacement"``; the default prices a battery kWh at
#: its stored cost).
value_energy = partial(_value_energy, battery_value="replacement")

T0 = datetime(2026, 9, 25, 7, 0, tzinfo=UTC)
SLOT = timedelta(minutes=10)
IMPORT, CHEAP, EXPORT = 0.40, 0.30, 0.08
#: What the stub adds to a refill's price, by source: tells the paths apart.
MARK = {ChargeSource.PV: 2.0, ChargeSource.GRID: 1.0}


@dataclass
class Stub:
    """A ``Storage`` delegating to this site's for every answer but the ones a test
    turns, and recording what the rule asked. No ``battery``: the rule needs none."""

    base: PlantStorage = field(default_factory=lambda: PlantStorage(Battery(10.0, 5.0, 5.0)))
    refills: bool = True
    can_top_up: bool = True
    divertible_share: float = 1.0
    #: What ``can_serve`` answers; None: this site's answer.
    serves: bool | None = None
    asked: list[tuple[ChargeSource, datetime]] = field(default_factory=list)

    @property
    def capacity_kwh(self) -> float:
        return self.base.capacity_kwh

    @property
    def reserve_soc(self) -> float:
        return self.base.reserve_soc

    @property
    def saturation_soc(self) -> float:
        return self.base.saturation_soc

    @property
    def wear_discharge_per_kwh(self) -> float:
        return self.base.wear_discharge_per_kwh

    def charge_wear_rate(self, source: ChargeSource) -> float:
        return self.base.charge_wear_rate(source)

    def divertible_kw(self, flow: SlotFlow) -> float:
        return self.divertible_share * self.base.divertible_kw(flow)

    def can_charge_more(
        self, flow: SlotFlow, source: ChargeSource, *, margin_kw: float = 0.0
    ) -> bool:
        return self.can_top_up and self.base.can_charge_more(flow, source, margin_kw=margin_kw)

    def at_discharge_limit(self, flow: SlotFlow, *, margin_kw: float = 0.0) -> bool:
        return self.base.at_discharge_limit(flow, margin_kw=margin_kw)

    def can_serve(self, flow: SlotFlow, more_kw: float) -> bool:
        return self.base.can_serve(flow, more_kw) if self.serves is None else self.serves

    def grid_refills_diverted_pv(self, flow: SlotFlow) -> bool:
        return self.refills

    def takes_clipped_pv(self, flow: SlotFlow) -> bool:
        return False

    def headroom(self, flow: SlotFlow, hours: float) -> float:
        return self.base.headroom(flow, hours)

    def refill_value(self, price: float, source: ChargeSource, flow: SlotFlow) -> float:
        self.asked.append((source, flow.start))
        return price + MARK[source]

    def displaced_value(self, price: float, flow: SlotFlow | None = None) -> float:
        return self.base.displaced_value(price, flow)

    def dead_band(self, stored_value: float, flow: SlotFlow) -> tuple[float, float]:
        return self.base.dead_band(stored_value, flow)

    def stored_per_kwh(self, source: ChargeSource, flow: SlotFlow | None = None) -> float:
        return self.base.stored_per_kwh(source, flow)


def _storage(stub: Stub) -> Storage:
    return stub  # mypy: the stub is a Storage


def _slot(index: int, soc_start: float, soc_end: float, **kw: Any) -> SlotFlow:
    return SlotFlow(start=T0 + index * SLOT, soc_start=soc_start, soc_end=soc_end, **kw)


def test_a_fill_is_refilled_from_pv_at_the_fill() -> None:
    flows = [
        _slot(0, 0.90, 0.95, pv_to_battery=2.0),
        _slot(1, 0.95, 1.00, pv_to_battery=2.0),
        _slot(2, 1.00, 1.00, pv_export=3.0),  # full, spilling
    ]
    stub = Stub()
    priced = price_flows(_storage(stub), flows, [IMPORT] * 3, [EXPORT] * 3, 144)
    assert priced.events[:2] == (Event.FILL, Event.FILL)
    fill_at = priced.event_at[0]
    assert stub.asked == [(ChargeSource.PV, flows[fill_at].start)] * 2
    assert priced.stored_value[0] == pytest.approx(EXPORT + MARK[ChargeSource.PV])


def test_a_top_up_is_refilled_from_the_grid_at_the_top_up() -> None:
    flows = [
        _slot(0, 0.50, 0.45, battery_to_load=2.0),
        _slot(1, 0.45, 0.60, grid_to_battery=4.0),  # the plant tops up (metered)
        _slot(2, 0.60, 0.75, grid_to_battery=4.0),
    ]
    stub = Stub()
    priced = price_flows(_storage(stub), flows, [IMPORT, CHEAP, CHEAP], [EXPORT] * 3, 144)
    assert priced.events[0] is Event.TOPUP
    assert stub.asked[0] == (ChargeSource.GRID, flows[1].start)
    assert priced.stored_value[0] == pytest.approx(CHEAP + MARK[ChargeSource.GRID])


def test_pv_diverted_where_the_top_up_binds_is_refilled_there_only_if_the_plant_says() -> None:
    flows = [
        _slot(0, 0.5, 0.6, pv_to_battery=1.0, grid_to_battery=2.0),
        _slot(1, 0.6, 0.7, grid_to_battery=4.0),
    ]
    refilled = price_flows(_storage(Stub()), flows, [IMPORT, CHEAP], [EXPORT] * 2, 144)
    assert refilled.event_at[0] == 0  # the grid refills it in the same slot
    assert refilled.stored_value[0] == pytest.approx(IMPORT + MARK[ChargeSource.GRID])
    later = price_flows(_storage(Stub(refills=False)), flows, [IMPORT, CHEAP], [EXPORT] * 2, 144)
    assert later.event_at[0] == 1  # a plant that charges otherwise: the next top-up
    assert later.stored_value[0] == pytest.approx(CHEAP + MARK[ChargeSource.GRID])


def test_a_model_top_up_needs_room_the_plant_says_it_has() -> None:
    # The plant model's window (``topup_gap``): the grid buys back where it can still
    # charge more. A plant that says it cannot has no top-up to buy back at.
    flows = [
        _slot(0, 0.50, 0.45, battery_to_load=2.0),
        _slot(1, 0.45, 0.45, dispatch=SlotDispatch(topup_gap=0.5)),
        _slot(2, 0.45, 0.30, battery_to_load=2.0),
    ]
    prices = [IMPORT, CHEAP, IMPORT]
    room = price_flows(_storage(Stub()), flows, prices, [EXPORT] * 3, 144)
    assert room.events[0] is Event.TOPUP and room.event_at[0] == 1
    full = price_flows(_storage(Stub(can_top_up=False)), flows, prices, [EXPORT] * 3, 144)
    assert Event.TOPUP not in full.events


def test_the_stacks_pv_step_is_what_a_load_could_take() -> None:
    flow = _slot(0, 0.5, 0.6, pv_to_battery=2.0)
    steps = supply_stack(
        _storage(Stub(divertible_share=0.9)),
        flow,
        SLOT / timedelta(hours=1),
        served=Served.PV,
        price=0.1,
        p_import=IMPORT,
        stored_value=0.1,
    )
    assert steps[0].source is Served.PV
    assert steps[0].kw == pytest.approx(1.8)  # not pv_to_battery: what the load receives


def test_importing_asks_the_plant_whether_the_battery_could_carry_it() -> None:
    # A small import with the battery above its reserve: regulation the battery
    # carries, if the plant says it could; the house importing, if not.
    regulation = _slot(0, 0.6, 0.6, grid_to_load=0.2, battery_to_load=1.0)
    assert not importing(_storage(Stub()), regulation, OBSERVED)
    assert importing(_storage(Stub(serves=False)), regulation, OBSERVED)


def test_a_battery_within_the_meters_noise_of_its_limit_has_nothing_more_to_give() -> None:
    # On the meters (OBSERVED: need_kw 0.12), a battery delivering within 0.12 kW of
    # its 5 kW limit is at it: the grid serves a kWh more (``at_discharge_limit``'s
    # margin), though the house is not importing.
    near = _slot(0, 0.6, 0.6, battery_to_load=4.95)
    priced = price_flows(
        _storage(Stub()),
        [near, replace(near, start=T0 + SLOT)],
        [IMPORT] * 2,
        [EXPORT] * 2,
        144,
        noise=OBSERVED,
    )
    assert not importing(_storage(Stub()), near, OBSERVED)
    assert priced.served[0] is Served.GRID
    clear = _slot(0, 0.6, 0.6, battery_to_load=4.5)
    priced = price_flows(
        _storage(Stub()),
        [clear, replace(clear, start=T0 + SLOT)],
        [IMPORT] * 2,
        [EXPORT] * 2,
        144,
        noise=OBSERVED,
    )
    assert priced.served[0] is Served.BATTERY


def test_float_residue_of_spare_pv_does_not_stop_a_top_up() -> None:
    # ``can_charge_more``'s margin for the grid is also the spare-PV threshold: PV
    # spare by float residue (under 1e-9 kW) does not mean PV will refill it.
    flows = [
        _slot(0, 0.50, 0.45, battery_to_load=2.0),
        _slot(1, 0.45, 0.45, pv_export=5e-10, dispatch=SlotDispatch(topup_gap=0.5)),
        _slot(2, 0.45, 0.30, battery_to_load=2.0),
    ]
    priced = price_flows(_storage(Stub()), flows, [IMPORT, CHEAP, IMPORT], [EXPORT] * 3, 144)
    assert priced.events[0] is Event.TOPUP and priced.event_at[0] == 1


def test_full_is_the_plants_saturation_not_a_full_soc() -> None:
    # A battery that counts as full at 95 % (``saturation_soc``) fills there: spare
    # PV at 96 % refills it, though 96 % is short of 100 %.
    stub = Stub(base=PlantStorage(Battery(10.0, 5.0, 5.0, saturation_soc=0.95)))
    flows = [
        _slot(0, 0.90, 0.93, pv_to_battery=2.0),
        _slot(1, 0.93, 0.96, pv_to_battery=2.0),
        _slot(2, 0.96, 0.96, pv_export=3.0),  # at its saturation, spilling
    ]
    priced = price_flows(_storage(stub), flows, [IMPORT] * 3, [EXPORT] * 3, 144)
    assert priced.events[0] is Event.FILL and priced.event_at[0] == 2


# --- The valuation on a plant model it does not know -----------------------------


@dataclass
class Plant:
    """A ``PlantModel`` delegating to this site's and counting what it is asked: the
    price model must ask it, through the port, and nothing else."""

    base: SitePlant
    #: The battery as the stub answers for it; None: this site's.
    battery: Storage | None = None
    projected: list[int] = field(default_factory=list)
    stopped: int = 0
    metered: int = 0
    #: Each projection the plant returned, in order.
    flows: list[tuple[SlotFlow, ...]] = field(default_factory=list)

    @property
    def storage(self) -> Storage | None:
        return self.base.storage if self.battery is None else self.battery

    @property
    def has_pv(self) -> bool:
        return self.base.has_pv

    @property
    def pv_arrays(self) -> tuple[str, ...]:
        return self.base.pv_arrays

    @property
    def pv_per_array(self) -> bool:
        return self.base.pv_per_array

    @property
    def import_limit_kw(self) -> float | None:
        return self.base.import_limit_kw

    @property
    def has_dc_loads(self) -> bool:
        return self.base.has_dc_loads

    def project(
        self,
        starts: Sequence[datetime],
        durations: Sequence[timedelta],
        pv_kw: PvKw,
        load_kw: Sequence[float],
        soc: float | None,
        *,
        dc_load_kw: Sequence[float] | None = None,
        held_at_start: bool | None = None,
        stop: Callable[[int, SlotFlow], bool] | None = None,
    ) -> tuple[SlotFlow, ...]:
        self.projected.append(len(starts))
        self.stopped += stop is not None
        flows = self.base.project(
            starts,
            durations,
            pv_kw,
            load_kw,
            soc,
            dc_load_kw=dc_load_kw,
            held_at_start=held_at_start,
            stop=stop,
        )
        self.flows.append(flows)
        return flows

    @property
    def meters(self) -> Mapping[str, str]:
        return self.base.meters

    def pv_from_meters(self, reading: MeterReading, *, per_array: bool) -> PvKw | None:
        return self.base.pv_from_meters(reading, per_array=per_array)

    def read_meters(self, reading: MeterReading) -> Flows | None:
        return self.base.read_meters(reading)

    def metered_flow(
        self, start: datetime, soc: float | None, reading: MeterReading
    ) -> SlotFlow | None:
        self.metered += 1
        return self.base.metered_flow(start, soc, reading)


def _plant_model(plant: Plant) -> PlantModel:
    return plant  # mypy: the stub is a PlantModel


NATIVE = NativePlant(
    Battery(10.0, 5.0, 5.0, eta_charge=0.95, eta_discharge=0.95, reserve_soc=0.1),
    ZoneInfo("America/Los_Angeles"),
    standby_kw=0.04,
    working_kw=0.05,
    grid_charge=GridCharge(start=time(12), end=time(15), target_soc=0.8, timing="late"),
)


def _day() -> tuple[list[datetime], list[list[float]], list[float], list[float], list[float]]:
    starts = [T0 + k * SLOT for k in range(144)]  # from 00:00 PDT
    hour = [(T0 + k * SLOT).astimezone(NATIVE.timezone).hour for k in range(144)]
    sunny = [max(0.0, 5.0 * (1 - abs(h - 13) / 6)) for h in hour]
    pv = [sunny, [0.5 * kw for kw in sunny]]
    load = [0.7] * 144
    p_import = [0.45 if 16 <= h < 21 else 0.30 for h in hour]
    return starts, pv, load, p_import, [EXPORT] * 144


def test_the_valuation_asks_the_plant_model_and_prices_as_this_sites() -> None:
    starts, pv, load, p_import, p_export = _day()
    plant = Plant(plant_of(NATIVE))
    through = value_energy(
        _plant_model(plant), starts, pv, load, 0.4, p_import, p_export, step=SLOT
    )
    direct = value_energy(plant_of(NATIVE), starts, pv, load, 0.4, p_import, p_export, step=SLOT)
    assert plant.projected == [144, 144]  # one projection per scenario
    assert (through.price, through.price_p10) == (direct.price, direct.price_p10)
    assert through.model is not None and through.model.plant is plant
    # A what-if (the test's oracle, ``rerun_cost``): the same plant projected again,
    # from that slot, to where the battery agrees again.
    hour = timedelta(hours=1)
    costs = [rerun_cost(through.model, s, 60, 2.0, hour) for s in (0, 1)]
    assert direct.model is not None
    assert costs == [rerun_cost(direct.model, s, 60, 2.0, hour) for s in (0, 1)]
    assert plant.stopped == 2 and plant.projected[2:] == [84, 84]


def test_the_surplus_forecast_asks_the_plant_model_up_to_the_first_unknown() -> None:
    starts, pv, load, _, p_export = _day()
    gappy: list[float | None] = [*load[:100], None, *load[101:]]
    plant = Plant(plant_of(NATIVE))
    found = forecast_surplus(
        _plant_model(plant),
        starts,
        pv[0],
        pv[1],
        gappy,
        0.4,
        first_remaining=SLOT / 2,
        p_export=p_export,
    )
    assert plant.projected == [100, 100]  # cut at the unknown load
    assert found.surplus_kw[99] is not None and found.surplus_kw[100] is None
    with pytest.raises(ValueError, match="within one slot"):
        forecast_surplus(
            _plant_model(plant), starts, pv[0], pv[1], load, 0.4, first_remaining=SLOT * 2
        )


def test_importing_now_reads_the_meters_through_the_plant_model() -> None:
    plant = Plant(plant_of(NATIVE))
    assert importing_now(_plant_model(plant), 0.6, now=T0, meters=site_meters(plant, grid=3.0))
    assert plant.metered == 1


def test_the_meters_reach_the_plant_model_whole() -> None:
    # 1 kW from the grid with the battery charging at 2 kW: the grid charges the
    # battery, the house does not import. With the battery idle, it does.
    plant = plant_of(NATIVE)
    assert not importing_now(plant, 0.6, now=T0, meters=site_meters(plant, grid=1.0, battery=2.0))
    assert importing_now(plant, 0.6, now=T0, meters=site_meters(plant, grid=1.0))


def test_each_pv_case_is_cut_at_its_own_first_unknown() -> None:
    starts, pv, load, _, _ = _day()
    gappy: list[float | None] = [*pv[1][:90], None, *pv[1][91:]]
    plant = Plant(plant_of(NATIVE))
    found = forecast_surplus(_plant_model(plant), starts, pv[0], gappy, load, 0.4)
    assert plant.projected == [144, 90]  # p50 whole; p10 to its own unknown
    assert found.surplus_kw[143] is not None
    assert found.surplus_kw_p10[89] is not None and found.surplus_kw_p10[90] is None
    with pytest.raises(ValueError, match="same length"):
        forecast_surplus(_plant_model(plant), starts, pv[0], pv[1][:-1], load, 0.4)
    # The start of the first unknown slot must be UTC too, as the plant checked it
    # before it stopped there; and a bad SOC is named before a bad slot under way.
    local = [*starts[:90], starts[90].astimezone(NATIVE.timezone), *starts[91:]]
    with pytest.raises(ValueError, match="UTC"):
        forecast_surplus(_plant_model(plant), local, gappy, gappy, load, 0.4)  # both cut at 90
    with pytest.raises(ValueError, match="soc"):
        forecast_surplus(
            _plant_model(plant), starts, pv[0], pv[1], load, 1.5, first_remaining=SLOT * 2
        )


def test_a_soc_reading_a_little_out_is_clamped_before_the_plant_sees_it() -> None:
    # The plant model refuses a SOC outside 0-1; the valuation clamps a reading.
    starts, pv, load, p_import, p_export = _day()
    plant = plant_of(NATIVE)

    def price(soc: float) -> tuple[float, ...]:
        return value_energy(plant, starts, pv, load, soc, p_import, p_export, step=SLOT).price

    assert price(1.02) == price(1.0)
    assert price(-0.01) == price(0.0)
    with pytest.raises(ValueError, match="soc"):
        plant.project(starts, [SLOT] * 144, pv[0], load, 1.02)
