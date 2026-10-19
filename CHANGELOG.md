# Changelog

Each user-visible change, under the release it ships in.

## [Unreleased]

### Added

- The Home Assistant integration's setup proposes what Home Assistant already knows: the
  price, PV and load forecast integrations installed (TariffKit, Nord Pool, Amber,
  Tibber, Octopus, ENTSO-E, OpenEI, Solcast, EMHASS, Predbat), each read at once where
  it needs nothing more; the measured entities and the battery's capacity the Energy
  dashboard names. Each meter's sign is checked against two days of the recorder's
  history and, where the records say it reads the other way, set on confirmation. A
  plant file and a `sources/1` file can be imported together. A plant is set up once.
- `microgrid.cli.signs`: each meter's sign, told from what the meters recorded (PV and
  DC-bus loads positive, the battery against its SOC, the grid by the balance).
- A Home Assistant integration (`custom_components/microgrid`, installed with HACS from
  a release's `microgrid.zip`): the plant (`plant/2`, pasted or built step by step),
  its measured entities and its sources configured in Home Assistant, each checked by
  the library and each source read once before it is kept. It publishes the valuation
  (its slots in an unrecorded attribute), the live value (marginal cost, surplus,
  regime, values by source), the prices in force and the battery's floor as entities,
  a problem sensor, and `microgrid.get_valuation` (`valuation/2`, the live value); runs
  the battery's ledger over the recorder's history; raises a repair issue for a source
  that keeps failing. *Run it in Home Assistant* documents it.
- Price readers for more Home Assistant integrations, in `sources/1`: Nord Pool
  (`from = "nordpool"`), Amber Electric (`amber`), Tibber (`tibber`), Octopus Energy
  (`octopus`), ENTSO-E (`entsoe`) and OpenEI (`openei`), each reading what the
  integration publishes. OpenEI publishes no rate past the next change, so its prices,
  and the valuation's horizon, end there.
- `from = "action"`: any input from an action whose response holds rows, as an entity's
  attribute would.
- A fixed `export` price per kWh, for prices whose source publishes the import alone
  (an entity's or an action's rows, and the named readers with no export price).
  Import and export prices from separate entities are split at each change of either.
- A forecast read from a source has each interval that starts or ends inside an engine
  slot split where the slots begin, so quarter-hour prices are priced at 10-minute
  slots, each slot at the mean of its pieces.
- `microgrid.cli.sources.api.HomeAssistantApi`: what a reader asks of Home Assistant.
  The readers take any client that answers it, and import without an HTTP client.
  `read.read_forecast`, `read.source_name`, `config.forecast_source` and
  `config.measured_source` are public.

### Changed (documentation)

- The documentation and the module docstrings no longer rebut what nobody said:
  denials, disclaimers and contrasts with an alternative the text never raised are gone
  ("not the average one", "never guessed", "not one class per topology", "fetches
  none", "Pure: no I/O", ...). A contrast stays where the text argues it, as *The
  marginal price* does the average cost.
  - *Scope* keeps what a client brings and what the library can't model; its *Prices
    energy, models no load* and *No I/O* sections are gone.
  - *The plant model*'s section is *It models the plant's own rules*; *Replay*'s is
    *What a replay reads*; *The make-up rule*'s *What the rule does not decide* is
    *Grid charging*. R-5 is *Marginal*.
  - Nothing is "yours to decide", "yours to add" or "the client's to correct": each
    says who does what. No "your": *Describe a plant*, *Run a first valuation*, *Write
    a plant model*, *Get prices from live sources*, and the text speaks of a plant, the
    meters, the recorder.

## [0.1.3] - 2026-10-18

### Changed (documentation)

- The README, the documentation's home page, the install page and the package
  docstrings no longer say that the plant model stands alone; how to use it without the
  prices is its how-to (*Use the plant model on its own*).

## [0.1.2] - 2026-10-18

### Fixed (documentation)

- The documentation's home page: *What makes it different* is *How it prices*; the
  parts name `microgrid.plant`, the plant model, apart from `microgrid.engine`; the
  line saying to install from the git repository is gone (it is on PyPI).

## [0.1.1] - 2026-10-18

### Changed (documentation)

- The README, the documentation's home page and the package's summary say what the
  library takes and returns, without the "data in, data out" slogan; why it does no I/O
  is in *Scope* (`docs/methodology/scope.md`).

## [0.1.0] - 2026-10-18

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

### Added (releases)

- **Versions and releases:** semantic versioning, explained beside the formats', the
  API's and the model's versions (`docs/reference/versions.md`); `microgrid --version`
  prints the release with the API (`engine-api/5`) and the model (`pricing/1.3`) it
  carries. Releases are published to
  PyPI from a tag (`vX.Y.Z`).

### Added (documentation)

- **The documentation:** the methodology and the library's requirements (R-1 to R-9),
  the concepts, how-to guides and the reference, published with each release at
  `https://eman.github.io/microgrid/`.
