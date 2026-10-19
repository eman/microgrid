# `dataset/1`

A dataset is a directory of series in the library's own terms, so every `microgrid`
command runs without the plant it came from. The format is the library's
(`microgrid.cli.dataset`): whatever recorded the data, a client writes it out as
these files. [Build a dataset](../how-to/build-dataset.md)
walks through making one.

```text
my-dataset/
  manifest.json          what each entry is, and where its file is
  plant.toml             the plant (plant/2)
  engine.toml            optional: the engine's settings (engine/2)
  series/soc.json        a series of values
  forecasts/prices.json  a series of forecasts, as they were received
  ...
```

## `manifest.json`

```json
{
  "format": "dataset/1",
  "entries": {
    "soc":       {"file": "series/soc.json",       "kind": "number", "forecasts": false},
    "prices":    {"file": "forecasts/prices.json", "kind": "price",  "forecasts": true}
  }
}
```

| Field | Meaning |
|---|---|
| `format` | always `"dataset/1"` |
| `entries` | each entry by its name |
| `entries.<name>.file` | the entry's file, relative to the directory; it must stay inside it |
| `entries.<name>.kind` | how its values read: `number`, `bool`, `price` or `quantiles` |
| `entries.<name>.forecasts` | `true` for a series of forecasts, `false` for a series of values |
| `limits` | optional: how the record's silences are judged ([below](#limits)) |

A name is letters, digits, `_`, `-` and `.`.

## `limits`

Optional, each key too: how long a silence the record's recorder can have before what it
recorded is unknown. A command's flag wins over it; where neither says, the default
holds.

| Key | Default | Meaning |
|---|---|---|
| `telemetry_seconds` | 180 | the meters are unknown once the `heartbeat` has been silent this long (`--stale-after`) |
| `dc_stale_after_seconds` | 300 | a DC-bus load's reading other than 0, silent this long, is stale: the 0 that ended its draw was lost (`--dc-stale-after`) |

The defaults were measured on one plant (its heartbeat reported every few seconds and was
silent at most 157 s in a week; its DC charger wrote every 5 s while it drew, and was
silent at most 103 s). A dataset from another recorder states its own:

```json
{"format": "dataset/1", "limits": {"telemetry_seconds": 600}, "entries": {}}
```

## Values by kind

| Kind | A value | Example |
|---|---|---|
| `number` | a finite number | `0.42` |
| `bool` | `true` or `false` | `true` |
| `price` | import and export per energy unit | `{"import": 0.30, "export": 0.07}` |
| `quantiles` | any quantiles between 0 and 1 that include the median, `"0.5"` | `{"0.1": 1.7, "0.5": 2.8, "0.9": 3.4}`, `{"0.25": 2.1, "0.5": 2.8, "0.75": 3.2}` or `{"0.5": 2.8}` |

`null` in place of a value means no value from that sample to the next: the source
reported none, or the writer cannot vouch for that stretch.

## A series file (`forecasts: false`)

```json
{
  "unit": "%",
  "known_until": "2026-10-27T23:00:00+00:00",
  "samples": [
    ["2026-10-20T22:00:00+00:00", 50.0],
    ["2026-10-20T22:10:00+00:00", 49.3],
    ["2026-10-21T03:00:00+00:00", null]
  ]
}
```

- **`samples`** are `[time, value]` pairs in time order, each value holding until the
  next ([Series](../concepts/series.md#a-series-values-that-hold)). Many recorders store
  only changes; write their rows as they are, without filling in repeats.
- **`known_until`** is how far the writer vouches that no change went unreported. No
  sample may be later than it. Past it, the series has no value.
- **`unit`** is the source's unit, converted on reading (*Units*, below).

## A forecasts file (`forecasts: true`)

Each forecast issue, as the client received it:

```json
{
  "unit": "kW",
  "known_until": "2026-10-27T23:00:00+00:00",
  "issues": [
    {
      "received_at": "2026-10-21T04:00:30+00:00",
      "issued_at": "2026-10-21T04:00:00+00:00",
      "source": "my-pv-forecast",
      "intervals": [
        ["2026-10-21T13:00:00+00:00", "2026-10-21T13:30:00+00:00", {"0.1": 1.68, "0.5": 2.80, "0.9": 3.36}],
        ["2026-10-21T13:30:00+00:00", "2026-10-21T14:00:00+00:00", {"0.1": 1.35, "0.5": 2.25, "0.9": 2.70}]
      ]
    },
    {"received_at": "2026-10-21T05:00:30+00:00", "issue": null}
  ]
}
```

| Field | Meaning |
|---|---|
| `received_at` | when the client had the issue: what the engine reads it by |
| `issued_at` | the source's own stamp: what the issue's age is judged by |
| `source` | a label, for the record |
| `intervals` | `[start, end, value]`, half-open `[start, end)`, sorted, not overlapping; a gap is unknown, never an error |
| `"issue": null` | the source answered nothing at `received_at`; the issue before it stays in force until it is too old |

## Times

ISO 8601 with an offset, every one. They are read as UTC. A time without an offset is
an error.

## Units

Each file states its source's unit. Reading the dataset converts it to the engine's and
refuses a unit it does not know ([Units](../concepts/units.md) lists them).

## The names

The names are conventional; [Inputs](../concepts/inputs.md) defines what each holds and
which command needs which.

| Name | Kind | Forecasts | What it holds |
|---|---|---|---|
| `prices` | `price` | yes | the prices as received |
| `pv_forecast`, or `pv_forecast.<array>` for each array | `quantiles` | yes | the PV forecast |
| `load_forecast` | `number` | yes | the AC loads' forecast |
| `dc_load_forecast` | `number` | yes | optional: the DC-bus loads' forecast |
| `soc` | `number` | no | the battery's SOC |
| `battery_first` | `bool` | no | whether the plant runs the dispatch its model describes (the engine's `dispatch_as_modelled`); without it a plant with a battery falls back, naming `work_mode`. A plant that always does records one `true` sample |
| each of the plant's meters, by its `plant/2` name (default `pv`, `grid`, `battery`, `dc_load`) | `number` | no | the meters |
| `house` | `number` | no | optional: the AC loads' metered draw |
| `heartbeat` | `number` (any values) | no | optional: any series whose samples prove the readings were flowing; without one, each series is trusted up to its `known_until` |
| `import_price`, `export_price` | `number` | no | optional: the prices in force as recorded; without them, the ledger takes those the price forecasts put in force |
| `inverter` | `number` | no | optional: the AC power at the battery's converter, for `fit-battery` |

The names are kept as `dataset/1` gave them (`battery_first`, `inverter`); a new name
would be a new format.

`pv_forecast.<array>` joins the entry name and the array's name with a dot, for a plant
whose arrays have both couplings. `plant/2`'s default PV meters for such a plant are
named `pv:<array>`, with a colon, which a dataset name cannot hold: name those meters
in `plant/2` (`[[meters]]`, for example `pv_roof`) to record them in a dataset.
