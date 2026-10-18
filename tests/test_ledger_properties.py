"""The ledger conserves energy and value, and draws restricted-first, whatever the flows."""

from datetime import UTC, datetime, timedelta

import pytest
from hypothesis import given
from hypothesis import strategies as st

from microgrid.engine.extend import Flows
from microgrid.engine.ledger import Amount, Ledger, Source, run_ledger
from microgrid.plant import Battery

T0 = datetime(2026, 9, 1, 7, 0, tzinfo=UTC)
BATTERY = Battery(capacity_kwh=12.0, charge_kw=6.0, discharge_kw=6.0)
HOURS = 1 / 6

kw = st.floats(0.0, 6.0, allow_nan=False)
#: Prices may be negative (wholesale-linked exports).
price = st.floats(-0.5, 1.5, allow_nan=False)


def _flows(pv_in: float, grid_in: float, to_load: float, export: float) -> Flows:
    return Flows(0.0, 0.0, to_load, 0.0, pv_in, grid_in, 0.0, export)


slot = st.one_of(st.none(), st.builds(_flows, kw, kw, kw, kw))


def _sum(parts: list[Amount]) -> Amount:
    total = Amount()
    for part in parts:
        total = total + part
    return total


@given(
    st.lists(
        st.tuples(slot, price, price, st.one_of(st.none(), st.floats(0.0, 12.0))),
        min_size=1,
        max_size=40,
    ),
    st.floats(0.0, 8.0),
)
def test_energy_and_value_are_conserved(
    spec: list[tuple[Flows | None, float, float, float | None]], unknown_kwh: float
) -> None:
    slots = run_ledger(
        BATTERY,
        [T0 + i * timedelta(minutes=10) for i in range(len(spec))],
        [flows for flows, *_ in spec],
        HOURS,
        [pv for _, pv, _, _ in spec],
        [grid for _, _, grid, _ in spec],
        [stored for *_, stored in spec],
        unknown_kwh=unknown_kwh,
    )
    added = _sum([part for s in slots for part in s.charged.values()])
    drawn = _sum([part for s in slots for part in (*s.to_load.values(), *s.to_export.values())])
    unexplained = _sum([s.unexplained for s in slots])
    held = _sum(list(slots[-1].held.values()))
    assert unknown_kwh + added.kwh + unexplained.kwh == pytest.approx(
        drawn.kwh + held.kwh, abs=1e-6
    )
    assert added.value + unexplained.value == pytest.approx(drawn.value + held.value, abs=1e-6)
    for s in slots:
        assert all(lot.kwh >= -1e-9 for lot in s.held.values())
        assert set(s.held) == set(Source)


@given(st.lists(st.tuples(kw, kw, kw, price, price), min_size=1, max_size=30))
def test_without_the_meter_nothing_is_unexplained(
    spec: list[tuple[float, float, float, float, float]],
) -> None:
    """Not a residual: with no metered SOC and draws the lots can cover, the lots move
    only by what was charged and drawn."""
    ledger = Ledger(BATTERY)
    added, drawn = Amount(), Amount()
    for pv_in, grid_in, out, pv_price, grid_price in spec:
        held = sum(lot.kwh for lot in ledger.held.values())
        stored_in = (pv_in + grid_in) * HOURS * BATTERY.eta_charge
        # Deliver at most what the lots hold after charging.
        deliver = min(out, (held + stored_in) * BATTERY.eta_discharge / HOURS * 0.999)
        result = ledger.step(
            T0,
            _flows(pv_in, grid_in, deliver, 0.0),
            HOURS,
            pv_price=pv_price,
            grid_price=grid_price,
            stored_after=None,
        )
        assert result.unexplained == Amount()
        added = _sum([added, *result.charged.values()])
        drawn = _sum([drawn, *result.to_load.values()])
    held_total = _sum(list(ledger.held.values()))
    assert added.kwh == pytest.approx(drawn.kwh + held_total.kwh, abs=1e-6)
    assert added.value == pytest.approx(drawn.value + held_total.value, abs=1e-6)


@given(kw, kw, st.floats(0.01, 6.0))
def test_the_house_never_takes_pv_while_grid_lots_could_serve_it(
    pv_in: float, grid_in: float, out: float
) -> None:
    """Restricted-first: grid energy can serve only the house, so it goes first."""
    ledger = Ledger(BATTERY)
    ledger.step(
        T0, _flows(pv_in, grid_in, 0.0, 0.0), HOURS, pv_price=0.1, grid_price=0.4, stored_after=None
    )
    grid_held = ledger.held[Source.GRID].kwh
    result = ledger.step(
        T0, _flows(0.0, 0.0, out, 0.0), HOURS, pv_price=0.1, grid_price=0.4, stored_after=None
    )
    need = out * HOURS / BATTERY.eta_discharge
    if need <= grid_held:
        assert Source.PV not in result.to_load
