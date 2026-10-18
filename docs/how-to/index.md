# How-to guides

**Getting started**

- [Install](../install.md), then [run your first valuation](first-valuation.md) on the
  example dataset.

**Your plant and your data**

- [Get prices from your own sources](own-sources.md): a `sources/1` file, `microgrid
  sources` and `microgrid now`.
- [Configure a source](configure-source.md): each provider, with a working section.
- [Build a dataset](build-dataset.md) from your own records, with a worked CSV example.
- [Describe your plant](describe-plant.md) in `plant/2`: PV of either coupling, a
  battery on its own inverter or on a hybrid's DC bus, DC-bus loads, grid limits,
  grid-charge and export windows, meters.
- [Tune the engine](tune-engine.md): when to change an `engine/2` setting.

**In your own code**

- [Use the library from Python](python.md): `Inputs`, `Engine.value`, `Engine.live`,
  `to_mapping`.
- [Use the prices in a scheduler](scheduler.md): which output answers which question,
  and a worked decision.
- [Run it continuously](run-continuously.md): record inputs, value, serve the live value.
- [Write your own plant model](own-plant-model.md), and check it keeps the contract.

**Checking the models**

- [Replay and score a period](replay.md): valuations at past times against what the
  meters recorded.
- [Check a plant model against its meters](check-plant.md).
- [Read the battery's lots](ledger.md): where its energy came from and what it cost.
- [Fit the battery's conversion](fit-battery.md) from the meters.
