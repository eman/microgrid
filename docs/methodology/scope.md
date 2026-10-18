# Scope

**The library models the plant and produces marginal prices.** Its inputs come from the
client, its prices go to the client, and what the client does with them is the
client's.

## What a client brings

| The library needs | Where it comes from |
|---|---|
| import and export prices, per interval | any source of prices, or a file |
| a PV forecast, with quantiles or a single value | any solar forecast, or a file |
| a load forecast | any forecast of the house's load, or a file |
| the battery's SOC and the meters | the plant's own monitoring, a recorder, a file |
| the plant's topology (`plant/2`) | you, once |

Each is a series ([Inputs](../concepts/inputs.md)). None of the sources is assumed:
a [`sources/1` file](../configuration/data-sources.md) names a provider per input, or a
client hands the series over from a [dataset](../reference/dataset.md) or Python.

## Advisory only

The prices are advice. The library decides nothing and drives no device: it does not
tell the battery to charge, start a load, or change the plant's mode. The plant model
describes what the plant does on its own rules, and the price says what a kWh costs
under those rules.

## What it can't model

- **Tariffs beyond a per-kWh price.** Prices are an import and an export price per kWh,
  per interval. Demand charges, standing charges, tiered or block rates and netting over
  a billing period are not modelled, because none of them is a price on one more kWh in
  one slot. A standing charge does not change with use, so it never affects a marginal
  price. A demand charge prices the peak kW over a billing period, which a price per kWh
  cannot express; at most a client can raise the import price in hours it expects a new
  peak, an approximation of its own.
- **A plant run by an outside optimiser or controller.** The plant model describes the
  plant's own self-consumption rules. When something else drives the battery, the plant
  is not running [as modelled](../concepts/inputs.md#dispatch-as-modelled), and every
  valuation falls back to the import price. To price such a plant, run it in a mode the
  model describes, or [write a plant model](../how-to/own-plant-model.md) that projects
  what your controller will do.
- **Topologies checked against real meters: one.** The plant model has been checked
  against one real plant's meters: AC-coupled PV with a battery on a hybrid's DC bus and
  a load on that bus. Every other topology `plant/2` accepts runs through the same
  dispatch but has not yet been checked; [check yours](../how-to/check-plant.md).

## Prices energy, models no load

No load's run is priced, and none is taken out of the house load: the load is the
plant's metered consumption, as it is. A load on the plant's DC bus is in the plant
model only as topology: where it connects.

## No I/O

The library fetches nothing, publishes nothing and stores nothing, so the same inputs
give the same answer wherever it runs, and a replay at a past moment is exactly what a
live client would have computed then. The `microgrid` command and any service that
publishes prices are clients of it.
