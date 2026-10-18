# Causes and kinds

Every valuation and live value says how it was made (its **kind**) and, when an input
could not be used, why (its **causes**, in `degraded`). [Missing and stale
inputs](../methodology/degradation.md) gives the reasons; the input each cause names is
defined in [Inputs](../concepts/inputs.md).

## Kinds

`ValuationKind`, the `kind` field:

| Kind | When | What the values are |
|---|---|---|
| `stored_value_v1` | every input could be used, or only the battery's lots were missing (`battery_lots`) | valued by the model (the stored-value method, version 1): the marginal cost (a battery kWh at its stored cost, by default), surplus, regimes, certainties, values by source |
| `fallback_import` | at least one input could not be used | every slot at the import price, no surplus, regime `import`, certainties 0, no values by source; the causes in `degraded` |
| `unpriced` | no price for the slot under way | nothing valued: a valuation has no slots, a live value has no prices; `degraded` names `prices` |

A live value warming up names `smoothing`. The first warm-up after the client began
reading, with nothing else wrong, is `stored_value_v1`: the valuation's price for the
slot, and no surplus until the averages settle. A warm-up after a gap is
`fallback_import`.

## Causes

`Cause`, the entries of `degraded` and the keys of a valuation's `inputs`. The values
are the input names.

### A valuation's

| Cause | The input could not be used when |
|---|---|
| `prices` | no price covers the slot under way: none received, the newest too old for `price_max_age_minutes` (26 hours by default), or its intervals cross the slots (15-minute prices on 10-minute slots: a source with that interval length needs another slot). The valuation is then `unpriced` |
| `pv_forecast` | no PV forecast received; the newest older than `pv_max_age_hours`; one that does not cover the slot under way; one per array missing an array; intervals that cross the slots |
| `battery_soc` | no SOC in force at the moment; the series not vouched for up to it (`known_until`); a value more than `soc_tolerance` (0.01, one percentage point) outside 0-1 |
| `work_mode` | [dispatch as modelled](../concepts/inputs.md#dispatch-as-modelled) is false, or unknown at the moment |
| `load_forecast` | no load forecast known at the slot under way: none received, the newest too old for `load_max_age_minutes` (by issue time or first interval), intervals that cross the slots |
| `plant_model` | the plant model could not project every slot |
| `battery_lots` | no [lots](../concepts/inputs.md#the-batterys-lots) in force at the moment, for a plant with a battery on the stored-cost rule (`battery_value = "stored_cost"`). **Not a fallback**: the valuation stays `stored_value_v1`, with all the battery holds priced as bought at the import |
| `pv_spread` | the PV forecast gives no quantile below its median (a point forecast, say): the pessimistic case (`marginal_cost_p10`, `surplus_kw_p10`) is the median, as nothing is known of the downside. **Not a fallback**: the valuation stays `stored_value_v1` and its values stand |

A plant without PV has no `pv_forecast` input, and one without a battery no
`battery_soc` or `work_mode`: an input the plant does not have is never a cause.

A gap in the load forecast later in the horizon (a slot unknown with known ones after
it) is not a cause: the slots before the gap are valued and `horizon_cut` is true.

### A live value's own

| Cause | When |
|---|---|
| `pv_power` | measured PV cannot be read: a PV meter unknown, or the meters cannot give the PV the valuation takes |
| `load_power` | the measured [house load](../concepts/inputs.md#house-load) is unknown (or, with DC-bus loads, their meter) |
| `smoothing` | the averages have not run for one time constant since a start or a gap |
| `issue` | the valuation is past its `valid_until`; its prices still serve |

A live value also names `battery_soc` and `work_mode` from the measured series,
`battery_lots` where it has no lots, `pv_spread` where its valuation named it, and,
where the valuation it runs again fell back, that valuation's causes.

## The client's own causes, and mistakes

A client passes its own causes as `external` (`Engine.live(..., external=[...])`); any
makes the live value `fallback_import`, and they are kept beside the engine's, never in
their place. The `microgrid` command names one of its own beside a valuation's:
`dc_load_forecast`, where it valued a plant's DC-bus loads at 0 kW for want of a
forecast of them (the engine was given that forecast, so it names nothing). A client's mistake (an input the plant does not have, a meter name it does
not meter, a negative PV forecast, a time that is not UTC) raises `ValueError` instead
of degrading ([why](../methodology/degradation.md#a-mistake-is-not-a-degraded-input)).
