# `plant/2`

A plant's topology and parameters as plain data: what `PlantConfig.from_mapping`
reads and `PlantConfig.to_mapping` writes back exactly. The `microgrid` command reads it
from TOML (`plant.toml`); a Python client may build the mapping from anything.
[Describe a plant](../how-to/describe-plant.md) shows how to
write one for each topology.

**Validation names every problem with its path** (`battery.capacity_kwh: must be
positive`), all of them at once: each field's and each part's first, then the rules
across parts once the parts are sound. `microgrid config check` prints them. A key
given as null is a key left out; a key the schema does not have is an error.

**Units:** power kW, energy kWh, SOC a fraction 0-1, wear in the prices' currency per
kWh ([Currency](../concepts/units.md#currency)).
Times of day are local, in the plant's zone, written `"HH:MM:SS"` without an offset.

## The top level

| Key | Required | Meaning |
|---|---|---|
| `schema` | yes | `"plant/2"` |
| `timezone` | yes | the plant's zone, an IANA name (`"Europe/Berlin"`); windows are local times in it |
| `pv` | no | a list of arrays; none: a plant without PV |
| `battery` | no | the battery and where it connects; none: a plant without one |
| `hybrid` | no | the hybrid inverter, where the plant has a DC bus |
| `grid` | no | the grid connection's limits |
| `loads` | no | the loads by connection point; default one AC load, `house` |
| `dispatch` | no | what the plant does on its own beyond self-consumption |
| `meters` | no | the meters the plant reads; default the set below |

## `[[pv]]`: an array

| Key | Default | Meaning |
|---|---|---|
| `name` | required | the array's name; unique, and not a load's |
| `coupling` | required | `"ac"`: its own inverters (micro-inverters, a string inverter) on the AC bus; `"dc"`: strings on a hybrid's DC bus |
| `dc_rating_kw` | none | the MPPT limit, DC kW: it caps the array's DC power |
| `ac_rating_kw` | none | an AC-coupled array's own inverter's rating, AC kW; a DC-coupled array has none |

An array's forecast is in its coupling's terms: AC kW for an AC-coupled array, DC kW for
a DC-coupled one. A DC-coupled array needs a `hybrid`.

## `[battery]`

The battery's parameters and its connection, in one table.

| Key | Default | Meaning |
|---|---|---|
| `capacity_kwh` | required | usable capacity, stored kWh |
| `charge_kw` | required | the charge limit, kW |
| `discharge_kw` | required | the discharge limit, kW |
| `eta_charge` | 0.94 | AC to stored, at low power; in (0.5, 1] |
| `eta_discharge` | 0.94 | stored to AC, at low power; in (0.5, 1] |
| `reserve_soc` | 0.0 | the floor the plant keeps (a backup reserve) |
| `saturation_soc` | 1.0 | where charging counts as full (a BMS that tapers near the top) |
| `wear_charge_per_kwh` | 0.0 | the battery's wear per AC kWh charged; 0 prices cycling as free, so set it ([how to estimate](../how-to/describe-plant.md#the-battery-parameters)) |
| `wear_discharge_per_kwh` | 0.0 | the battery's wear per AC kWh discharged |
| `loss_charge_per_kw` | 0.0 | how the conversion's loss grows with power, charging: kW of loss per (DC kW)², so its unit is 1/kW. At 0.003, charging at 4 kW DC loses 0.003 × 16 = 0.048 kW more than the low-power efficiency says |
| `loss_discharge_per_kw` | 0.0 | the same, discharging |
| `meter_charge` | 1.0 | stored kWh per kWh the battery's meter reads charging; in (0.5, 1.5] |
| `meter_discharge` | 1.0 | stored kWh drawn per kWh the meter reads discharging; in (0.5, 1.5] |
| `connection` | required | `"ac"`: on the AC bus behind its own inverter; `"dc_bus"`: on a hybrid's DC bus |
| `form` | `"folded"` | on a DC bus: `"folded"`, its conversion one path from the AC bus to stored energy, metered at the DC bus; `"split"`, its conversion the DC bus to stored energy alone, with the hybrid's `conversion` from the DC bus to AC |
| `inverter` | none | a battery on the AC bus: its own inverter (`[battery.inverter]`, below), required there |
| `dc_charge_kw`, `dc_discharge_kw` | none | a battery on a DC bus: its own DC limits; a folded battery with anything else on its bus needs them |

`0 <= reserve_soc < saturation_soc <= 1`. With the defaults (no loss growth, a meter
that reads stored energy) the conversion is one efficiency each way.
`microgrid fit-battery` fits the efficiencies, the loss growth, the meter factors and
the inverter's draws from the meters.

## `[hybrid]` and `[battery.inverter]`: an inverter

| Key | Default | Meaning |
|---|---|---|
| `ac_rating_kw` | none | its AC rating, on the net power crossing it |
| `standby_kw` | 0.0 | its own draw, always |
| `working_kw` | 0.0 | its extra draw while the battery works through it |
| `working_threshold_kw` | 0.05 | the battery works above this, kW either way |
| `conversion` | none | the hybrid's DC bus to AC (`[hybrid.conversion]`): required where no folded battery holds it, refused where one does; never on a battery's own inverter |

`[hybrid.conversion]`:

| Key | Default | Meaning |
|---|---|---|
| `eta_in` | required | AC to DC bus, at low power; in (0, 1] |
| `eta_out` | required | DC bus to AC, at low power; in (0, 1] |
| `loss_in_per_kw`, `loss_out_per_kw` | 0.0 | loss growth each way, kW of loss per (DC kW)² (unit 1/kW) |

A folded battery with anything else on its DC bus (DC PV, a DC load) needs the hybrid's
`ac_rating_kw` and its own `dc_charge_kw` and `dc_discharge_kw`, and must not store
more per DC kWh than it draws (`meter_charge <= meter_discharge`).

## `[grid]`

| Key | Default | Meaning |
|---|---|---|
| `import_limit_kw` | none | the most the site can import |
| `export_limit_kw` | none | the most it may export; 0: it may not export |

## `[[loads]]`

| Key | Default | Meaning |
|---|---|---|
| `name` | required | the load's name, unique |
| `connection` | `"ac"` | `"ac"`, or `"dc_bus"`: on a hybrid's DC bus |

At least one load is on the AC bus: the house. A DC-bus load needs a hybrid. The load
forecast owns every load's kW; the plant only says where each connects. **The names are
labels:** the engine takes the loads by connection point, all the AC loads' forecast as
one series (`Inputs.load`) and the DC-bus loads' as another (`Inputs.dc_load`), never by
name (the plant model knows connection points, not load names). A name is
checked unique and read by nothing else; it says, for a reader of the file, which load
is which.

## `[dispatch]`

| Key | Default | Meaning |
|---|---|---|
| `grid_charge` | none | the grid-charge windows (below) |
| `export_windows` | none | the battery-export windows (below) |
| `charge_order` | `"pv_first"` | in a grid-charge window, `"pv_first"`: the grid tops up only what the PV still to come in the window will not; `"target_first"`: the grid tops up as the target needs, not counting that PV. PV still charges first within a slot either way |
| `hold_rules` | none | when the plant holds its battery through a DC-bus load's session (below) |

### `[[dispatch.grid_charge]]`: a grid-charge window

Inside the window the grid tops the battery up to a target that PV alone would not
reach by `end`.

| Key | Default | Meaning |
|---|---|---|
| `start`, `end` | required | local times; the window lies within one local day (`start` before `end`) |
| `target_soc` | 1.0 | the target; above the battery's reserve |
| `cover_until` | none | a target from need: what the house will draw from `end` until this time (net of PV and of discharge losses), when that is lower than `target_soc`; at or before `end` means the next day; not inside the window |
| `timing` | `"early"` | `"early"`: from the window's start, as soon as PV will fall short; `"late"`: only what the rest of the window could no longer add at the charge limit, so the top-up comes as late as it can |

No two windows overlap, grid-charge or export.

### `[[dispatch.export_windows]]`: a battery-export window

The plant discharging the battery to the grid.

| Key | Default | Meaning |
|---|---|---|
| `start`, `end` | required | local times, within one day |
| `power_kw` | required | AC kW, held through the window; within the battery's discharge limit, its inverter's rating and the grid's export limit |
| `floor_soc` | required | the window stops at this SOC, at or above the reserve and below saturation |
| `yields` | `"pv"` | which yields at the export limit: `"pv"` or `"battery"` |

### `[[dispatch.hold_rules]]`: holding through a DC-bus load's session

While the DC-bus loads draw more than `dc_load_above_kw`, a session that starts with the
SOC below `start_below_soc` keeps the battery from discharging until it ends.

| Key | Meaning |
|---|---|
| `dc_load_above_kw` | a session is the DC-bus loads drawing more than this, DC kW |
| `start_below_soc` | the SOC a session must start below to hold the battery; above the reserve |

A hold rule needs a battery and a DC-bus load.

## `[[meters]]`

The meters the plant reads, each by name. A live reading and a dataset key each meter's
kW by its name.

| Key | Default | Meaning |
|---|---|---|
| `name` | required | unique |
| `measures` | required | `"pv"`, `"grid"`, `"battery"`, `"consumption"` or `"dc_load"`. `consumption` is the AC loads and the inverters' draw, metered in place of the grid; it is not the dataset's [house load](../concepts/inputs.md#house-load) |
| `array` | none | a PV meter: the array it meters; none: every array together (only where they share one coupling) |
| `at` | none | a battery meter: `"dc"` (its DC side, the default) or `"ac"` (its own inverter's AC side, for a battery on the AC bus only). `fit-battery` reads the battery's meter as DC |
| `sign` | 1 | 1 where positive is PV made, importing, charging or drawing; -1 where it is the opposite |

The set must close the plant's balance: the grid or the consumption metered (one gives
the other), the battery where there is one, the DC-bus loads where there are any, and
every array's PV, each or together.

**The default set**, without `meters`: `pv` for every array together (with arrays of
both couplings, one per array named `pv:<array>`, which a dataset cannot name:
[name them](dataset.md#the-names) to record them), `grid`, `battery` at its DC
side where there is a battery, and `dc_load` where a load is on the DC bus, each with
sign 1.

## Examples

The library ships four example plants (`microgrid.plant.examples`), each with
synthetic, round parameters.

=== "AC-coupled PV, battery on a hybrid"

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
    loss_charge_per_kw = 0.003
    loss_discharge_per_kw = 0.003
    meter_charge = 0.99
    meter_discharge = 1.0
    connection = "dc_bus"
    form = "folded"

    [hybrid]
    standby_kw = 0.03
    working_kw = 0.06

    [[dispatch.grid_charge]]
    start = "01:00:00"
    end = "05:00:00"
    target_soc = 0.6
    timing = "late"
    ```

=== "DC-coupled PV, export limit"

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

    [grid]
    export_limit_kw = 5.0

    [[dispatch.grid_charge]]
    start = "02:00:00"
    end = "05:00:00"
    target_soc = 0.5
    ```

=== "PV only"

    ```toml
    schema = "plant/2"
    timezone = "Europe/Berlin"

    [[pv]]
    name = "east"
    coupling = "ac"
    dc_rating_kw = 3.5
    ac_rating_kw = 3.0

    [[pv]]
    name = "west"
    coupling = "ac"
    dc_rating_kw = 3.5
    ac_rating_kw = 3.0
    ```

=== "Battery only"

    ```toml
    schema = "plant/2"
    timezone = "Europe/Berlin"

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

    [[dispatch.grid_charge]]
    start = "00:00:00"
    end = "05:00:00"
    target_soc = 0.9

    [[dispatch.export_windows]]
    start = "17:00:00"
    end = "20:00:00"
    power_kw = 3.0
    floor_soc = 0.3
    ```
