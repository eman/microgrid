# Run your first valuation

The repository ships an example dataset: one synthetic week of an example plant, so no
hardware or accounts are needed.

## 1. Install

You need Python 3.14 and [uv](https://docs.astral.sh/uv/) ([Install](../install.md) has
the other ways).

```sh
git clone https://github.com/eman/microgrid.git
cd microgrid
uv sync
```

`uv sync` makes a virtual environment with the library, its `microgrid` command and the
development tools.

## 2. Look at the example

```sh
ls examples/dataset
cat examples/dataset/plant.toml
```

The example is a plant in Europe/Berlin with AC-coupled PV and a 12 kWh battery on a
hybrid inverter's DC bus, which the grid tops up overnight (01:00-05:00, to 60 %). Its
week runs from 2026-10-21 to 2026-10-27, with the change from summer to winter time on
2026-10-25 inside it. Its files are series and forecasts as a client would have recorded
them: prices, a PV forecast with quantiles, a load forecast, the SOC, the meters
([`dataset/1`](../reference/dataset.md)).

Check the plant's and engine's files:

```sh
uv run microgrid config check --data examples/dataset
```
```text
ok: the plant (12.0 kWh battery) and the engine
```

## 3. Value one moment

```sh
uv run microgrid value --data examples/dataset --at 2026-10-22T13:00 --slots 6
```
```text
valued at 2026-10-22 13:00 CEST: stored_value_v1, 285 slots, degraded: none
slot                     import   export  marginal  surplus kW  regime
2026-10-22 13:00 CEST    0.3000   0.0700    0.0700       0.000  battery
2026-10-22 13:10 CEST    0.3000   0.0700    0.0700       0.000  battery
2026-10-22 13:20 CEST    0.3000   0.0700    0.0700       0.000  battery
2026-10-22 13:30 CEST    0.3000   0.0700    0.0700       0.000  battery
2026-10-22 13:40 CEST    0.3000   0.0700    0.0700       0.000  battery
2026-10-22 13:50 CEST    0.3000   0.0700    0.0700       0.000  battery
```

The first line gives the valuation's kind: `stored_value_v1` means every input could be
used, so the values are the model's, not the fallback ([Causes and
kinds](../reference/causes.md#kinds)). The time has no offset, so it is the plant's local
time. The command read the dataset
as a client would have had it at 13:00: the forecasts received by then, the SOC in force
then.

## 4. Read the result

- **285 slots**: the horizon runs 48 hours from the slot under way, cut where the load
  forecast received by then ends.
- **import 0.30, export 0.07**: the input prices for the slot.
- **marginal 0.0700**: one more kWh used now costs the export price, well under the
  import price. PV is charging the battery, and a kWh used now would take PV from it:
  solar, which costs the export the site gives up by using it
  ([why](../methodology/stored-value.md#the-dead-band)).
- **surplus 0.000**: nothing is spilling; the battery takes all the spare PV.
- **regime battery**: one more kWh comes out of the battery's cycle.

A load bigger than the PV now charging the battery takes the rest from the battery,
whose energy cost more: the week's solar and the night's top-up at 0.18
([The supply stack](../methodology/stored-value.md#the-supply-stack)).

## 5. Try other moments

```sh
uv run microgrid value --data examples/dataset --at 2026-10-21T12:00   # a sunny midday
uv run microgrid value --data examples/dataset --at 2026-10-22T19:00   # the evening: 0.1284
uv run microgrid value --data examples/dataset --at 2026-10-25T02:30   # the hour that repeats
```

In the evening the battery serves the house with what it holds, mostly the day's solar:
a kWh costs 0.1284 against an import price of 0.42. The command reads what the battery holds from the
dataset's meters ([The battery's lots](../methodology/lots.md)).

On 2026-10-25 the clocks go back and 02:30 happens twice; a bare time is the first.
Give an offset to choose: `2026-10-25T02:30+01:00` is the second.

## 6. Watch the live value

```sh
uv run microgrid live --data examples/dataset --from 2026-10-22T13:00 --to 2026-10-22T13:05 --every 1m
```
```text
at                         marginal   import  surplus kW  regime   degraded
2026-10-22 13:00:00 CEST     0.0700   0.3000       0.000  battery  -
2026-10-22 13:01:00 CEST     0.0700   0.3000       0.000  battery  -
...
```

Each line is the valuation run again from the meters at that moment
([The live value](../methodology/live-value.md)).

## Next

- [Use the library from Python](python.md), to get every output as data.
- [Build a dataset](build-dataset.md) from your own plant's records.
- [Describe your plant](describe-plant.md) in `plant/2`.
