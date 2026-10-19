# Data sources

A `sources/1` file says where each input comes from, for `microgrid now`, `microgrid
sources` and `microgrid fetch` ([the commands](#the-commands)). Each forecast input
(prices, the PV forecast, the load forecast) comes from one of several
[providers](../glossary.md#provider): any entity's attribute, or a preset for a named
tool. Measured values (`[measured]`) are read from Home Assistant only;
without it, they come from a [dataset](../reference/dataset.md) you build. The readers
need the `sources` extra ([Install](../install.md#the-sources-extra)).
[Get prices from live sources](../how-to/own-sources.md) builds a file step by step.

```toml
schema = "sources/1"
plant = "plant.toml"

[home_assistant]
url = "http://homeassistant.local:8123"

[prices]
from = "tariffkit"

[pv_forecast]
from = "entity"
entity = "sensor.my_pv_forecast"
attribute = "forecast"
period_minutes = 30
keys = { value = "watts" }
unit = "W"

[load_forecast]
from = "emhass"

[measured]
from = "home_assistant"
soc = "sensor.battery_soc"
dispatch_as_modelled = true
```

## The file

| Key | Required | Meaning |
|---|---|---|
| `schema` | yes | `"sources/1"` |
| `plant` | yes | the plant's [`plant/2`](../reference/plant.md) file |
| `engine` | no | an [`engine/2`](../reference/engine.md) file; without one, the library's defaults (an `engine.toml` beside the file is not read unless named) |
| `[home_assistant]` | when a section reads Home Assistant | the connection |
| `[prices]`, `[pv_forecast]`, `[load_forecast]` | no | each input's provider |
| `[measured]` | no | what the plant measures |

- **Paths are relative to the file.**
- **An input with no section is not read.** A valuation that needs it names it as a
  [cause](../reference/causes.md) and falls back.
- **No entity has a default**, because each site names its own. The exceptions are
  tools that publish under a fixed name (EMHASS, Predbat); a section may rename those
  too.
- **Every problem is named at once**, with its path (`prices.area: is required`). A key
  the schema does not have is an error.

## `[home_assistant]`

| Key | Default | Meaning |
|---|---|---|
| `url` | required | the instance's base URL: `https`, or `http` to a host on the local network (a name without a dot, `.local`, `.lan`, `.home.arpa`, `.internal`, `.localdomain`; or a private, loopback, link-local or carrier-grade NAT address, such as Tailscale's) |
| `token_env` | `"MICROGRID_HA_TOKEN"` | the environment variable holding a long-lived access token: `HA_TOKEN` or a name ending in `_HA_TOKEN` |

**The token is never in the file.** It is read from the variable `token_env` names, so
the file can be shared or committed. Because the token is sent to `url`, a file cannot
send another variable, nor send the token in cleartext beyond the local network: a
shared file pointing elsewhere is refused. Use a token of a user of its own, not an
administrator's: the library reads states and calls the actions the configured
readers name (TariffKit's, Nord Pool's, Tibber's, or the one `from = "action"` names).
Finding a TariffKit or Nord Pool config entry is the one call that needs an
administrator; name `config_entry` to avoid it. The section is required when any section
reads Home Assistant, which every reader does.

## Units

Power is W, kW or MW. A price unit is a currency, as a code or a symbol, before `/Wh`,
`/kWh` or `/MWh` (`EUR/MWh`, `USD/kWh`, `¢/kWh`); per-Wh and per-MWh prices become per
kWh, and the currency is never converted ([Currency](../concepts/units.md#currency)).

## Any entity: `from = "entity"`

Any input can come from an entity whose attribute holds **a list of rows, each with a
start time, an end time or a fixed length, and its values**.

| Key | Default | Meaning |
|---|---|---|
| `entity` | required | the entity |
| `attribute` | required | the attribute holding the rows |
| `start_key` | `"start"` | each row's start time |
| `end_key` | `"end"` | each row's end time; leave it out and set `period_minutes` for rows of a fixed length |
| `period_minutes` | none | each row's length, where rows have no end time |
| `keys` | required | which of the row's keys holds each value: `import`, and `export` unless `export` is set, for prices; `value`, or `p10`, `p50` and `p90`, for PV; `value` for a load |
| `unit` | the entity's `unit_of_measurement` | the values' unit: power for PV and loads; a price unit for prices, where none means per kWh |
| `export` | none | prices only: a fixed export price per kWh, for rows that hold the import alone |

Times are ISO 8601 with an offset (`T` or a space between the date and the time). The issue time is the state's `last_updated`. A row
that cannot be read is an error, not skipped, since nothing is known about the provider's
faults.

**A provider that does not publish rows like these cannot be read as an entity**, for
example one whose entities hold only daily energy totals, or only the current power.
Check what the integration's entity holds (its attributes, in Home Assistant's
developer tools) before configuring it.

## Any action: `from = "action"`

Some integrations publish a forecast as an action's response. Any input can come from
such an action whose response holds **rows as an entity's attribute would**.

| Key | Default | Meaning |
|---|---|---|
| `action` | required | the action, as `domain.action` |
| `data` | `{}` | the action's fields |
| `rows` | `[]` | the keys leading from the response to the list of rows (`["forecast", "rows"]` for `{"forecast": {"rows": [...]}}`) |
| `start_key`, `end_key`, `period_minutes`, `keys`, `export` | as for an entity | the rows |
| `unit` | none | the values' unit: required for power; for prices, none means per kWh |

The response carries no time it was made, so the issue time is the time of the read.
The action is called with the same fields at every read: one whose fields name dates
needs a reader of its own.

## Presets for named tools

These read a named tool's published shape.

### Prices: `from = "tariffkit"`

TariffKit computes a utility tariff's prices; its Home Assistant integration's
`get_rates` service returns all-in import and export prices per kWh, by the hour.

| Key | Default | Meaning |
|---|---|---|
| `config_entry` | the one loaded | the integration's config entry, where more than one is loaded |

The reader asks for the horizon plus one hour from the top of the hour under way, so a
slot that starts mid-hour is still priced at the far end; the horizon may be at most
167 hours. The answer is one forecast issue, stamped when TariffKit answered
(`generated_at`). A gap between its hours is an error. TariffKit prices in US dollars,
and a dataset saved from it says `USD/kWh`.

### Prices with no export price

Most price integrations publish what a kWh costs to import and nothing for an export.
Their readers take a fixed `export` price per kWh, which is a number in the file, as the
export price for every slot. Where an integration publishes the export on its own
entities (Amber's feed-in channel, Octopus's export meter, OpenEI's sell rate), the
reader takes those instead. Where the import and export prices change at different
times, the prices are split at each change of either, and end where either ends.

### Prices: `from = "nordpool"`

Home Assistant's Nord Pool integration publishes the day-ahead market's prices. Its
`nordpool.get_prices_for_date` action answers each delivery period's price per MWh, in
the currency the integration is set to.

| Key | Default | Meaning |
|---|---|---|
| `area` | required | the price area (`SE3`, `DK1`, `FI`, ...) |
| `export` | required | the export price per kWh |
| `config_entry` | the one loaded | the integration's config entry |
| `currency` | the integration's | the currency to ask for (`EUR`, `SEK`, `NOK`, `DKK`, `PLN`) |

The reader asks for today's and tomorrow's prices (UTC dates). Tomorrow's are published
in the early afternoon, Central European time; until then the prices end with today's.
These are the market's prices, without a supplier's fees or taxes. The issue time is
the time of the read.

### Prices: `from = "amber"`

Home Assistant's Amber Electric integration publishes each channel's price under way on
one entity and the intervals after it on another (its `forecasts` attribute), in dollars
per kWh with GST.

| Key | Default | Meaning |
|---|---|---|
| `price` | required | the general channel's price entity (`sensor.<site>_general_price`) |
| `forecast` | required | the general channel's forecast entity (`sensor.<site>_general_forecast`) |
| `feed_in_price`, `feed_in_forecast` | none | the feed-in channel's entities: the export price |
| `export` | none | a fixed export price per kWh, without the feed-in entities |

The feed-in channel's price is what an exported kWh earns, so a negative one costs to
export. The issue time is the earlier `last_updated` of the entities read. A dataset
saved from it says `AUD/kWh`.

### Prices: `from = "tibber"`

Home Assistant's Tibber integration's `tibber.get_prices` action answers each home's
total price per kWh (energy and tax), by quarter hour.

| Key | Default | Meaning |
|---|---|---|
| `export` | required | the export price per kWh |
| `home` | the only one | the home's name, where the account has more than one |

The reader asks from the top of the hour under way over the horizon. Each quarter hour
lasts until the next starts. The issue time is the time of the read.

### Prices: `from = "octopus"`

The Octopus Energy integration publishes each meter's rates on event entities, one per
day (`event.octopus_energy_electricity_<serial>_<mpan>_current_day_rates`, and
`..._next_day_rates`), as half hours in pounds per kWh with VAT.

| Key | Default | Meaning |
|---|---|---|
| `entities` | required | the import meter's rates entities, today's first |
| `export_entities` | none | the export meter's rates entities (`..._export_current_day_rates`): the export price |
| `export` | none | a fixed export price per kWh, without the export entities |

Today's entity must hold rates; tomorrow's may not yet. The issue time is the earliest
`last_updated` of the entities that held rates. A dataset saved from it says `GBP/kWh`.

### Prices: `from = "entsoe"`

The ENTSO-E integration publishes the day-ahead prices on its average-price entity's
`prices` attribute, hourly or by quarter hour, in its `unit_of_measurement`. They are
the prices its own settings make: its price modifier and VAT.

| Key | Default | Meaning |
|---|---|---|
| `entity` | required | the average-price entity |
| `export` | required | the export price per kWh |

Each row lasts until the next starts. The issue time is the entity's `last_updated`.

### Prices: `from = "openei"`

The OpenEI integration publishes a utility rate plan from the U.S. Utility Rate
Database: the rate in force, its adjustment, its sell rate, and when the rate structure
next changes.

| Key | Default | Meaning |
|---|---|---|
| `current_rate` | required | the rate in force (`sensor.<plan>_current_energy_rate`) |
| `next_structure_time` | required | when the rate next changes (`sensor.<plan>_next_energy_rate_structure_time`) |
| `adjustment` | none | the plan's adjustment in force, added to the rate |
| `sell_rate` | none | the plan's sell rate: the export price |
| `export` | none | a fixed export price per kWh, without the sell rate |

**OpenEI publishes no rate past the next change, so the prices end there**: the
valuation's horizon ends at that change, a shorter valuation rather than a fallback.
With no change to come, the rate holds over the whole horizon. The change time is the
plant's local time. A change time already past means the integration's rate is out of
date, and the prices are not read. The issue time is the time of the read.

### PV: `from = "solcast"`

The Solcast integration publishes one entity per forecast day, each with a
`detailedForecast` attribute of half-hourly rows: the mean kW at P50 (`pv_estimate`),
P10 and P90.

| Key | Default | Meaning |
|---|---|---|
| `entities` | required | the daily forecast entities, today's first; today, tomorrow and day 3 cover a 48-hour horizon from any time |
| `last_polled` | required | the entity holding the integration's last successful poll |

The forecast's age is the last poll, not the entities' update times, which move at
every local midnight and restart. An entity that cannot be read is skipped and named;
two that disagree about a half hour are an error; at a gap the forecast stops.

### Loads: `from = "emhass"`

EMHASS publishes its load forecast as an entity whose attribute lists rows such as
`{"date": "2026-10-12T22:00:00-07:00", "p_load_forecast": "1511.82"}`: the mean power
over its optimisation step from `date`.

| Key | Default | Meaning |
|---|---|---|
| `entity` | `"sensor.p_load_forecast"` | the entity |
| `attribute` | `"forecasts"` | the attribute holding the rows |
| `value_key` | `"p_load_forecast"` | each row's value |
| `period_minutes` | `30` | EMHASS's optimisation step: each row's length |

The unit is the entity's `unit_of_measurement`. The issue time is the state's
`last_updated`. A row that cannot be read is skipped and logged; two rows for one time
that disagree, rows that overlap, or no usable row are an error.

### Loads: `from = "predbat"`

Predbat publishes today's predicted load as cumulative energy from local midnight, in
kWh, by time.

| Key | Default | Meaning |
|---|---|---|
| `entity` | `"predbat.load_energy_predicted"` | the entity |
| `attribute` | `"results"` | the attribute mapping times to cumulative kWh |
| `step_minutes` | `5` | Predbat's step: each value includes the step starting at its time |

The load over each step is the energy it adds, as a mean power. **Predbat publishes
today only, so the valuation's horizon ends at local midnight**: a shorter valuation,
not a fallback, with no cause named and `horizon_cut` false. The first time's own step
is not read, because the energy before it is unknown; Predbat writes from local
midnight, so that is the night's first 5 minutes and the slot under way is covered. Only
a forecast that starts mid-day loses its first 5 minutes. The issue time is the state's
`last_updated`.

### A load forecast must cover the slot under way

`now` reads one issue, received now. Some load forecasters publish from the next step
(some EMHASS setups publish rows from the next half hour), so that issue alone leaves the
[slot under way](../concepts/slots-horizon.md) uncovered, and the valuation falls back
with `load_forecast` named. The engine would take such a slot from the previous issue
([the rule for loads](../concepts/series.md#how-the-engine-reads-them-at-a-moment)),
but one read has no previous issue. A client that runs continuously and keeps every
issue it receives does ([Run it continuously](../how-to/run-continuously.md)).

## `[measured]`

What the plant measures, read from Home Assistant.

| Key | Default | Read by | Meaning |
|---|---|---|---|
| `from` | required | | `"home_assistant"` |
| `soc` | none | `now`, `fetch` | the battery's SOC |
| `soc_unit` | `"%"` | `now`, `fetch` | the SOC's unit where its entity states none; empty: a fraction |
| `dispatch` | none | `now`, `fetch` | an entity whose state is the plant's mode |
| `modelled_modes` | required with `dispatch` | `now`, `fetch` | the modes in which the plant runs the dispatch its model describes |
| `dispatch_as_modelled` | none | `now` | `true` or `false` for always, in place of `dispatch` (not both) |
| `[measured.meters]` | none | `now` (the lots), `fetch` | each of the plant's meters, by its `plant/2` name, to an entity |
| `house` | none | `now` (the lots), `fetch` | the [house load](../concepts/inputs.md#house-load) |
| `heartbeat` | the house load's entity | `now` (the lots), `fetch` | an entity that reports every few seconds ([heartbeat](../concepts/inputs.md#heartbeat)) |
| `import_price`, `export_price` | none | `now` (the lots), `fetch` | the [recorded prices](../concepts/inputs.md#recorded-prices), in a price unit |
| `inverter` | none | `fetch` | the battery inverter's AC power, positive out |

The mode entity's states are compared as text with `modelled_modes`: the plant runs as
modelled while its state is one of them ([dispatch as
modelled](../concepts/inputs.md#dispatch-as-modelled)).

## The commands

| Command | Reads | Writes |
|---|---|---|
| `microgrid sources --sources FILE` | every configured input, now | one line per input: `yes` and its provider, or `no` and why; exits 1 if any is not read |
| `microgrid now --sources FILE [--slots N] [--save DIR] [--lots-run-in D]` | every configured input, now; for a plant with a battery, `[measured]`'s history over the two days before now (`--lots-run-in`), for the battery's lots | a valuation at the current time, as `value` prints one; with `--save`, the inputs read as a [dataset](../reference/dataset.md), with the plant's and engine's files |
| `microgrid fetch --sources FILE --from T --to T --out DIR` | `[measured]`'s history over the span | a dataset of what was measured, for `check-plant`, `ledger` and `fit-battery` |

`now` and `sources` read each forecast once, as one issue received now, and each
measured value as the value in force, from when it changed. An input that cannot be read
is reported on standard error and left out, and the valuation names its cause.

For a plant with a battery, `now` also runs the [battery's
ledger](../methodology/lots.md) over what `[measured]`'s meters, SOC and recorded prices
recorded before now, as `fetch` reads them, and hands the engine the lots it ends with:
a battery kWh is priced at what the energy it holds cost. Where it cannot (no
`[measured]` meters or recorded prices, no heartbeat or house meter, nothing recorded),
standard error says why (`not read: battery_lots: ...`) and the valuation names
`battery_lots`, everything held priced as bought at the import.

A dataset written by `now --save` re-runs with `microgrid value --data DIR --at T`, where
`T` is the time of the read: the prices entry's `received_at`. At any earlier moment
nothing had been received, so the valuation is [`unpriced`](../reference/causes.md#kinds).

`fetch` reads Home Assistant's recorded history (its changes; the recorder keeps ten days
by default), in the unit each entity states now. It needs a heartbeat or the house load's
entity: a meter that writes only changes (PV at 0 all night) would otherwise mark the
record silent. Times without an offset are the plant's local time; `--from` is included,
`--to` is not. For `check-plant`, whose `--to` is an included day, fetch from the first
day's local midnight to the midnight after the last:

```sh
uv run microgrid fetch --sources sources.toml --from 2026-10-01T00:00 --to 2026-10-08T00:00 --out record
uv run microgrid check-plant --data record --from 2026-10-01 --to 2026-10-07
```

`check-plant`'s hourly 48-hour runs stop at the data's end, so the last day's runs are
shorter.

## Not supported yet

- **Measured values from anything but Home Assistant.** `[measured]` and `fetch` read
  Home Assistant only. Without it, write the measured series into a
  [dataset](../how-to/build-dataset.md) directly.
- **A recorder other than Home Assistant's for `fetch`**, such as InfluxDB.
- **Forecast history.** `fetch` writes what was measured, not forecasts: Home Assistant
  does not keep what a forecaster published in a form that says when it was known. A
  dataset for `value`, `live` or `replay` needs its forecasts recorded as they were
  received. `now --save` writes the inputs of one moment, and a second save to the same
  directory replaces the first; keeping the issues over time is a continuous client's
  job ([Run it continuously](../how-to/run-continuously.md)).
- **PV forecasts per array.** A plant with arrays of both couplings takes one forecast
  per array, which `sources/1` does not read yet; `now` says so. Use a dataset with
  `pv_forecast.<array>` entries.
