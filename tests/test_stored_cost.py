"""A kWh the battery supplies is priced at the running average cost of all it holds:
solar at the export it gave up, grid energy at what it was bought for, energy of
unknown origin as solar; the pool runs forward along each scenario. Where
solar would refill the room a draw frees before the battery needs the grid, the draw
costs no more than that solar: a full battery must not be kept full."""

from dataclasses import replace
from datetime import UTC, datetime, time
from pathlib import Path

import pytest

from engine_fixtures import EXAMPLE_PLANT, PACIFIC, plant_of, storage_of
from microgrid.engine.extend import ChargeSource, value_energy
from microgrid.engine.ledger import Amount, Ledger, Lots, Source, project_average
from microgrid.engine.series import SLOT
from microgrid.plant import GridCharge, Plant
from microgrid.plant.examples import example_plant
from microgrid.plant.flows import Flows, SlotFlow

PLANT = plant_of(EXAMPLE_PLANT)
BATTERY = EXAMPLE_PLANT.battery
EVENING = datetime(2026, 10, 22, 2, 0, tzinfo=UTC)  # 19:00 Pacific: no sun
MORNING = datetime(2026, 10, 22, 15, 0, tzinfo=UTC)  # 08:00 Pacific
PEAK, NIGHT, EXPORT = 0.45, 0.18, 0.07
STORED_PER_AC = BATTERY.stored_by(BATTERY.charge_kw) / BATTERY.charge_kw


def _bought(kwh: float, ac_price: float) -> Amount:
    """``kwh`` stored, charged at ``ac_price`` per AC kWh (the ledger's basis)."""
    return Amount(kwh, kwh / STORED_PER_AC * ac_price)


def _pool(*parts: Amount) -> Lots:
    pool = sum(parts, Amount())
    return Lots(EVENING, {}, pool)


def _flows(**kw: float) -> Flows:
    zero = dict.fromkeys(
        ("consumed", "pv_to_load", "battery_to_load", "grid_to_load", "pv_to_battery",
         "grid_to_battery", "pv_export", "battery_export"),
        0.0,
    )  # fmt: skip
    return Flows(**(zero | kw))


def _first_price(
    lots: Lots | None,
    *,
    start: datetime = EVENING,
    pv_kw: float = 0.0,
    soc: float = 0.8,
    rule: str = "stored_cost",
    slots: int = 12,
) -> float:
    """The price of a kWh in the first slot: a 1 kW house, flat PV, the peak import
    (the battery lasts the horizon at night: no empty event)."""
    energy = value_energy(
        PLANT,
        [start + i * SLOT for i in range(slots)],
        [[pv_kw] * slots],
        [1.0] * slots,
        soc,
        [PEAK] * slots,
        [EXPORT] * slots,
        step=SLOT,
        lots=lots,
        battery_value=rule,  # type: ignore[arg-type]
    )
    return energy.price[0]


def test_a_draw_takes_the_average_so_the_draw_order_does_not_move_it() -> None:
    """The lots draw grid energy first for the house; the pool's average does not move."""
    ledger = Ledger(BATTERY, unknown_basis=0.0)
    ledger.step(EVENING, _flows(pv_to_battery=2.0, grid_to_battery=2.0), 1.0,
                pv_price=EXPORT, grid_price=NIGHT, stored_after=None)  # fmt: skip
    before = ledger.pool.unit
    at_4_kw = BATTERY.stored_by(4.0) / 4.0
    assert before == pytest.approx((EXPORT + NIGHT) / 2 / at_4_kw)
    ledger.step(EVENING + SLOT, _flows(battery_to_load=1.5), 1.0,
                pv_price=EXPORT, grid_price=NIGHT, stored_after=None)  # fmt: skip
    assert ledger.pool.unit == pytest.approx(before)
    assert ledger.pool.kwh == pytest.approx(sum(lot.kwh for lot in ledger.held.values()))


def test_energy_of_unknown_origin_is_priced_as_bought_at_the_first_import() -> None:
    """Not knowing what it cost moves the price toward the import (R-7)."""
    ledger = Ledger(BATTERY, unknown_kwh=5.0)
    ledger.step(EVENING, None, 1.0, pv_price=EXPORT, grid_price=NIGHT, stored_after=None)
    assert ledger.pool.unit == pytest.approx(NIGHT / STORED_PER_AC)


