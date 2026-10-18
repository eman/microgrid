"""What a projection is: per-slot flows, the surplus they leave, and the regime
(``microgrid.plant.flows``, public).

These are the types the plant model returns, for any use of it; the price model
(``microgrid.engine``) reads them too, and they know nothing of it: a plant model
projects :class:`SlotFlow` s, and the price model reads their surplus and regime
without knowing the plant. :class:`Flows` is the metered counterpart: one slot's
meters, split between sources and sinks by the plant (``plant._meters.split_paths``).

**Surplus** is PV the site cannot use: more than the house takes and the
battery can store, so the plan exports it (or curtails it above an export
limit). It is what the plant would observably export, slot by slot, and it is
additive: its sum over a day is the PV energy there is to share. While the
battery is still charging and nothing is exported, there is no surplus.
Battery export is never surplus. A load that is running is in the load series,
so the surplus is net of its draw.

Pure: no I/O. Times are UTC.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType

__all__ = [
    "Flows",
    "MeterReading",
    "PvKw",
    "Regime",
    "RunEnd",
    "SlotDispatch",
    "SlotFlow",
    "Surplus",
    "pv_frozen",
    "pv_joined",
    "pv_slice",
    "pv_slots",
    "regimes",
    "surplus",
]

_EPS = 1e-9


#: PV over slots, kW: one series for the site, in its arrays' one coupling's terms (AC kW
#: for AC-coupled arrays, DC kW for DC ones), or one per array by name, each in its own. A
#: plant with both couplings takes it per array. The plant model reads it; the price model
#: only slices and passes it on.
type PvKw = Sequence[float] | Mapping[str, Sequence[float]]


def pv_slots(pv: PvKw) -> int:
    """How many slots ``pv`` covers (every array the same; a mapping with none, 0)."""
    if isinstance(pv, Mapping):
        lengths = {len(series) for series in pv.values()}
        if len(lengths) > 1:
            raise ValueError("every array's PV must cover the same slots")
        return lengths.pop() if lengths else 0
    return len(pv)


def pv_slice(pv: PvKw, start: int, end: int | None = None) -> PvKw:
    """``pv`` over slots ``start`` to ``end``: a list for one series, as callers built
    it before PV per array, or each array's."""
    if isinstance(pv, Mapping):
        return {name: list(series[start:end]) for name, series in pv.items()}
    return list(pv[start:end])


def pv_frozen(pv: PvKw) -> PvKw:
    """``pv`` that cannot be changed: a tuple, or a read-only mapping of tuples."""
    if isinstance(pv, Mapping):
        return MappingProxyType({name: tuple(series) for name, series in pv.items()})
    return tuple(pv)


def pv_joined(first: float | Mapping[str, float], rest: PvKw, slots: Sequence[int]) -> PvKw:
    """``first`` for one slot, then ``rest`` at ``slots``: the live value's window at
    measured PV, then the forecast (``_live.revalue``). A valuation per array takes PV
    measured per array, and one for the site its total."""
    if isinstance(rest, Mapping):
        if not isinstance(first, Mapping) or set(first) != set(rest):
            raise ValueError("a valuation per array takes PV measured per array")
        return {name: [first[name], *(series[i] for i in slots)] for name, series in rest.items()}
    if isinstance(first, Mapping):
        raise ValueError("a valuation for the site takes its measured total")
    return [first, *(rest[i] for i in slots)]


class Regime(StrEnum):
    """What +1 kWh of flexible load in a slot would displace."""

    #: PV that would be exported.
    EXPORT = "export"
    #: PV that would be curtailed.
    CURTAIL = "curtail"
    #: Energy the battery would otherwise hold or deliver.
    BATTERY = "battery"
    #: Grid import.
    IMPORT = "import"


#: The plant's meters now, kW keyed by the configured meters' names, each in its own sign
#: convention; a meter missing, or None, is unknown.
type MeterReading = Mapping[str, float | None]


@dataclass(frozen=True, slots=True)
class Flows:
    """One slot's metered flows, mean kW, allocated to sources and sinks: AC, except
    the DC-bus paths, each in its path's terms as ``SlotFlow``'s are.
    ``consumed`` is what the AC side served: its loads, the inverters' draw, and what
    the hybrid took from it for DC-bus loads."""

    consumed: float
    pv_to_load: float
    battery_to_load: float
    grid_to_load: float
    pv_to_battery: float
    grid_to_battery: float
    pv_export: float
    battery_export: float
    #: DC PV into a battery on the DC bus, DC kW; the battery serving DC-bus loads.
    dc_pv_to_battery: float = 0.0
    battery_to_dc_load: float = 0.0


