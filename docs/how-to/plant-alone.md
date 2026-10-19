# Use the plant model on its own

The plant model and its simulator are a package of their own, `microgrid.plant`. A
client that wants to know what a solar + battery plant will do (where each kWh goes,
what the battery holds, what is imported and exported) uses it without the price
model.

## Project a day

A plant is its `plant/2` description ([Describe your plant](describe-plant.md)), read
by `PlantConfig.from_mapping` or taken from `microgrid.plant.examples`. `Plant` is
the model of it; `Plant.project` runs it over the slots you give it, from a state of
charge, with the PV and the load in each:

```python
import math
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from microgrid.plant import Plant, examples

plant = Plant(examples.example_plant("ac_coupled_hybrid", ZoneInfo("Europe/Berlin")))

start = datetime(2026, 6, 21, 22, 0, tzinfo=UTC)  # midnight in Berlin
slot = timedelta(minutes=10)
starts = [start + i * slot for i in range(144)]  # one day
durations = [slot] * len(starts)
hour = [((t.astimezone(ZoneInfo("Europe/Berlin")).hour) + t.minute / 60) for t in starts]
pv_kw = [max(0.0, 5.0 * math.sin(math.pi * (h - 6) / 14)) if 6 <= h <= 20 else 0.0 for h in hour]
load_kw = [0.6] * len(starts)

flows = plant.project(starts, durations, pv_kw, load_kw, soc=0.3)

for flow in flows[::18]:
    print(
        f"{flow.start.astimezone(ZoneInfo('Europe/Berlin')):%H:%M}  "
        f"SOC {flow.soc_start:4.0%} -> {flow.soc_end:4.0%}  "
        f"PV to battery {flow.pv_to_battery:4.2f} kW  "
        f"exported {flow.pv_export:4.2f} kW  imported {flow.grid_to_load:4.2f} kW"
    )
```

```text
00:00  SOC  30% ->  29%  PV to battery 0.00 kW  exported 0.00 kW  imported 0.00 kW
03:00  SOC  24% ->  24%  PV to battery 0.00 kW  exported 0.00 kW  imported 0.63 kW
06:00  SOC  54% ->  53%  PV to battery 0.00 kW  exported 0.00 kW  imported 0.00 kW
09:00  SOC  74% ->  77%  PV to battery 2.43 kW  exported 0.00 kW  imported 0.00 kW
12:00  SOC 100% -> 100%  PV to battery 0.00 kW  exported 4.24 kW  imported 0.00 kW
15:00  SOC 100% -> 100%  PV to battery 0.00 kW  exported 3.87 kW  imported 0.00 kW
18:00  SOC 100% -> 100%  PV to battery 0.00 kW  exported 1.54 kW  imported 0.00 kW
21:00  SOC  93% ->  92%  PV to battery 0.00 kW  exported 0.00 kW  imported 0.00 kW
```

The example plant tops its battery up from the grid in its night window (the SOC climbs
from 24 % to 54 % by 06:00), charges from PV in the morning, then exports once it is
full.

## What a projection gives

Each `SlotFlow` (`microgrid.plant.flows`) is one slot's flows in kW: PV to the load,
the battery, export or curtailment; the battery to the load or export; the grid to the
load or the battery; the paths of a DC bus where the plant has one; and the state of
charge at the slot's start and end. `surplus` and `regimes` read the PV left over and
each slot's regime from a projection. Times are UTC throughout, the plant's own time
zone only placing its windows.

## Your own plant model

A model of a plant this one does not describe implements the port,
`microgrid.plant.port.PlantModel` ([Write your own plant model](own-plant-model.md)); the
price model reads any such model the same way.
