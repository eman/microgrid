# Series and forecasts

Apart from the plant's setup, every input arrives as a time series: a meter's power, the
battery's SOC, each price and forecast as it was issued (`microgrid.engine.series`).

## A series: values that hold

```python
from datetime import datetime, UTC
from microgrid.engine.series import Series

soc = Series.of(
    [
        (datetime(2026, 10, 22, 6, 0, tzinfo=UTC), 0.41),
        (datetime(2026, 10, 22, 6, 7, tzinfo=UTC), 0.42),
        (datetime(2026, 10, 22, 6, 30, tzinfo=UTC), None),   # no value from here
        (datetime(2026, 10, 22, 6, 34, tzinfo=UTC), 0.44),
    ],
    known_until=datetime(2026, 10, 22, 7, 0, tzinfo=UTC),
)                                       # an SOC as a fraction: no unit
```

- **A value holds until the next sample.** Many recorders store only changes: a value
  that does not change writes nothing, and the last value stands. At 06:20 the SOC above is 0.42.
- **So the age of a value says nothing.** A reading from an hour ago may be exactly
  right. What can fail is the [telemetry path](../glossary.md#telemetry-path) the
  samples come by.
- **`known_until` is how far the client vouches for the telemetry path:** up to then, no change
  went unreported. Past it, the series has no value. A read of a recorder's history sets
  it to the read's end; a live client sets it from its [heartbeat](inputs.md#heartbeat).
- **A `None` sample is a gap:** no value from then until the next sample. A source that
  reported no value and a stretch the client cannot vouch for read the same way. A number
  that is not finite (`nan`) reads as `None` too.

## A forecast: intervals and an issue time

```python
from microgrid.engine.series import Forecast, Interval, Price

prices = Forecast(
    (
        Interval(datetime(2026, 10, 22, 16, tzinfo=UTC), datetime(2026, 10, 22, 17, tzinfo=UTC),
                 Price(p_import=0.30, p_export=0.07)),
        Interval(datetime(2026, 10, 22, 17, tzinfo=UTC), datetime(2026, 10, 22, 18, tzinfo=UTC),
                 Price(p_import=0.42, p_export=0.07)),
    ),
    issued_at=datetime(2026, 10, 22, 10, tzinfo=UTC),
    source="my-tariff",
)
```

- **Intervals are half-open**, `[start, end)`, in time order, not overlapping.
- **A gap between intervals is unknown**, never an error; so is time past the last.
- **`issued_at`** is the source's stamp; the issue's age is judged by it.
- **`source`** is a label, for the record.

The values: `Price(p_import, p_export)` for prices, `Quantiles` for PV, a number of kW
for loads ([Inputs](inputs.md)).

## A forecast input is a series of forecasts

A forecast changes over time: a new [issue](../glossary.md#issue) every half hour,
every hour, every morning. So a forecast input is a `Series[Forecast[...]]`: each sample
is an issue, at the time the client **received** it. One issue is a series of one.

```python
from microgrid.engine.series import Sample

price_series = Series(
    (Sample(datetime(2026, 10, 22, 10, 0, 5, tzinfo=UTC), prices),),
    known_until=datetime(2026, 10, 22, 10, 0, 5, tzinfo=UTC),
    unit="USD/kWh",
)
```

A sample with no value means the source answered nothing at that time; the issue before
it stays in force until it is too old.

## How the engine reads them at a moment

`Engine.value(inputs, at=t)` reads every series as it stood at `t`. These are the
freshness rules; other pages link here.

- **Prices and PV: the issue received last** by `t`, alone. Each fetch replaces the one
  before; filling a slot the newest issue leaves uncovered from an older one would reuse
  an input the source has replaced.
- **Loads: slot by slot, the newest fresh issue that covers the slot**
  (`series.compose`). Some load forecasters publish from the next half hour; the slot
  under way then comes from the issue before.
- **Measured values (the SOC, dispatch as modelled): the value in force at `t`**, if
  `t` is within `known_until`, and when that value took effect (the input's `as_of`).
- **Age.** A forecast issued longer ago than its limit cannot be used:
  `price_max_age_minutes` (26 hours by default: a day-ahead market's daily issue, and
  two hours' grace; set less for a source fetched more often), `pv_max_age_hours` (20),
  `load_max_age_minutes` (90). A load forecast is also too old when its first interval
  starts longer ago than its limit, so a source that republishes an old prediction with
  a fresh stamp still goes stale. A PV forecast is judged by its issue time alone,
  because some PV forecasts reach back to the start of their day.
- **Coverage.** An input whose issue does not cover the slot under way cannot be used.

**Nothing received after `t` is read.** So `Engine.value(recorded_inputs, at=past_t)`
is a replay that cannot look ahead, by construction.

## Aligning onto the slots

`series.align` puts a forecast's values on the [slot](slots-horizon.md) grid:

- **An interval that holds the whole slot** gives its value: an hourly price, or a
  half-hourly forecast, over 10-minute slots.
- **Intervals that tile the slot** (each wholly inside it, together covering it) give
  their mean, weighted by length: 5-minute prices over 10-minute slots.
- **A slot not wholly covered** (before the first interval, in a gap, past the end) is
  unknown, never guessed.
- **An interval that crosses the slot's edge without holding the whole slot** (15-minute
  prices over 10-minute slots) is refused: that source needs another slot length. For
  PV or load it makes the input unusable; for prices it prices no slot, so the
  valuation is `unpriced`, naming `prices`. 15-minute prices on 15-minute (or 5-minute)
  slots work.

## The toolkit

`microgrid.engine.series` holds the operations every client needs, so none writes its
own holding, staleness or alignment: `value_at`, `means` and `signed_means`
(time-weighted means over slots), `segments` (several series swept together, cut
wherever any changes), `alive` (where a heartbeat shows the path was up),
`counter_delta` (a cumulative counter across its resets), `align`, `compose`,
`is_stale`, `prune` and `to_forecast` (a recorded sensor as a forecast's intervals).
None assumes a slot length or hourly prices: each takes the slot it works on. See the
[API reference](../reference/api/series.md).
