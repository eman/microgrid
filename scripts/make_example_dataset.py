"""Make the example dataset (``examples/dataset``).

One synthetic week, 2026-10-21 to 10-28 in Europe/Berlin (Europe's DST change on 10-25
inside it), of the library's example AC-coupled hybrid (``plant.examples``,
``ac_coupled_hybrid``): its meters as the plant model runs it on the week's PV and load,
and the forecasts a client would have received. A new user runs every command on it
without a home; the CLI's end-to-end test runs on it offline.

Deterministic: the same files every run (a seeded generator, no clock), so the test can
check the committed dataset is this script's.

    uv run python scripts/make_example_dataset.py [--out DIR]
"""

from __future__ import annotations

import argparse
import math
import random
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from microgrid.cli.dataset import (
    BATTERY_FIRST,
    EXPORT_PRICE,
    HEARTBEAT,
    HOUSE,
    IMPORT_PRICE,
    INVERTER,
    LOAD_FORECAST,
    PRICES,
    PV_FORECAST,
    SOC,
    Entry,
    write_dataset,
)
from microgrid.cli.offline import ENGINE_FILE, PLANT_FILE
from microgrid.cli.toml_text import dumps
from microgrid.engine.series import (
    SLOT,
    Forecast,
    Interval,
    Price,
    Quantiles,
    Sample,
    Series,
    slot_starts,
)
from microgrid.plant import Plant, examples

TZ = ZoneInfo("Europe/Berlin")
FIRST, DAYS = datetime(2026, 10, 21).date(), 7
PLANT_NAME = "ac_coupled_hybrid"
#: The array's peak, kW, on a clear October day at this latitude.
PEAK_KW = 4.0
#: Each day's sky: the share of a clear day's PV it makes.
SKY = (0.95, 0.4, 0.75, 0.9, 0.2, 0.6, 0.85)
HALF = timedelta(minutes=30)
HOUR = timedelta(hours=1)
OUT = Path(__file__).resolve().parents[1] / "examples" / "dataset"


def _local(day_offset: int, hours: float = 0.0) -> datetime:
    day = FIRST + timedelta(days=day_offset)
    return (datetime.combine(day, time(), TZ) + timedelta(hours=hours)).astimezone(UTC)


def _clear_kw(at: datetime) -> float:
    local = at.astimezone(TZ)
    hour = local.hour + local.minute / 60
    return PEAK_KW * max(0.0, math.sin(math.pi * (hour - 7.5) / 10))


def _sky(at: datetime) -> float:
    return SKY[(at.astimezone(TZ).date() - FIRST).days % DAYS]


def _typical_load(at: datetime) -> float:
    local = at.astimezone(TZ)
    hour = local.hour + local.minute / 60
    evening = 0.9 * math.exp(-(((hour - 19.5) / 1.6) ** 2))
    morning = 0.4 * math.exp(-(((hour - 7.0) / 0.8) ** 2))
    return 0.35 + evening + morning


