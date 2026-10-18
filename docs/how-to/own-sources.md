# Get prices from your own sources

`microgrid now` values your plant at this moment, reading each input from the provider
you name in a [`sources/1`](../configuration/data-sources.md) file. You need the
`sources` extra ([Install](../install.md#the-sources-extra)), a `plant/2` file for your
plant ([Describe your plant](describe-plant.md)), and a source for each input.

## 1. Name a source for each input

Put `sources.toml` beside your `plant.toml`. Each input names its provider: a tool's
integration in Home Assistant, or any entity whose attribute holds the forecast. This
one reads prices from an entity, the PV forecast from Solcast's integration and the
load forecast from EMHASS:

```toml
schema = "sources/1"
plant = "plant.toml"

[home_assistant]
url = "http://homeassistant.local:8123"

[prices]
from = "entity"
entity = "sensor.electricity_rates"
attribute = "rates"
keys = { import = "buy", export = "sell" }
unit = "EUR/kWh"

[pv_forecast]
from = "solcast"
entities = [
    "sensor.solcast_pv_forecast_forecast_today",
    "sensor.solcast_pv_forecast_forecast_tomorrow",
    "sensor.solcast_pv_forecast_forecast_day_3",
]
last_polled = "sensor.solcast_pv_forecast_api_last_polled"

[load_forecast]
from = "emhass"
```

Use your own entity names: none is assumed. Put a long-lived access token in the
environment, never in the file:

```sh
export MICROGRID_HA_TOKEN=...      # or the variable [home_assistant] token_env names
```

## 2. Check what it reads

```sh
uv run microgrid sources --sources sources.toml
```
```text
sources.toml: the plant plant.toml, the engine defaults
  yes  load_forecast  emhass
  yes  prices         entity
  yes  pv_forecast    solcast
  no   soc            no source configured
```

One line per input: `yes` and its provider, or `no` and why. This plant has a battery,
so it needs the SOC too, which no forecast gives: that takes a `[measured]` section.

## 3. Add what is measured

The SOC, and whether the plant runs [as modelled](../concepts/inputs.md#dispatch-as-modelled),
come from the plant's monitoring. `sources/1` reads them from Home Assistant:

```toml
[measured]
from = "home_assistant"
soc = "sensor.battery_state_of_charge"
dispatch_as_modelled = true
```

`dispatch_as_modelled = true` says the plant always runs its own dispatch. If it
switches modes, name the mode entity instead:

```toml
dispatch = "select.inverter_mode"
modelled_modes = ["Self consumption"]
```

## 4. Swap a provider

Each input takes whichever provider you have: prices from TariffKit's integration
(`from = "tariffkit"`) or an entity; the PV forecast from Solcast's integration or an
entity; the load forecast from EMHASS, Predbat or an entity. [Configure a
source](configure-source.md) covers each one.

## 5. Value now

```sh
uv run microgrid now --sources sources.toml --slots 3
```

With step 1 alone, for a plant with a battery:

```text
not read: soc: no source configured
valued at 2026-10-17 21:17 CEST: fallback_import, 288 slots, degraded: battery_soc, work_mode
slot                     import   export  marginal  surplus kW  regime
2026-10-17 21:10 CEST    0.3000   0.0700    0.3000       0.000  import
2026-10-17 21:20 CEST    0.3000   0.0700    0.3000       0.000  import
2026-10-17 21:30 CEST    0.3000   0.0700    0.3000       0.000  import
```

The input that could not be read is named on standard error, and the valuation falls
back to the import price ([`fallback_import`](../reference/causes.md#kinds)), naming
its causes (`battery_soc`, and `work_mode` for [dispatch as
modelled](../concepts/inputs.md#dispatch-as-modelled)) rather than guessing. With
`[measured]` in place, the same command gives the model's valuation (`stored_value_v1`), printed as
`value` prints one ([Run your first valuation](first-valuation.md#4-read-the-result)).

To keep what was read, add `--save DIR`: it writes the inputs as a
[dataset](../reference/dataset.md) with the plant's
and engine's files. Re-run it with `microgrid value --data DIR --at T`, where `T` is
the time of the read (the prices entry's `received_at`).

## Next

- For the scoring commands, `microgrid fetch` writes what was measured over a span as a
  dataset ([the commands](../configuration/data-sources.md#the-commands)).
- To publish prices continuously: [Run it continuously](run-continuously.md).
