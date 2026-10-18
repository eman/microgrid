"""The recorded history the CLI's ledger, replay and ground truth read: prices by the slot,
any slot, never read ahead; a dataset without a heartbeat, or without recorded prices."""

import asyncio
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from microgrid.cli.dataset import Dataset, DatasetHistory, Entry
from microgrid.cli.history import Bindings, read_recorded
from microgrid.cli.inputs import no_dc_forecast, prices_in_force
from microgrid.cli.offline import with_recorded_prices
from microgrid.engine.series import (
    Forecast,
    Interval,
    Price,
    Series,
    align,
    horizon_slots,
    value_at,
)
from microgrid.plant import Plant
from microgrid.plant.examples import example_plant

SYDNEY = ZoneInfo("Australia/Sydney")
T0 = datetime(2026, 10, 22, 10, 0, tzinfo=UTC)
END = T0 + timedelta(hours=1)
QUARTER = timedelta(minutes=15)
TEN = timedelta(minutes=10)
PLANT = Plant(example_plant("ac_coupled_hybrid", SYDNEY))


def _flat(value: float, start: datetime = T0 - timedelta(hours=1)) -> Series[float]:
    return Series.of([(start, value)], known_until=END, unit="kW")


def _dataset(*, heartbeat: bool = True, prices: bool = True) -> Dataset:
    # 15-minute import prices, recorded as each takes effect; a flat export.
    quarters = Series.of([(T0 + k * QUARTER, 0.1 * (k + 1)) for k in range(4)], known_until=END)
    entries = {
        "pv": Entry("number", _flat(2.0)),
        "grid": Entry("number", _flat(-0.5)),
        "battery": Entry("number", _flat(1.0)),
        "soc": Entry(
            "number", Series.of([(T0 - timedelta(hours=1), 50.0)], known_until=END, unit="%")
        ),
    }
    if prices:
        entries["import_price"] = Entry("number", quarters)
        entries["export_price"] = Entry("number", Series.of([(T0, 0.05)], known_until=END))
    if heartbeat:
        beats = [T0 - timedelta(minutes=1) + k * timedelta(minutes=1) for k in range(61)]
        entries["heartbeat"] = Entry(
            "number", Series.of(((t, 1.0) for t in beats), known_until=END)
        )
    return Dataset(entries)


def _bindings(heartbeat: bool) -> Bindings:
    def entity(name: str) -> str:
        return f"dataset.{name}"

    return Bindings(
        plant=PLANT,
        pv=entity("pv"),
        grid=entity("grid"),
        battery=entity("battery"),
        soc=entity("soc"),
        soc_unit="%",
        house=entity("house"),
        heartbeat=entity("heartbeat") if heartbeat else None,
        telemetry=timedelta(minutes=3),
        import_price=entity("import_price"),
        export_price=entity("export_price"),
    )


def _imports(step: timedelta, *, heartbeat: bool = True) -> tuple[float, ...]:
    history = DatasetHistory(_dataset(heartbeat=heartbeat))
    recorded = asyncio.run(read_recorded(history, _bindings(heartbeat), T0, END, step=step))
    assert all(flow is not None for flow in recorded.flows)
    return recorded.p_import


def test_15_minute_prices_on_15_minute_slots_are_each_slots_own() -> None:
    assert _imports(QUARTER) == pytest.approx((0.1, 0.2, 0.3, 0.4))


def test_15_minute_prices_on_10_minute_slots_are_never_read_ahead() -> None:
    """A slot a price change falls in takes each price for the time it was in force: the
    hourly reading took the later one for the whole hour from its start."""
    assert _imports(TEN) == pytest.approx((0.1, 0.15, 0.2, 0.3, 0.35, 0.4))


def test_a_record_without_a_heartbeat_is_trusted_to_its_known_until() -> None:
    assert _imports(TEN, heartbeat=False) == _imports(TEN)


def test_prices_without_a_record_are_those_the_forecasts_put_in_force() -> None:
    """F8: a dataset need carry its prices once. Each moment takes the issue received last
    that covers it, from its receipt on: never one received later."""
    early = Forecast((Interval(T0, END, Price(0.30, 0.05)),), T0 - timedelta(minutes=5), "tariff")
    late = Forecast((Interval(T0, END, Price(0.40, 0.06)),), T0 + timedelta(minutes=20), "tariff")
    issues = Series.of(
        [(T0 - timedelta(minutes=5), early), (T0 + timedelta(minutes=20), late)],
        known_until=END,
        unit="USD/kWh",
    )
    imported, exported = prices_in_force(issues)
    assert value_at(imported, T0 + timedelta(minutes=19)) == 0.30
    assert value_at(imported, T0 + timedelta(minutes=20)) == 0.40
    assert value_at(exported, T0 + timedelta(minutes=30)) == 0.06
    entries = dict(_dataset(prices=False).entries)
    entries["prices"] = Entry("price", issues, forecasts=True)
    derived = with_recorded_prices(Dataset(entries))
    assert {"import_price", "export_price"} <= set(derived.entries)


def test_the_made_up_dc_forecast_holds_any_slot() -> None:
    """F3: one interval from the slot under way, so 10- and 15-minute slots both align."""
    now = T0 + timedelta(minutes=7)
    for step in (TEN, QUARTER, timedelta(minutes=5)):
        slots = horizon_slots(now, timedelta(hours=48), step)
        forecast = no_dc_forecast(now, timedelta(hours=48), step)
        assert align(forecast, slots, step) == (0.0,) * len(slots)
