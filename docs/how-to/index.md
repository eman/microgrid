# How-to guides

**Getting started**

- [Install](../install.md), then [run a first valuation](first-valuation.md) on the
  example dataset.

**A plant and its data**

- [Get prices from live sources](own-sources.md): a `sources/1` file, `microgrid
  sources` and `microgrid now`.
- [Configure a source](configure-source.md): each provider, with a working section.
- [Build a dataset](build-dataset.md) from a plant's records, with a worked CSV example.
- [Describe a plant](describe-plant.md) in `plant/2`: PV of either coupling, a
  battery on its own inverter or on a hybrid's DC bus, DC-bus loads, grid limits,
  grid-charge and export windows, meters.
- [Tune the engine](tune-engine.md): when to change an `engine/2` setting.

**From Python**

- [Use the library from Python](python.md): `Inputs`, `Engine.value`, `Engine.live`,
  `to_mapping`.
- [Use the prices in a scheduler](scheduler.md): which output answers which question,
  and a worked decision.
- [Run it continuously](run-continuously.md): record inputs, value, serve the live value.
- [Write a plant model](own-plant-model.md), and check it keeps the contract.

**Checking the models**

- [Replay and score a period](replay.md): valuations at past times against what the
  meters recorded.
- [Check a plant model against its meters](check-plant.md).
- [Read the battery's lots](ledger.md): where its energy came from and what it cost.
- [Fit the battery's conversion](fit-battery.md) from the meters.
