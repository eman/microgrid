# PV scenarios and certainty

**What serves a kWh, what the battery will hold, and whether the sun will refill it all
depend on tomorrow's sun. So the library prices each slot under several equally likely
PV days and publishes the average, a pessimistic case, and how sure it is.**

## From quantiles to ten days

A PV forecast in the [accepted form](../concepts/inputs.md#pv-forecast) gives, for each
interval, its quantiles: any levels that include the median, or the median alone.
Solcast's are three: a power the PV will fall short of one time in ten (p10), half the
time (p50) and nine times in ten (p90). The library reads them as a distribution per
slot:

- **Linear between the levels**, and extended linearly beyond the outermost on a side
  that has a level other than the median; on a side that has none, the median holds,
  as nothing is known of the spread there.
- **Cut into equal-probability traces**, ten by default (`engine/2`'s `scenarios`): the
  5th, 15th, ..., 95th percentile.
- **Each trace takes the same percentile in every slot.** A day is bright or dull as a
  whole: the 15th-percentile trace is a dull day all day, not dull at 10:00 and bright at
  11:00.
- Quantiles that cross (a p10 above the p50) are sorted first; a trace never goes below
  0 kW.
- Any levels: deciles cut the traces from nine levels, quartiles from three, the same
  way.

For one slot with p10 = 2.0 kW, p50 = 3.0 kW and p90 = 3.6 kW, the ten traces take:

| Percentile | 5th | 15th | 25th | 35th | 45th | 55th | 65th | 75th | 85th | 95th |
|---|---|---|---|---|---|---|---|---|---|---|
| kW | 1.875 | 2.125 | 2.375 | 2.625 | 2.875 | 3.075 | 3.225 | 3.375 | 3.525 | 3.675 |

A single value (the median alone) stands for every quantile: the ten traces are the
same day, and the price is that day's. A plant without PV has one case, at 0 kW.

## One price from ten days

The plant is projected once per trace, and each projection is priced: what serves the
kWh in that trace, and what the battery holds there. The slot's `marginal_cost` is the
average over the traces, each weighing one tenth. So **the chance that the sun refills
the battery is in the price**: a battery that surely fills is priced near the export, one
that surely does not at what its energy cost, and everything between is a weighted mix.

**An uncertain morning, worked through.** The plant of [the make-up
rule](make-up-rule.md#a-worked-day): at 07:00 the battery is at 80 %, all of it charged
from the grid overnight at 0.18; the house draws 0.5 kW, and the forecast's p10, p50 and
p90 days peak at 1.0, 2.5 and 3.5 kW. What does one more kWh at 07:00, from the battery,
cost?

| Traces | What happens | The 07:00 kWh costs |
|---|---|---|
| 9 of 10 (the brighter days) | the battery fills between 09:50 and 14:00 and spills | 0.1197: the sun refills the room the kWh frees, at the export it gives up, through the battery |
| 1 of 10 (the dullest) | the battery never fills; it runs empty at 20:30 | 0.2194: the grid energy it holds, through the battery |

`marginal_cost` = 0.9 × 0.1197 + 0.1 × 0.2194 = **0.1297**, the library's own run.
`certainty_spill` is 0.9.

This is how the weather enters the price. On nine days in ten, keeping the kWh in the
battery would cost a kWh of the day's sun, so it is cheap to use now; on the tenth, it is
grid energy, and costs what it was bought for.

## The pessimistic case

`marginal_cost_p10` prices the slot on one more PV day: the forecast's 0.1 quantile in
every slot (read between its levels where 0.1 is not one), a day as dull as the
forecast's one in ten. A forecast with no level below the median says nothing of a dull
day: the pessimistic case is then the median, and the valuation names `pv_spread`, its
values standing. It is not one of the ten
scenarios, which run from the 5th to the 95th percentile. `surplus_kw_p10` is the surplus on that day, never above the
median's. A cautious scheduler can plan on these. In the example, `marginal_cost_p10` is 0.2194:
the dull day, on which nothing refills the battery. It is usually, not always, the
dearer number: on a dull day the battery may hold different energy later, or run empty
where a brighter day would have kept it serving.

## How sure the price is

`certainty_spill` and `certainty_covered` ([defined in Outputs](../concepts/outputs.md#certainties))
are each a share of these scenarios. In the example, `certainty_spill` is 0.9: in nine
of ten days the kWh is made up only with PV that would have spilled.

## Why scenarios and not the median alone

The median day hides the asymmetry a battery creates. On the median day of the example
the battery fills, and the kWh costs 0.1197. But on one day in ten it does not, and
there the kWh costs nearly twice as much. Pricing the median alone would tell a
scheduler that the risk is not there.

## Where the PV forecast comes from

Any source: the library takes a series of forecast issues. A forecast
with three quantiles gives the scenarios their spread; a single value gives one day.
[Data sources](../configuration/data-sources.md) lists the providers.