def test_energy_the_meter_finds_in_an_empty_model_is_priced_at_that_slots_import() -> None:
    """The unknown basis follows the import, not the one when the ledger began."""
    ledger = Ledger(BATTERY)
    ledger.step(EVENING, None, 1.0, pv_price=EXPORT, grid_price=NIGHT, stored_after=0.0)
    ledger.step(EVENING + SLOT, None, 1.0, pv_price=EXPORT, grid_price=PEAK, stored_after=2.0)
    assert ledger.pool.unit == pytest.approx(PEAK / STORED_PER_AC)


def test_a_battery_full_of_solar_prices_its_kwh_near_the_export_not_the_peak() -> None:
    held = BATTERY.capacity_kwh * 0.8
    solar = _first_price(_pool(_bought(held, EXPORT)))
    # The export it gave up, through the round trip's losses and the discharge's wear.
    assert EXPORT < solar < 0.15
    # The make-up rule prices the same kWh at what replaces it: the peak import.
    assert _first_price(_pool(_bought(held, EXPORT)), rule="replacement") > 0.4


def test_grid_energy_costs_what_it_was_bought_for() -> None:
    held = BATTERY.capacity_kwh * 0.8
    grid = _first_price(_pool(_bought(held, NIGHT)))
    solar = _first_price(_pool(_bought(held, EXPORT)))
    half = _first_price(_pool(_bought(held / 2, EXPORT), _bought(held / 2, NIGHT)))
    assert NIGHT < grid < PEAK
    assert solar < half < grid


def test_without_lots_all_it_holds_is_valued_as_bought_at_the_import() -> None:
    held = BATTERY.capacity_kwh * 0.8
    assert _first_price(None) == pytest.approx(_first_price(_pool(_bought(held, PEAK))))


def test_lots_without_a_pool_are_priced_at_their_blend() -> None:
    held = BATTERY.capacity_kwh * 0.8
    lots = Lots(EVENING, {Source.GRID: _bought(held, NIGHT)})
    assert _first_price(lots) == pytest.approx(_first_price(_pool(_bought(held, NIGHT))))


def test_the_pool_is_scaled_to_the_charge_level_now() -> None:
    """The pool a client gives may not match the SOC: scaled to it at its average, as
    the ledger scales to the meter."""
    assert _first_price(_pool(_bought(1.0, NIGHT))) == pytest.approx(
        _first_price(_pool(_bought(9.0, NIGHT)))
    )


def test_the_price_holds_while_the_battery_only_discharges() -> None:
    """A discharge takes the average: a mixed pool's price stays put, slot after slot."""
    held = BATTERY.capacity_kwh * 0.8
    energy = value_energy(
        PLANT,
        [EVENING + i * SLOT for i in range(18)],
        [[0.0] * 18],
        [1.0] * 18,
        0.8,
        [PEAK] * 18,
        [EXPORT] * 18,
        step=SLOT,
        lots=_pool(_bought(held / 2, EXPORT), _bought(held / 2, NIGHT)),
    )
    assert max(energy.price) - min(energy.price) < 0.005


def test_a_window_top_up_adds_grid_energy_that_later_slots_pay_for() -> None:
    """The pool runs forward: after the plant tops the battery up from the grid in its
    window, a battery kWh costs more than before."""
    window = plant_of(
        replace(
            EXAMPLE_PLANT,
            grid_charge=GridCharge(start=time(20, 0), end=time(21, 0), target_soc=0.95),
        )
    )
    energy = value_energy(
        window,
        [EVENING + i * SLOT for i in range(18)],
        [[0.0] * 18],
        [0.5] * 18,
        0.5,
        [NIGHT] * 18,
        [EXPORT] * 18,
        step=SLOT,
        lots=_pool(_bought(BATTERY.capacity_kwh * 0.5, EXPORT)),
    )
    model = energy.model
    assert model is not None
    topped = [i for i, flow in enumerate(model.flows[0]) if flow.grid_to_battery > 0.1]
    assert topped, "the window tops the battery up"
    assert energy.price[topped[-1] + 1] > energy.price[0] + 0.01