@dataclass(frozen=True, slots=True)
class RunEnd:
    """What a stored kWh less does in the slot where a battery-export run reaches its
    floor: the run ends sooner. Each per stored kWh: the AC export the site loses (net of
    PV the run's end frees), the AC the battery no longer delivers (the discharge wear
    saved), the AC the house imports instead (where the floor is the reserve), the AC of
    freed PV that charges the battery (at its charge wear), and the stored kWh still short
    after the slot (signed: below 0, ahead), which the next event prices. The plant
    model's."""

    export_lost: float
    not_delivered: float
    imported: float
    #: Freed PV charged: on the AC bus (AC kWh), and on the battery's DC bus (DC kWh).
    refilled: float
    refilled_dc: float
    carried: float


@dataclass(frozen=True, slots=True, kw_only=True)
class SlotDispatch:
    """One slot's dispatch bookkeeping: what a plant model that decides the battery's
    dispatch knows of the slot beyond its flows. A plant model without it
    (``SlotFlow.dispatch`` left at its default) gets every field's default: no hold, no
    grid-charge window (the price rule reads the grid charging the battery instead), no
    battery-export run's end, no PV clipped on a DC bus, all unserved load reachable,
    no hold session, and no hybrid's AC (the ground truth then meters a battery on a
    DC bus by its own flows, which leaves out DC PV and DC-bus loads crossing).

    **What reads it.** The price rule reads ``held``, ``topup_gap``, ``run_end``,
    ``bus_clipped`` and ``unserved_unreachable`` (through
    :attr:`SlotFlow.unserved_reachable`); the live value reads ``hold_session``; the
    general dispatch's ``Storage`` reads ``crossing``, ``battery_port`` and its gross
    ``battery_port_in`` and ``battery_port_out``; the ground truth reads ``crossing_ac``.
    """

    #: The plant holds the battery (tops it up, keeps it at its target, or a hold rule
    #: keeps it through a DC-bus load's session) and the grid serves the loads. False
    #: for metered flows.
    held: bool = False
    #: In the plant's grid-charge window, how far short of its course to the target
    #: the battery is, stored kWh, before the slot's top-up (> 0: it tops up; about
    #: 0: on course, after an early top-up; < 0: PV will carry it there, or later in
    #: the window for a late top-up). A kWh less where it is not below 0 is bought
    #: back from the grid. None outside the window and for metered flows, which show
    #: only the grid charging.
    topup_gap: float | None = None
    #: The net DC kW the hybrid inverter converts from its DC bus to the AC side (< 0:
    #: from the AC side to the DC bus), and the battery's power at its own port, kW
    #: (> 0 charging; DC at a DC bus, AC on the AC bus): what a plant with a DC bus
    #: answers its ``Storage`` questions from.
    crossing: float = 0.0
    battery_port: float = 0.0
    #: The AC kW the hybrid's net crossing gives the AC side (< 0: takes from it): what
    #: its AC meter reads before its own draw, the battery's flows, the DC PV crossing
    #: and the AC it takes for DC-bus loads all in it. Each part's own, so a slot run
    #: in parts holds their mean (the curve at the mean ``crossing`` is not it). 0
    #: without a hybrid; None for metered flows and a plant model that does not give
    #: it.
    crossing_ac: float | None = None
    #: What the battery took in at its port and gave out over the slot, kW, each >= 0 in
    #: ``battery_port``'s terms: a metered slot can both charge and discharge, which the
    #: net ``battery_port`` hides. A plant model that leaves both at 0 gets its storage's
    #: answers from the net: in where ``battery_port`` > 0, out where < 0.
    battery_port_in: float = 0.0
    battery_port_out: float = 0.0
    #: Of ``SlotFlow.pv_clipped``, what the hybrid clipped at its rating on its DC bus:
    #: what a battery there, with room, would take (the fill's clipped PV). The rest was
    #: capped at an array's own rating, before the bus. 0 for metered flows.
    bus_clipped: float = 0.0
    #: Where a battery-export run reaches its floor in this slot, what a stored kWh less
    #: does (:class:`RunEnd`); None elsewhere, and for metered flows.
    run_end: RunEnd | None = None
    #: Of ``SlotFlow.unserved``, what no stored kWh could serve: a DC-bus load beyond
    #: the hybrid's rating, the battery not on its bus (on its own inverter, the battery
    #: crosses the same rating). A kWh more or less in the battery leaves it as it is,
    #: so the price rule does not read it as unserved.
    unserved_unreachable: float = 0.0
    #: In a hold rule's session, whether it holds the battery; None outside one, and for
    #: metered flows. A projection that starts in a session takes it as
    #: ``held_at_start``, as the plant decided it where the session began.
    hold_session: bool | None = None


_NO_DISPATCH = SlotDispatch()


