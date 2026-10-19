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

The setup proposes what Home Assistant already knows; each step shows its proposal to
keep or change. It asks, in order:

1. **The plant.** Describe it step by step: the PV's coupling, a load on a hybrid's DC
   bus, the grid's limits, and the battery as its specification gives it (its capacity,
   charge and discharge limits, reserve and inverter rating). Where the Energy
   dashboard names the battery's sensors, what they recorded over the last week is
   proposed: the capacity from the energy it took against its SOC (or the dashboard's
   own), and the most it was seen to charge and discharge, which may be under its
   limit. Its efficiencies and the inverter's draw take the library's defaults (the
   plant file sets them; `microgrid fit-battery` fits them from a plant's meters). Or
   paste a [`plant/2`](../reference/plant.md)
   file, or import a plant file and a [`sources/1`](../configuration/data-sources.md)
   file together, as the `microgrid` command reads them (its `home_assistant` section
   is not needed). A file without `timezone` takes Home Assistant's. Grid-charge and
   export windows and hold rules are optional: without them the plant runs as the
   model's default ([Describe a plant](describe-plant.md)); the options edit the file
   later.
2. **What the plant measures.** An entity for each of the plant's meters, the battery's
   SOC, the house load, and the plant's mode: an entity and the modes that run the
   dispatch the model describes, or *the plant always runs as modelled*. Where the
   Energy dashboard names power sensors for the grid, the PV and the battery, and the
   battery's SOC, they are proposed. An SOC entity that states no unit is read in the
   unit chosen here (percent or a fraction). A heartbeat entity, one that reports every
   few seconds, is optional: with one, a silent connection to the plant reads as
   unknown, not as steady values, in the valuation, the live value and the battery's
   ledger.

   **Each meter's sign is checked.** The Energy dashboard's power sensors have a
   convention of their own: its grid sensor is positive importing, as the plant's grid
   meter is, and its battery sensor positive discharging, the opposite of the plant's
   battery meter, which reads positive charging. Then what the recorder kept of the
   chosen entities over the last two days says which way each reads: PV and a DC-bus
   load are positive, the battery charging raises the SOC, and the grid closes the
   balance with the house. Where either says a meter reads the other way from the plant
   file, the setup names the meter and why, and offers to set its sign. The recorder's
   check needs a day of hours with every meter recorded; with less, it is skipped.
3. **Prices.** The price integrations installed are found and proposed first:
   TariffKit, Nord Pool (one per area), Amber Electric, Tibber, Octopus Energy,
   ENTSO-E and OpenEI. One that publishes an export price (TariffKit, Amber's feed-in
   channel, Octopus's export meter, OpenEI's sell rate) is read at once; one that does
   not asks for a fixed export price per kWh. Any entity's attribute or action's
   response that holds rows of prices can be set up by hand
   ([the readers](../configuration/data-sources.md)).
4. **The PV forecast** (for a plant with PV): Solcast is found (its daily forecasts as
   far as they are enabled: day 3 on are disabled by default, and a 48-hour horizon
   needs it), or an entity's or action's rows.
5. **The load forecast**: EMHASS (its load forecast sensor, `p_load_forecast` under any
   prefix or suffix, with its step) and Predbat are found, or an entity's or action's
   rows.

Each section is checked by the library as the command checks a `sources/1` file, every
problem named, and each source is read once before its step is accepted. A plant is set
up once: a second entry for the same grid meter is refused. The options edit each part
and the engine's settings ([`engine/2`](../reference/engine.md): the horizon, the slot,
the inputs' longest ages, the live smoothing, how a battery kWh is priced).

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
  number (`unavailable`) is a gap. While a configured heartbeat is silent longer than
  3 minutes the series end where it last reported: the SOC and the meters read as
  unknown, and the live value names `telemetry`.
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

A source that has failed every read for 30 minutes raises a repair issue, which clears
when it reads again. The entry's diagnostics hold the configuration (entity ids redacted), what each
input rests on, the causes and the lots.

## What it does not read yet

- **A PV forecast per array**, for a plant with arrays of both couplings.
- **OpenEI past the next rate change**: the integration publishes the rate in force and
  when it changes, so the horizon ends at that change.