def test_a_battery_full_of_grid_energy_is_drawn_when_solar_will_refill_it() -> None:
    """Kept full, it would spill the day's solar: a kWh drawn now costs the solar that
    refills its room, not the grid energy it holds."""
    full = _pool(_bought(BATTERY.capacity_kwh * 0.95, NIGHT))
    grid_cost = _first_price(full, start=MORNING, soc=0.95)  # no sun: nothing refills it
    sunny = _first_price(full, start=MORNING, soc=0.95, pv_kw=5.0, slots=36)
    assert grid_cost > NIGHT
    assert sunny < EXPORT / STORED_PER_AC + 0.01
    assert sunny < grid_cost - 0.1


def test_pv_diverted_while_the_grid_tops_up_costs_that_purchase() -> None:
    """A kWh of PV diverted where the plant's top-up binds is bought back from the grid
    in the same slot: certain, not forecast, so it costs that import, not the export."""
    window = plant_of(
        replace(
            EXAMPLE_PLANT,
            grid_charge=GridCharge(start=time(13, 0), end=time(15, 0), target_soc=0.9),
        )
    )
    noon = datetime(2026, 10, 22, 20, 0, tzinfo=UTC)  # 13:00 Pacific
    slots = 24
    energy = value_energy(
        window,
        [noon + i * SLOT for i in range(slots)],
        [[0.8] * slots],  # a dull day: PV charges a little while the grid tops up
        [0.3] * slots,
        0.3,
        [0.30] * slots,
        [EXPORT] * slots,
        step=SLOT,
        lots=_pool(_bought(BATTERY.capacity_kwh * 0.3, EXPORT)),
    )
    model = energy.model
    assert model is not None
    flows = model.flows[0]
    both = [i for i, f in enumerate(flows) if f.grid_to_battery > 0.5 and f.pv_to_battery > 0.2]
    assert both, "PV and the grid charge together in the window"
    assert all(energy.price[i] > 0.25 for i in both)


def test_the_supply_stack_prices_the_battery_and_pv_as_the_slot_does() -> None:
    """The stack's battery step is the stored cost's, and its PV step the export."""
    from microgrid.cli.dataset import read_dataset
    from microgrid.cli.offline import load_setup, value

    data = Path(__file__).parents[1] / "examples" / "dataset"
    valuation = value(
        load_setup(data), read_dataset(data), datetime(2026, 10, 22, 11, 3, tzinfo=UTC)
    )
    checked = 0
    for slot in valuation.slots:
        steps = {step.source.value: step.per_kwh for step in slot.supply}
        if slot.battery_value is not None and "battery" in steps and slot.regime == "battery":
            assert steps["battery"] == pytest.approx(slot.battery_value.per_kwh, abs=0.01)
            checked += 1
        if "pv" in steps and slot.regime == "battery":  # no top-up in the slot
            assert steps["pv"] == pytest.approx(slot.p_export)
    assert checked > 50


def test_a_live_revaluation_prices_from_the_lots_now_else_the_valuations() -> None:
    """``revalue``: the client's lots at the moment, else the ones the valuation ran on."""
    from microgrid.engine._live import revalue

    held = BATTERY.capacity_kwh * 0.8
    energy = value_energy(
        PLANT,
        [EVENING + i * SLOT for i in range(12)],
        [[0.0] * 12],
        [1.0] * 12,
        0.8,
        [PEAK] * 12,
        [EXPORT] * 12,
        step=SLOT,
        lots=_pool(_bought(held, EXPORT)),
    )
    model = energy.model
    assert model is not None

    def live(lots: Lots | None) -> float:
        found = revalue(
            model, 0, EVENING, measured_pv_kw=0.0, measured_load_kw=1.0, soc=0.8, lots=lots
        )
        return found.energy.price[0]

    assert live(None) == pytest.approx(energy.price[0], abs=0.002)  # the valuation's
    assert live(_pool(_bought(held, NIGHT))) > energy.price[0] + 0.05  # the client's now