@dataclass(frozen=True, slots=True, kw_only=True)
class SlotFlow:
    """Mean power flows (kW) over one slot, by path, and the SOC (0-1) at its ends.

    **Paths.** The ``pv_*`` fields without ``dc_`` are PV
    on the AC bus: an AC array's, or a DC array's that the hybrid converted
    (``dc_pv_to_ac``). The ``dc_pv_*`` fields are a DC array's PV on the DC bus.
    ``pv_to_load``, ``battery_to_load`` and ``grid_to_load`` serve the AC side: its
    loads, the inverters' draw (``inverter_draw``) and what the hybrid takes from
    it for DC-bus loads (``ac_to_dc_load``).

    **Units.** Energy into the battery is in
    its source's terms (``pv_to_battery`` AC, ``dc_pv_to_battery`` DC,
    ``grid_to_battery`` AC at the meter); what serves a load is what the load
    receives (AC on the AC side, DC for a DC-bus load); what crosses the site's
    meter is AC at the meter; ``dc_pv_to_ac`` is what left the DC array.

    **Balance.** Over a slot ``hours`` long, in which the stored energy moves by
    ``stored`` kWh (from the SOC), sources equal sinks: the PV +
    ``grid_to_load`` + ``grid_to_battery`` = the loads - ``unserved`` +
    ``inverter_draw`` + ``pv_export`` + ``battery_export`` + ``pv_curtail`` +
    ``pv_clipped`` + ``conversion_loss`` + ``stored / hours``. The PV (each
    array's output, in its own terms) and the loads are the slot's inputs to the
    projection, not fields: once a DC path carries energy, neither can be rebuilt
    from the fields, since the AC a DC array delivers and the AC a DC-bus load
    takes are inside the AC fields.

    A path the plant does not have is 0. On this site's plant (AC-coupled PV, the
    battery folded on the hybrid's DC bus, one AC load) every ``dc_`` path,
    ``battery_export``, ``ac_to_dc_load`` and ``unserved`` is 0, and the fields up
    to ``pv_clipped`` carry every flow.

    Keyword-only; the dispatch's bookkeeping is one record, ``dispatch``
    (:class:`SlotDispatch`).
    """

    start: datetime
    #: None for a plant without a battery.
    soc_start: float | None
    soc_end: float | None
    pv_to_load: float = 0.0
    pv_to_battery: float = 0.0
    pv_export: float = 0.0
    pv_curtail: float = 0.0
    battery_to_load: float = 0.0
    grid_to_load: float = 0.0
    grid_to_battery: float = 0.0
    #: PV the plant could neither convert to AC nor store, such as DC beyond a hybrid
    #: inverter's AC rating with the battery full: no AC load can take it, so it is
    #: neither spill nor surplus, and nothing that prices spare PV reads it. The
    #: AC-coupled plant reports 0: its clip happens in the micro-inverters, before the
    #: forecast.
    pv_clipped: float = 0.0
    # Each path is 0 by default: a path the plant does not have.
    #: DC PV into the battery on the DC bus, DC kW.
    dc_pv_to_battery: float = 0.0
    #: DC PV the hybrid converts to the AC side, DC kW: what it delivers is in the AC
    #: bus's PV fields, and its loss in ``conversion_loss``.
    dc_pv_to_ac: float = 0.0
    #: DC PV serving DC-bus loads, DC kW.
    dc_pv_to_dc_load: float = 0.0
    #: The battery serving DC-bus loads, DC kW.
    battery_to_dc_load: float = 0.0
    #: The battery exported, AC kW at the meter. The plant model's: a metered slot
    #: leaves it out until the price rule has the export event.
    battery_export: float = 0.0
    #: The AC side serving DC-bus loads through the hybrid, DC kW the loads receive.
    #: The AC it takes is inside the ``*_to_load`` fields, and its loss in
    #: ``conversion_loss``.
    ac_to_dc_load: float = 0.0
    #: Load no source could serve, kW: what an import limit leaves (AC at the meter), or
    #: a DC-bus load beyond what the hybrid's rating lets the AC side send it (DC at the
    #: load).
    unserved: float = 0.0
    #: Energy lost converting, kW: what the battery's charge took less what it
    #: stored, what its discharge drew less what it delivered, and the hybrid's other
    #: conversions. The plant model's; 0 for metered flows.
    conversion_loss: float = 0.0
    #: The inverters' own draw, kW: standby, and the overhead while the battery works.
    #: It is inside the ``*_to_load`` fields, which serve the load and it, and the
    #: balance counts it once. The plant model's; 0 for metered flows.
    inverter_draw: float = 0.0
    #: The plant model's dispatch bookkeeping (:class:`SlotDispatch`); a plant model
    #: without it leaves it at the default, every field's default.
    dispatch: SlotDispatch = field(default_factory=lambda: _NO_DISPATCH)

    @property
    def unserved_reachable(self) -> float:
        """Unserved load a stored kWh would serve: what the price rule reads."""
        return self.unserved - self.dispatch.unserved_unreachable

    @property
    def battery_out(self) -> float:
        """All the battery delivers, kW: to the AC side, to DC-bus loads and exported,
        each in its path's terms."""
        return self.battery_to_load + self.battery_to_dc_load + self.battery_export

    @property
    def moving(self) -> float:
        """What the battery charges or discharges, kW, each path in its terms (all AC
        on this site's plant)."""
        return (
            self.pv_to_battery
            + self.grid_to_battery
            + self.battery_to_load
            + self.dc_pv_to_battery
            + self.battery_to_dc_load
            + self.battery_export
        )

    @property
    def pv_spare(self) -> float:
        """PV the loads do not use: charging, exported or curtailed (not clipped,
        which nothing could use)."""
        return self.pv_to_battery + self.pv_export + self.pv_curtail + self.dc_pv_to_battery


