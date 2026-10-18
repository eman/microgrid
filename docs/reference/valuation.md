# `valuation/2`

The outputs as plain data: what `Valuation.to_mapping()` and `LiveValue.to_mapping()`
give. The library ships the JSON Schema as a data file
(`microgrid/engine/schemas/valuation-2.schema.json`); `microgrid schema` prints it, and
the library's tests check every output against it.

Values are unrounded. Prices are in the input prices' currency per kWh, whatever the
field names say ([Currency](../concepts/units.md#currency)); power in kW; weights and
certainties 0-1; times ISO 8601 with their offset; enums their string values. Each
field's meaning is defined once in [Outputs](../concepts/outputs.md).

## A valuation

| Field | Type | Meaning |
|---|---|---|
| `model_version` | string | the version of the price rule and plant model that made it (`pricing/1.3`); it changes when a change to either would change a value |
| `now` | time | the moment valued, to the whole second |
| `kind` | `stored_value_v1`, `fallback_import`, `unpriced` | how the values were made ([kinds](causes.md#kinds)) |
| `degraded` | list of causes | the inputs that could not be used ([causes](causes.md)) |
| `inputs` | cause → `{as_of, usable}` | each input: when it was known, and whether it could be used |
| `load_forecast` | string or null | the load forecast's source label |
| `slots` | list of slots | the horizon, slot by slot; empty when `unpriced` |
| `horizon_cut` | bool | a gap in the load forecast cut the horizon short; the slots before it are valued |

## A slot

| Field | Type | Meaning |
|---|---|---|
| `start` | time | the slot's start |
| `p_import`, `p_export` | number | the input prices for the slot |
| `marginal_cost` | number | **the price of one more kWh used in the slot**, expected over the PV scenarios |
| `marginal_cost_p10` | number | the same, priced on the pessimistic (p10) PV case |
| `regime` | `export`, `curtail`, `battery`, `import` | what one more kWh would displace in the median case ([Regimes and surplus](../methodology/regimes-surplus.md)) |
| `surplus_kw` | number ≥ 0 | PV the projection exports or curtails in the median case: power there is to share |
| `surplus_kw_p10` | number ≥ 0 | the same in the pessimistic case, never above `surplus_kw` |
| `certainty_spill` | 0-1 | scenario weight in which the kWh costs only PV that would be exported or curtailed |
| `certainty_covered` | 0-1 | scenario weight in which the battery covers the expensive hours ahead without an import |
| `pv_value` | object or null | a PV kWh diverted to a flexible load: `per_kwh`, `routing` (`export`, `curtail`, `battery`: where it would have gone), `weight` (the scenario weight in which PV had spare) |
| `battery_value` | object or null | a battery kWh drawn for one: `per_kwh`, `displaces` (`later_import` or `export`: what makes it up, the [make-up rule](../methodology/make-up-rule.md)'s event), `weight` |
| `supply` | list of steps | the supply stack in the median case: `source` (`spill`, `pv`, `battery`, `grid`), `kw` (null: the grid, no limit), `per_kwh`, in the order the plant serves a load |
| `unserved` | 0-1 | scenario weight in which the slot has load a stored kWh would serve but cannot (an import limit); its price is then the import as a floor |

## A live value

| Field | Type | Meaning |
|---|---|---|
| `at` | time | the moment |
| `kind` | kind | as above; `unpriced` without a price for the slot under way |
| `surplus_kw` | number ≥ 0 | PV spilling now, from the measured values |
| `regime` | regime | as above |
| `p_import`, `p_export`, `marginal_cost` | number or null | null when `unpriced` |
| `certainty_spill`, `certainty_covered` | 0-1 | as above, for the slot from now |
| `degraded` | list of causes | the engine's causes |
| `external` | list of strings | the client's own causes, kept beside the engine's |
| `pv_value`, `battery_value` | object or null | as above |

## An example

A valuation of the example dataset at 13:00 local time on 2026-10-22 (11:00 UTC, as the
JSON's times show), its first slot only. PV is charging the battery, which holds the
week's solar and the night's top-up at 0.18 (the lots from the dataset's meters):

```json
{
  "model_version": "pricing/1.3",
  "now": "2026-10-22T11:00:00+00:00",
  "kind": "stored_value_v1",
  "degraded": [],
  "inputs": {
    "prices": {"as_of": "2026-10-22T10:00:05+00:00", "usable": true},
    "pv_forecast": {"as_of": "2026-10-22T08:00:00+00:00", "usable": true},
    "battery_soc": {"as_of": "2026-10-22T11:00:00+00:00", "usable": true},
    "work_mode": {"as_of": "2026-10-20T22:00:00+00:00", "usable": true},
    "load_forecast": {"as_of": "2026-10-22T10:02:00+00:00", "usable": true},
    "plant_model": {"as_of": "2026-10-22T11:00:00+00:00", "usable": true}
  },
  "load_forecast": "example-load",
  "slots": [
    {
      "start": "2026-10-22T11:00:00+00:00",
      "p_import": 0.3,
      "p_export": 0.07,
      "surplus_kw": 0.0,
      "surplus_kw_p10": 0.0,
      "regime": "battery",
      "marginal_cost": 0.07,
      "marginal_cost_p10": 0.07,
      "certainty_spill": 0.0,
      "certainty_covered": 1.0,
      "pv_value": {"per_kwh": 0.07, "routing": "battery", "weight": 1.0},
      "battery_value": {"per_kwh": 0.13651794998444183, "displaces": "later_import", "weight": 1.0},
      "supply": [
        {"source": "pv", "kw": 1.0833000000000002, "per_kwh": 0.07},
        {"source": "battery", "kw": 6.0, "per_kwh": 0.13651794998444183},
        {"source": "grid", "kw": null, "per_kwh": 0.3}
      ],
      "unserved": 0.0
    }
  ],
  "horizon_cut": false
}
```

Read it as: one more kWh now costs 0.07, because it would take PV that is charging the
battery, and that PV costs the export the site gives up. Up to about 1.08 kW is
available at that price; beyond it the battery gives up to 6 kW at about 0.137, what
the energy it holds cost through its losses and wear, and beyond that the grid at 0.30.

`marginal_cost` and `pv_value` average over the ten PV scenarios; `supply` is the median
scenario's alone, so the two can differ; here every scenario agrees.
[What a stored kWh is worth](../methodology/stored-value.md#the-cases-with-numbers) has
the arithmetic.
