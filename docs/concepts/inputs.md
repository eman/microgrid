# Inputs

Each input has one meaning, defined here, and a name on each surface: the entry in a
[dataset](../reference/dataset.md), the field in Python
([`Inputs`](../reference/api/engine.md), `LiveReading`), and the
[cause](../reference/causes.md) named when it cannot be used. Every input is a
[series](series.md).

## The names

| Input | Dataset entry | Python | Cause |
|---|---|---|---|
| [prices](#prices) | `prices` | `Inputs.prices` | `prices` |
| [PV forecast](#pv-forecast) | `pv_forecast`, or `pv_forecast.<array>` per array | `Inputs.pv` (a series, or a mapping by array name) | `pv_forecast` |
| [load forecast](#load-forecast) | `load_forecast` | `Inputs.load` | `load_forecast` |
| [DC-bus load forecast](#load-forecast) | `dc_load_forecast` | `Inputs.dc_load` | `load_forecast` |
| [SOC](#soc) | `soc` | `Inputs.soc`, `LiveReading.soc` | `battery_soc` |
| [dispatch as modelled](#dispatch-as-modelled) | `battery_first` | `Inputs.dispatch_as_modelled`, `LiveReading.dispatch_as_modelled` | `work_mode` |
| [meters](#meters) | each by the plant's meter name | `Inputs.meters`, `LiveReading.meters` | `pv_power`; a DC-bus load's meter: `load_power` |
| [house load](#house-load) | `house` | `Inputs.measured_load`, `LiveReading.load_kw` | `load_power` |
| [heartbeat](#heartbeat) (optional) | `heartbeat` | none: sets `known_until`, or a client's `external` cause | the client's own |
| [recorded prices](#recorded-prices) (optional) | `import_price`, `export_price` | none | none |
| [inverter AC power](#inverter-ac-power) (optional) | `inverter` | none | none |
| [the battery's lots](#the-batterys-lots) | none: from the meters, the SOC and the prices | `Inputs.lots` | `battery_lots` |


## Which command needs which

| Command | Needs |
|---|---|
| `value` | prices; the PV forecast (a plant with PV); the load forecast; the SOC and dispatch as modelled (a plant with a battery). A DC-bus load forecast is optional: without one, the command assumes 0 kW and names its own cause, `dc_load_forecast`, beside the engine's. The battery's lots come from `ledger`'s inputs where the dataset has them; without them, all the battery holds is priced as bought at the import, the valuation names `battery_lots`, and the command says why on stderr |
| `live` | `value`'s, and the plant's meters. The house load and a heartbeat are optional: without the house load the live value names `load_power`; without a heartbeat, nothing judges the telemetry path |
| `replay` | `value`'s, and the `pv`, `grid` and `battery` meters and the SOC. The prices the battery was charged at are the recorded prices where the dataset has them, else the price forecasts' (below); a heartbeat is optional |
| `ledger` | the `pv`, `grid` and `battery` meters, the SOC and the prices as `replay` takes them; a heartbeat is optional |
| `check-plant` | `ledger`'s, and the house load |
| `fit-battery` | the `pv`, `grid` and `battery` meters, the SOC, the house load and the inverter's AC power |

The commands that score against the meters need a plant with a battery and PV.

`now`, `sources` and `fetch` read their inputs from the providers a
[`sources/1`](../configuration/data-sources.md) file names instead of a dataset. `now`
gives the engine the battery's lots by running the ledger over the recorded history of
the `[measured]` entities (the two days before now, `--lots-run-in`); where it cannot, it
says why on stderr and the valuation names `battery_lots`.

## Prices

What one more kWh imported costs and one more exported earns, per interval: `Price(p_import,
p_export)`. Either may be negative, and the rule carries the sign through: with a
negative export price, a kWh that would have spilled earns money when a load uses it,
and its price is negative. Any shape works: a flat rate, time-of-use bands, a
dynamic price that changes every interval. The currency is yours ([Units](units.md)).

## PV forecast

The PV the plant will make, per interval, in kW in its arrays' terms: AC kW for
AC-coupled arrays, DC kW for DC-coupled ones. Each value is the forecast's quantiles:
any levels between 0 and 1 that include the median (0.5), such as Solcast's 0.1, 0.5
and 0.9, a forecaster's deciles, or the median alone (a point forecast). The scenarios
are cut by interpolating between the levels given; beyond the outermost, the outermost
step extends on a side that has a level, and the median holds on a side that has none.
Quantiles give the price its [scenarios](../methodology/scenarios.md); a single value
gives one day. A forecast with no level below the median says nothing of the downside:
the pessimistic case (`marginal_cost_p10`) is then the median, and the valuation names
`pv_spread`, its values standing. A
plant whose arrays have both couplings takes one forecast per array.

## Load forecast

What the house will draw, per interval, in kW: the AC side's loads. A plant with loads on
a hybrid's DC bus takes their forecast separately (`dc_load_forecast`), and the AC
forecast then leaves them out.

## SOC

The battery's state of charge, as a fraction 0-1 inside the library (a dataset may give
percent). A reading more than 0.01 outside 0-1 (one percentage point) is a mistake and
cannot be used; within that, it is clamped.

## Dispatch as modelled

**Whether the plant is running the dispatch its model describes.** The plant model
describes a self-consumption dispatch: PV serves the house, then charges the battery,
then is exported; the battery serves the house before the grid; plus whatever windows
`plant/2` declares. It is **true** while the inverter runs that way, and **false** when
it does not: an outside controller drives the battery, or the inverter is in a mode the
model does not describe, such as forced charging or backup only. False or unknown makes a
valuation fall back, because the model would be predicting a plant that is not running.
If your inverter only ever runs one matching mode, a constant `true` series is right.

## The battery's lots

**What the battery holds, by source, and what it cost** (`Lots`): the stored kWh from
PV, from the grid and of unknown origin, each with its cost, and all of it as one pool
whose running average cost prices a kWh the battery delivers ([What a stored kWh is
worth](../methodology/stored-value.md)). A client that keeps the battery's ledger
(`microgrid.engine.ledger`) gives them as a series: each `Lots` in force from when the
ledger booked it. The `microgrid` command runs the ledger over the dataset's meters
itself ([The battery's lots](../methodology/lots.md)), from the first whole slot its
meters cover: a moment before that has none; `microgrid now` runs it over the recorded
history before now.

Without lots, everything the battery holds is priced as bought, at the first slot's
import price, and a battery valuation or live value on the stored-cost rule names
`battery_lots` in `degraded`. It stays `stored_value_v1`: priced at the import, its
values are already the conservative ones, so they stand
([Missing and stale inputs](../methodology/degradation.md#what-happens)).

## Meters

The plant's own power readings, in kW, each named and signed as `plant/2` declares
([meters](../reference/plant.md#meters)): by default `pv`, `grid` (positive importing),
`battery` (positive charging, at its DC side) and, with a DC-bus load, `dc_load`. The live
value reads them for PV now and to tell whether the house is importing; the scoring
commands read what the plant did.

## House load

**What the plant's AC loads draw, as the plant meters it**, in kW: the figure many
inverters report as home consumption. It is not one of `plant/2`'s meters: those close
the plant's balance, and a `consumption` meter there is an alternative to the grid meter
for that. The house load is read on its own: by the live value (what the house draws
now) and by `check-plant` and `fit-battery` (the load the plant served).

## Heartbeat

**A series whose samples show the readings were flowing** (optional): any series that
reports at least every few minutes while the [telemetry path](../glossary.md#telemetry-path)
is up, such as a meter that reports every few seconds, or a tick the client writes. Its
values do not matter; only when its samples arrived. Many recorders store only changes,
so a steady SOC and a dead connection look the same in the record; the heartbeat tells
them apart. The commands judge the meters unknown once it has been silent longer than
the record's limit: `--stale-after`, else the dataset's
[`limits`](../reference/dataset.md#limits), else 3 minutes (measured on one plant whose
heartbeat reported every few seconds; set your recorder's).

**Without a heartbeat**, each series is trusted as far as the dataset says it is known
(`known_until`), and a gap in it is a sample with no value. That suits a record you know
has no unmarked gaps. If you only have meters that report on change, do not use one as
the heartbeat: a quiet meter would read as a dead connection.

## Recorded prices

The import and export prices in force, as recorded over time, each a plain series of
numbers (optional). The ledger books each kWh charged at them, and replay prices what a
kWh realised with them: each slot at the time-weighted mean of the price in force over
it, from when each sample says it took effect, so any price interval works on any slot
and no price is read before it took effect. **Without them**, the commands derive them
from the price forecasts as received: at each moment, the issue received last that
covers it, from its receipt on, as the engine reads prices. A dataset then carries its
prices once. Where both are given, the recorded series win: what was in force is what
the battery was charged at.

## Inverter AC power

The AC power at the battery's converter (its own inverter, or the hybrid it is folded
into), positive out to the house (optional). `fit-battery` alone reads it, to fit the
converter's loss.
