"""What the battery holds and what it cost, and that cost run forward along a projection:
the accounting the ledger fills and the price reads: a battery kWh is priced at what the
energy stored in the battery cost (docs/methodology/stored-value.md).

Neutral, like :mod:`microgrid.plant.flows`: these are the types both sides use,
so the ledger (``microgrid.engine.ledger``, which fills :class:`Lots` from the meters)
and the price model (which reads them) meet here and know nothing of each other. A plant
answers its part through the port: what a kWh charged from each source adds to what the
battery stores, by that source's own path (``Storage.stored_per_kwh``), so a battery on a
hybrid's DC bus, folded or split, costs grid and PV energy by the paths they take. Any
plant model whose ``Storage`` answers that question has the stored-cost price
(:func:`project_average`); ``microgrid.engine.testing`` checks it.

Pure: no I/O.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from microgrid.plant.flows import SlotFlow
from microgrid.plant.port import ChargeSource, Storage

_EPS = 1e-9

#: Below this, a lot holds nothing (kWh).
_LOT_EPS = 1e-9


class Source(StrEnum):
    """Where a lot's energy came from."""

    #: Of unknown origin: held when the ledger started, or appearing when the model
    #: held nothing. No known cost.
    UNKNOWN = "unknown"
    PV = "pv"
    GRID = "grid"


@dataclass(frozen=True, slots=True)
class Amount:
    """Stored kWh and their value, in the prices' currency."""

    kwh: float = 0.0
    value: float = 0.0

    def __add__(self, other: Amount) -> Amount:
        return Amount(self.kwh + other.kwh, self.value + other.value)

    @property
    def unit(self) -> float:
        """Value per stored kWh (0 when empty)."""
        return self.value / self.kwh if self.kwh > _LOT_EPS else 0.0


@dataclass(frozen=True, slots=True)
class Lots:
    """What the battery holds, by source, as of the end of the last slot the ledger saw;
    and all of it as one pool, its running average cost (``pool``: what prices a kWh
    the battery delivers)."""

    as_of: datetime
    held: Mapping[Source, Amount]
    #: All the battery holds and its cost, every source pooled: charging adds at cost, a
    #: draw takes at the average. None: not kept (the price derives it from ``held``).
    pool: Amount | None = None


def blended_basis(held: Mapping[Source, Amount], unknown_basis: float) -> float | None:
    """The blended cost of what the battery holds, per stored kWh: solar at the export it
    gave up, grid energy at what it was bought for, energy of unknown origin at
    ``unknown_basis`` (per stored kWh); None when it holds nothing."""
    stored = sum(lot.kwh for lot in held.values())
    if stored <= _EPS:
        return None
    unknown = held.get(Source.UNKNOWN, Amount())
    value = sum(lot.value for source, lot in held.items() if source is not Source.UNKNOWN)
    return (value + unknown.kwh * unknown_basis) / stored


def project_average(
    storage: Storage,
    flows: Sequence[SlotFlow],
    hours: Sequence[float],
    p_import: Sequence[float],
    p_export: Sequence[float],
    *,
    lots: Lots | None,
) -> list[float | None]:
    """Per stored kWh, at the start of each slot of ``flows``: the running average cost
    of all the battery holds then, its pool now (``lots``; a client that keeps
    the lots alone: their blend) run forward along the flows.

    Charging adds at cost, through each source's own path (``storage.stored_per_kwh``):
    solar at the export it gives up (AC or DC PV alike, per kWh of it), the grid at the
    import it pays. After each slot the pool is scaled to the projection's stored energy,
    which takes a discharge at the average. Energy of unknown origin (all of it without
    ``lots``, or what appears in an empty pool) is priced as bought: the slot's import
    per stored kWh through the grid's path at the charge limit (tariffkit#67 R-14: a
    missing input moves the price toward the import). None where it holds nothing."""
    if not (len(flows) == len(hours) == len(p_import) == len(p_export)):
        raise ValueError("per-slot inputs must have the same length")
    if not flows:
        return []
    capacity = storage.capacity_kwh
    as_bought = 1.0 / storage.stored_per_kwh(ChargeSource.GRID)
    pool: Amount | None = None
    if lots is not None:
        pool = lots.pool
        if pool is None:  # a client that keeps lots only: their blend
            kwh = sum(amount.kwh for amount in lots.held.values())
            basis = blended_basis(lots.held, p_import[0] * as_bought)
            pool = None if basis is None else Amount(kwh, kwh * basis)

    def scaled(held: Amount, stored: float | None, index: int) -> Amount:
        if stored is None:
            return held
        if held.kwh <= _EPS:  # nothing known: energy of unknown origin
            return Amount(stored, stored * p_import[index] * as_bought)
        return Amount(stored, held.unit * stored)

    first = flows[0].soc_start
    held = scaled(
        pool if pool is not None else Amount(), None if first is None else first * capacity, 0
    )
    found: list[float | None] = []
    for index, slot in enumerate(flows):
        found.append(held.unit if held.kwh > _EPS else None)
        for source, kw, price in (
            (ChargeSource.PV, slot.pv_to_battery, p_export[index]),
            (ChargeSource.GRID, slot.grid_to_battery, p_import[index]),
            (ChargeSource.DC_PV, slot.dc_pv_to_battery, p_export[index]),
        ):
            if kw > _EPS:
                kwh = kw * hours[index]
                held = held + Amount(kwh * storage.stored_per_kwh(source, slot), kwh * price)
        after = slot.soc_end
        held = scaled(held, None if after is None else after * capacity, index)
    return found
