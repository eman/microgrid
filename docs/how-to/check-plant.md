# Check a plant model against its meters

A plant model is only worth pricing with if what it projects is what the plant does.
`microgrid check-plant` feeds the model what actually happened (the metered PV, the load
the plant served, the metered SOC at the start), so no forecast error is in the way, and
compares its projection with the meters.

## What it needs

A dataset with, for the days to check, the plant's `pv`, `grid` and `battery` meters, the
SOC, the [house load](../concepts/inputs.md#house-load) and the prices (recorded, or
the price forecasts'), and optionally a [heartbeat](../concepts/inputs.md#heartbeat)
([Inputs](../concepts/inputs.md#which-command-needs-which)). The plant needs PV and a
battery: the command reads both meters, and without a battery there is nothing to
project.

## Run it

```sh
uv run microgrid check-plant --data examples/dataset --from 2026-10-21 --to 2026-10-26
```

The days are local, `--to` included. It runs the model two ways:

- **From each local midnight, for the day**: the day's import and export, how far the
  SOC strays, and when the battery tops up, first fills and first runs empty.
- **From each hour, over 48 hours**: the SOC's error by hours ahead, and whether the
  model's first event (fill, top-up, empty) is the meters' first event.

## Read it

```text
plant model against the meters, 2026-10-21 .. 2026-10-26 (Europe/Berlin)
day         import kWh m/r   export kWh m/r   SOC MAE/max pp  top-up m|r     full m|r       reasons
2026-10-21    3.2 /   3.2    13.4 /  13.4     0.0 /   0.0   04:40|04:40  11:40|11:40  -
...
over 6 days without a telemetry gap (model - meters):
  import kWh/day         bias  +0.000  MAE  0.000
  export kWh/day         bias  +0.000  MAE  0.000
  ...
  SOC by hours ahead, MAE/max: 0-1h 0.0/0.8 pp (n 870), 1-3h 0.0/0.8 pp (n 1722), ...
  first event agreed in 135 of 138 hourly runs

routing, model -> meters (slots):
  battery serving -> battery serving   387
  PV charging -> PV charging           170
  ...
```

- **`m/r`** is the model, then the meters (recorded).
- **The day table**: each day's import and export, the SOC's mean and largest error in
  percentage points, and the times of the top-up and of the first full battery. A day
  the telemetry was silent is labelled and left out of the summary.
- **The summary**: each measure's bias (model minus meters) and mean absolute error over
  the days, and the median gap between the model's event times and the meters'.
- **SOC by hours ahead**: how the error grows over the 48-hour runs. The price rule
  looks that far ahead, so this is the error the prices carry from the plant model.
- **First event agreed**: in how many hourly runs the model's first fill, top-up or
  empty was the meters' first. A fill bounds a battery kWh's price, and when the battery
  charges sets what it holds, so this is the measure closest to the price.
- **Routing**: slot by slot, what the model said served the house against what the
  meters said.

The example dataset was made by the plant model itself, so it agrees with itself almost
everywhere. A real plant's will not, and the differences say what to fix.

## What to do with the result

- **Large SOC errors from midnight**: the battery's parameters. Fit its conversion
  ([Fit the battery's conversion](fit-battery.md)) and check `capacity_kwh` and
  `reserve_soc`.
- **Top-up times or amounts off**: the grid-charge window in `plant/2` (its target,
  [`cover_until` and `timing`](../reference/plant.md#dispatchgrid_charge-a-grid-charge-window)).
- **Import or export off by a steady amount**: a load the plant serves that the house
  meter does not see, or the inverter's own draw (`standby_kw`, `working_kw`).
- **A topology that does not fit at all**: see [Describe a plant](describe-plant.md),
  or [write a plant model](own-plant-model.md).

Try a candidate without changing the dataset's file: `--plant candidate.toml`.

The command reports measures; it does not pass or fail a plant. You add the days a
model is not meant to describe (a day the plant was serviced, a day it ran another
mode) and the pass marks a model must meet before you trust its prices.
