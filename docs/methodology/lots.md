# The battery's lots

**The ledger keeps track of where the energy in the battery came from and what it cost:
its lots, and all of it as one pool.** It answers "what does the battery hold, from
which source, at what cost per kWh?". The price reads the pool: a kWh the battery
delivers costs the pool's running average, through the discharge's loss and wear
([What a stored kWh is worth](stored-value.md)).

## Lots and the pool

The ledger keeps the same energy two ways:

- **The lots**, by source: PV, the grid, and energy of unknown origin, each with its
  cost. They say what the battery's energy is made of, and they set the floor below.
  To book a discharge they must take it from some lot first, in an order (below).
- **The pool**: all the battery holds, and what it cost, as one. Charging adds to it at
  cost; a discharge takes energy at its average, so its average does not move. The
  price reads it.

Both hold the same energy and are scaled to the same meter. They differ only in what a
discharge takes: the lots, the energy of the lot drawn first; the pool, the average.
The order lots are drawn in is a convention, and a price that followed it would move
with the convention, not with anything the battery did. So the price reads the pool.

The lots still matter:

- **The cost split of the stored energy**: what the battery holds by source, and what
  a client publishes about it.
- **A guard for a scheduler that does not model the battery.** The cost of the next kWh
  drawn (`floor_per_kwh`) is a sanity floor, never the price: a controller that
  treated the basis of a single lot as the price would strand grid-charged energy
  whenever prices fell.

## How a lot is booked

A lot is a source (PV, the grid, or unknown), an amount of **stored** energy (the
battery's side of the inverter), and a cost basis per stored kWh.

**Charging.** Charging `c` AC kWh at price `p` stores `c × η_c` kWh at a basis of
`p / η_c` per stored kWh. The price is what the site gave up for it:

- a **PV lot** at the export price when it was charged (the export the site gave up to
  store it);
- a **grid lot** at the import price.

The price is the recorded price in force over the slot, its time-weighted mean, from
when each sample says it took effect: any price interval works on any slot, and no price
counts before it took effect. A dataset with no recorded prices is charged at those its
price forecasts put in force, as received
([Recorded prices](../concepts/inputs.md#recorded-prices)).

**Discharging.** Delivering `d` AC kWh draws `d / η_d` stored kWh, and the value drawn
is the delivery's cost, the conversion loss included.

**Order.** Energy of unknown origin is drawn first, by the house and by exports alike.
After it, the house is served from grid lots before PV lots, and exports come from PV
lots before grid lots: grid energy can go to only one destination, PV energy to either,
so drawing the restricted lots first keeps the remaining inventory worth the most.
Within a source, lots are a weighted average.

**Unknown energy.** What the battery held when the ledger started, or what the meter
shows when the model held nothing, has no known cost. In the lots it is kept apart at no
value, reported as such, and never sets the floor. In the pool it is priced as bought,
at the import: the ledger's first slot's for what it held when it began, the slot's for
energy the meter finds. The price needs a cost for every kWh the battery delivers, and
not knowing what one cost moves it toward the import ([Energy of unknown
origin](stored-value.md#energy-of-unknown-origin)).

## The meter wins

After each slot the lots, and the pool, are scaled to the metered stored energy, both
ways and pro rata at their own basis. What the model does not explain (self-discharge, the battery
management system, meter timing, an efficiency slightly off) shows as **unexplained**.

## Flows at the meters' resolution

The ledger reads the metered powers at every change of any of them, not as 10-minute
means. A 10-minute *net* mean blurs two states into one: when a DC-bus load's session
starts, the battery can swing from charging on PV to discharging into the load, and the
mean looks like the grid charging the battery. Only stretches where the telemetry path
was alive count (a heartbeat, `series.alive`); a slot with any silent stretch moves
nothing, and the meter catches it up.

## Efficiencies

The battery's meter reads DC; it is converted to AC with the battery's fitted
conversion, whose loss grows with power. A charge is booked by the path it took, as the
plant model works it out from the metered AC (`Storage.stored_per_kwh`): PV and the grid
charge through the same inverter on an AC-coupled plant, so they share it, and a grid
top-up loses more only because it runs at the charge limit; on a hybrid's DC bus the AC
crosses the hybrid and DC PV does not.

## Using it

`microgrid ledger` runs the ledger over a span of a dataset and prints, by source, what
was charged and at what cost, what went to the house and to export, what is held at the
end and its cost per kWh, and the unexplained energy. See [Read the battery's
lots](../how-to/ledger.md). `microgrid value`, `live` and `replay` run the same ledger
over the dataset's meters and give the engine its lots at each moment; `microgrid now`
runs it over the two days of recorded history before now.

From Python, `microgrid.engine.ledger` ([API](../reference/api/ledger.md)): a `Ledger`
stepped slot by slot, its `held` lots and its `pool`, and `Lots(as_of, held, pool)` to
hand the engine as `Inputs.lots` ([Inputs](../concepts/inputs.md#the-batterys-lots)).

