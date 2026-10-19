# Missing and stale inputs

**When an input is missing or too old, the price moves to the import price and the output
says why. A stale discount is never reused.**

## Why lean toward the import price

A flexible load reads a low price as permission to run. If the library published a low
price it could not justify (a sunny forecast from yesterday, an SOC from an hour before
the telemetry path went quiet), the load would run on energy that may not be there, and
the site would import it. The import price is what a kWh costs when nothing better can
be shown: the grid can always supply it. So when the library cannot tell, it says
"assume you import", and names the reason.

The opposite mistake, the import price when energy was in fact cheap, costs a missed
opportunity, not a bill.

## What happens

- **Any input that cannot be used, except the prices and the battery's lots**: the valuation is
  `fallback_import`. Every slot is at the import price; surplus is 0; the regime is
  `import`; the certainties are 0; there are no values by source. The causes are in
  `degraded`, and each input's state (when it was known, whether it could be used) in
  `inputs`.
- **No lots for the battery** (on the stored-cost rule): not a fallback. All the battery
  holds is priced as bought, at the import price, and `degraded` names `battery_lots`;
  the valuation stays `stored_value_v1`, because those values are already the
  conservative ones ([Energy of unknown
  origin](stored-value.md#energy-of-unknown-origin)).
- **No spread below the PV forecast's median** (a point forecast): not a fallback. The
  pessimistic case is the median, and `degraded` names `pv_spread`; the values stand.
- **No price for the slot under way** (none received, too old, or on intervals that
  cross the slots): the valuation is `unpriced`, with no slots. Without a price there is
  nothing to fall back to.
- **A gap later in the load forecast**: not a fallback. The slots before the gap are
  valued, the horizon stops there, and `horizon_cut` says so.

What makes each input unusable is listed in [Causes and kinds](../reference/causes.md);
the age and coverage rules are in [Series and
forecasts](../concepts/series.md#how-the-engine-reads-them-at-a-moment).

## Never reuse a stale discount

The rule is stricter than "use the last good value":

- **Prices and PV are read from the issue received last, alone.** A slot the newest
  issue leaves uncovered is not filled from an older issue the source has replaced.
- **A forecast that is too old is not used at all**
  and ignored for the rest.
- **A measured value holds only while its path is vouched for** (`known_until`). The
  age of a value says nothing (a steady SOC sends nothing); a silent path does.
- **The live value's averages restart at a gap**, and a warm-up after a gap falls back.
- **A valuation past its `valid_until`** is not run again as current: the live value
  falls back and names `issue`.

## The client's own causes

A client may know something the engine cannot see: its connection to the plant dropped,
a meter it reads is broken. It passes those as `external` causes to the live value. Any
external cause makes the value `fallback_import`. The client's causes are kept beside
the engine's, in their own field.

## A mistake is not a degraded input

Passing an input the plant does not have (a PV forecast to a plant without PV), a meter
the plant does not meter, a negative PV forecast, or a time that is not UTC is a bug in
the client. The engine raises rather than falling back, so the bug is found.
