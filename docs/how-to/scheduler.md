# Use the prices in a scheduler

A valuation gives a scheduler several numbers per slot. Each answers a different
question.

| The question | Read |
|---|---|
| When is it cheapest to run a small load? | `marginal_cost`: the expected cost of one more kWh |
| What if the day turns out dull? | `marginal_cost_p10`: the same, on the forecast's one-in-ten dull day |
| Is there spare PV to soak up, and how much? | `surplus_kw` (and `surplus_kw_p10`), for loads that should only run on spare PV |
| How sure is the price? | `certainty_spill`: the chance the kWh costs only PV that would have spilled |
| What does a load bigger than the margin cost? | `supply`: how much power each price holds for |
| Can I trust this valuation at all? | `kind`: `fallback_import` means the slots are at the import price, `unpriced` that there are none. A live value that is `stored_value_v1` but names `smoothing` is warming up: the valuation's price, no surplus yet ([Causes and kinds](../reference/causes.md)) |

Each is defined in [Outputs](../concepts/outputs.md).

## Size the load against the stack

`marginal_cost` is the price of one more kWh. A load larger than the cheapest step of
the [supply stack](../methodology/stored-value.md#the-supply-stack) pays the next step
for the rest. In the example dataset at midday on Thursday, about 1 kW of PV is charging
the battery: a 1 kW load runs on it at the export price, 0.07, but a 2 kW load takes
the other kW from the battery at about 0.15. So cost a load across the stack, step by
step.

## A worked decision

Run a 2 kW load for 3 hours, some time in the next 24 hours, from 07:00 on Thursday
2026-10-22, on the example dataset. Value once, then cost every 3-hour window.
`offline.value` gives the engine the battery's lots from the dataset's meters; your own
client gives its ledger's (`Inputs.lots`). Without them the valuation names
`battery_lots` and prices what the battery holds as bought at the import, so a battery
kWh looks dearer than it may be ([The battery's lots](../concepts/inputs.md#the-batterys-lots)).

```python
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from microgrid.cli.dataset import read_dataset
from microgrid.cli.offline import load_setup, value
from microgrid.engine import SlotValue, ValuationKind

data = Path("examples/dataset")
tz = ZoneInfo("Europe/Berlin")
now = datetime(2026, 10, 22, 7, 0, tzinfo=tz).astimezone(UTC)
valuation = value(load_setup(data), read_dataset(data), now)
assert valuation.kind is ValuationKind.STORED_VALUE_V1, valuation.degraded

LOAD_KW, SLOTS = 2.0, 18                      # 2 kW for 3 hours of 10-minute slots
HOURS = 10 / 60                               # one slot


def stack_cost(slot: SlotValue, kw: float = LOAD_KW) -> float:
    """The load's cost in one slot, taking the supply stack's steps in order."""
    left, cost = kw, 0.0
    for step in slot.supply:
        take = left if step.kw is None else min(left, step.kw)
        cost += take * HOURS * step.per_kwh
        left -= take
        if left <= 0:
            break
    return cost


day = [s for s in valuation.slots if s.start < now + timedelta(hours=24)]
windows = []
for i in range(len(day) - SLOTS + 1):
    run = day[i : i + SLOTS]
    windows.append((
        sum(stack_cost(s) for s in run),                          # the median day, sized
        sum(LOAD_KW * HOURS * s.marginal_cost for s in run),      # averaged, small-load price
        sum(LOAD_KW * HOURS * s.marginal_cost_p10 for s in run),  # a dull day
        run[0].start.astimezone(tz),
    ))
best = min(windows, key=lambda w: w[0])
print(f"cheapest on the median day: {best[3]:%a %H:%M}")
for w in windows:
    if w[3].minute == 0 and w[3].hour in (7, 11, 18, 1, 4):
        print(f"{w[3]:%a %H:%M}  stack {w[0]:.2f}  marginal {w[1]:.2f}  p10 {w[2]:.2f}")
```

```text
cheapest on the median day: Thu 11:30
Thu 07:00  stack 0.92  marginal 0.66  p10 0.79
Thu 11:00  stack 0.62  marginal 0.42  p10 0.42
Thu 18:00  stack 0.78  marginal 0.80  p10 0.86
Fri 01:00  stack 1.08  marginal 1.08  p10 1.08
Fri 04:00  stack 0.84  marginal 0.87  p10 1.19
```

Each figure is the cost of the whole 6 kWh run, in the prices' currency: `stack` on the
median PV day, costed across the supply stack; `marginal` at the small-load price,
averaged over the PV scenarios; `p10` at the small-load price on the dull day. Reading
them:

- **Thursday late morning is cheapest (0.61 from 11:30).** About 1.1 kW of PV is
  charging the battery, there to divert at the export price, and the other kW comes
  from the battery at about 0.15: the week's solar and the night's top-up.
- **The weather barely moves it**: the dull day's figure is the same (0.42 at the
  small-load price). Even then PV is charging the battery at midday.
- **Thursday evening costs about the same for a small load or a large one (0.78)**: the
  battery serves it at what its energy cost, about 0.13 a kWh, against an import of
  0.42.
- **Friday 01:00 costs 1.08 whatever the weather**: the plant holds the battery in its
  grid-charge window, so the load runs on the night import at 0.18.
- **Friday 04:00 (0.84) is a bet on the sun.** From 05:00 the battery holds the night's
  grid energy, but Friday's sun is expected to refill it (`certainty_spill` 0.9 there),
  so a kWh drawn costs the refill, about 0.12. On a dull Friday the same run costs 1.19.

The `p10` column is priced at the small-load price, so it understates a large load's
cost; it is a guide to the weather risk, not a sized cost.

## Loads that should only use spare PV

Read `surplus_kw`: power the plant would otherwise export or curtail. Run while it
covers the load's draw; for a cautious rule, while `surplus_kw_p10` does. A load already
running is in the load forecast and the measured house load, so add its own draw back
before comparing.

## When there are no prices

A valuation of kind `unpriced` has no slots: there was no price for the slot under way,
so there is nothing to schedule on. Keep the last plan, or fall back to your own rule
(run at a fixed hour). A `fallback_import` valuation has slots, all at the import
price: schedule on that if you must run, knowing it says nothing about the sun or the
battery.

## Commit a run, then value again

Each slot is priced as an unannounced kWh: the plant running as forecast, plus one kWh.
The supply stack holds within one slot. A 3-hour run is many slots, and a large one can
change what the plant does later: it can make the battery run empty sooner, or fill
later, and so move the event that prices every slot before it. So once you commit a
run, add it to the load forecast and value again. The engine prices a load in the load
forecast as the house's own, so the next decision sees the plant with your run in it.

To add it, append a new issue to the load forecast series: the last forecast you
received, with the run's kW added to the slots it covers, issued and received now. The
newest issue wins slot by slot, so the run is in from now on. A load on the plant's DC
bus goes into the DC-bus load forecast (`Inputs.dc_load`, `dc_load_forecast`) instead.

## How often to re-plan

Value again when an input changes (a new PV forecast issue, new prices) and on a clock
([Run it continuously](run-continuously.md)), and re-plan from the new valuation. To
decide whether to start this minute, read the [live value](../methodology/live-value.md):
it is the same price for the slot from now, made from the meters.

## How far ahead to trust it

A price 2 hours ahead rests on a forecast that is 2 hours old by then; one 40 hours
ahead, on one much older. [Replay](replay.md) your own record: it scores the marginal
cost, the surplus and the regime by hours ahead (0-1 h, 1-3 h, 3-12 h, 12-48 h), which
says how far ahead your plant's prices hold.
