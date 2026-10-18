# The live value

**The live value is the price *now*, made from the meters between valuations: the latest
valuation run again for one slot, from what the plant is actually doing.** The client
decides how often to value and how often to read the meters
([Run it continuously](../how-to/run-continuously.md)).

## Why a live value

A valuation made at 13:00 assumed the 13:00 forecast. By 13:07 a cloud may have cut PV
in half, or the house may have switched on a kettle. The slot's price may no longer
hold.

## How it is made

`Engine.live(inputs, at=t, valuation=..., valid_until=...)`:

1. **Smooth the measured PV and [house load](../concepts/inputs.md#house-load)** over the series up to `t` (below).
2. **Run one slot from `t`** at the smoothed PV and load, from the measured SOC: a full
   slot wherever `t` falls, so the value does not jump as the slot runs out.
3. **Then the valuation's forecast** for the rest of the horizon, with the same PV
   scenarios.
4. **Price that first slot** by the same rule as the valuation.

**Surplus now** is what that one slot exports or curtails: PV beyond what the house
takes and the battery can store. While the battery is charging with room to spare, there
is none.

**The house importing now.** If the meters show the grid serving the house right now
(more than the small imports an inverter makes while holding its grid connection near
zero, `regulation_kw`), a kWh used now is imported too, for example while the plant holds
its battery in a way the model did not foresee. The live value then says so, at the
import price.

## Smoothing

Measured power is noisy: a passing cloud, a compressor starting. So PV and the house
load are averaged exponentially over time, with a time constant of 300 seconds by
default (`engine/2`'s `smoothing_seconds`; 0 turns it off).

- **Each value weighs by how long it held.** The average is of the measured signal
  itself, whatever the samples' spacing, so the value at a moment does not depend on
  how often it is asked for.
- **A stray reading must be confirmed.** A change in house load larger than
  `load_jump_kw` (0.5 kW) is held back until a later reading agrees with it, or it has
  held for 30 seconds, and is taken from then on.
- **The average restarts at a gap** and is not trusted until it has run for one time
  constant. A spike right after a start or a gap is not published at full weight.
- **The SOC is not smoothed.** SOC readings move in small steps and rarely reverse, so
  an average would only add lag: up to about 2.5 percentage points at full charge rate.

A client keeps its readings for `EngineConfig.smoothing_window` (ten time constants)
before the moment, and the one in force then; what came before weighs too little to
matter.

## Warm-up

Right after a client begins reading, nothing has been measured for long enough. The
live value then names `smoothing`:

- **The first warm-up**, with nothing else wrong, keeps the valuation's price for the
  slot and offers no surplus: the price never rested on the measurement, and no surplus
  has been measured yet.
- **A warm-up after a gap** falls back to the import price. A gap means the client lost
  track of the plant; until the averages settle again, the conservative answer holds.

## When the valuation is too old

Each valuation is valid until a time the client sets. Past it, the live value names
`issue` and falls back to the import price, though the valuation's prices still serve
as the import and export prices of the slot. A valuation of any age is never run again
as current; that is why `valid_until` is required with one.

## For a client that reads one value at a time

`Engine.live` takes series. A client that receives readings one at a time (a push
stream, a polling loop) keeps them with `engine.live_session()`, whose `update(reading,
valuation, valid_until)` adds each reading to the series and returns the live value.
Both give the same answer for the same readings.

See [Missing and stale inputs](degradation.md) for every way the live value falls
back, and [Use the library from Python](../how-to/python.md) for code.
