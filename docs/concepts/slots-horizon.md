# Slots and the horizon

**A slot** is the unit of time the engine prices: 10 minutes by default
(`engine/2`'s `slot_minutes`). Slots are counted from the epoch in UTC, so every slot
starts on the same grid whatever the time zone: 13:00, 13:10, 13:20 UTC. The slot length
must divide an hour.

**The horizon** is how far a valuation reaches: 48 hours by default (`horizon_hours`),
from the slot under way. A valuation at 13:04 starts with the slot 13:00-13:10, of which
6 minutes remain; that first slot is valued for the time left in it.

## Where the horizon stops short

The horizon ends at the first slot an input cannot fill:

- **the prices**: past the last priced interval;
- **the PV forecast**: past the forecast's end;
- **the load forecast**: past its end. A gap inside it (a slot unknown with known slots
  after it) also cuts the horizon there, and the valuation says so with `horizon_cut`.

So a valuation can have fewer slots than 48 hours' worth. In the example dataset the
load forecast received by 11:00 UTC on 2026-10-22 ends at 10:30 UTC two days later, so
a valuation at 11:00 UTC (13:00 local) has 285 slots, not 288.

## Why fixed slots

- **One grid for every input.** A forecast's intervals are aligned onto the slots: an
  hourly price holds over six 10-minute slots; two 5-minute prices average into one.
  An interval that crosses a slot's edge without covering the slot (15-minute prices
  over 10-minute slots) is refused: prices so are `unpriced`, naming
  `prices`; choose a slot they tile (`slot_minutes = 15`, or 5). Every command, the
  scoring and the ledger among them, works on the engine's slot. See [Series and
  forecasts](series.md#aligning-onto-the-slots).
- **The noise floors are for 10-minute slots.** The default thresholds below which a
  metered flow is not an event were measured on 10-minute means
  ([`observed`](../reference/engine.md#observed-the-meters-noise-floors)). Another slot
  length may need others.

## A day

Where a rule needs "a day", it means 24 hours of slots: the [certainty that the battery
covers the expensive hours](outputs.md#certainties) compares prices with the cheapest
import in the 24 hours from the slot, and the [horizon rule](../methodology/make-up-rule.md#the-horizon-rule)
uses the horizon's last 24 hours.

## Times are UTC inside

The engine refuses a time that is not UTC. Python compares and subtracts two aware
times that share a time zone by wall clock, which is wrong across a daylight-saving
change. So a client converts at its edge, and the engine never sees local time. The one
exception is the plant's own clock: grid-charge and export windows are local times in
the zone `plant/2` names, because that is how a plant's settings are written.
