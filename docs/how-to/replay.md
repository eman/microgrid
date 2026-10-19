# Replay and score a period

A replay values past moments from the inputs as they were known then, and scores each
valuation against what the meters recorded afterwards. It is how a change to the plant
file, the engine's settings or the model itself is judged: replay it over recorded
inputs.

## What a replay reads

`Engine.value(inputs, at=t)` reads only what was received by `t`: the forecast issues
received by then, the measured values in force then. So a replay at a past moment is
exactly what a live client would have computed at that moment, provided the dataset
holds every forecast issue as it was received ([Build a dataset](build-dataset.md)).

## Run it

```sh
uv run microgrid replay --data examples/dataset --from 2026-10-22 --to 2026-10-23
```

It values the dataset every hour in the span (`--every`, default `1h`) and scores every
slot that falls inside the span; past `--to`, nothing was metered.

```text
24 valuations every 1:00:00 scored, 1800 slots inside the span; fell back (not scored): 0; no prices: 0

surplus kW against the PV exported or curtailed, by hours ahead:
bucket       n     bias     MAE   p10 n   p10 met
0-1h        144   +0.000   0.000       0     0.0%
...

marginal cost against the price a kWh realised (USD/kWh), by hours ahead:
bucket       n  outcomes     bias     MAE    off
0-1h         30         5  +0.0056  0.0056   0.0%
1-3h         42         5  +0.0060  0.0060   0.0%
...

regime against the metered regime:
  0-1h     138 of 144 agreed
...
```

## Read it

Every score is split by how far ahead the slot was valued: 0-1 h, 1-3 h, 3-12 h,
12-48 h. Near-term errors matter most to a load deciding now.

- **Surplus** against the PV the plant in fact exported or curtailed. `bias` is forecast
  minus realised (positive: over-forecast). `p10 met` is the share of slots with a
  pessimistic surplus above zero where the realised surplus reached it. A p10 day is
  one in ten, so it should be met in about nine slots in ten.
- **Marginal cost** against **the price a kWh realised**: the same rule run over what
  the meters recorded, so the event that actually happened bounds the kWh, and on the
  default rule a battery kWh costs what the battery held then (the ledger over the
  meters). `n` is
  the slots scored; **`outcomes`** is how many distinct events they rest on (one fill
  prices every slot before it, so `n` overstates the independent evidence); **`off`** is
  the share of slots whose `certainty_spill` was on the wrong side of one half: priced
  as probably made up by spilled PV when it was not, or the reverse.
- **Regime** against the regime the meters show.

A valuation that fell back to the import price is counted, not scored: it is not a
forecast.

## Compare two versions

Replay the same span with each candidate and compare the scores:

```sh
uv run microgrid replay --data my-dataset --from 2026-09-01 --to 2026-10-01
uv run microgrid replay --data my-dataset --from 2026-09-01 --to 2026-10-01 --plant candidate.toml
uv run microgrid replay --data my-dataset --from 2026-09-01 --to 2026-10-01 --engine tuned.toml
```

The marginal-cost bias by hours ahead is the first number to watch: a model that is
consistently dearer or cheaper than what kWh realised is telling loads the wrong thing
the same way every day.

## From Python

The replay is `Engine.value` called at past moments; the scoring functions are the
library's (`microgrid.engine.analysis.scoring`: `realise`, `observed_flows`,
`observed_regime`) and the command's (`microgrid.cli.evaluation`). See the [API
reference](../reference/api/analysis-scoring.md).
