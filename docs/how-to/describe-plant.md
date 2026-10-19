# Describe a plant

The plant model takes a plant's topology and parameters from a `plant/2` file, built
up here part by part. The [`plant/2` reference](../reference/plant.md) lists every
field; the [glossary](../glossary.md) defines the hardware terms.

Check a file at any point:

```sh
uv run microgrid config check --plant plant.toml
```

It names every problem with its path, all at once (`battery.capacity_kwh: must be
positive`).

## Start: the zone and the house

```toml
schema = "plant/2"
timezone = "America/Denver"
```

`timezone` is the plant's IANA zone. Every window below is a local time in it, as a
plant's own settings are. With nothing else, this is a plant with no PV and no battery,
one AC load named `house`: every kWh costs the import price.

## PV

**[AC-coupled](../glossary.md#ac-coupling)** PV has its own inverters (micro-inverters, or a string inverter) on the
house's AC bus. Its forecast is in AC kW.

```toml
[[pv]]
name = "roof"
coupling = "ac"
dc_rating_kw = 7.2     # optional: the panels' MPPT limit
ac_rating_kw = 6.0     # optional: its own inverter's rating; clips above it
```

**[DC-coupled](../glossary.md#dc-coupling)** PV is strings on a [hybrid
inverter's](../glossary.md#hybrid-inverter) [DC bus](../glossary.md#dc-bus). Its forecast is in DC kW. It
needs a `[hybrid]` (below).

```toml
[[pv]]
name = "strings"
coupling = "dc"
dc_rating_kw = 8.0
```

Several arrays are several `[[pv]]` tables. Arrays that share a coupling take one
forecast for the site; a plant with arrays of both couplings takes a forecast per array
(`pv_forecast.<array>` in a dataset).

## A battery on its own inverter

An AC battery: its own inverter on the AC bus.

```toml
[battery]
capacity_kwh = 13.5
charge_kw = 5.0
discharge_kw = 5.0
eta_charge = 0.95
eta_discharge = 0.95
reserve_soc = 0.2
wear_charge_per_kwh = 0.03
wear_discharge_per_kwh = 0.03
connection = "ac"

[battery.inverter]
ac_rating_kw = 5.0
standby_kw = 0.03
working_kw = 0.03
```

## The battery parameters

- **`capacity_kwh`** is the usable capacity. **`reserve_soc`** is the floor the plant
  keeps ([reserve](../glossary.md#reserve)): to the price rule, that is empty.
- **`eta_charge` and `eta_discharge`** are AC to stored and stored to AC, at low power;
  their product is the round trip. With only a datasheet round trip, use its square
  root each way: a 90 % round trip is 0.949 each way. `microgrid fit-battery` fits them
  from the meters, with how the loss grows with power (`loss_charge_per_kw`,
  `loss_discharge_per_kw`; [Fit the battery's conversion](fit-battery.md)).
- **The wear** (`wear_charge_per_kwh`, `wear_discharge_per_kwh`) is what a kWh
  through the battery costs it, per AC kWh each way, in the prices' currency. It is the
  hurdle a charge or a discharge has to clear ([What a stored kWh is
  worth](../methodology/stored-value.md)). **The default is 0, which prices cycling as
  free; set it.** An estimate: the battery's price divided by its lifetime throughput
  (usable capacity × rated cycles), split between the two directions. A 10 kWh battery
  costing 3000 and rated for 6000 cycles: 3000 / (10 × 6000) = 0.05 per kWh cycled, so
  0.025 each way.

## A battery on a hybrid's DC bus

A hybrid inverter has a DC bus that the battery (and DC PV, and DC loads) share. The
battery is **folded** or **split**:

- **Folded**: the battery's conversion is one path from the AC bus to stored energy,
  metered at the DC bus. Use it when the battery is alone on the bus with AC-coupled PV,
  and you fit the conversion end to end.
- **Split**: the battery holds the DC bus to stored energy; the hybrid's own
  `conversion` holds the DC bus to AC. Use it when DC PV or a DC load shares the bus.

**AC-coupled PV, a folded battery on a hybrid:**

```toml
schema = "plant/2"
timezone = "Europe/Berlin"

[[pv]]
name = "roof"
coupling = "ac"

[battery]
capacity_kwh = 12.0
charge_kw = 6.0
discharge_kw = 6.0
eta_charge = 0.96
eta_discharge = 0.96
reserve_soc = 0.1
wear_charge_per_kwh = 0.02
wear_discharge_per_kwh = 0.02
connection = "dc_bus"
form = "folded"

[hybrid]
standby_kw = 0.03
working_kw = 0.06
```

**DC-coupled PV, a split battery, a hybrid with an AC rating:**

```toml
schema = "plant/2"
timezone = "Europe/Berlin"

[[pv]]
name = "strings"
coupling = "dc"
dc_rating_kw = 8.0

[battery]
capacity_kwh = 10.0
charge_kw = 5.0
discharge_kw = 5.0
eta_charge = 0.98
eta_discharge = 0.98
reserve_soc = 0.1
wear_charge_per_kwh = 0.02
wear_discharge_per_kwh = 0.02
connection = "dc_bus"
form = "split"

[hybrid]
ac_rating_kw = 6.0
standby_kw = 0.03
working_kw = 0.04

[hybrid.conversion]
eta_in = 0.97
eta_out = 0.97
loss_in_per_kw = 0.002
loss_out_per_kw = 0.002
```

The hybrid's AC rating clips the strings: DC PV that can neither cross to the AC side
nor charge the battery is clipped, and the price rule knows a battery on that bus takes
clipped PV first, at no cost.

## A load on the DC bus

Some plants feed a load straight from the hybrid's DC bus, for example an EV charger on
the DC bus. Declare it by where it connects, and give the battery its own DC limits and the hybrid its AC
rating, which a folded battery needs once anything else shares its bus:

```toml
schema = "plant/2"
timezone = "Europe/Berlin"

[[pv]]
name = "roof"
coupling = "ac"

[battery]
capacity_kwh = 12.0
charge_kw = 6.0
discharge_kw = 6.0
eta_charge = 0.96
eta_discharge = 0.96
reserve_soc = 0.1
connection = "dc_bus"
form = "folded"
dc_charge_kw = 6.0
dc_discharge_kw = 6.0

[hybrid]
ac_rating_kw = 8.0

[[loads]]
name = "house"

[[loads]]
name = "ev_charger"
connection = "dc_bus"

[[dispatch.hold_rules]]
dc_load_above_kw = 1.0
start_below_soc = 0.8
```

The DC-bus loads' forecast comes in separately (`dc_load_forecast`). The **hold rule**
describes a plant that protects its battery during a DC-bus load's session: a session
(the DC-bus loads drawing more than 1 kW) that starts below 80 % keeps the battery from
discharging until it ends, and the grid serves the load. Leave it out if the plant
drains the battery into the load.

## The grid's limits

```toml
[grid]
import_limit_kw = 11.0   # the most the site can import
export_limit_kw = 5.0    # the most it may export; 0: it may not export
```

Above an export limit, PV is curtailed, and a kWh more takes curtailed PV first, at no
cost. At an import limit, a kWh more cannot come from the grid, and the price rule
prices that ([The make-up rule](../methodology/make-up-rule.md#limits-and-unusual-plants)).

## A grid-charge window

If the plant tops the battery up from the grid on a schedule, say so: it decides what
the battery holds, and so what a kWh from it costs, through the night and the next day
([What a stored kWh is worth](../methodology/stored-value.md#run-forward-along-each-scenario)).

```toml
[[dispatch.grid_charge]]
start = "01:00:00"
end = "05:00:00"
target_soc = 0.6
timing = "late"
```

`target_soc` is the target the grid tops up to where PV alone would not reach it. Two
settings decide when and how much, each defined in the
[reference](../reference/plant.md#dispatchgrid_charge-a-grid-charge-window): `timing`
(default `"early"`: from the window's start; `"late"`, as here: as late as the charge
limit allows) and `cover_until` (a target from the house's need instead). `charge_order`
in `[dispatch]` (default `"pv_first"`) says whether the grid leaves room for the PV still
to come in the window.

Several windows are several tables; they may not overlap. A window lies within one local
day.

## A battery-export window

If the plant discharges the battery to the grid at set hours:

```toml
[[dispatch.export_windows]]
start = "17:00:00"
end = "20:00:00"
power_kw = 3.0
floor_soc = 0.3
```

It exports at `power_kw` until the SOC reaches `floor_soc`, and does not export again in
that window.

## Meters

Without `[[meters]]`, the plant reads the default set: `pv`, `grid`, `battery` (at its DC
side) and `dc_load` where there is one, each positive for PV made, importing, charging or
drawing. Name the meters when they differ, for example a grid meter that reads
exports as positive:

```toml
[[meters]]
name = "pv"
measures = "pv"

[[meters]]
name = "grid"
measures = "grid"
sign = -1

[[meters]]
name = "battery"
measures = "battery"
```

The set must close the plant's balance: the grid or the consumption, the battery where
there is one, the DC-bus loads where there are any, and every array's PV.

## What to check

- **Run `config check`** after every change.
- **[Check it against the meters](check-plant.md)** once you have a week of them in a
  dataset.
- **Fit the battery** with `fit-battery` once you have a month
  ([Fit the battery's conversion](fit-battery.md)).

Only one topology has been checked against a real plant's meters so far
([Scope](../methodology/scope.md#what-it-cant-model)); `check-plant` on a plant's
meters shows whether the model fits it.
