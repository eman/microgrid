# Tune the engine

The engine's settings (`engine/2`) say how the price is computed; the plant is
`plant/2`'s. Every field has a default, and `schema = "engine/2"` alone is the engine as it runs.
Change one when your data or your plant gives a reason; the
[reference](../reference/engine.md) lists them all.

Put the file beside the dataset as `engine.toml`, or pass it with `--engine`. In Python,
`EngineConfig.from_mapping(mapping, plant=plant)`.

## Your price source is fetched on a schedule

```toml
schema = "engine/2"
price_max_age_minutes = 60
```

By default prices never go stale. If your client fetches prices every hour, a fetch
that stops arriving should make the prices unusable after a while. Set the limit a
little above the fetch interval, and record each fetch as a new issue: some price
sources stamp unchanged rates with the time they first published them, and those would
otherwise look as old as the first fetch.

## Your forecasts arrive less or more often

```toml
schema = "engine/2"
pv_max_age_hours = 26
load_max_age_minutes = 120
```

A PV forecast older than `pv_max_age_hours` (20 h) or a load forecast older than
`load_max_age_minutes` (90 min) makes the valuation fall back. Set each a little above
how often its source normally issues, so a missed issue is tolerated and a dead source
is not.

## Your prices come in 15-minute intervals

```toml
schema = "engine/2"
slot_minutes = 15
```

An interval that crosses a slot's edge without covering it is refused, so 15-minute
prices need 15- (or 5-) minute slots. The slot must divide an hour. The meters' noise
floors (`[observed]`) were measured on 10-minute slots; check them against your meters
when you change it.

## You want a shorter or longer view

```toml
schema = "engine/2"
horizon_hours = 24
```

The price rule looks ahead for the event that makes up a kWh (a fill bounds a battery
kWh's price), and runs the battery's stored cost forward over the horizon. Too short a
horizon misses a fill the next morning, and under `battery_value = "replacement"` ends
more slots at [the horizon rule](../methodology/make-up-rule.md#the-horizon-rule) instead
of a real event. 48 hours covers a full day ahead from any moment.

## You want a kWh priced by its replacement

```toml
schema = "engine/2"
battery_value = "replacement"
```

By default a kWh from the battery's cycle costs what the energy in the battery cost
(`"stored_cost"`): the battery's solar at the export it gave up, its grid energy at what
it was bought for, through the battery's losses and wear, and no more than the sun that
will refill it. `"replacement"` prices it by what will make it up instead: the sun that
refills it, the plant's next grid top-up, or the import once it runs empty
([The make-up rule](../methodology/make-up-rule.md)).

Choose by the question your scheduler asks. The stored cost answers "what does the
energy I would use cost?", from what is known now. The replacement answers "what will
the site pay to put it back?", and is only as good as the forecast of the plant's
future: it assumes the projected top-up or shortfall happens. Replay both over your
recorded data and compare ([Check it](#check-it)).

## Your PV forecast is a point, or you want a finer spread

```toml
schema = "engine/2"
scenarios = 20
```

More scenarios resolve the chance a battery fills more finely, at the cost of one plant
projection each. With a point forecast every scenario is the same day, so set it to 1
and save the work.

## The live value is too jumpy, or too slow

```toml
schema = "engine/2"
smoothing_seconds = 120
load_jump_kw = 1.0
```

`smoothing_seconds` is the time constant of the live value's average of measured PV
and load (0 turns it off). `load_jump_kw` is how large a load change must be confirmed
before it counts. A house with large, frequent switching loads may want a larger jump.

## Your meters are noisier or cleaner

```toml
schema = "engine/2"

[observed]
spill_kw = 0.2
regulation_kw = 0.5
```

The noise floors say below what a metered flow is not an event: a small export that is
the meters reading at different instants, a small import that is the inverter holding
its grid connection near zero. The defaults were measured on one plant's 10-minute means and are a
starting point. To set yours: replay a month and run `check-plant`; where the scoring
shows events the plant did not have (a "spill" of 0.1 kW while the battery charged,
an "import" of 0.2 kW while the battery was serving the house), raise that floor just
above them; where it misses real ones, lower it.

## Check it

```sh
uv run microgrid config check --data my-dataset --engine tuned.toml
uv run microgrid replay --data my-dataset --from 2026-09-01 --to 2026-10-01 --engine tuned.toml
```

[Replay](replay.md) the change over recorded data before using it: the scores say
whether it helped.
