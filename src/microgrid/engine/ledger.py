"""The battery's lots: where its energy came from and what it cost.

The method: docs/methodology/lots.md. What the battery holds by source, and what it
cost: a client publishes the split, and the floor (:func:`floor_per_kwh`). All it holds
also as one pool (:attr:`Ledger.pool`), whose running average cost prices a kWh it
delivers, run forward along each projection (:func:`project_average`, through the
plant's ``Storage``): a battery kWh is priced at what the energy stored in the battery
cost. The pool, not the lots' blend: a draw takes the average, so only what charges it
moves the price, not the order the lots are drawn in. The types (:class:`Lots`,
:class:`Amount`, :class:`Source`) and the projection are the engine's neutral
accounting, which the price model reads without this module.

**Units.** Lots hold *stored* kWh (DC side). The plant model's meter reading
(``PlantModel.read_meters``) turns the plant's DC
battery power into AC flows; charging ``c`` AC kWh at price ``p``
stores ``c·η_c`` at a basis of ``p/η_c`` per stored kWh.
Delivering ``d`` AC kWh draws ``d/η_d`` stored kWh, and the drawn value is the
delivery's cost, conversion loss included.

**Flows at sample resolution.** :func:`integrate` reads the metered powers at every
change of any of them and sums over the slot. A 10-minute *net* mean blurs two states
into one: when a DC-bus load's session starts the battery swings from charging on PV to
discharging into it, and the mean looks like the grid charging the battery (on the
reference plant's meters, 10-minute means booked 55-250 Wh of such "grid lots" a
session). Only stretches where the telemetry path was alive count (the caller's alive
spans, ``series.alive``); a slot with any silent stretch moves nothing, and the meter
anchor catches up.

**Order.** The house is served from grid lots first and exports come from PV
lots first (restricted-first: grid energy can go to one destination, PV
to either); within a source, a weighted average. Energy of unknown origin is
drawn first by both.

**The meter wins.** After each slot the lots are scaled to the metered stored
energy, both ways and pro rata at their own basis, so what the model does not
explain (self-discharge, the BMS, meter timing, a wrong efficiency) shows as
``unexplained``, never as drift, and never as free energy. A draw larger than
the lots hold scales them up the same way first.

**Unknown energy** (one rule): what the battery held when the ledger started,
or what the meter shows when the model held nothing, has no known cost. It is
kept apart (:attr:`Source.UNKNOWN`) at no value, reported as such, and never
sets a floor (:func:`floor_per_kwh`). The price values it at the import price, as
if bought (:func:`blended_basis`'s ``unknown_basis``): not knowing what it cost moves
the price toward the import, as every missing input does (R-7).

**Efficiencies.** A charge is booked through the plant's paths where the caller gives
the slot as the plant model's (:class:`Ledger`): each source's stored kWh per kWh, by
the path it took (``Storage.stored_per_kwh``). Else through the battery's fitted
conversion (``Battery.stored_by``), whose loss grows with power: PV and the grid
charge through the same inverter on an AC-coupled plant, so they share it, and the
grid's top-ups lose more only because they run at the charge limit. A discharge is
drawn through ``Battery.drawn_by``.

Pure: no I/O.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, fields
from datetime import datetime, timedelta

from microgrid.engine._stored import Amount, Lots, Source, blended_basis, project_average
from microgrid.engine.series import Series, segments
from microgrid.plant._battery import Battery
from microgrid.plant.flows import Flows, SlotFlow
from microgrid.plant.port import ChargeSource, PlantModel, Storage

_EPS = 1e-9
#: Float residue, not energy: 1 Wh.
_RESIDUE_KWH = 0.001

__all__ = [
    "Amount",
    "Ledger",
    "LedgerSlot",
    "Lots",
    "Source",
    "blended_basis",
    "floor_per_kwh",
    "integrate",
    "project_average",
    "run_ledger",
    "soc_boundary",
]

_HOUR = timedelta(hours=1)


_FLOW_FIELDS = tuple(flow_field.name for flow_field in fields(Flows))


def integrate(
    series: Mapping[str, Series[float]],
    starts: Sequence[datetime],
    step: timedelta,
    *,
    plant: PlantModel,
    alive_spans: Sequence[tuple[datetime, datetime]] | None = None,
    stale_nonzero: Mapping[str, timedelta] | None = None,
) -> list[Flows | None]:
    """Each slot's flows, as ``plant`` reads its meters (``PlantModel.read_meters``) over
    every stretch the metered powers hold (``series.segments``; mean kW).

    ``series`` holds one power series per meter of ``plant``, named as ``plant.meters``
    names them: any meter set. A stretch counts only while every meter has a value and
    the path is alive (``alive_spans``, from ``series.alive``; none: always); a slot
    with any stretch that does not is None. ``stale_nonzero``: a meter whose reading
    other than 0 has been silent that long has no value (a lost 0 that ended a DC-bus
    load's session, say).
    """
    if set(series) != set(plant.meters):
        raise ValueError(f"one series per meter of the plant ({sorted(plant.meters)})")
    if not starts:
        return []
    end = starts[-1] + step
    hours = step / _HOUR
    sums = [dict.fromkeys(_FLOW_FIELDS, 0.0) for _ in starts]
    silent = [False] * len(starts)
    slot = 0
    for segment in segments(
        series, [*starts, end], alive_spans=alive_spans, stale_nonzero=stale_nonzero
    ):
        while slot + 1 < len(starts) and starts[slot + 1] <= segment.start:
            slot += 1
        values = segment.values
        if not segment.alive or any(value is None for value in values.values()):
            silent[slot] = True
            continue
        share = (segment.end - segment.start) / _HOUR / hours
        flows = plant.read_meters(values)
        if flows is None:  # pragma: no cover - every meter is known here
            silent[slot] = True
            continue
        totals = sums[slot]
        for name in _FLOW_FIELDS:
            totals[name] += getattr(flows, name) * share
    return [None if gap else Flows(**totals) for gap, totals in zip(silent, sums, strict=True)]


def soc_boundary(soc_means: Sequence[float | None], index: int) -> float | None:
    """The SOC at the end of slot ``index``, from 10-minute means.

    The mean of the two slots either side of the boundary: a slot's mean is
    its middle's value, and the next slot's alone would lag the boundary by
    half a slot (the lots would be anchored five minutes late).
    """
    here = soc_means[index]
    after = soc_means[index + 1] if index + 1 < len(soc_means) else None
    if here is None or after is None:
        return after if here is None else here
    return (here + after) / 2


@dataclass(frozen=True, slots=True)
class LedgerSlot:
    """What one slot did to the lots."""

    start: datetime
    #: Stored kWh added, and what the AC energy cost (price x AC kWh).
    charged: Mapping[Source, Amount]
    #: Stored kWh drawn for the house, and their value.
    to_load: Mapping[Source, Amount]
    #: Stored kWh drawn for export, and their value.
    to_export: Mapping[Source, Amount]
    #: The meter against the model: kWh > 0 more than the model held, < 0 less; at the
    #: lots' own basis (at no cost only when the model held nothing).
    unexplained: Amount
    #: The lots after the slot.
    held: Mapping[Source, Amount]
    #: All held after the slot, pooled, and its cost (:attr:`Ledger.pool`).
    pool: Amount = field(default_factory=Amount)
    #: AC kWh the slot's discharge delivered per stored kWh it drew (0: none).
    delivered_per_drawn: float = 0.0
    #: Stored kWh drawn for DC-bus loads, and their value; and the DC kWh they
    #: received per stored kWh (0: none).
    to_dc_load: Mapping[Source, Amount] = field(default_factory=dict)
    dc_delivered_per_drawn: float = 0.0


_LOAD_ORDER = (Source.UNKNOWN, Source.GRID, Source.PV)
_EXPORT_ORDER = (Source.UNKNOWN, Source.PV, Source.GRID)


class Ledger:
    """The battery's lots, advanced one slot at a time.

    **What it needs of the plant** (the extension tier). A charge is booked
    through ``storage``'s paths where the caller gives each slot as the plant model's
    (``step``'s ``slot``: ``Plant.metered_slot``), so a battery on a hybrid's DC bus costs
    grid and PV energy by the paths they took (``Storage.stored_per_kwh``); without them,
    through ``battery``'s own law (an AC-coupled battery's, exactly). A discharge is drawn
    through ``battery``'s law either way: the meter anchor (``stored_after``) scales what
    any law leaves out. So a client with its own plant model passes a :class:`Battery`
    describing its battery's conversion and its ``Storage``; or keeps its lots itself and
    hands them to the engine as ``Lots``, whose price needs ``Storage.stored_per_kwh``
    alone (``project_average``).
    """

    def __init__(
        self,
        battery: Battery,
        unknown_kwh: float = 0.0,
        *,
        unknown_basis: float | None = None,
        storage: Storage | None = None,
    ) -> None:
        """``unknown_kwh``: what the battery holds now, of unknown origin: at no cost in
        the lots; in the pool at ``unknown_basis`` per stored kWh, else at the first
        slot's import price, as if bought (R-7: toward the import),
        through ``storage``'s grid path where given. What the meter later finds in an
        empty model is priced at that slot's import."""
        self._battery = battery
        self._storage = storage
        #: Per stored kWh, the import of the slot being stepped (None: none stepped).
        self._unknown_basis: float | None = None
        self._start_priced = unknown_basis is not None
        unknown = max(unknown_kwh, 0.0)
        self._lots: dict[Source, Amount] = {
            Source.UNKNOWN: Amount(unknown, 0.0),
            Source.PV: Amount(),
            Source.GRID: Amount(),
        }
        #: Everything held, pooled: its running average cost (``pool``).
        self._pool = Amount(unknown, unknown * (unknown_basis or 0.0))

    @property
    def pool(self) -> Amount:
        """All the battery holds and its cost, pooled: charging adds at cost, a draw
        takes at the average, the meter's scaling keeps it. Its ``unit`` is the running
        average cost per stored kWh, what prices a kWh the battery delivers."""
        return self._pool

    @property
    def held(self) -> Mapping[Source, Amount]:
        return dict(self._lots)

    def _draw(self, kwh: float, order: Sequence[Source]) -> dict[Source, Amount]:
        drawn: dict[Source, Amount] = {}
        for source in order:
            if kwh <= _EPS:
                break
            lot = self._lots[source]
            taken = min(kwh, lot.kwh)
            if taken <= _EPS:
                continue
            part = Amount(taken, taken * lot.unit)
            self._lots[source] = Amount(lot.kwh - part.kwh, lot.value - part.value)
            drawn[source] = part
            kwh -= taken
            pool = self._pool
            self._pool = Amount(pool.kwh - taken, pool.value - taken * pool.unit)
        return drawn

    def step(
        self,
        start: datetime,
        flows: Flows | None,
        hours: float,
        *,
        pv_price: float,
        grid_price: float,
        stored_after: float | None,
        slot: SlotFlow | None = None,
    ) -> LedgerSlot:
        """Advance one slot.

        ``flows`` None (telemetry missing) moves nothing; ``pv_price`` and
        ``grid_price`` are per kWh charged in the slot (AC, or DC for DC PV);
        ``stored_after`` is the metered stored energy at the slot's end, kWh (None:
        unknown). ``slot``: the same slot as the plant model's (``Plant.metered_slot``),
        whose charge ``storage`` converts by path (the class's docstring).
        """
        battery = self._battery
        storage = self._storage
        # Energy of unknown origin, priced as if bought: the import, per stored kWh
        # (R-7: a missing input moves the price toward the import).
        if storage is not None:
            basis = grid_price / storage.stored_per_kwh(ChargeSource.GRID)
        else:
            basis = grid_price * battery.charge_kw / battery.stored_by(battery.charge_kw)
        if not self._start_priced:  # what it held when it began
            self._pool = Amount(self._pool.kwh, self._pool.value + self._pool.kwh * basis)
            self._start_priced = True
        self._unknown_basis = basis
        charged: dict[Source, Amount] = {}
        to_load: dict[Source, Amount] = {}
        to_export: dict[Source, Amount] = {}
        to_dc_load: dict[Source, Amount] = {}
        unexplained = Amount()
        per_drawn = dc_per_drawn = 0.0
        if flows is not None:
            # PV and the grid charge through the one inverter, whose loss grows with
            # the power it converts: each stores its share of the whole charge.
            charging = flows.pv_to_battery + flows.grid_to_battery
            stored_per_ac = battery.stored_by(charging) / charging if charging > _EPS else 0.0
            by_path = storage is not None and slot is not None
            for source, path, ac_kw, price in (
                (Source.PV, ChargeSource.PV, flows.pv_to_battery, pv_price),
                (Source.GRID, ChargeSource.GRID, flows.grid_to_battery, grid_price),
            ):
                if ac_kw > _EPS:
                    per = (
                        storage.stored_per_kwh(path, slot)
                        if storage is not None and slot is not None
                        else stored_per_ac
                    )
                    added = Amount(ac_kw * hours * per, ac_kw * hours * price)
                    charged[source] = added
                    self._lots[source] = self._lots[source] + added
                    self._pool = self._pool + added
            # DC PV into a battery on its own bus: a solar lot at the export price
            # per DC kWh (what the hybrid could have exported), its stored kWh through the
            # battery's port where the slot gives it, else as metered DC (the meter anchor
            # catches its losses).
            if flows.dc_pv_to_battery > _EPS:
                dc_kwh = flows.dc_pv_to_battery * hours
                if by_path and storage is not None and slot is not None:
                    stored_kwh = dc_kwh * storage.stored_per_kwh(ChargeSource.DC_PV, slot)
                else:
                    stored_kwh = dc_kwh
                added = Amount(stored_kwh, dc_kwh * pv_price)
                charged[Source.PV] = charged.get(Source.PV, Amount()) + added
                self._lots[Source.PV] = self._lots[Source.PV] + added
                self._pool = self._pool + added
            delivered = flows.battery_to_load + flows.battery_export
            if delivered > _EPS:
                per_drawn = delivered / battery.drawn_by(min(delivered, battery.discharge_kw))
            # The battery serving a DC-bus load on its own bus converts nothing: a
            # folded battery draws ``meter_discharge`` stored kWh per DC kWh it gives
            # (``Battery``). It is load: drawn in the house's order, after the house and
            # before the export.
            dc_drawn = flows.battery_to_dc_load * hours * battery.meter_discharge
            if dc_drawn > _EPS:
                dc_per_drawn = 1.0 / battery.meter_discharge
            need = (delivered * hours / per_drawn if per_drawn else 0.0) + dc_drawn
            if need > sum(lot.kwh for lot in self._lots.values()) + _EPS:
                unexplained = self._scale_to(need)  # delivered more than the model held
            if per_drawn:
                to_load = self._draw(flows.battery_to_load * hours / per_drawn, _LOAD_ORDER)
            if dc_per_drawn:
                to_dc_load = self._draw(dc_drawn, _LOAD_ORDER)
            if per_drawn:
                to_export = self._draw(flows.battery_export * hours / per_drawn, _EXPORT_ORDER)
        if stored_after is not None:
            unexplained = unexplained + self._scale_to(max(stored_after, 0.0))
        return LedgerSlot(
            start=start,
            charged=charged,
            to_load=to_load,
            to_export=to_export,
            unexplained=unexplained,
            held=dict(self._lots),
            pool=self._pool,
            delivered_per_drawn=per_drawn,
            to_dc_load=to_dc_load,
            dc_delivered_per_drawn=dc_per_drawn,
        )

    def _scale_to(self, stored: float) -> Amount:
        """Scale the lots to ``stored`` kWh, pro rata at their own basis; the change."""
        total = sum(lot.kwh for lot in self._lots.values())
        if abs(stored - total) <= _EPS:
            return Amount()
        if total <= _EPS:  # the model holds nothing: energy of unknown origin
            gain = Amount(stored - total, 0.0)
            self._lots[Source.UNKNOWN] = self._lots[Source.UNKNOWN] + gain
            self._pool = Amount(stored, stored * (self._unknown_basis or 0.0))
            return gain
        factor = stored / total
        self._pool = Amount(stored, self._pool.unit * stored)
        change = Amount()
        for source, lot in self._lots.items():
            scaled = Amount(lot.kwh * factor, lot.value * factor)
            change = change + Amount(scaled.kwh - lot.kwh, scaled.value - lot.value)
            self._lots[source] = scaled
        return change


def run_ledger(
    battery: Battery,
    starts: Sequence[datetime],
    flows: Sequence[Flows | None],
    hours: float,
    pv_price: Sequence[float],
    grid_price: Sequence[float],
    stored_after: Sequence[float | None],
    *,
    unknown_kwh: float = 0.0,
) -> list[LedgerSlot]:
    """The ledger over ``starts``, every per-slot sequence covering them."""
    if not (len(starts) == len(flows) == len(pv_price) == len(grid_price) == len(stored_after)):
        raise ValueError("per-slot inputs must have the same length")
    ledger = Ledger(battery, unknown_kwh)
    return [
        ledger.step(
            start,
            flow,
            hours,
            pv_price=pv_price[index],
            grid_price=grid_price[index],
            stored_after=stored_after[index],
        )
        for index, (start, flow) in enumerate(zip(starts, flows, strict=True))
    ]


def floor_per_kwh(
    held: Mapping[Source, Amount], battery: Battery, *, draw_kwh: float, draw_kw: float
) -> float | None:
    """The myopic guard: what the next ``draw_kwh`` drawn cost the site.

    A draw takes restricted-first (grid-charged energy before PV), down to the battery's
    reserve at ``draw_kw``; the floor is the average basis of what it takes, per kWh
    delivered (``basis/eta_d`` at that power), plus the wear a delivered kWh carries
    (``Battery.wear_per_delivered_kwh``). Averaged over a draw, not the first lot: a
    sliver of grid energy must not price the whole draw. A consumer that cannot read the
    forward-looking price should not spend battery energy on a use worth less. The price
    takes the running average of all the battery holds (:attr:`Ledger.pool`); this takes
    the next draw's.

    None when the battery holds nothing above its reserve, or when energy of
    unknown origin (no known cost) would be drawn.
    """
    if draw_kwh <= 0 or draw_kw <= 0:
        raise ValueError("draw_kwh and draw_kw must be positive")
    power = min(draw_kw, battery.discharge_kw)
    eta = power / battery.drawn_by(power)
    stored = sum(lot.kwh for lot in held.values())
    drawable = stored - battery.reserve_soc * battery.capacity_kwh
    need = min(draw_kwh / eta, drawable)
    if need <= _RESIDUE_KWH:
        return None
    left, value = need, 0.0
    for source in _LOAD_ORDER:
        lot = held.get(source, Amount())
        taken = min(left, lot.kwh)
        if taken <= _RESIDUE_KWH:
            continue
        if source is Source.UNKNOWN:
            return None
        value += taken * lot.unit
        left -= taken
        if left <= _EPS:
            break
    delivered = (need - max(left, 0.0)) * eta
    if delivered <= _EPS:
        return None
    return value / delivered + battery.wear_per_delivered_kwh
