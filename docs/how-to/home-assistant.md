# Run it in Home Assistant

The Home Assistant integration (`custom_components/microgrid`) runs the library inside
Home Assistant: it reads the forecasts and the meters from Home Assistant's own
entities, values them, and publishes the valuation and the live value as entities and
through an action. It runs the same engine, the same readers and the same ledger as the
command, configured from Home Assistant's interface.

## Install

With HACS: add `https://github.com/eman/microgrid` as a custom repository (category
*Integration*), install *Microgrid*, and restart Home Assistant. Each release attaches
the integration as `microgrid.zip`; its manifest installs the same version of the
library from PyPI. It needs Home Assistant 2026.10 or later, and the recorder for the
battery's lots.

Then *Settings → Devices & services → Add integration → Microgrid*.

## Configure

The setup asks, in order:

1. **The plant.** Paste a [`plant/2`](../reference/plant.md) file, or describe the
   common shapes step by step: the PV's coupling, the battery and its inverter, a load on
   a hybrid's DC bus, the grid's limits. A file without `timezone` takes Home
   Assistant's. Grid-charge and export windows, hold rules and meters with another sign
   are set in the file ([Describe a plant](describe-plant.md)); the options edit it
   later.
2. **What the plant measures.** An entity for each of the plant's meters (by the
   plant's meter names, each in the sign the plant file states), the battery's SOC, the
   house load, and the plant's mode: an entity and the modes that run the dispatch the
   model describes, or *the plant always runs as modelled*. An SOC entity that states
   no unit is read in the unit chosen here (percent or a fraction). A heartbeat entity,
   one that reports every few seconds, is optional; without it the house load stands
   for one.
3. **Prices**, from one of the [price readers](../configuration/data-sources.md):
   TariffKit, Nord Pool, Amber Electric, Tibber, Octopus Energy, ENTSO-E, OpenEI, or
   any entity's attribute or action's response that holds rows of prices. A source that
   publishes no export price takes a fixed `export` price per kWh.
4. **The PV forecast** (for a plant with PV): Solcast, or an entity's or action's rows.
5. **The load forecast**: EMHASS, Predbat, or an entity's or action's rows.

Each section is checked by the library as the command checks a `sources/1` file, every
problem named, and each source is read once before its step is accepted. The options
edit each part and the engine's settings ([`engine/2`](../reference/engine.md): the
horizon, the slot, the inputs' longest ages, the live smoothing, how a battery kWh is
priced).

## The entities

One device per entry, with:

| Entity | State | Attributes |
|---|---|---|
| *Valuation* | the published valuation's kind: `stored_value_v1`, `fallback_import`; unavailable once past its `valid_until` | `issued_at`, `valid_until`, `reason`, `degraded`, `inputs`, `horizon_end`, `slots` (each slot's `start`, `marginal_cost`, `marginal_cost_p10`, `surplus_kw`, `surplus_kw_p10`, `p_import`, `p_export`, `regime`), the sources read and the inputs not read |
| *Marginal cost* | the live value's price of one more kWh now, per kWh | `kind`, the certainties, `degraded` |
| *Surplus* | the live value's surplus now, kW | as above |
| *Regime* | what one more kWh displaces now: `export`, `curtail`, `battery`, `import` | as above |
| *PV value*, *Battery value* | a kWh by source now, per kWh | |
| *Import price*, *Export price* | the prices in force now, from the prices read | |
| *Battery floor* | what the battery's next kWh drawn cost (its lots) | the lots by source, their average cost |
| *Valuation problem* | on while the valuation falls back, is missing, or has lapsed | `degraded`, the inputs not read |

The slots are not recorded (`_unrecorded_attributes`), only the state and the other
attributes. The prices' unit is Home Assistant's currency per kWh; the library never
converts a currency, so it should be the prices' own.

## The action

`microgrid.get_valuation` returns the published valuation as
[`valuation/2`](../reference/valuation.md), with why it was published and until when it
holds, and the live value:

```yaml
action: microgrid.get_valuation
response_variable: found
```

```json
{
  "valuation": {"model_version": "pricing/1.3", "now": "2026-10-22T10:03:00+00:00",
                "kind": "stored_value_v1", "degraded": [], "slots": ["..."]},
  "reason": "forecast_basis",
  "valid_until": "2026-10-22T10:33:00+00:00",
  "live": {"at": "2026-10-22T10:05:30+00:00", "kind": "stored_value_v1", "surplus_kw": 0.4,
           "marginal_cost": 0.05, "...": "..."},
  "problems": {}
}
```

With more than one entry loaded, name one (`config_entry`). [Use the prices in a
scheduler](scheduler.md) says which output answers which question.

## How it runs

- **Forecasts** are read every 5 minutes, each read one issue received then; a source
  that failed (an integration not loaded yet, at Home Assistant's start) is read again
  the next minute. The load forecast keeps its recent issues, so a forecaster that
  publishes from the next half hour still covers the slot under way.
- **The measured entities** feed series as their states change. A state that is not a
  number (`unavailable`) is a gap. While the heartbeat is silent longer than 3 minutes
  the series end where it last reported: the SOC and the meters read as unknown, and
  the live value names `telemetry`.
- **A valuation** is made every minute and published on a new forecast basis (new
  prices, a new PV or load forecast, a change in which inputs are usable) or every 10
  minutes, and holds for 30 minutes. A valuation with no price for the slot under way is
  not published; the last one lapses at its `valid_until`.
- **The live value** runs every 30 seconds from the measured series and the published
  valuation.
- **The battery's lots** are the library's ledger, run in over the recorder's last two
  days when the integration starts (in the recorder's executor), then stepped each slot, over the plant's meters,
  the SOC, the house load and the integration's own *Import price* and *Export price*
  as recorded. On a new install those have no history yet: the valuation names
  `battery_lots` and prices what the battery holds as bought at the import until the
  recorder has them.

A source that fails six reads in a row raises a repair issue, which clears when it reads
again. The entry's diagnostics hold the configuration (entity ids redacted), what each
input rests on, the causes and the lots.

## What it does not read yet

- **A PV forecast per array**, for a plant with arrays of both couplings.
- **OpenEI past the next rate change**: the integration publishes the rate in force and
  when it changes, so the horizon ends at that change.
