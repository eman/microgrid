# Build a dataset

A dataset ([`dataset/1`](../reference/dataset.md)) is how a plant's data reaches the
`microgrid` command. Here four CSV files become one. They stand for whatever you have:
exports from a monitoring portal, a recorder's history, a tariff's published rates.

## What you need

To value a moment: the prices, a PV forecast (a plant with PV), a load forecast, and the
SOC and [dispatch as modelled](../concepts/inputs.md#dispatch-as-modelled) (a plant with
a battery). The commands that score against the meters need more;
[Inputs](../concepts/inputs.md#which-command-needs-which) lists what each needs.

## 1. The CSV files

Times carry their offset; this plant is in Europe/Madrid, at +02:00 in June. Prices are
hourly, the PV forecast and the load forecast half-hourly, for two days from midnight.

`csv/prices.csv`:

```text
start,end,import,export
2026-06-10T00:00:00+02:00,2026-06-10T01:00:00+02:00,0.12,0.05
2026-06-10T01:00:00+02:00,2026-06-10T02:00:00+02:00,0.12,0.05
...
```

`csv/pv.csv`, the forecast's quantiles in kW:

```text
start,end,p10,p50,p90
2026-06-10T14:00:00+02:00,2026-06-10T14:30:00+02:00,1.997,3.994,4.593
2026-06-10T14:30:00+02:00,2026-06-10T15:00:00+02:00,1.972,3.943,4.535
...
```

`csv/load.csv`, the load forecast in kW, and `csv/soc.csv`, the SOC as it was recorded,
in percent:

```text
start,end,kw
2026-06-10T00:00:00+02:00,2026-06-10T00:30:00+02:00,0.4
...
```
```text
time,percent
2026-06-10T06:00:00+02:00,35
2026-06-10T06:45:00+02:00,34
2026-06-10T07:30:00+02:00,36
```

## 2. Write the dataset

`make_dataset.py` reads them and writes the dataset with the library's own types and
writer (`microgrid.cli.dataset.write_dataset`):

```python
"""CSV files to a dataset/1 directory: one issue of each forecast, and the SOC."""

import csv
from datetime import UTC, datetime
from pathlib import Path

from microgrid.cli.dataset import Entry, write_dataset
from microgrid.engine.series import Forecast, Interval, Price, Quantiles, Sample, Series


def utc(text: str) -> datetime:
    return datetime.fromisoformat(text).astimezone(UTC)


def rows(name: str) -> list[dict[str, str]]:
    with Path("csv", name).open(newline="") as f:
        return list(csv.DictReader(f))


def one_issue(intervals, *, received: datetime, source: str, unit: str) -> Series:
    """A series of forecasts holding one issue, received (and issued) at ``received``."""
    issue = Forecast(tuple(intervals), issued_at=received, source=source)
    return Series((Sample(received, issue),), known_until=received, unit=unit)


received = utc("2026-06-10T07:30:00+02:00")

prices = one_issue(
    (
        Interval(utc(r["start"]), utc(r["end"]), Price(float(r["import"]), float(r["export"])))
        for r in rows("prices.csv")
    ),
    received=received, source="my-tariff", unit="EUR/kWh",
)
pv = one_issue(
    (
        Interval(utc(r["start"]), utc(r["end"]),
                 Quantiles({0.1: float(r["p10"]), 0.5: float(r["p50"]), 0.9: float(r["p90"])}))
        for r in rows("pv.csv")
    ),
    received=received, source="my-pv-forecast", unit="kW",
)
# A load forecast is stale once its first interval is too old: keep what is still ahead.
load = one_issue(
    (
        Interval(utc(r["start"]), utc(r["end"]), float(r["kw"]))
        for r in rows("load.csv")
        if utc(r["end"]) > received
    ),
    received=received, source="my-load-forecast", unit="kW",
)
soc = Series(
    tuple(Sample(utc(r["time"]), float(r["percent"])) for r in rows("soc.csv")),
    known_until=received,
    unit="%",
)
# The plant runs its own dispatch, as its model describes, throughout.
dispatch = Series((Sample(utc("2026-06-10T00:00:00+02:00"), True),), known_until=received)

write_dataset(
    Path("my-dataset"),
    {
        "prices": Entry("price", prices, forecasts=True),
        "pv_forecast": Entry("quantiles", pv, forecasts=True),
        "load_forecast": Entry("number", load, forecasts=True),
        "soc": Entry("number", soc),
        "battery_first": Entry("bool", dispatch),
    },
)
```

Run it where the library is installed (`uv run python make_dataset.py` from a
checkout). It writes `my-dataset/` with its
`manifest.json`, `series/` and `forecasts/`.

Three things to notice:

- **Every time becomes UTC** at the edge (`utc`). The files keep the offset; the engine
  never sees local time.
- **Units are stated, not converted.** The SOC stays in percent and the prices in
  EUR/kWh; reading the dataset converts them, and refuses a unit it does not know.
- **Each forecast is an issue received at a time.** Here every forecast was received at
  07:30. A real record would hold every issue as it arrived, so a replay sees what was
  known at each moment.

## 3. Add the plant

Put the plant's file in the dataset as `plant.toml` ([Describe a plant](describe-plant.md)). This one has AC-coupled PV and a 10 kWh battery on its own
inverter:

```toml
schema = "plant/2"
timezone = "Europe/Madrid"

[[pv]]
name = "roof"
coupling = "ac"

[battery]
capacity_kwh = 10.0
charge_kw = 5.0
discharge_kw = 5.0
eta_charge = 0.95
eta_discharge = 0.95
reserve_soc = 0.1
wear_charge_per_kwh = 0.02
wear_discharge_per_kwh = 0.02
connection = "ac"

[battery.inverter]
ac_rating_kw = 5.0
```

```sh
uv run microgrid config check --data my-dataset
```
```text
ok: the plant (10.0 kWh battery) and the engine
```

## 4. Value it

```sh
uv run microgrid value --data my-dataset --at 2026-06-10T07:30 --slots 4
```
```text
valued at 2026-06-10 07:30 CEST: stored_value_v1, 243 slots, degraded: battery_lots
slot                     import   export  marginal  surplus kW  regime
2026-06-10 07:30 CEST    0.2000   0.0500    0.0595       0.000  battery
2026-06-10 07:40 CEST    0.2000   0.0500    0.0595       0.000  battery
2026-06-10 07:50 CEST    0.2000   0.0500    0.0595       0.000  battery
2026-06-10 08:00 CEST    0.2000   0.0500    0.0500       0.000  battery
```

A kWh now costs about the export price: on most of the forecast's days PV is already
charging the battery at 07:30, and the sun will refill whatever the battery gives
before it needs the grid. `degraded` names `battery_lots`: the dataset has no meters,
so the command cannot run the battery's ledger, and what the battery holds is priced as
bought at the import. The valuation stands (`stored_value_v1`); add the meters to give
it the lots ([The battery's lots](../concepts/inputs.md#the-batterys-lots)). The horizon is 243 slots, not 288: the prices end at midnight on
the second day.

## 5. What `known_until` is for

Value half an hour later:

```sh
uv run microgrid value --data my-dataset --at 2026-06-10T08:00 --slots 1
```
```text
valued at 2026-06-10 08:00 CEST: fallback_import, 240 slots, degraded: battery_soc, work_mode
...
```

The SOC and dispatch as modelled are vouched for only up to 07:30 (`known_until`). At 08:00 the
engine cannot tell whether the SOC changed, so it will not use it: the valuation falls
back to the import price and names why ([Missing and stale
inputs](../methodology/degradation.md)). A record that runs on would set `known_until`
to its end.

## Entries you may not have

- **Dispatch as modelled** (`battery_first`). If the inverter only ever runs a mode the
  plant model describes, write a constant `true`, as above. If it switches modes, record
  the mode and map each to true or false ([what counts](../concepts/inputs.md#dispatch-as-modelled)).
- **The house load** (`house`). Many inverters report home consumption; record it. If
  the inverter does not, compute it from the meters, reading by reading, as below (it then
  includes the inverters' own draw). `live`, `check-plant` and `fit-battery` read it.
- **A heartbeat** (`heartbeat`), optional: it tells a quiet meter from a dead
  connection ([Heartbeat](../concepts/inputs.md#heartbeat)). Without one, each series is
  trusted as far as its `known_until`, a gap in it a `null` sample. If the recorder's
  cadence is not a few seconds, state how long a silence it can have
  ([`limits`](../reference/dataset.md#limits)).
- **The recorded prices** (`import_price`, `export_price`), optional: the prices in
  force over time, for `replay`, `ledger` and `check-plant`, read as numbers per kWh in
  the prices' currency, each slot at the mean of the price in force over it. Without
  them the commands take the prices the `prices` forecasts put in force, as received:
  give them only where what was in force differs from what was forecast.

```python
from datetime import UTC, datetime, timedelta

from microgrid.cli.dataset import Entry
from microgrid.engine.series import Sample, Series

start = datetime(2026, 6, 10, 0, 0, tzinfo=UTC)
end = start + timedelta(days=1)

# A heartbeat: one sample a minute, for a record you know has no gaps.
beats = [start + timedelta(minutes=k) for k in range(24 * 60)]
heartbeat = Series(tuple(Sample(t, 1.0) for t in beats), known_until=end)

# The house load from one reading of the meters, in their default signs
# (grid > 0 importing, battery > 0 charging): PV + grid - battery.
pv_kw, grid_kw, battery_kw = 2.0, -0.5, 1.0     # exporting 0.5 kW, charging 1 kW
house = Series((Sample(start, pv_kw + grid_kw - battery_kw),), known_until=end, unit="kW")

# The prices in force: one sample at each interval's start, per kWh.
hours = [start + timedelta(hours=h) for h in range(24)]
import_price = Series(tuple(Sample(t, 0.12 if t.hour < 7 else 0.20) for t in hours),
                      known_until=end)
export_price = Series(tuple(Sample(t, 0.05) for t in hours), known_until=end)

extra = {
    "heartbeat": Entry("number", heartbeat),
    "house": Entry("number", house),
    "import_price": Entry("number", import_price),
    "export_price": Entry("number", export_price),
}
print(house.samples[0].value)     # 0.5 kW
```

Add `extra`'s entries to the mapping you pass to `write_dataset`.

## A fuller dataset

`scripts/make_example_dataset.py` in the repository builds the example dataset: a week,
with each forecast's issues as they arrived (the prices and the load hourly, the PV every
six hours) and every meter the scoring commands read.
