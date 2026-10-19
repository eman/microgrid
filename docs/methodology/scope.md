# Scope

**The library models the plant and produces marginal prices** from what a client
brings.

## What a client brings

| The library needs | Where it comes from |
|---|---|
| import and export prices, per interval | any source of prices, or a file |
| a PV forecast, with quantiles or a single value | any solar forecast, or a file |
| a load forecast | any forecast of the house's load, or a file |
| the battery's SOC and the meters | the plant's own monitoring, a recorder, a file |
| the plant's topology (`plant/2`) | you, once |

Each is a series ([Inputs](../concepts/inputs.md)). A [`sources/1` file](../configuration/data-sources.md) names a provider per input, or a
client hands the series over from a [dataset](../reference/dataset.md) or Python.

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
