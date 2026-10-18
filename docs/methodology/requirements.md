# Requirements

What the library holds itself to. The code and the other pages cite them by number
(R-7).

- **R-1: Per slot.** Values are given per time slot over a forward horizon, slot by
  slot, so a scheduler can compare the slots of the next two days
  ([The marginal price](marginal-price.md)).
- **R-2: Values by source.** For every slot, what one more kWh costs, and what a PV kWh
  and a battery kWh are worth to a flexible load. A PV kWh is worth what it would have
  done without the load: exported, stored in the battery with headroom, or spilled
  ([The marginal price](marginal-price.md)).
- **R-3: Each value says why.** A PV kWh's value carries the routing it assumed; a
  battery kWh's carries what it is bought back with
  ([What a stored kWh is worth](stored-value.md), [The make-up rule](make-up-rule.md)).
- **R-4: A value now.** Between valuations, the current slot's values follow the meters
  ([The live value](live-value.md)).
- **R-5: Marginal, not average.** Values are for one more kWh. The supply stack says how
  large a load can be before the price it pays changes
  ([The marginal price](marginal-price.md)).
- **R-6: A pessimistic case.** Beside the expected case, values for a dull day (the PV
  forecast's p10), whose surplus is never above the expected one
  ([PV scenarios and certainty](scenarios.md)).
- **R-7: Degrade conservatively.** A missing or stale input moves values toward the
  import price and is named in `degraded`; a stale value is never reused
  ([Missing and stale inputs](degradation.md)).
- **R-8: Prices are an input.** Import and export prices per interval come in as a
  series, from any source ([Scope](scope.md)).
- **R-9: The load forecast is an input.** The house's load as forecast comes in as a
  series, from any source, every load in it ([Scope](scope.md)).
