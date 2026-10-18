# Reference

The formats, the command and the API, each as the code defines it.

**Formats.** Each is versioned; a change that is not additive bumps the version.

| Format | What it holds | Read and written by |
|---|---|---|
| [`dataset/1`](dataset.md) | a directory of series and forecasts, the inputs and what the meters recorded | `microgrid.cli.dataset` |
| [`plant/2`](plant.md) | the plant's topology and parameters | `PlantConfig.from_mapping`, `to_mapping` |
| [`engine/2`](engine.md) | how the price is computed | `EngineConfig.from_mapping`, `to_mapping` |
| [`sources/1`](../configuration/data-sources.md) | where each input comes from | `microgrid.cli.sources.config.read_sources` |
| [`valuation/2`](valuation.md) | the outputs | `Valuation.to_mapping`, `LiveValue.to_mapping` |

**The command.** [`microgrid`](cli.md): every command and option.

**Causes and kinds.** [What `degraded` and `kind` say](causes.md).

**The API**, generated from the docstrings, one page per public module. Every other
module is private and may change without notice.

| Module | Holds |
|---|---|
| [`microgrid.engine`](api/engine.md) | `Engine`, `EngineConfig`, `Inputs`, the outputs and their enums, the versions |
| [`microgrid.engine.series`](api/series.md) | the form a client's data takes (`Series`, `Forecast`, `Price`, `Quantiles`), the operations on it, the slot grid |
| [`microgrid.engine.units`](api/units.md) | a series in the engine's units |
| [`microgrid.plant`](api/plant.md) | `PlantConfig` and its parts, `Plant`, the examples |
| [`microgrid.engine.ledger`](api/ledger.md) | the battery's lots and their cost split |
| [`microgrid.engine.analysis.scoring`](api/analysis-scoring.md) | what a kWh realised, over metered flows |
| [`microgrid.engine.analysis.ground_truth`](api/analysis-ground-truth.md) | a plant model's projection against the meters |
| [`microgrid.engine.extend`](api/extend.md) | the plant model port and the operations the engine is built from |
| [`microgrid.engine.testing`](api/testing.md) | `assert_plant_model_contract` |
| [`microgrid.cli.dataset`](api/cli-dataset.md) | `dataset/1`, read and written |
| [`microgrid.cli.inputs`](api/cli-inputs.md) | a dataset as the engine's `Inputs` |
| [`microgrid.cli.history`](api/cli-history.md) | a plant's recorded history, read through a port |
| [`microgrid.cli.offline`](api/cli-offline.md) | the library run on a dataset |
| [`microgrid.cli.sources.config`](api/cli-sources-config.md) | `sources/1`, read |
| [`microgrid.cli.sources.read`](api/cli-sources-read.md) | the inputs read now from the configured providers |
