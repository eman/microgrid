# Glossary

## AC coupling

PV whose own inverters (micro-inverters or a string inverter) feed the house's AC
wiring. Its forecast and its meter are in AC kW. Compare [DC coupling](#dc-coupling).

## BMS

The battery management system: the battery's own electronics. It protects the cells,
and near full it slows charging, which is why a battery can count as full below 100 %
([saturation](#saturation)).

## DC bus

The DC side of a [hybrid inverter](#hybrid-inverter), shared by whatever connects
there: the battery, DC-coupled PV, a DC load.

## DC coupling

PV strings wired to a [hybrid inverter's](#hybrid-inverter) [DC bus](#dc-bus), without
inverters of their own. Its forecast is in DC kW.

## Dead band

The gap between what PV diverted from charging the battery costs and what a kWh from
the battery costs, made by the battery's losses and wear. See [What a stored kWh is worth](methodology/stored-value.md#the-dead-band).

## Dispatch as modelled

Whether the plant is running the dispatch its model describes. Defined in
[Inputs](concepts/inputs.md#dispatch-as-modelled).

## Folded and split

Two ways to describe a battery on a [DC bus](#dc-bus). **Folded**: one conversion from
the AC side to stored energy, metered at the DC bus. **Split**: the battery's own step
from the DC bus to stored energy, with the hybrid's conversion from the DC bus to AC
separate. See [Describe your plant](how-to/describe-plant.md#a-battery-on-a-hybrids-dc-bus).

## Grid-charge window

Hours in which the plant tops the battery up from the grid toward a target, set in
`plant/2`. A **top-up** is that charge.

## Heartbeat

A series whose samples show that readings were flowing: a meter that reports every few
seconds, or a client's own tick. Optional: without one, each series is trusted as far as
its `known_until`. Defined in [Inputs](concepts/inputs.md#heartbeat).

## Horizon

How far ahead a valuation reaches: 48 hours by default. See [Slots and the
horizon](concepts/slots-horizon.md).

## House load

What the plant's AC loads draw, as the plant meters it. Defined in
[Inputs](concepts/inputs.md#house-load).

## Hybrid inverter

An inverter with a battery (and possibly PV strings and DC loads) on its own
[DC bus](#dc-bus), converting between that bus and the AC side.

## Issue

One forecast as published: its values over intervals, and when it was issued. A
forecast input is the series of its issues, each at the time the client received it.

## Make-up

How the battery replaces a kWh taken from it: by PV that would have spilled, by a grid
top-up, or by an import once it runs empty. A fill bounds a battery kWh's price; under
`battery_value = "replacement"` the make-up prices it. See [The make-up
rule](methodology/make-up-rule.md).

## Marginal cost

What one more kWh used in a slot costs the site. See [The marginal
price](methodology/marginal-price.md).

## MPPT

The maximum power point tracker: the part of an inverter that draws power from PV
strings. Its rating caps the strings' DC power (`dc_rating_kw`).

## Provider

Where an input comes from: a tool's integration or any entity's attribute, named per
input in a [`sources/1`](configuration/data-sources.md) file. Forecasts can come from
either; measured values come from Home Assistant only, today, or from a dataset.

## PV

Photovoltaic: solar panels.

## Regime

What one more kWh in a slot would displace: export, curtailment, the battery, or an
import. See [Outputs](concepts/outputs.md#regime).

## Reserve

The SOC the plant keeps back (a backup reserve), `reserve_soc`. To the price rule, a
battery at its reserve is empty.

## Pool

All the energy the battery holds, and what it cost, as one: its running average cost
per stored kWh prices a kWh the battery delivers. See [The battery's
lots](methodology/lots.md#lots-and-the-pool).

## Saturation

The SOC at which charging counts as full, `saturation_soc`: below 1 where the
[BMS](#bms) slows charging near the top.

## Slot

The unit of time the engine prices: 10 minutes by default.

## SOC

State of charge: how full the battery is, as a fraction of its capacity (0.62, not 62 %)
inside the library.

## Stored cost

What the energy in the battery cost per stored kWh, its pool's running average: solar
at the export it gave up, grid energy at the import it was bought at, energy of unknown
cost as if bought at the import. See [What a stored
kWh is worth](methodology/stored-value.md).

## Surplus

PV the plant would export or curtail. See [Outputs](concepts/outputs.md#surplus).

## Telemetry path

Whatever carries a plant's readings to the client: a local connection, a cloud service,
a recorder. When it goes quiet the readings stop, without the values changing.

## Valuation

The engine's answer for one moment: every slot of the horizon, priced. A **live value**
is one valuation run again from the meters for the slot from now.
