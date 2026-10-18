# `engine/2`

How the price is computed, as plain data: what `EngineConfig.from_mapping` reads and
`EngineConfig.to_mapping` writes. What the plant is comes apart (`plant/2`); where the
data comes from is the client's. **Every field is optional and defaults to the value
below**, so `schema = "engine/2"` alone is the engine as it runs. [Tune the
engine](../how-to/tune-engine.md) says when to change one.

```toml
schema = "engine/2"
scenarios = 10
battery_value = "stored_cost"
slot_minutes = 10
horizon_hours = 48
pv_max_age_hours = 20
load_max_age_minutes = 90
price_max_age_minutes = 1560
smoothing_seconds = 300
soc_tolerance = 0.01
load_jump_kw = 0.5

[observed]
spill_kw = 0.12
short_kw = 0.12
charge_kw = 0.12
topup_kw = 0.5
need_kw = 0.12
soc = 0.005
regulation_kw = 0.3
```

Durations are named by their unit; power is kW; the SOC is a fraction. Problems are
named with their path, all at once (`EngineConfigError`).

## Fields

| Key | Default | Rule | Meaning |
|---|---|---|---|
| `schema` | required | `"engine/2"` | |
| `scenarios` | 10 | whole, 1 to 50 | how many equal-probability PV scenarios are cut from the forecast's quantiles |
| `battery_value` | `"stored_cost"` | `"stored_cost"` or `"replacement"` | what a kWh from the battery's cycle costs: `stored_cost`, what the energy in the battery cost (its running average, run forward along each scenario), bounded by a fill ahead; `replacement`, what will make it up (the [make-up rule](../methodology/make-up-rule.md)) |
| `slot_minutes` | 10 | divides an hour | the slot a valuation prices, counted from the epoch in UTC |
| `horizon_hours` | 48 | more than 0 | how far a valuation reaches from the slot under way |
| `pv_max_age_hours` | 20 | more than 0 | a PV forecast issued longer ago cannot be used (`pv_forecast`) |
| `load_max_age_minutes` | 90 | more than 0 | a load forecast issued, or starting, longer ago cannot be used (`load_forecast`) |
| `price_max_age_minutes` | 1560 (26 hours) | more than 0 (in Python, `None`: no limit) | a price forecast issued longer ago cannot be used; set less when the price source is fetched on a schedule |
| `smoothing_seconds` | 300 | 0 or more; 0 is off | the live value's time constant for averaging measured PV and house load |
| `soc_tolerance` | 0.01 | 0 or more, under 1 | a fraction: an SOC up to this far outside 0-1 (0.01 is one percentage point) is clamped to it; further out it is a mistake and cannot be used (`battery_soc`) |
| `load_jump_kw` | 0.5 | 0 or more | a house-load change larger than this must be confirmed before the live value takes it |
| `observed` | below | a table | the noise floors for metered flows |

## `[observed]`: the meters' noise floors

Below these, a metered flow or SOC difference is not an event for the price rule: a
small export that is the meters reading at different instants, a small import that is
the inverter holding its grid connection near zero. They apply to what the meters recorded, never
to the plant model's own flows: the live value reads them to tell whether the house is
importing now, and the analysis functions take them as an argument (`microgrid replay`
and `check-plant` use the defaults). Each is 0 or more; `soc` is under 1.

**The defaults were measured on one plant's 10-minute meter means.** Yours may report
differently. To set your own, replay a period and run `check-plant`
([Tune the engine](../how-to/tune-engine.md#your-meters-are-noisier-or-cleaner)): where
the scoring shows events the plant did not have, raise that floor.

| Key | Default | Below it |
|---|---|---|
| `spill_kw` | 0.12 | PV exported or curtailed, net of what the battery and grid supplied in the same slot, is not a spill |
| `short_kw` | 0.12 | the house importing is not an import |
| `charge_kw` | 0.12 | PV charging the battery is not charging |
| `topup_kw` | 0.5 | the grid charging the battery is not a top-up |
| `need_kw` | 0.12 | the battery and grid serving the house is not a need |
| `soc` | 0.005 | full is within this of saturation, empty within this of the reserve |
| `regulation_kw` | 0.3 | an import this small, while the battery was above its reserve and could have served it, is the inverter holding its grid connection near zero, not the house importing |

## Why the defaults

- **`scenarios = 10`**: ten equal-probability PV days, the 5th to the 95th percentile.
  Each costs one plant projection, which is quick.
- **`battery_value = "stored_cost"`**: a kWh the battery delivers is the energy it
  holds, so it costs what that energy cost; what will replace it is a forecast that may
  be wrong either way ([What a stored kWh is
  worth](../methodology/stored-value.md#the-stored-cost-v)). A fill ahead bounds it, so a
  full battery is not kept full at the cost of the day's sun.
- **The forecasts' ages**: how each is judged is in [Series and
  forecasts](../concepts/series.md#how-the-engine-reads-them-at-a-moment).
  `price_max_age_minutes = 1560`: a day-ahead market publishes once a day, and two hours'
  grace for a late publication or fetch; a stale price is never reused (tariffkit#67
  R-14), and a source fetched hourly sets 60. `load_max_age_minutes = 90` was set for one plant's
  load forecaster, which publishes every half hour: three of its issues; one that
  publishes less often sets more. `pv_max_age_hours = 20`: a PV forecaster polled a few
  times a day.
- **`smoothing_seconds = 300`**: a passing cloud should not swing the live value. One
  stray 30-second reading under `load_jump_kw` moves a 300-second average by under 50 W.
- **`soc_tolerance = 0.01`**: rounding (100.4 %) is clamped; a percent passed as a
  fraction is caught.

## Versions

The configuration's schema is `engine/2` (`ENGINE_SCHEMA`). A file that says `engine/1`
is still read, as it was: the same keys, but with no `price_max_age_minutes` its prices
have no age limit, where `engine/2` defaults the limit to 26 hours. That default is why
the name went up: a file read differently is a new version. It is not the API's version: that is
`ENGINE_CONTRACT` (`engine-api/4`), which goes up with every change a client must
follow. A valuation names the price rule and plant model that made it
(`MODEL_VERSION`, `pricing/1.3`), and the outputs' schema is `valuation/2`.
