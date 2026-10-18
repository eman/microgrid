"""The supply stack of a slot.

tariffkit#67 R-10: values are for a marginal kWh; say how large a load can be
before the marginal value stops holding, "for example when a load shifts the
battery's state of charge enough to change routing". The stack answers it with
quantities: how much power each price holds for (docs/methodology/stored-value.md,
*The supply stack*).

**The stack** (:func:`supply_stack`): a slot's energy comes in steps, each
with its power and its price, in the order the plant serves a load from them
(not sorted by price: the battery's step can cost more than the grid's):

1. **spill**: PV exported or curtailed, at the export it would have earned
   (0 where curtailed);
2. **pv**: PV charging the battery, diverted, at the bottom of the battery's
   dead band (``core.value``, ``Served.PV``);
3. **battery**: discharge up to its limit and the energy above its reserve
   before the slot's charge (the plant model's ``Storage.headroom``: the PV
   charge is the pv step's), at the top of its dead band;
4. **grid**: without limit, at the import price.

The dead band is the margin's, at the power the inverter converts in the slot: a
step's price holds for a small load. Where the grid serves the slot
(``Served.GRID``: the house imports; or, with no PV charging, the plant holds
the battery or it has nothing left to give) the grid is the only step. A load
too small to matter pays the first step's price, which is the slot's price in
the same run (``_value.price_flows``).

Pure: no I/O.
"""

from __future__ import annotations

from dataclasses import dataclass

from microgrid.engine._value import MODEL, Served
from microgrid.plant.flows import SlotFlow
from microgrid.plant.port import Storage

#: Power below this is float residue, not a step, kW.
_RESIDUE_KW = MODEL.spill_kw


@dataclass(frozen=True, slots=True)
class Step:
    """Power available at one price through a slot."""

    source: Served
    #: kW; None for the grid, which has no limit here.
    kw: float | None
    per_kwh: float

    def to_mapping(self) -> dict[str, object]:
        """Plain data (``valuation/2``'s ``Step``)."""
        return {"source": self.source.value, "kw": self.kw, "per_kwh": self.per_kwh}


def supply_stack(
    storage: Storage | None,
    flow: SlotFlow,
    hours: float,
    *,
    served: Served,
    price: float,
    p_import: float,
    stored_value: float,
    import_limit_kw: float | None = None,
    pv_price: float | None = None,
    grid_diverted: float | None = None,
) -> tuple[Step, ...]:
    """The steps of ``flow``'s slot (module docstring), as ``_value.price_flows``
    classified and priced it: what serves it (``served``) at ``price``, and what a
    stored kWh is worth there (``stored_value``). Without a battery (``storage`` None)
    the stack is the spill, where PV spills, then the grid. At the grid's
    ``import_limit_kw``, :func:`_at_the_limit`. ``pv_price`` and ``grid_diverted``: what
    the rule priced diverted PV and the grid's diverted charge at
    (``FlowPrices.pv_prices``, ``grid_diverted``); else the dead band's charge side."""
    if import_limit_kw is not None:
        imported = flow.grid_to_load + flow.grid_to_battery
        if imported >= import_limit_kw - MODEL.short_kw and served is not Served.SPILL:
            return _at_the_limit(
                storage, flow, hours, price, p_import, stored_value, pv_price, grid_diverted
            )
    grid = Step(Served.GRID, None, p_import)
    if served is Served.GRID:
        return (grid,)
    if storage is None:
        return (Step(Served.SPILL, flow.pv_export + flow.pv_curtail, price), grid)
    band_pv, battery_price = storage.dead_band(stored_value, flow)
    pv_price = band_pv if pv_price is None else pv_price
    steps: list[Step] = []
    if served is Served.SPILL:
        steps.append(Step(Served.SPILL, flow.pv_export + flow.pv_curtail, price))
    # The PV now charging the battery, as AC a load would receive.
    divertible = storage.divertible_kw(flow)
    if divertible > _RESIDUE_KW:
        steps.append(Step(Served.PV, divertible, pv_price))
    headroom = storage.headroom(flow, hours)
    if headroom > _RESIDUE_KW:
        steps.append(Step(Served.BATTERY, headroom, battery_price))
    steps.append(grid)
    return tuple(steps)


def _at_the_limit(
    storage: Storage | None,
    flow: SlotFlow,
    hours: float,
    price: float,
    p_import: float,
    stored_value: float,
    pv_price: float | None = None,
    grid_diverted: float | None = None,
) -> tuple[Step, ...]:
    """A slot at the import limit (docs/methodology/make-up-rule.md, *Limits and unusual
    plants*): PV diverted from charging, the grid's charge diverted (``GRID``, at the
    band's charge side), the battery's headroom, then no grid step, since the limit
    leaves no room. Load beyond them is unserved, at the import as a floor: the one step
    where nothing else serves."""
    steps: list[Step] = []
    if storage is not None:
        band_pv, battery_price = storage.dead_band(stored_value, flow)
        divertible = storage.divertible_kw(flow)
        if divertible > _RESIDUE_KW:
            steps.append(Step(Served.PV, divertible, band_pv if pv_price is None else pv_price))
        if flow.grid_to_battery > _RESIDUE_KW:
            diverted = band_pv if grid_diverted is None else grid_diverted
            steps.append(Step(Served.GRID, flow.grid_to_battery, diverted))
        # The battery's step where the price rule lets it serve (``price_flows``'s
        # thresholds): below its discharge limit and above its reserve.
        headroom = storage.headroom(flow, hours)
        soc = flow.soc_end
        can = (
            not storage.at_discharge_limit(flow, margin_kw=MODEL.need_kw)
            and soc is not None
            and soc > storage.reserve_soc + MODEL.soc
        )
        if headroom > _RESIDUE_KW and can:
            steps.append(Step(Served.BATTERY, headroom, battery_price))
    return tuple(steps) or (Step(Served.GRID, 0.0, price),)
