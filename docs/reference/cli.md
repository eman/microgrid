# The `microgrid` command

`microgrid` runs the library on a dataset (`dataset/1`, [the format](dataset.md)), or
on inputs read now from the providers a [`sources/1`](../configuration/data-sources.md)
file names, and prints what the engine gives back.

```text
microgrid {value,now,sources,fetch,live,check-plant,ledger,replay,fit-battery,config,schema} ...
```

## Conventions

**Times.** Every time is ISO 8601. A time with an offset is taken as given. A time
without one is the plant's local time, in the zone its `plant/2` file names. Where the
clocks go back and an hour repeats, a bare time is the first of the two. A bare time
the clocks skip is refused. Printed times are the plant's local times.

**Durations** (`D`) are a number and a unit: `30s`, `5m`, `1h`, `2d`. They must be
positive.

**Spans.** `--from` is included and `--to` is not, except in `check-plant`, whose
`--from` and `--to` are local days, both included.

**The plant and the engine.** Every command that reads a dataset takes its plant from
the dataset's `plant.toml` and its engine settings from `engine.toml` (the library's
defaults where there is none). `--plant FILE` and `--engine FILE` run it with others.
Every command works on the engine's slot (`slot_minutes`), the ledger and the scoring
included.

**Exit status.** 0 on success. 1 with `error: ...` on standard error for a dataset, a
configuration or an argument the command cannot use; `value` also exits 1 when there is
no price for the slot under way.

## Options every dataset command takes

| Option | Meaning |
|---|---|
| `--data DIR` | the dataset (required) |
| `--plant FILE` | a `plant/2` file in place of the dataset's `plant.toml` |
| `--engine FILE` | an `engine/2` file in place of the dataset's `engine.toml` |

