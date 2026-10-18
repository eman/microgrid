# Fit the battery's conversion

The battery's efficiencies decide what a kWh through it costs
([What a stored kWh is worth](../methodology/stored-value.md)). A datasheet's round-trip
figure is a start; the plant's own meters say what it really does, and how its loss
grows with power. `microgrid fit-battery` fits the battery's parameters for `plant/2`
from a dataset of its meters.

## What it needs

A dataset with, over the window (30 days by default):

- the battery's meter (`battery`, at its DC side, positive charging) and the SOC;
- the battery inverter's AC power ([`inverter`](../concepts/inputs.md#inverter-ac-power),
  positive out to the house);
- the `pv` and `grid` meters and the [house load](../concepts/inputs.md#house-load);
- a DC-bus load's meter, if the plant has one (slots where it drew are left out).

## Run it

```sh
uv run microgrid fit-battery --data my-dataset --end 2026-09-30 --days 30
```

`--end` is the local day the window ends at; it is required, since a dataset has no
"now".

## What it fits, and how

Two least-squares fits over 10-minute slot means, bootstrapped together by day for
their intervals:

1. **The inverter's loss.** What goes in less what comes out, in every slot:

    `loss = standby + working × (battery working) + l_c P_c + q_c P_c² + l_d P_d + q_d P_d²`

    with `P_c` and `P_d` the battery's DC charge and discharge. The battery is
    "working" above `working_threshold_kw` (0.05 kW) either way. A PV term is fitted
    too, as a check that the PV meter reads AC: it should come out 0.

2. **The battery's meter against its SOC.** Each hour's change in stored energy
   (SOC × capacity) against its metered charge and discharge, without an intercept:
   `stored = a × charged − b × discharged`. By the hour, not by the day: a day charges
   about what it discharges, so a daily fit cannot tell `a` from `b`, while most hours
   only charge or only discharge.

It then prints the parameters for `plant/2`:

| `plant/2` field | From the fit |
|---|---|
| `battery.eta_charge` | `a / (1 + l_c)`: AC to stored, at low power |
| `battery.eta_discharge` | `(1 − l_d) / b`: stored to AC, at low power |
| `battery.loss_charge_per_kw` | `q_c` |
| `battery.loss_discharge_per_kw` | `q_d` |
| `battery.meter_charge` | `a` |
| `battery.meter_discharge` | `b` |
| `standby_kw`, `working_kw` (the inverter's) | `standby`, `working` |

each with its 90 % interval (sampling error only), the fit's residual by power band
(flat if the form is right), and the same figures under simpler forms. The spread
between forms is the larger uncertainty.

## Read it

```text
=> the plant's parameters (plant/2):
   [battery] eta_charge               0.95088 (90 % +0.9498 .. +0.9521)
   [battery] eta_discharge            0.96928 (90 % +0.9660 .. +0.9730)
   [battery] loss_charge_per_kw       0.00300 (90 % +0.0030 .. +0.0030)
   ...
   discharging 1 kW DC delivers 95.7% of it, draws included
```

Copy the figures into your `plant/2` and run [check-plant](check-plant.md) to confirm
the model's SOC tracks the meters better than before.

The fit assumes everything the battery stores or gives passes one converter that
meters both of its sides (AC-coupled PV, the battery's inverter metering its AC power
and the battery's DC). On other topologies, read the fit with care.
