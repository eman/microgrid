# What a stored kWh is worth

**A kWh in the battery is worth what the energy in the battery cost: the running
average over everything it holds, solar at the export it gave up and grid energy at
what it was bought for. A kWh drawn from it also pays the discharge's loss and wear.
Where the sun will refill the room a draw frees, a kWh drawn costs no more than that
sun.**

## The stored cost, `v`

The battery's energy came from somewhere, and it cost something to put there:

- **Solar** cost the export the site gave up to store it, at the export price when it
  was charged.
- **Grid energy** cost the import price it was bought at.

Charging one AC kWh at price `p` stores `η_c` kWh, so a stored kWh costs `p / η_c`.
`v` is the average of that cost over all the battery holds, weighted by energy: the
battery's **pool**, its stored kWh and what they cost together.

**Why this, and not what will replace the kWh.** A rule that prices a kWh by its
replacement must decide where the replacement comes from: the grid tonight, or the sun
tomorrow. That is a forecast about the plant's future, and it can be wrong both ways.
What the energy in the battery cost is known. A kWh the battery delivers is that
energy, so it costs what that energy cost.

### How `v` moves

- **Charging adds to the pool at cost.** A kWh of solar adds `p_export / η_c`, a kWh
  from the grid `p_import / η_c`, at the prices of the slot it charges in, where `η_c` is
  the stored kWh per kWh charged **by the path that source takes**, as the plant model
  answers it (`Storage.stored_per_kwh`).
  On a battery on its own inverter both cross the same conversion. On a hybrid's DC bus,
  DC-coupled PV reaches the battery's port as it is and pays only the port's conversion
  (none for a folded battery's meter factor aside), while the grid's AC crosses the
  hybrid first: a split battery stores less of a grid kWh than of a DC PV kWh.
- **Drawing takes energy at the average.** A discharge lowers the pool's energy and its
  cost in proportion, so `v` stays where it was. Only charging moves it.
- **The meter wins.** The pool is scaled to the stored energy the battery reports, at
  its own average, so energy the model cannot explain (self-discharge, meter timing)
  changes how much is held, not what it cost per kWh.

**Why the average, and not the next lot drawn.** [The battery's lots](lots.md) say what
the battery holds by source, and they must take each discharge from some lot first: the
house from grid energy, exports from solar. That order is bookkeeping. The energy in a
battery is all the same once stored; which lot a kWh "came from" is a convention, and a
price that moved with it would move for no physical reason. The pool's average moves
only when something charges the battery.

### Energy of unknown origin

Some of what the battery holds may have no known cost: what it held when the ledger
began, energy the meter finds when the model held none, or all of it when the client
gives no lots. It is **priced as bought, at the import price**: the first slot's for
what the ledger held when it began, that slot's for energy the meter finds, the
valuation's first slot's when no lots are given.

The reason is the library's rule for anything it cannot vouch for: [degrade
conservatively](degradation.md). A low price is permission to run; a battery kWh priced
as cheap solar that was in fact bought at the peak would send a load onto energy that
cost the site dearly. Not knowing what the energy cost moves its price toward the
import. Without lots the valuation also names `battery_lots` in `degraded`, and stays
`stored_value_v1`: its values are already the conservative ones, so they stand.

It washes out as the battery cycles: once the battery has been through a discharge and
a charge, the pool holds energy whose cost is known. A client gives the battery's lots
from its ledger ([Inputs](../concepts/inputs.md#the-batterys-lots)).

### Run forward along each scenario

`v` is not fixed over the horizon. For each PV scenario the pool is run forward along
that scenario's projection: what the plant will charge, from where, at what price. A
slot after an overnight grid top-up prices a battery that then holds that grid energy;
a slot after a sunny morning, one that holds the morning's solar. So a later slot prices
what the battery will hold then, under each scenario's weather.

## The opportunity cost of a full battery

A battery full of grid energy that the price kept full would have nowhere to put the
day's sun: the PV would be exported, or curtailed, while the battery held energy bought
at the import price. Not discharging has a cost: the room it does not free.

So the price looks ahead with [the make-up rule](make-up-rule.md)'s events. **Where the
first event after the slot is a fill**, the sun refills the room a draw frees before the
battery needs the grid, and a kWh drawn now is replaced by solar that would otherwise
have spilled. Then

`v = min(stored cost, the fill's refill cost)`

where the refill cost is the export the refilling PV gives up (0 where it would have
been curtailed), plus its charge wear, per stored kWh. A battery filled from the grid
overnight, with a sunny day ahead, is priced near the export price, and a scheduler
draws it down. Where no fill comes first (a top-up, the battery running empty, the
horizon), nothing refills the room for free, and the stored cost stands.

## The dead band

Given `v`, a kWh taken from the battery's cycle costs one of two things, depending on
where it comes from:

| The kWh comes from | It costs | Why |
|---|---|---|
| PV that was charging the battery | `p_export` now | it is solar: a load that takes it gives up the export the site gave up to store it |
| the battery, discharging or able to | `v / η_d + c_dis` | the battery delivers a kWh more: it draws `1/η_d` stored kWh at `v` each, and wears `c_dis` |

Here `η_c` and `η_d` are the charge and discharge efficiencies, and `c_dis` the wear per
AC kWh discharged, from the plant's `battery` settings. Each efficiency is the
inverter's at the margin of the power it already converts in that slot: the loss grows
with power, so a kWh more at 5 kW loses more than a kWh more at 0.5 kW. The charge's own
wear is not in `v`: it was spent when the energy was stored. The inverter's fixed draws
(standby, and its extra draw while the battery works) are the plant model's: a
marginal kWh does not pay them.

One exception: **where the grid is topping the battery up in the same slot**, PV a load
diverts from charging is replaced by the grid there and then, so it costs that import.
That replacement is no forecast; it happens in the slot. The same holds for the grid's
own charge diverted at an import limit.

Between the two lies the **dead band**: a battery kWh costs more than the solar that
charged it, by the round trip's loss and the discharge's wear. A load gains nothing by
taking energy through the battery that it could take from the sun directly.

## The cases, with numbers

A battery with `η_c = η_d = 0.95` and wear 0.02 per kWh each way (the charge's wear only
enters a refill). Export 0.07, a night import of 0.18:

| The battery holds | `v`, per stored kWh | A battery kWh costs |
|---|---|---|
| solar, charged at export 0.07 | 0.07 / 0.95 = 0.0737 | 0.0737 / 0.95 + 0.02 = **0.0976** |
| grid energy, bought at 0.18 | 0.18 / 0.95 = 0.1895 | 0.1895 / 0.95 + 0.02 = **0.2194** |
| half of each | (0.0737 + 0.1895) / 2 = 0.1316 | 0.1316 / 0.95 + 0.02 = **0.1585** |
| grid energy at 0.18, and the sun refills it first | min(0.1895, (0.07 + 0.02) / 0.95) = 0.0947 | 0.0947 / 0.95 + 0.02 = **0.1197** |
| energy of unknown origin, import now 0.18 | as bought: 0.1895 | **0.2194** |

PV diverted from charging costs the export now, 0.07, in every case (outside a slot
where the grid is topping the battery up).

Read the table this way:

- **A battery full of solar is cheap all evening.** 0.0976 against an evening peak of,
  say, 0.45: the energy cost the export the site gave up, plus the trip through the
  battery. A load that runs on it pays that.
- **Grid energy costs what it was bought for, and the trip.** 0.18 at night is 0.22 by
  the time it reaches the house. A battery kWh can cost more than importing would at
  some hours (a night import at 0.18), and the price says so.
- **A full battery with sun ahead is drawn down.** The fill bounds the price at
  0.1197, not 0.2194: a kWh kept would cost the site a kWh of the day's sun.

Run on the plant of [the make-up rule's worked day](make-up-rule.md#a-worked-day), with
a grid-charge window at 01:00-05:00 filling the battery from 10 % to full at 0.18: at
07:00 a kWh from the battery costs **0.2194** on a sunless day (it holds grid energy),
and **0.1197** on a sunny one.

## The minimum spread

The battery price is also the break-even of the energy it holds. A kWh charged at `p_c`
is only worth discharging into a use worth more than

`p_c / (η_c η_d) + c_dis`

which is what a battery kWh costs. With the battery above, energy charged from the grid
at 0.18 needs a use worth more than 0.2194; solar that would have exported at 0.07
needs more than 0.0976. A scheduler that runs a load where the price is lowest spends
the battery's energy where it is worth the most.

## The make-up alternative

`engine/2`'s `battery_value = "replacement"` prices a battery kWh by what will replace
it instead: the [make-up rule](make-up-rule.md)'s first event, carried through the
battery's conversion. There `v` is

- **after a fill or a top-up** at price `p`, what charging it again costs:
  `v = (p + c_ch) / η_c`;
- **after the battery runs empty, or at the horizon**, what it would have saved the
  house: `v = η_d × (p − c_dis)`;

and PV diverted costs `η_c × v − c_ch`, the battery charged a kWh less. With the
battery above:

| Make-up event | Make-up price `p` | `v` | PV diverted costs | A battery kWh costs |
|---|---|---|---|---|
| fill, spilling at export 0.07 | 0.07 | 0.0947 | 0.07 | 0.1197 |
| grid top-up at 0.18 | 0.18 | 0.2105 | 0.18 | 0.2416 |
| empty, cheapest import 0.30 | 0.30 | 0.2660 | 0.2327 | 0.30 |
| empty, inside a 0.45 peak | 0.45 | 0.4085 | 0.3681 | 0.45 |

It answers a different question: what the site will pay to put the kWh back, if the
plant's future goes as projected. Choose it when that is the question your scheduler
asks ([Tune the engine](../how-to/tune-engine.md#you-want-a-kwh-priced-by-its-replacement)).

## Values by source

A slot's price is what serves the kWh in it. A scheduler with a choice may want the
price of each source separately, and why it is what it is. Each slot gives:

- **`pv_value`**: a PV kWh diverted to a flexible load, at the bottom of the dead band
  (the export price), with its `routing`, where that PV would otherwise have gone:
  `export`, `curtail` or `battery`. Its `weight` is the share of scenarios in which PV
  had spare to give.
- **`battery_value`**: a battery kWh drawn for a flexible load, at the top of the dead
  band, with what `displaces` it: `export` (the battery would have filled and spilled,
  or an export run ends sooner) or `later_import` (a top-up, an import once it runs
  empty, or the horizon). `displaces` is the make-up rule's event, reported on either
  rule. Its `weight` is the share of scenarios in which the battery had a kWh to give.

Each is an expectation over the scenarios in which the source had energy to give, and
None where it had none.

## The supply stack

A price holds for a small load. A big one can take all the spilling PV and move on to
the battery, then the grid. The **supply stack** (`supply`) says how much power each
price holds for in the slot, in the order the plant serves a load:

1. **`spill`**: PV exported or curtailed, at the export it would have earned (0 where
   curtailed);
2. **`pv`**: PV charging the battery, diverted, at the bottom of the dead band;
3. **`battery`**: discharge up to its limit and the energy above its reserve, at the
   top of the dead band;
4. **`grid`**: without limit (`kw` null), at the import price.

The order is the plant's, not sorted by price: the battery's step can cost more than the
grid's, when it holds energy bought dearer than the import now. Where the grid already
serves the slot (the house importing, or the battery held or empty with no PV charging
it), the grid is the only step.

**The stack is the [median PV case](../concepts/outputs.md#the-median-pv-case)'s;
`marginal_cost` is the average over all the scenarios.** So the first step's price and
`marginal_cost` can differ. In the example dataset at 13:00 local on 2026-10-22 the
stack reads 1.08 kW of PV charging the battery at 0.07, then up to 6 kW from the
battery at 0.1365 (it holds the week's solar and the night's top-up), then the grid at
0.30; `marginal_cost` is 0.07. A 1 kW load runs on diverted PV; a 3 kW
load pays partly at the battery's step. [Use the prices in a
scheduler](../how-to/scheduler.md) prices a load across the stack.

## Setting the parameters

The efficiencies and the loss growth come from `plant/2`; `microgrid fit-battery` fits
them from your meters. The wear defaults to 0, which prices cycling as free; set it
([Describe your plant](../how-to/describe-plant.md#the-battery-parameters)).