Every command that reads the meters (`value` and `live` for the battery's lots, `replay`,
`check-plant`, `ledger`, `now`) also takes these; `fit-battery` accepts them, but reads
no heartbeat, so they do not change its result. Each defaults to the dataset's
[`limits`](dataset.md#limits), else the library's default, measured on one plant: set
the recorder's. Which dataset entries each command needs is in
[Inputs](../concepts/inputs.md#which-command-needs-which).


| Option | Default | Meaning |
|---|---|---|
| `--stale-after D` | the dataset's, else `3m` | the meters are unknown once the heartbeat (where the dataset has one) has been silent this long |
| `--dc-stale-after D` | the dataset's, else `5m` | for a plant with a DC-bus load whose meter reports only on change: a reading other than 0 counts as stale after this long without a new one |

## `value`

One valuation at a moment: the slots of the horizon from `--at`, each with its import
and export price, its marginal cost, its surplus and its regime.

```text
microgrid value --data DIR [--plant FILE] [--engine FILE] --at T [--slots N]
                [--stale-after D] [--dc-stale-after D]
```

| Option | Default | Meaning |
|---|---|---|
| `--at T` | required | the moment valued |
| `--slots N` | `12` | how many slots to print |

The dataset is read as a client would have had it at `T`: forecasts received by then,
measured values in force then. The first line names the valuation's kind, the number
of slots and the causes in `degraded` (the command's own after the engine's:
`dc_load_forecast` where it assumed 0 kW for DC-bus loads with no forecast), and says
when a gap in the load forecast cut the horizon short. Where the dataset cannot give the
battery's lots, standard error says why (`no battery lots: ...`) and the valuation names
`battery_lots`. A load forecast that simply ends early (Predbat's, at local midnight)
shortens the horizon without that note: the end of a forecast is not a gap.

```text
$ uv run microgrid value --data examples/dataset --at 2026-10-22T13:00 --slots 3
valued at 2026-10-22 13:00 CEST: stored_value_v1, 285 slots, degraded: none
slot                     import   export  marginal  surplus kW  regime
2026-10-22 13:00 CEST    0.3000   0.0700    0.0700       0.000  battery
2026-10-22 13:10 CEST    0.3000   0.0700    0.0700       0.000  battery
2026-10-22 13:20 CEST    0.3000   0.0700    0.0700       0.000  battery
```

## `live`

The live value over a span, as a client that reads the meters every `--every` and
values the dataset again every `--issue-every` would have published it.

```text
microgrid live --data DIR [--plant FILE] [--engine FILE] --from T --to T
               [--every D] [--issue-every D] [--valid-for D] [--stale-after D] [--dc-stale-after D]
```

| Option | Default | Meaning |
|---|---|---|
| `--from T`, `--to T` | required | the span: `--from` included, `--to` not |
| `--every D` | `30s` | a reading every `D` |
| `--issue-every D` | `5m` | a new valuation every `D` |
| `--valid-for D` | `30m` | each valuation valid this long; past it, the live value names `issue` |
| `--stale-after D` | the dataset's limits, else `3m` | the meters unknown after `D` without a heartbeat |

Each line is one live value: its time, marginal cost, import price, surplus, regime and
causes (the client's own first, then the engine's). See [The live
value](../methodology/live-value.md).

## `replay`

Valuations at past times, scored against what the meters then recorded: the surplus
against the PV the plant exported or curtailed, the marginal cost against what a kWh
realised by the same rule over the metered flows, and the regime against the metered
one. Only slots inside the span are scored.

```text
microgrid replay --data DIR [--plant FILE] [--engine FILE] [--stale-after D]
                 [--dc-stale-after D] --from T --to T [--every D]
```

| Option | Default | Meaning |
|---|---|---|
| `--from T`, `--to T` | required | the span: `--from` included, `--to` not |
| `--every D` | `1h` | a valuation every `D` |

See [Replay and score a period](../how-to/replay.md).

## `check-plant`

The plant model against the plant's own meters: fed what was metered (PV, the load the
plant served, the SOC at the start), from each local midnight for the day and from each
hour over 48 h, and compared with the meters.

```text
microgrid check-plant --data DIR [--plant FILE] [--engine FILE] [--stale-after D]
                      [--dc-stale-after D] --from FIRST --to LAST
```

| Option | Meaning |
|---|---|
| `--from FIRST` | the first local day (`2026-10-21`) |
| `--to LAST` | the last local day, included |

It reports measures, not a pass or fail. See [Check a plant model against its
meters](../how-to/check-plant.md).

## `ledger`

The battery's lots over a span: what it was charged with, by source and cost, what it
gave to the house and to export, what it holds at the end and at what cost per kWh, and
the energy the meters show that the model does not explain.

```text
microgrid ledger --data DIR [--plant FILE] [--engine FILE] [--stale-after D]
                 [--dc-stale-after D] --from T --to T
```

The span: `--from` included, `--to` not. See [Read the battery's lots](../how-to/ledger.md).

## `fit-battery`

The battery's conversion and the inverter's own draw, fitted from the meters.

```text
microgrid fit-battery --data DIR [--plant FILE] [--engine FILE] [--stale-after D]
                      [--dc-stale-after D] --end END [--days DAYS]
```

| Option | Default | Meaning |
|---|---|---|
| `--end END` | required | the local day the window ends at; a dataset has no "now", so the command refuses to run without it |
| `--days DAYS` | `30` | the window, in days |

See [Fit the battery's conversion](../how-to/fit-battery.md).

## `now`

A valuation at the current time, from the inputs the sources file names, each read once
now. It prints as `value` does; an input that could not be read is named on standard
error, and the valuation names its cause. For a plant with a battery it gives the engine
the battery's lots: the ledger over the recorded history of the `[measured]` entities
before now, as `fetch` reads it; where it cannot (no `[measured]` meters or recorded
prices, no heartbeat or house meter), standard error says why
(`not read: battery_lots: ...`) and the valuation names `battery_lots`. Needs the
`sources` extra.

```text
microgrid now --sources FILE [--slots N] [--save DIR] [--lots-run-in D]
```

| Option | Default | Meaning |
|---|---|---|
| `--sources FILE` | required | a `sources/1` file |
| `--slots N` | `12` | slots shown |
| `--save DIR` | none | also write the inputs read as a dataset, with the plant's and engine's files; re-run it with `value --at` the read's time |
| `--lots-run-in D` | `2d` | how far back the ledger starts: what the battery held then is of unknown origin, priced as bought. Two days drew that down on one plant's record; a battery held full for longer keeps some |

## `sources`

Reads every configured input now and prints one line per input: `yes` and its provider,
or `no` and why. Exits 1 if any input was not read. Needs the `sources` extra.

```text
microgrid sources --sources FILE
```

## `fetch`

Writes what the `[measured]` section's entities recorded over a span as a dataset, for
`check-plant`, `ledger` and `fit-battery`. Forecasts are not fetched. Needs the `sources`
extra.

```text
microgrid fetch --sources FILE --from T --to T --out DIR
```

| Option | Meaning |
|---|---|
| `--sources FILE` | a `sources/1` file with `[measured]` and `[home_assistant]` |
| `--from T`, `--to T` | the span: `--from` included, `--to` not |
| `--out DIR` | the dataset's directory |

See [Data sources](../configuration/data-sources.md#the-commands).

## `config check`

Reads a plant's file and an engine's and reports every problem with its path, or `ok`.

```text
microgrid config check [--data DIR] [--plant FILE] [--engine FILE]
```

With `--data`, the dataset's `plant.toml` and `engine.toml`; `--plant` and `--engine`
name others.

```text
$ uv run microgrid config check --data examples/dataset
ok: the plant (12.0 kWh battery) and the engine
```

## `schema`

The outputs' JSON Schema, [`valuation/2`](valuation.md).

```text
microgrid schema [--write DIR]
```

Without `--write` it prints the schema; with it, it writes
`valuation-2.schema.json` into `DIR`.
