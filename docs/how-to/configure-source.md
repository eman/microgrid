# Configure a source

One section of `sources.toml` per input, each naming its provider with `from`. Every key
is in the [Data sources reference](../configuration/data-sources.md). Sections that read
Home Assistant need `[home_assistant]` and a token in the environment
([Get prices from your own sources](own-sources.md#1-name-a-source-for-each-input)).
Check each with `microgrid sources --sources sources.toml`.

## Any entity

An entity can feed any input if one of its attributes holds **a list of rows, each with
a start time, an end time or a fixed length, and its values**. Say
`sensor.dynamic_tariff` holds prices in an attribute `rates`, with rows like
`{"from": "2026-10-22T11:00:00+01:00", "to": "2026-10-22T12:00:00+01:00", "buy": 250,
"sell": 80}`, in euros per MWh:

```toml
[prices]
from = "entity"
entity = "sensor.dynamic_tariff"
attribute = "rates"
start_key = "from"
end_key = "to"
keys = { import = "buy", export = "sell" }
unit = "EUR/MWh"
```

`keys` maps each value the input needs to the row's own key, and `unit` converts: that
row is 0.25 EUR/kWh to import and 0.08 to export. For rows of a fixed length with no end
time, leave `end_key` out and set `period_minutes`. A PV forecast names
`keys = { value = "..." }`, or `p10`, `p50` and `p90`; a load forecast names
`keys = { value = "..." }`, with a power `unit` unless the entity states one.

**Check the entity first.** Look at its attributes (Home Assistant's developer tools
show them). An integration whose entities hold only daily energy totals, or only the
power now, has no rows to read.

## Presets for named tools

Each reads a tool's published shape, so it needs fewer keys.

### TariffKit, for prices

```toml
[prices]
from = "tariffkit"
```

It calls the TariffKit integration's `get_rates` service for hourly import and export
prices. With more than one TariffKit entry loaded, name one:
`config_entry = "<the entry's ID>"`.

### Solcast, for the PV forecast

```toml
[pv_forecast]
from = "solcast"
entities = [
    "sensor.solcast_pv_forecast_forecast_today",
    "sensor.solcast_pv_forecast_forecast_tomorrow",
    "sensor.solcast_pv_forecast_forecast_day_3",
]
last_polled = "sensor.solcast_pv_forecast_api_last_polled"
```

List the daily forecast entities your integration created, today's first. Today,
tomorrow and day 3 cover a 48-hour horizon from any time. `last_polled` gives the
forecast's age.

### EMHASS, for the load forecast

```toml
[load_forecast]
from = "emhass"
```

EMHASS publishes under a fixed name, so the defaults usually fit. If yours publishes
under another entity or with a 15-minute optimisation step:

```toml
[load_forecast]
from = "emhass"
entity = "sensor.p_load_forecast_custom"
period_minutes = 15
```

If it publishes from the next step, one read leaves the slot under way uncovered
([why](../configuration/data-sources.md#a-load-forecast-must-cover-the-slot-under-way)).

### Predbat, for the load forecast

```toml
[load_forecast]
from = "predbat"
```

Predbat forecasts today only, so the valuation's horizon ends at local midnight: a
shorter valuation, not a fallback. Use it where a same-day horizon is enough.
