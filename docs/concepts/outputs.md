# Outputs

Each output is defined here once; the [methodology](../methodology/index.md) explains
how it is made, and [`valuation/2`](../reference/valuation.md) gives its exact form.

## A valuation's slots

| Output | Definition |
|---|---|
| `marginal_cost` | what one more kWh used in the slot costs the site, averaged over the PV [scenarios](../methodology/scenarios.md) |
| `marginal_cost_p10` | the same, priced on one PV day: the forecast's 0.1 quantile in every slot |
| `regime` | [what one more kWh displaces](#regime), in the [median PV case](#the-median-pv-case) |
| `surplus_kw`, `surplus_kw_p10` | [surplus](#surplus), in the median and the p10 PV case |
| `certainty_spill`, `certainty_covered` | [the certainties](#certainties) |
| `pv_value`, `battery_value` | the value of a kWh by source, and what set it ([values by source](../methodology/stored-value.md#values-by-source)) |
| `supply` | the [supply stack](../methodology/stored-value.md#the-supply-stack): how much power each price holds for |
| `unserved` | [unserved load](#unserved) |
| `p_import`, `p_export` | the input prices for the slot |

A valuation also has its `kind`, its causes (`degraded`), each input's state (`inputs`)
and `horizon_cut` ([Causes and kinds](../reference/causes.md)). A battery valuation
without the battery's lots names `battery_lots` and keeps its values: all the battery
holds is priced as bought, at the import.

## The median PV case

The forecast's 0.5 quantile in every slot, projected once on its own. It is not one of
the ten [scenarios](../methodology/scenarios.md), which run from the 5th to the 95th
percentile. The regime, `surplus_kw` and the supply stack come from it; `marginal_cost`
and the certainties come from the scenarios.

## A live value

A [live value](../methodology/live-value.md) is for the slot from now. It has
`marginal_cost`, `regime`, `surplus_kw`, `certainty_spill`, `certainty_covered`,
`pv_value`, `battery_value`, `p_import` and `p_export` as above (the prices null when it
is `unpriced`), plus its time `at`, its `kind`, the engine's causes (`degraded`) and the
client's own (`external`). It has no p10 values, no supply stack and no `unserved`.

## Regime

What one more kWh in the slot would displace:

| Regime | One more kWh would be | So it costs about |
|---|---|---|
| `export` | PV that would have been exported | the export price |
| `curtail` | PV that would have been curtailed (more curtailed than exported) | nothing |
| `battery` | energy the battery would otherwise hold or deliver | what the energy in the battery cost, through its losses and wear; no more than the sun that will refill it |
| `import` | an import | the import price |

The regime says where the kWh comes from; `marginal_cost` says what that costs. A
`battery` slot can cost about the export price (the battery holds solar, or the sun will
refill it) or more than the import price now (it holds grid energy bought dearer).

## Surplus

**PV the plant would export or curtail, in the projection**: more than the house takes
and the battery can store. While the battery is still charging and nothing is exported
there is none, however strong the sun. Battery export is never surplus. Every load that
is running is in the load forecast, so the surplus is net of its draw. Summed over a
day, it is the PV energy there is to share.

## Certainties

Each is a share of the scenario weight, 0 to 1.

- **`certainty_spill`**: in what share of the scenarios the kWh is paid for only with PV
  that would have been exported or curtailed, now or when the battery fills.
- **`certainty_covered`**: in what share of the scenarios the battery covers the
  expensive hours ahead without an import. A slot is **expensive** when the battery or
  the grid serves the house in it (PV does not cover it) and its import price is above
  the cheapest import in the 24 hours from this slot. The **expensive hours ahead** are
  the first unbroken run of expensive slots at or after this one. Covered means the house
  does not import from this slot (or the run's start, if later) to the run's end. The run
  is read from the price curve, whatever its shape.

## Unserved

**The share of the scenario weight in which the slot has load a stored kWh would serve,
but cannot**: at the grid's import limit, or a DC-bus load beyond the hybrid's rating
with the battery on its bus at its reserve or limit. In those scenarios the kWh is
priced at the import price, as a floor. Load beyond any stored kWh's reach is not
counted.
