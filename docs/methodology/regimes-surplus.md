# Regimes and surplus

The [regime](../concepts/outputs.md#regime) and the [surplus](../concepts/outputs.md#surplus)
are read from the [median PV case](../concepts/outputs.md#the-median-pv-case)'s
projection, slot by slot.

## How a slot's regime is read

In order, the first that holds:

1. **PV spilling** (exported or curtailed): `export`, or `curtail` where more is
   curtailed than exported.
2. **At the grid's import limit**: `battery`, since a kWh more cannot be imported and
   comes from the battery's charge or discharge; or `import` where load is
   [unserved](../concepts/outputs.md#unserved), as a floor.
3. **The grid serving the house or charging the battery**: `import`.
4. **The battery serving a load, or PV charging it that a load could take**: `battery`.
5. Otherwise `import`.

## Why surplus is the projection's export

Surplus is what the projection exports or curtails, not PV minus load. The difference is
the battery: on a morning when the battery is charging with room to spare, PV exceeds
the load, yet nothing is spare, because every kWh is going into the battery that the
house will draw on tonight. Taking it is a cost the price captures.
Battery export is left out for the same reason: it is stored energy, priced, not spare
PV.

Surplus is the projection's on the median day and on the 0.1-quantile day (`surplus_kw`,
`surplus_kw_p10`); the p10 one is never above the median's.

## Why both the price and the surplus

The price answers "what would it cost?"; the surplus answers "is there spare power to
soak up, and how much?". A load that should only use spare PV reads `surplus_kw`. A load
that will run anyway and wants the cheapest time reads `marginal_cost`. On a morning
with the battery charging there is no surplus, yet the price can be low, because the
battery will fill anyway.
