# Read the battery's lots

The ledger books where the energy in the battery came from and what it cost
([The battery's lots](../methodology/lots.md)). `microgrid ledger` runs it over a span
of a dataset.

## What it needs

A dataset with the plant's `pv`, `grid` and `battery` meters, the SOC, and the prices
each lot was charged at: the [recorded prices](../concepts/inputs.md#recorded-prices),
else those the dataset's price forecasts put in force. A
[heartbeat](../concepts/inputs.md#heartbeat) is optional.

## Run it

```sh
uv run microgrid ledger --data examples/dataset --from 2026-10-21 --to 2026-10-27
```
```text
the battery's lots, 2026-10-21 00:00 CEST to 2026-10-27 00:00 CET (870 slots)
source    charged kWh  cost USD  to load kWh      USD  exported kWh  held kWh      USD  USD/kWh
unknown         0.000     0.000        6.000    0.000         0.000     0.000    0.000        -
pv             33.846     2.480       27.516    2.017         0.000     6.423    0.470   0.0732
grid           13.921     2.650       13.921    2.650         0.000     0.000    0.000        -
unexplained: 0.093 kWh (the meters against the SOC)
```

## Read it

One row per source:

- **charged kWh, cost**: what went into the battery from that source over the span, and
  what it cost: PV at the export price it gave up, the grid at the import price.
- **to load kWh, USD**: what the battery gave the house from that source's lots, and the
  cost it carried.
- **exported kWh**: what it gave to the grid.
- **held kWh, USD, USD/kWh**: what it holds at the end, and its cost per kWh.

In the example, the battery started the week holding 6 kWh of **unknown** origin: what
it held when the ledger started has no known cost, so it is kept apart at no value, and
it was drawn first. (The pool, which prices a battery kWh, prices that energy as bought
at the import instead.) By the end it held 6.4 kWh of PV at about 0.073 per kWh: PV stored
when the export price was 0.07, plus the charge's loss. Every grid kWh went to the
house (after unknown energy, grid lots serve the house first).

**Unexplained** is the energy the meters show that the booked flows do not account for
(self-discharge, meter timing, an efficiency slightly off). The lots are scaled to the
metered SOC after every slot, so it shows here instead of drifting into the lots. A
large figure means the battery's conversion in `plant/2` needs fitting
([Fit the battery's conversion](fit-battery.md)).

**The price reads the pool, not these lots.** The table is the lots: what the battery
holds by source, each discharge taken from a lot in the order above. A battery kWh's
price is the pool's running average over all it holds, which takes each discharge at
its average instead ([The battery's lots](../methodology/lots.md#lots-and-the-pool)). The
two hold the same energy; after a span of mixed charging their cost per kWh can differ.

The money columns are in the dataset's prices' currency, which their headers name (the
example dataset's prices are `USD/kWh`).

## From Python

`microgrid.engine.ledger` (`Ledger`, `Lots`, `run_ledger`, `floor_per_kwh`) books
lots from any meter set you feed it; `microgrid.cli.history.run_ledger` first reads a
recorded history, from a dataset or from any recorder you adapt to its `History`
interface (three read methods). See the [API
reference](../reference/api/ledger.md).