def test_a_dc_pv_charge_is_a_solar_lot_at_the_export_through_the_battery_port() -> None:
    """DC PV into a battery on the hybrid's DC bus: a PV lot at the export per DC kWh,
    stored through the battery's port (a split battery's own law), alike in the ledger,
    given the slot as the plant model's, and in the projection, not 1:1 whatever the
    port."""
    plant = Plant(example_plant("dc_coupled_hybrid", PACIFIC))
    storage = plant.storage
    assert storage is not None
    battery = plant.battery
    per_dc = battery.stored_by(3.0) / 3.0  # the split battery's own law, at 3 kW
    flows = _flows(dc_pv_to_battery=3.0)
    ledger = Ledger(battery, unknown_basis=0.0, storage=storage)
    slot = ledger.step(
        EVENING,
        flows,
        1.0,
        pv_price=EXPORT,
        grid_price=NIGHT,
        stored_after=None,
        slot=plant.metered_slot(EVENING, 0.3, 0.6, flows),
    )
    assert slot.charged[Source.PV].kwh == pytest.approx(3.0 * per_dc)
    assert slot.charged[Source.PV].value == pytest.approx(3.0 * EXPORT)
    # From 3 kWh (0.3 of 10 kWh), what the charge adds.
    flow = plant.metered_slot(EVENING, 0.3, 0.3 + 3.0 * per_dc / battery.capacity_kwh, flows)
    held = Lots(EVENING, {}, Amount(3.0, 3.0 * NIGHT))
    found = project_average(storage, [flow, flow], [1.0] * 2, [NIGHT] * 2, [EXPORT] * 2, lots=held)
    assert found[0] == pytest.approx(NIGHT)
    # 3 kWh of grid energy, then 3 x per_dc stored from 3 DC kWh at the export.
    assert found[1] == pytest.approx((3.0 * NIGHT + 3.0 * EXPORT) / (3.0 + 3.0 * per_dc))


def test_a_split_battery_costs_the_grid_and_dc_pv_on_their_own_paths() -> None:
    """On a split DC-bus battery the grid's AC crosses the hybrid's conversion and then
    the battery's port; DC PV only the port. The stored cost books each by its path
    (``Storage.stored_per_kwh``), not by the battery's law alone."""
    plant = Plant(example_plant("dc_coupled_hybrid", PACIFIC))
    storage = plant.storage
    assert storage is not None
    battery = plant.battery
    port_kw = 5.0  # the battery's charge limit at its port
    hybrid_ac = port_kw / 0.97 + 0.002 * port_kw**2  # the hybrid's conversion, AC in
    assert storage.stored_per_kwh(ChargeSource.DC_PV) == pytest.approx(
        battery.stored_by(port_kw) / port_kw
    )
    assert storage.stored_per_kwh(ChargeSource.GRID) == pytest.approx(
        battery.stored_by(port_kw) / hybrid_ac
    )
    # The battery's law alone would book the grid's kWh as DC PV's.
    assert storage.stored_per_kwh(ChargeSource.GRID) < storage.stored_per_kwh(ChargeSource.DC_PV)


def test_the_projection_runs_the_pool_as_the_ledger_steps_it() -> None:
    """An oracle: AC charging from PV and the grid, then a discharge, through both."""
    capacity = BATTERY.capacity_kwh
    flows = [(2.0, 1.0, 0.0), (0.0, 0.0, 2.5), (1.0, 0.0, 0.0)]  # PV in, grid in, out
    starts = [EVENING + i * SLOT for i in range(3)]
    ledger = Ledger(BATTERY, unknown_kwh=3.0, unknown_basis=0.2)
    stored = [3.0, 5.8, 3.2, 4.1]  # the meter's, after each slot: both scale to it
    expected = []
    for i, (start, (pv_kw, grid_kw, out_kw)) in enumerate(zip(starts, flows, strict=True)):
        expected.append(ledger.pool.unit)
        ledger.step(
            start,
            _flows(pv_to_battery=pv_kw, grid_to_battery=grid_kw, battery_to_load=out_kw),
            1.0,
            pv_price=EXPORT,
            grid_price=NIGHT,
            stored_after=stored[i + 1],
        )
    projected = [
        SlotFlow(
            start=start,
            pv_to_load=0.0,
            pv_to_battery=pv_kw,
            pv_export=0.0,
            pv_curtail=0.0,
            battery_to_load=out_kw,
            battery_export=0.0,
            grid_to_load=0.0,
            grid_to_battery=grid_kw,
            soc_start=stored[i] / capacity,
            soc_end=stored[i + 1] / capacity,
        )
        for i, (start, (pv_kw, grid_kw, out_kw)) in enumerate(zip(starts, flows, strict=True))
    ]
    found = project_average(
        storage_of(PLANT),
        projected,
        [1.0] * 3,
        [NIGHT] * 3,
        [EXPORT] * 3,
        lots=Lots(EVENING, {}, Amount(3.0, 0.6)),
    )
    assert found == pytest.approx(expected)
