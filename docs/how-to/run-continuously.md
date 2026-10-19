# Run it continuously

A client that publishes prices around the clock does four things: it records what it
receives, values on a schedule and when inputs change, serves the live value between
valuations, and says when it cannot vouch for its readings. The defaults below are the
`microgrid live` command's.

## 1. Record inputs as received

- **Keep every forecast issue**, with the time you received it, as a sample of that
  input's series. Do not overwrite the last one: the engine reads each input as it
  stood at the moment you ask about, which is what makes a later replay exact.
- **Keep measured values** (the SOC, [dispatch as
  modelled](../concepts/inputs.md#dispatch-as-modelled), the meters, the house load) as
  series, each value holding until the next.
- **Keep the battery's lots**: run its ledger (`microgrid.engine.ledger.Ledger`) slot by
  slot over the meters, and record each slot's `Lots` as it is booked. Without them a
  battery valuation prices all the battery holds as bought at the import and names
  `battery_lots` ([The battery's lots](../concepts/inputs.md#the-batterys-lots)).
- **Keep a [heartbeat](../concepts/inputs.md#heartbeat)**: the last time readings
  arrived. Set each measured series' [`known_until`](../concepts/series.md#a-series-values-that-hold) to it, so a quiet connection reads
  as unknown, not as a steady value.
- **Keep readings long enough**: for the live value, for [`EngineConfig.smoothing_window`](../methodology/live-value.md#smoothing)
  (50 minutes by default) before the moment, and the sample in force then. Prune older samples if memory
  matters ([`series.prune`](../reference/api/series.md)).

## 2. Value on a schedule and on change

Call `engine.value(inputs, at=now)`:

- **on a clock** (`microgrid live` values every 5 minutes), so a valuation is never old;
- **when an input changes**: a new forecast issue, new prices, a change of plant mode.

Give each valuation a `valid_until` (30 minutes in `microgrid live`). If a valuation is
`unpriced` (no price for the slot under way), keep the last one: past its
`valid_until`, the live value falls back by itself.

## 3. Serve the live value between valuations

Every reading (every 30 seconds in `microgrid live`), compute the
[live value](../methodology/live-value.md): `engine.live(inputs, at=now, valuation=...,
valid_until=...)` over the measured series, or, for readings that arrive one at a time,
`engine.live_session()` and `session.update(reading, valuation, valid_until)`.

## 4. Say what you cannot vouch for

When the heartbeat has been silent longer than you tolerate (`microgrid live`: the
dataset's `limits`, else 3 minutes, measured on one plant whose heartbeat reports every
few seconds), pass a cause of its own, for example `external=["telemetry"]`. The live
value then falls back to the import price and keeps that cause beside the engine's.

## A minimal client

Sources here are stand-ins (one issue of each forecast, a steady reading); replace them
with the real sources.

```python
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from microgrid.engine import Engine, EngineConfig, Inputs, LiveReading
from microgrid.engine.ledger import Amount, Lots, Source
from microgrid.plant import Plant, examples
from microgrid.engine.series import Forecast, Interval, Price, Quantiles, Sample, Series

plant = Plant(examples.example_plant("ac_coupled_hybrid", ZoneInfo("Europe/Berlin")))
engine = Engine(EngineConfig(plant))


class Record:
    """What the client has received: every forecast issue, every reading, as it arrived."""

    def __init__(self) -> None:
        self.issues: dict[str, list[Sample]] = {"prices": [], "pv": [], "load": []}
        self.readings: dict[str, list[Sample]] = {"soc": [], "dispatch": [], "lots": []}
        self.heard_at: datetime | None = None    # the telemetry path's last sign of life

    def issue(self, name: str, forecast: Forecast, received_at: datetime) -> None:
        self.issues[name].append(Sample(received_at, forecast))

    def reading(self, name: str, value: object, at: datetime) -> None:
        self.readings[name].append(Sample(at, value))
        self.heard_at = at

    def inputs(self, now: datetime) -> Inputs:
        vouched = self.heard_at or now    # known up to the last time the path was heard
        return Inputs(
            prices=Series(tuple(self.issues["prices"]), now),
            pv=Series(tuple(self.issues["pv"]), now),
            load=Series(tuple(self.issues["load"]), now),
            soc=Series(tuple(self.readings["soc"]), vouched),
            dispatch_as_modelled=Series(tuple(self.readings["dispatch"]), vouched),
            lots=Series(tuple(self.readings["lots"]), vouched),
        )


# A stand-in for the sources: one issue of each forecast, and the SOC.
start = datetime(2026, 10, 22, 6, 0, tzinfo=UTC)
slots = [start + timedelta(minutes=30 * k) for k in range(96)]


def flat[V](value: V, source: str) -> Forecast[V]:
    return Forecast(tuple(Interval(s, s + timedelta(minutes=30), value) for s in slots),
                    issued_at=start, source=source)


record = Record()
record.issue("prices", flat(Price(0.30, 0.07), "my-tariff"), start)
record.issue("pv", flat(Quantiles({0.1: 0.5, 0.5: 1.5, 0.9: 2.0}), "my-pv"), start)
record.issue("load", flat(0.6, "my-load"), start)
record.reading("soc", 0.5, start)
record.reading("dispatch", True, start)
# The battery's lots, from the ledger: here 6 stored kWh of solar, charged at export 0.07.
solar = Amount(6.0, 6.0 / 0.96 * 0.07)
record.reading("lots", Lots(start, {Source.PV: solar}, pool=solar), start)

# Value now, valid for 30 minutes; between valuations, the live value.
valuation = engine.value(record.inputs(start), at=start)
valid_until = start + timedelta(minutes=30)
session = engine.live_session()
for k in range(1, 21):                      # a reading every 30 s for 10 minutes
    at = start + timedelta(seconds=30 * k)
    live = session.update(
        LiveReading(at=at, load_kw=0.6, soc=0.5, dispatch_as_modelled=True,
                    meters={"pv": 1.4, "grid": 0.0, "battery": 0.8}),
        valuation,
        valid_until,
    )
print(valuation.kind, round(valuation.slots[0].marginal_cost, 4))
print(live.kind, live.degraded, round(live.marginal_cost or 0, 4))
```

```text
stored_value_v1 0.0752
stored_value_v1 () 0.07
```

`microgrid.cli.offline.live` is a complete client of this shape, run over a dataset
instead of live sources ([API](../reference/api/cli-offline.md)).
