# Changelog

Each user-visible change, under the release it ships in. Nothing is released yet.

## [Unreleased]

The first version, under the MIT license.

### Added (library)

- **The plant model, `microgrid.plant`.** A plant described as data (`plant/2`): PV
  arrays of either coupling, a battery on its own inverter or on a hybrid's DC bus,
  loads on either bus, the grid's limits, grid-charge and export windows, hold rules
  and the meters. `Plant.project` runs it slot by slot. It stands alone: importing it
  loads none of the price code. Four example plants (`microgrid.plant.examples`), and
  a port for a plant model of your own (`microgrid.plant.port`: `PlantModel`,
  `Storage`), checked by `microgrid.engine.testing.assert_plant_model_contract`.
- **The price model, `microgrid.engine`.** Marginal prices from inputs given as time
  series (`Inputs`: prices, PV forecast quantiles, the load forecast, the SOC, the
  battery's lots). For each slot it gives:
  - the marginal cost, and the same for a dull day (the PV forecast's p10);
  - the regime, the certainties and the surplus;
  - the supply stack;
  - the value of a PV kWh and of a battery kWh diverted to a flexible load.

  `Engine.value` gives these as a valuation (`valuation/2`). `Engine.live_session`
  gives a live value between valuations.
  - **A battery kWh** is priced at the running average cost of what the battery holds
    (`Pricing.battery_value = "stored_cost"`), or by the make-up rule (`"replacement"`).
  - **Missing or stale inputs** move values toward the import price, and the valuation
    names them in `degraded`.
  - **Configuration:** `engine/2`.
- **Series and units** (`microgrid.engine.series`, `microgrid.engine.units`).
  - Forecasts are series of issues, read as a client had them at a moment.
  - Units are converted at the edge.
  - Money is in the prices' currency, never converted, and no name says one.
- **The battery's lots** (`microgrid.engine.ledger`): what the battery holds by source,
  and what it cost.
- **Analysis** (`microgrid.engine.analysis`): the plant model scored against its meters,
  and valuations scored against what each kWh realised.

### Added (command)

- **`microgrid` on a dataset** (`dataset/1`): `value`, `live`, `replay`, `check-plant`,
  `ledger`, `fit-battery`, `config check` and `schema`. A dataset whose prices name two
  currencies is refused.
- **`microgrid` on live sources** (`sources/1`, the `sources` extra): `sources`, `now` and
  `fetch`. Each input comes from a provider's Home Assistant integration (TariffKit,
  Solcast, EMHASS, Predbat) or an entity.
- **The example dataset** (`examples/dataset`): a synthetic week of an example plant in
  Europe/Berlin, across the DST change, with the outputs it is expected to give.

### Added (documentation)

- **The documentation:** the methodology and the library's requirements (R-1 to R-9),
  the concepts, how-to guides and the reference.