#: Surplus below this is float noise, not energy: 1 W.
_SURPLUS_FLOOR_KW = 0.001


@dataclass(frozen=True, slots=True)
class Surplus:
    """PV the plan exports or curtails in each slot, and what a kWh of it is worth: the
    average over the slot's surplus, export and curtailment weighted by energy, as a
    load taking the surplus pays it. A marginal kWh is priced at the margin instead
    (``_value._spill_value``: curtailed PV first, at 0, under an export limit)."""

    kw: tuple[float, ...]
    #: True where more of it is curtailed than exported.
    curtailed: tuple[bool, ...]
    #: The price per kWh a flexible load takes from it: the slot's export price for the
    #: exported part, 0 for the curtailed part, weighted by energy. None where
    #: there is no surplus, or exported surplus has no price.
    value: tuple[float | None, ...]


def surplus(flows: Sequence[SlotFlow], p_export: Sequence[float | None] | None = None) -> Surplus:
    """Surplus per slot of ``flows`` (module docstring), valued at ``p_export``."""
    if p_export is not None and len(p_export) < len(flows):
        raise ValueError("p_export must cover every flow")
    kws: list[float] = []
    curtailed: list[bool] = []
    values: list[float | None] = []
    for index, flow in enumerate(flows):
        kw = flow.pv_export + flow.pv_curtail
        if kw < _SURPLUS_FLOOR_KW:
            kws.append(0.0)
            curtailed.append(False)
            values.append(None)
            continue
        price = p_export[index] if p_export is not None else None
        value = None
        if flow.pv_export <= _EPS:
            value = 0.0  # all curtailed: it would have been worth nothing
        elif price is not None:
            value = flow.pv_export * price / kw
        kws.append(kw)
        curtailed.append(flow.pv_curtail > flow.pv_export)
        values.append(value)
    return Surplus(kw=tuple(kws), curtailed=tuple(curtailed), value=tuple(values))


def regimes(
    flows: Sequence[SlotFlow],
    surplus: Surplus,
    import_limit_kw: float | None = None,
    divertible_kw: Callable[[SlotFlow], float] | None = None,
) -> tuple[Regime, ...]:
    """What +1 kWh of flexible load would displace in each slot. At the grid's
    ``import_limit_kw`` a kWh more is not imported: the battery's (its charge or its
    discharge), or, where load is unserved, the import as a floor.
    ``divertible_kw`` is the storage's answer for the PV charging it that a load could
    take (``Storage.divertible_kw``): without it, the AC PV charging it."""
    result = []
    for flow, spare, curtailed in zip(flows, surplus.kw, surplus.curtailed, strict=True):
        at_limit = (
            import_limit_kw is not None
            and flow.grid_to_load + flow.grid_to_battery
            >= import_limit_kw - _SURPLUS_FLOOR_KW  # the price rule's MODEL.short_kw
        )
        if spare > _EPS:
            result.append(Regime.CURTAIL if curtailed else Regime.EXPORT)
        elif at_limit:
            unserved = flow.unserved_reachable > _EPS
            result.append(Regime.IMPORT if unserved else Regime.BATTERY)
        elif flow.grid_to_load > _EPS or flow.grid_to_battery > _EPS:
            result.append(Regime.IMPORT)
        elif (
            flow.battery_to_load > _EPS
            or (divertible_kw(flow) if divertible_kw is not None else flow.pv_to_battery) > _EPS
            or flow.battery_to_dc_load > _EPS
        ):
            # The battery serves a load, or PV charges it that a load could take (DC PV
            # only within the hybrid's rating): a kWh more is its.
            result.append(Regime.BATTERY)
        else:
            result.append(Regime.IMPORT)
    return tuple(result)
