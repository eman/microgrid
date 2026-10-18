# Write your own plant model

The library's `Plant` covers the topologies `plant/2` describes. If your plant behaves
in a way it cannot express (a dispatch rule of its own, a battery chemistry with a
different conversion), you can bring your own plant model. The price rule asks only the
questions of the port (`microgrid.engine.extend.PlantModel`), so any object that answers
them can stand in for `Plant` ([The plant model](../concepts/plant-model.md)).

## What a plant model answers

| Member | What |
|---|---|
| `project(starts, durations, pv_kw, load_kw, soc, *, dc_load_kw, held_at_start, stop)` | the plant's flows slot by slot (`SlotFlow`), from the SOC given, as it would dispatch them |
| `storage` | the battery as the price rule asks about it (`Storage`); None without one |
| `has_pv`, `pv_arrays`, `pv_per_array` | its PV, and whether it takes PV per array |
| `import_limit_kw` | the grid's import limit; None where none binds |
| `has_dc_loads` | whether a load is on a hybrid's DC bus |
| `meters` | its meters, by name, to what each measures |
| `pv_from_meters`, `read_meters`, `metered_flow` | its meters read as PV, as flows, and as a slot for the live value |

`Storage.stored_per_kwh(source, flow)` is the one question the default price rule (the
stored cost, `battery_value = "stored_cost"`) asks of your battery: stored kWh per kWh
charged from `source` (`ChargeSource.PV`, `GRID` or `DC_PV`, in that source's own terms)
by the path it takes, averaged over the slot's charge, or at the battery's charge limit
without a slot. Answer it for your topology and your model has the stored cost; the
engine runs the average forward along your projections (`ledger.project_average`).

**The battery's lots** are an input you keep. The library's ledger
(`microgrid.engine.ledger.Ledger`) books metered charges through your `Storage`'s paths
when you give it your storage and each slot as your model's (`step(..., slot=)`), and
draws through a `plant.Battery` describing your battery's conversion; or keep the lots
with your own bookkeeping and hand them in as `Lots`.

The [API reference for `extend`](../reference/api/extend.md) documents every member,
`SlotFlow`'s fields, and `SlotDispatch`, the dispatch's bookkeeping the price rule reads
(whether the plant holds the battery, how far a top-up is from its target). A model
without that bookkeeping leaves it at its defaults.

## Start from the library's model

The shortest path is to wrap `Plant` and replace only what differs. This skeleton
delegates everything:

```python
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from microgrid.engine import Engine, EngineConfig
from microgrid.engine.extend import Flows, MeterReading, PlantModel, PvKw, SlotFlow, Storage
from microgrid.plant import Plant, examples
from microgrid.engine.testing import assert_plant_model_contract


class MyPlant:
    """A plant model that is ``base``, except where it says otherwise."""

    def __init__(self, base: PlantModel) -> None:
        self._base = base

    def project(
        self,
        starts: Sequence[datetime],
        durations: Sequence[timedelta],
        pv_kw: PvKw,
        load_kw: Sequence[float],
        soc: float | None,
        *,
        dc_load_kw: Sequence[float] | None = None,
        held_at_start: bool | None = None,
        stop: Callable[[int, SlotFlow], bool] | None = None,
    ) -> tuple[SlotFlow, ...]:
        # Your dispatch goes here.
        return self._base.project(
            starts, durations, pv_kw, load_kw, soc,
            dc_load_kw=dc_load_kw, held_at_start=held_at_start, stop=stop,
        )

    @property
    def storage(self) -> Storage | None:
        return self._base.storage

    @property
    def has_pv(self) -> bool:
        return self._base.has_pv

    @property
    def pv_arrays(self) -> tuple[str, ...]:
        return self._base.pv_arrays

    @property
    def pv_per_array(self) -> bool:
        return self._base.pv_per_array

    @property
    def import_limit_kw(self) -> float | None:
        return self._base.import_limit_kw

    @property
    def has_dc_loads(self) -> bool:
        return self._base.has_dc_loads

    @property
    def meters(self) -> Mapping[str, str]:
        return self._base.meters

    def pv_from_meters(self, reading: MeterReading, *, per_array: bool) -> PvKw | None:
        return self._base.pv_from_meters(reading, per_array=per_array)

    def read_meters(self, reading: MeterReading) -> Flows | None:
        return self._base.read_meters(reading)

    def metered_flow(
        self, start: datetime, soc: float | None, reading: MeterReading
    ) -> SlotFlow | None:
        return self._base.metered_flow(start, soc, reading)


base = Plant(examples.example_plant("ac_coupled_hybrid", ZoneInfo("Europe/Berlin")))
model = MyPlant(base)
assert_plant_model_contract(model)      # raises AssertionError naming what broke
engine = Engine(EngineConfig(model))    # the engine takes it as it takes Plant
```

It type-checks under `mypy --strict` as a `PlantModel`.

## Check it keeps the contract

`microgrid.engine.testing.assert_plant_model_contract(model)` runs the model over a few
synthetic days (no PV, a sunny day, a full battery under a heavy load; a DC-bus load
where the plant has one) and raises `AssertionError` where it breaks what the port
promises:

- every slot it was given, in order, the SOC it was given at the start and carried from
  slot to slot, and kept within 0-1;
- no flow negative; no more PV routed than there was; every load served, or reported as
  unserved;
- **each slot's energy balances**: PV and the battery and the grid in, loads, losses and
  the battery out;
- a what-if with more load is as long and serves that load; `stop` ends a projection
  early;
- a battery's capacity, reserve and saturation make sense, and its dead band is a band;
- **the stored cost runs over its projections**: each charge converts by a stored kWh
  per kWh in (0, 1.5], and the running average stays between nothing and the dearest kWh
  charged, starting at the lots' own.

It needs no test framework: call it from any test, or from a script.

## What the contract catches

Suppose `project` added a standing load the forecast leaves out:

```python
load = [kw + 0.1 for kw in load_kw]
return self._base.project(starts, durations, pv_kw, load, soc, ...)
```

The contract refuses it:

```text
AssertionError: the slot's energy balances: 0.09999999999999976 != 0.0
```

A plant model accounts for exactly the PV and load it is given. A load the forecast
misses belongs in the load forecast, not in the plant model, and the same goes for a PV
forecast that runs high: correct the input, not the plant.

## Then check it against the meters

The contract says the model is self-consistent. Whether it is *right* is a question
for your plant's meters. `microgrid check-plant` runs the library's `Plant`; for your
own model, call `microgrid.engine.analysis.ground_truth.project_against_meters` with a
`project` function that runs it (its one call into a plant model, `Projector`), as
`microgrid.cli.check_plant` does for `Plant` ([Check a plant model against its
meters](check-plant.md)).
