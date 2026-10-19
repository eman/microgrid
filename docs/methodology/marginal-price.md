# The marginal price

**The library prices one more kWh: what a flexible load's kWh in a slot costs the site,
by the source that would serve it.** PV that would have spilled costs the export given
up; an import costs the import price; a kWh from the battery costs [what the energy in
the battery cost](stored-value.md).

## A flexible load needs the next kWh's cost

A load that can run at 13:00 or at 20:00 does not care what the house's energy cost on
average. It cares what *its* kWh will cost in each slot. That is a marginal question:
which source gives the kWh, and what does that source's energy cost?

Two things make the answer differ from the price on the tariff:

- **A kWh of PV is not free.** If the plant would have exported it, using it costs the
  export you gave up. If it would have been curtailed, it costs nothing.
- **A battery moves energy in time.** A kWh used at 20:00 may come out of the battery,
  which holds the afternoon's solar, or the night's grid energy, or both. The kWh costs
  what that energy cost, through the battery's losses and wear.

So the library projects the plant forward to see which source serves each slot, and
what the battery will hold then.

## Why not the average cost?

An average says what the energy that served the house cost, slot by slot, all sources
blended. It fails in two ways.

**At night it has no answer.** The house runs on the battery. No source is producing in
the slot, so the average cost of the slot's sources is 0/0. The battery needs a price of
its own: what the energy it holds cost.

**It flattens the signal a load needs.** Take a day with a 20 kWh house load and a
battery, an import price of 0.30 all day, export at 0.07, and losses ignored:

| Day | What happens | The price of a kWh | Imports billed | PV used, at the export it gave up |
|---|---|---|---|---|
| **Sunny** | the battery fills by 14:00 and spills; it carries the house through the night | 0.07 in every slot: the sun, or the battery's solar | 0 | 20 × 0.07 = 1.40 |
| **Short** | 12 kWh of PV, none spilled; the plant buys 8 kWh at 0.30 | 0.07 where the sun or the battery's solar serves it, 0.30 where the grid does | 8 × 0.30 = 2.40 | 12 × 0.07 = 0.84 |

On the short day the average cost is (2.40 + 0.84) / 20 = 0.162 in every slot, morning
and evening alike. It hides the fact that matters: some hours run on the sun's energy
and some on the grid's, and a load that runs in the first costs the site 0.07 a kWh, in
the second 0.30.

The battery's price is an average too, but of one thing: the energy in the battery, the
source the next kWh would come from. Which lot of that energy a kWh is "drawn" from is
bookkeeping, so the library takes the battery's running average
([why](stored-value.md#how-v-moves)).

## Does the money add up?

Charge every kWh the house used at its price, and compare with what the site actually
paid: the imports billed, plus the PV used at the export it gave up.

| Day | Load at its price | Imports + PV used | Difference, *R* |
|---|---|---|---|
| Sunny | 20 × 0.07 = 1.40 | 0 + 1.40 | **0** |
| Short | 12 × 0.07 + 8 × 0.30 = 3.24 | 2.40 + 0.84 | **0** |

Losses ignored, the books balance exactly: each kWh pays what its own energy cost. With
losses they still balance: a battery kWh's price includes the loss of the round trip,
and the site paid for that loss in the extra solar or grid energy it stored. What is
left is small and named:

- **the discharge's wear**, which the load pays and no bill shows: the battery's own
  account, for its life;
- **the fill's bound**, where the sun will refill the battery and a kWh is priced below
  what its energy cost ([the opportunity cost](stored-value.md#the-opportunity-cost-of-a-full-battery)):
  the load pays less than the energy cost, and the site keeps a kWh of the sun it would
  otherwise have spilled;
- **the forecast's errors**: the projection said the battery would hold one thing, and
  it held another.

The [make-up alternative](stored-value.md#the-make-up-alternative) prices the battery's
kWh by what replaces it instead. On the short day it says every kWh costs 0.30, because
any kWh is made up by an import: the load pays 6.00 against 3.24, and *R* = 2.76 is the
battery's arbitrage, the solar it stored at 0.07 serving the house in place of imports
at 0.30.

## Each slot is priced as an unannounced kWh

Each slot's price assumes the plant runs as forecast up to that slot, and asks what one
more kWh *there* costs. A load that is already planned is in the load forecast. Two
loads that both read a slot's price and both run there will each have assumed the other
was absent; the [supply stack](stored-value.md#the-supply-stack) says how much power
each price holds for, so a scheduler can see when a load is too big for the margin.

## The general rule

> The marginal price of a slot is what one more kWh used in it costs the site: the
> export given up for PV that would have spilled, the import price for the grid, and
> for the battery what the energy it holds cost, through its losses and wear, but no
> more than the sun that will refill it. All of it under the plant's own dispatch,
> given the prices, the PV and load forecasts and the battery's state and lots now.

"Under the plant's own dispatch" matters: which source serves a slot, and what the
battery will hold then, depend on what the plant will actually do with the battery, not
on what an optimal controller could do with it
([The plant model](../concepts/plant-model.md#it-models-the-plants-own-rules)).