def _price(at: datetime) -> Price:
    hour = at.astimezone(TZ).hour
    p_import = 0.42 if 17 <= hour < 21 else 0.18 if hour < 6 else 0.30
    return Price(p_import, 0.07)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", type=Path, default=OUT)
    out = parser.parse_args().out
    rng = random.Random(20261021)
    plant = Plant(examples.example_plant(PLANT_NAME, TZ))
    start, end = _local(0), _local(DAYS)
    starts = list(slot_starts(start, end))
    # Rounded, and noise from ``random()`` alone (its sequence is fixed for a seed;
    # ``gauss``'s is not promised across versions), so the files are the same bytes on
    # any machine.
    pv = [round(_clear_kw(at) * _sky(at), 4) for at in starts]
    load = [round(max(0.15, _typical_load(at) + rng.uniform(-0.12, 0.12)), 4) for at in starts]
    flows = plant.project(starts, [SLOT] * len(starts), pv, load, 0.5)
    battery = plant.config.battery
    assert battery is not None
    part = battery.battery

    meters: dict[str, list[tuple[datetime, float]]] = {name: [] for name in plant.meters}
    soc: list[tuple[datetime, float]] = [(start, 50.0)]
    house: list[tuple[datetime, float]] = []
    inverter: list[tuple[datetime, float]] = []
    for at, flow, kw in zip(starts, flows, load, strict=True):
        # The battery's DC meter from the AC it took or gave, by the battery's own law,
        # as the dataset was first made (``flow.battery_port`` agrees to float noise).
        charging = flow.pv_to_battery + flow.grid_to_battery
        delivering = flow.battery_to_load + flow.battery_export
        dc = (
            part.stored_by(charging) / part.meter_charge
            if charging > 0
            else -part.drawn_by(delivering) / part.meter_discharge
        )
        grid = flow.grid_to_load + flow.grid_to_battery - flow.pv_export - flow.battery_export
        by_kind = {
            "pv": flow.pv_to_load + flow.pv_to_battery + flow.pv_export,
            # + 0.0: the dispatch's float noise (an export of 1e-17) rounds to -0.0.
            "grid": round(grid, 4) + 0.0,
            "battery": dc,
        }
        for name, kind in plant.meters.items():
            meters[name].append((at, round(by_kind[kind], 4)))
        house.append((at, round(kw, 4)))
        # The battery's inverter, AC, > 0 out (for fitting the battery's conversion).
        ac_out = flow.battery_to_load + flow.battery_export - charging
        inverter.append((at, round(ac_out, 4)))
        assert flow.soc_end is not None
        soc.append((at + SLOT, round(flow.soc_end * 100, 3)))

    def series(points: list[tuple[datetime, float]], unit: str | None) -> Series[float]:
        return Series.of(points, known_until=end, unit=unit)

    entries: dict[str, Entry] = {
        name: Entry("number", series(points, "kW")) for name, points in meters.items()
    }
    entries[HOUSE] = Entry("number", series(house, "kW"))
    entries[INVERTER] = Entry("number", series(inverter, "kW"))
    # The prices in force, hour by hour, as the tariff's price sensors record them.
    hours = [start + k * HOUR for k in range(int((end - start) / HOUR))]
    entries[IMPORT_PRICE] = Entry(
        "number", series([(at, _price(at).p_import) for at in hours], "USD/kWh")
    )
    entries[EXPORT_PRICE] = Entry(
        "number", series([(at, _price(at).p_export) for at in hours], "USD/kWh")
    )
    entries[SOC] = Entry("number", series([p for p in soc if p[0] <= end], "%"))
    entries[BATTERY_FIRST] = Entry("bool", Series.of([(start, True)], known_until=end))
    beats = [
        start + k * timedelta(minutes=1) for k in range(int((end - start) / timedelta(minutes=1)))
    ]
    entries[HEARTBEAT] = Entry("number", Series.of(((at, 1.0) for at in beats), known_until=end))

    # PV forecasts: four polls a day (Solcast-like), three days of half hours from the
    # local midnight, each day's sky guessed within 15 %.
    pv_issues: list[Sample[Forecast[Quantiles]]] = []
    for day in range(DAYS):
        guess = [_sky(_local(day + d, 12)) * rng.uniform(0.85, 1.15) for d in range(3)]
        for hour in (6, 10, 14, 18):
            issued = _local(day, hour)
            pv_intervals: list[Interval[Quantiles]] = []
            for k in range(3 * 48):
                at = _local(day) + k * HALF
                p50 = round(min(PEAK_KW, _clear_kw(at + HALF / 2) * guess[k // 48]), 4)
                pv_intervals.append(
                    Interval(
                        at,
                        at + HALF,
                        Quantiles(
                            {
                                0.1: round(0.6 * p50, 4),
                                0.5: p50,
                                0.9: round(min(PEAK_KW, 1.2 * p50), 4),
                            }
                        ),
                    )
                )
            pv_issues.append(
                Sample(
                    issued + timedelta(seconds=30),
                    Forecast(tuple(pv_intervals), issued, "example-pv"),
                )
            )
    entries[PV_FORECAST] = Entry("quantiles", Series(tuple(pv_issues), end, "kW"), forecasts=True)

    # Load forecasts: hourly, 48 h of half hours from the next half hour (EMHASS-like).
    load_issues: list[Sample[Forecast[float]]] = []
    at = start
    while at < end:
        issued = at + timedelta(minutes=2)
        first = at + HALF
        load_intervals = [
            Interval(
                first + k * HALF,
                first + (k + 1) * HALF,
                round(_typical_load(first + k * HALF + HALF / 2), 4),
            )
            for k in range(96)
        ]
        load_issues.append(
            Sample(
                issued + timedelta(seconds=20),
                Forecast(tuple(load_intervals), issued, "example-load"),
            )
        )
        at += HOUR
    entries[LOAD_FORECAST] = Entry("number", Series(tuple(load_issues), end, "kW"), forecasts=True)

    # Prices: fetched hourly, 49 h from the hour, stamped when fetched (TariffKit-like).
    price_issues: list[Sample[Forecast[Price]]] = []
    at = start
    while at < end:
        fetched = at + timedelta(seconds=5)
        price_intervals = [
            Interval(at + k * HOUR, at + (k + 1) * HOUR, _price(at + k * HOUR)) for k in range(49)
        ]
        price_issues.append(
            Sample(fetched, Forecast(tuple(price_intervals), fetched, "example-prices"))
        )
        at += HOUR
    entries[PRICES] = Entry("price", Series(tuple(price_issues), end, "USD/kWh"), forecasts=True)

    write_dataset(out, entries)
    plant_text = dumps(
        examples.example_mapping(PLANT_NAME, TZ),
        comment="The example dataset's plant: plant.examples' ac_coupled_hybrid (plant/2).",
    )
    (out / PLANT_FILE).write_text(plant_text, encoding="utf-8")
    engine = dumps(
        {"schema": "engine/2", "price_max_age_minutes": 60},
        comment="The example dataset's engine (engine/2): prices fetched hourly.",
    )
    (out / ENGINE_FILE).write_text(engine, encoding="utf-8")
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
