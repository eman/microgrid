"""The general dispatch: a plant with a DC bus.

Every plant with a battery or a DC bus runs here: DC-coupled PV, a battery on a hybrid's
DC bus with anything else there (folded or split), a hybrid inverter with an AC rating,
a hybrid beside a battery on its own inverter, and today's configurations, whose own
arithmetic the property tests keep as the oracle this one is held to
(``tests/legacy_dispatch.py``, ``test_paths.py``).

**The hybrid converts the net power crossing it:** one curve (:class:`Curve`) on the net
DC kW from its DC bus to the AC side, ``x``, with its AC rating on the AC power that
crossing gives. DC PV serving the AC side while the grid charges the DC battery is
netted, and the loss is the net's. A folded battery holds that curve (its AC to DC step,
with its meter at the DC bus); a split one leaves it to the hybrid's
:class:`~microgrid.plant.Conversion` and holds the DC bus to stored energy itself
(:class:`Port`).

**The dispatch, per slot** (the plant's self-consumption, PV charging first, as the
legacy dispatch's):

1. AC PV serves the AC side (the house, and the inverters' draws).
2. DC PV serves what is left through the hybrid, within its rating.
3. PV left charges the battery: on the DC bus, DC PV first (it is there), then AC PV
   through the hybrid; on the AC bus, AC PV first, then DC PV through the hybrid.
4. In a grid-charge window (any of several) the grid tops the battery up toward its
   target, as today's (the look-ahead, the target rule, the timing, the hold),
   through the hybrid for a battery on its DC bus: netted against what crosses the
   other way. PV first, the grid leaves what PV will still add in the window; the
   target first, it does not wait for it.
5. PV still left is exported, AC PV first, then DC PV through the hybrid within its
   rating; above the export limit it is curtailed. DC PV that neither crosses nor
   charges is clipped, as is PV above an array's own rating.
6. The battery serves what PV did not, within its limits and the hybrid's rating
   (PV serves the AC side before the battery does), and the grid the rest.

In a battery-export window the battery does not charge while it exports: it exports at
the window's power, within its limits, the rating (PV crosses first) and what the export
limit leaves (``ExportWindow.yields`` says which yields there), and serves the house
beside it from what is left, until the window's floor. On its DC bus it serves the
DC-bus loads first, as they are, and converts only what is left of its limit. The slot
where it reaches the floor runs in two parts, the export until the floor and
self-consumption after; the window then stays stopped until it ends (the battery may
charge from PV, but does not export again), and the slot reports what a stored kWh less
does there (``SlotDispatch.run_end``: the run ends sooner), by running the slot again
from a little less (the derivative of the projection). A run that reaches its floor
exactly at a slot's end reports none: the next slot finds it at the floor.

**DC-bus loads** are served on their bus: DC PV first, as it is; then, once the AC side
is served, AC PV through the hybrid (rectifying, within its rating); the battery on the
DC bus serves them as they are before the AC side, and one on its own inverter through
the hybrid after it; the grid serves what is left through the hybrid, and what its
rating keeps from them is ``unserved``. A **hold rule** keeps the battery from
discharging through a DC-bus load's session that started below its SOC (``HoldRule``):
the grid serves the loads, and an export window does not export.

At an import limit the limit caps the grid's charge first; above it the battery serves
beyond its policy (held, or past its order) down to its reserve, and what neither covers
is ``unserved``. The top-up's look-ahead counts only what the grid can still give under
the limit.

**Curtailed DC PV** is what the export limit holds back that a load could take, so it is
counted in AC (``pv_curtail``), and the conversion it was spared in ``conversion_loss``:
the balance counts DC PV in DC kW (``SlotFlow``'s *Balance*).

**PV** reaches the plant per array, each in its coupling's terms (AC kW for an AC array,
DC kW for a DC one) and capped at its own rating; or, where every array has one
coupling, as one series in those terms, shared by the arrays' DC ratings and each share
capped.

Unvalidated against meters: no site of this topology has been checked.

Pure: no I/O. Times are UTC; the grid-charge window is local time.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, fields, replace
from datetime import datetime, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from microgrid._checks import require_utc
from microgrid.plant._battery import Battery
from microgrid.plant._config import ExportWindow, GridCharge, HoldRule, PlantConfig
from microgrid.plant.flows import (
    PvKw,
    RunEnd,
    SlotDispatch,
    SlotFlow,
    pv_slots,
)
from microgrid.plant.port import ChargeSource

_EPS = 1e-9


def _spans(count: int, durations: Sequence[timedelta]) -> list[float]:
    """Each slot's length in hours."""
    if len(durations) != count:
        raise ValueError("durations must have one length per slot")
    if any(d <= timedelta(0) for d in durations):
        raise ValueError("durations must be positive")
    return [d / timedelta(hours=1) for d in durations]


@dataclass(frozen=True, slots=True)
class Curve:
    """A converter between a DC bus and the AC side, on the net DC power ``x`` crossing
    it (> 0: inverting, DC to AC). Rectifying ``p`` DC kW takes ``k_in p + q_in p^2``
    AC; inverting ``p`` gives ``k_out p - q_out p^2``: ``Battery``'s law, each way."""

    k_in: float
    q_in: float
    k_out: float
    q_out: float

    @classmethod
    def folded(cls, battery: Battery) -> Curve:
        """A folded battery's AC to DC step: its meter is at the DC bus (``Battery``)."""
        return cls(
            k_in=battery.meter_charge / battery.eta_charge,
            q_in=battery.loss_charge_per_kw,
            k_out=battery.eta_discharge * battery.meter_discharge,
            q_out=battery.loss_discharge_per_kw,
        )

    @property
    def peak(self) -> float:
        """The DC kW at which inverting gives the most AC."""
        return math.inf if self.q_out == 0 else self.k_out / (2 * self.q_out)

    def ac(self, x: float) -> float:
        """The AC kW the crossing ``x`` gives the AC side (< 0: takes from it)."""
        if x >= 0:
            return self.k_out * x - self.q_out * x * x
        p = -x
        return -(self.k_in * p + self.q_in * p * p)

    def dc(self, a: float) -> float:
        """The crossing that gives the AC side ``a`` kW (the inverse of :meth:`ac`), at
        most :attr:`peak` inverting."""
        if a >= 0:
            k, q = self.k_out, self.q_out
            disc = k * k - 4 * q * a
            if disc <= 0:
                return self.peak
            return 2 * a / (k + math.sqrt(disc))
        k, q, b = self.k_in, self.q_in, -a
        return -(2 * b / (k + math.sqrt(k * k + 4 * q * b)))

    def slope_down(self, x: float) -> float:
        """AC per DC kW of a crossing a little lower than ``x`` (a charge): the margin
        is one-sided, inverting above 0 and rectifying from 0 down."""
        if x > 0:
            return self.k_out - 2 * self.q_out * x
        return self.k_in + 2 * self.q_in * -x

    def slope_up(self, x: float) -> float:
        """AC per DC kW of a crossing a little higher than ``x`` (a discharge)."""
        if x >= 0:
            return self.k_out - 2 * self.q_out * x
        return self.k_in + 2 * self.q_in * -x


@dataclass(frozen=True, slots=True)
class Port:
    """The battery at its own port: the DC bus for a battery there (``folded``: the
    meter factors are the DC bus to stored step; ``split``: ``Battery``'s whole law,
    its "AC" the DC bus), the AC bus for one on its own inverter (``ac``)."""

    battery: Battery
    kind: Literal["ac", "folded", "split"]
    #: The most it charges and discharges at its port, kW.
    charge_max: float
    discharge_max: float

    def stored(self, p: float) -> float:
        """Stored kW a charge of ``p`` at the port adds."""
        if self.kind == "folded":
            return self.battery.meter_charge * p
        return self.battery.stored_by(p)

    def for_stored(self, stored: float) -> float:
        """Port kW that adds ``stored`` stored kW."""
        if self.kind == "folded":
            return stored / self.battery.meter_charge
        return self.battery.charge_for(stored)

    def drawn(self, p: float) -> float:
        """Stored kW a discharge delivering ``p`` at the port draws."""
        if self.kind == "folded":
            return self.battery.meter_discharge * p
        return self.battery.drawn_by(p)

    def delivered(self, drawn: float) -> float:
        """Port kW delivered by drawing ``drawn`` stored kW."""
        if self.kind == "folded":
            return drawn / self.battery.meter_discharge
        return self.battery.delivered_by(drawn)

    def stored_per_port(self, p: float) -> float:
        """Stored per port kWh, for one more charged at ``p``."""
        if self.kind == "folded":
            return self.battery.meter_charge
        return self.battery.eta_charge_at(p)

    def port_per_stored(self, p: float) -> float:
        """Port kWh per stored kWh, for one more delivered at ``p``."""
        if self.kind == "folded":
            return 1 / self.battery.meter_discharge
        return self.battery.eta_discharge_at(p)


@dataclass(frozen=True, slots=True)
class Paths:
    """A plant with a DC bus, as the general dispatch needs it."""

    timezone: ZoneInfo
    #: Per array: its name, its coupling, its share of a site-total PV series, and its
    #: own cap (kW in its coupling's terms; inf: none).
    arrays: tuple[tuple[str, Literal["ac", "dc"], float, float], ...]
    port: Port | None
    #: The battery is on the hybrid's DC bus (else on its own inverter, or none).
    on_bus: bool
    curve: Curve | None
    #: The hybrid's AC rating, kW (inf: none binds).
    rating: float
    export_limit_kw: float | None
    #: The grid's import limit, kW AC at the meter (None: none binds).
    import_limit_kw: float | None
    #: The grid-charge windows, none overlapping (``plant/2`` validation).
    grid_charges: tuple[GridCharge, ...]
    #: The battery-export windows, overlapping none of them.
    export_windows: tuple[ExportWindow, ...]
    #: The grid tops up as a window's target needs, not waiting for the PV still to
    #: come in the window (``Dispatch.charge_order`` ``"target_first"``).
    #: PV still charges first within a slot.
    target_first: bool
    #: When the plant holds the battery through a DC-bus load's session.
    hold_rules: tuple[HoldRule, ...]
    standby_kw: float
    working_kw: float
    working_threshold_kw: float


def paths_of(config: PlantConfig) -> Paths:
    """The general dispatch's view of ``config`` (which ``Plant`` has checked it can
    run: at most one grid-charge window, nothing beyond). Each array's share is of a
    site-total series (by DC rating where every array has one, else equal), which only
    a plant of one coupling takes; PV per array takes none."""
    arrays = config.pv
    ratings = [array.dc_rating_kw for array in arrays]
    known = [r for r in ratings if r is not None]
    total = math.fsum(known) if len(known) == len(arrays) and known else 0.0
    shared = tuple(
        (
            array.name,
            array.coupling,
            (rating / total) if total > 0 and rating is not None else 1.0 / len(arrays),
            _cap(array.coupling, array.dc_rating_kw, array.ac_rating_kw),
        )
        for array, rating in zip(arrays, ratings, strict=True)
    )
    hybrid, part = config.hybrid, config.battery
    curve: Curve | None = None
    if hybrid is not None:
        if hybrid.conversion is not None:
            conversion = hybrid.conversion
            curve = Curve(
                k_in=1 / conversion.eta_in,
                q_in=conversion.loss_in_per_kw,
                k_out=conversion.eta_out,
                q_out=conversion.loss_out_per_kw,
            )
        elif part is not None:  # validation: a folded battery holds the hybrid's curve
            curve = Curve.folded(part.battery)
    rating = math.inf if hybrid is None or hybrid.ac_rating_kw is None else hybrid.ac_rating_kw
    port: Port | None = None
    on_bus = part is not None and part.connection == "dc_bus"
    if part is not None:
        battery = part.battery
        if part.connection == "ac":
            own = part.inverter.ac_rating_kw if part.inverter is not None else None
            limit = math.inf if own is None else own
            port = Port(
                battery, "ac", min(battery.charge_kw, limit), min(battery.discharge_kw, limit)
            )
        elif part.form == "split":
            port = Port(
                battery,
                "split",
                min(battery.charge_kw, _or_inf(part.dc_charge_kw)),
                min(battery.discharge_kw, _or_inf(part.dc_discharge_kw)),
            )
        else:
            # Folded: its own DC limits; without them (nothing else on its bus) its
            # AC-side limits, at the DC bus through its curve.
            folded = Curve.folded(battery)
            charge = part.dc_charge_kw
            discharge = part.dc_discharge_kw
            port = Port(
                battery,
                "folded",
                -folded.dc(-battery.charge_kw) if charge is None else charge,
                folded.dc(battery.discharge_kw) if discharge is None else discharge,
            )
    inverters = [i for i in (hybrid, part.inverter if part is not None else None) if i is not None]
    through = config.battery_inverter
    grid_charge = config.dispatch.grid_charge
    return Paths(
        timezone=config.tz,
        arrays=shared,
        port=port,
        on_bus=on_bus,
        curve=curve,
        rating=rating,
        export_limit_kw=config.grid.export_limit_kw,
        import_limit_kw=config.grid.import_limit_kw,
        grid_charges=tuple(grid_charge),
        export_windows=config.dispatch.export_windows,
        target_first=config.dispatch.charge_order == "target_first",
        hold_rules=config.dispatch.hold_rules,
        standby_kw=math.fsum(i.standby_kw for i in inverters),
        working_kw=through.working_kw if through is not None else 0.0,
        working_threshold_kw=through.working_threshold_kw if through is not None else 0.0,
    )


def _or_inf(kw: float | None) -> float:
    return math.inf if kw is None else kw


def _cap(coupling: Literal["ac", "dc"], dc_rating: float | None, ac_rating: float | None) -> float:
    """An array's own cap in its forecast's terms: an AC array's inverter rating (its
    forecast is after its inverter), a DC array's MPPT limit."""
    return _or_inf(ac_rating if coupling == "ac" else dc_rating)


def _in_window(paths: Paths, start: datetime) -> tuple[GridCharge, datetime] | None:
    """The grid-charge window ``start`` is in, and its end (``start``'s zone); None
    outside every window."""
    local = start.astimezone(paths.timezone)
    for window in paths.grid_charges:
        if window.start <= local.time() < window.end:
            end = datetime.combine(local.date(), window.end, tzinfo=paths.timezone)
            return window, end.astimezone(start.tzinfo)
    return None


def _in_export(paths: Paths, start: datetime) -> ExportWindow | None:
    """The battery-export window ``start`` is in, if any (local time)."""
    local = start.astimezone(paths.timezone).time()
    for window in paths.export_windows:
        if window.start <= local < window.end:
            return window
    return None


def _export_end(paths: Paths, start: datetime) -> datetime | None:
    """When the export window ``start`` is in ends (``start``'s zone)."""
    window = _in_export(paths, start)
    if window is None:
        return None
    local = start.astimezone(paths.timezone)
    end = datetime.combine(local.date(), window.end, tzinfo=paths.timezone)
    return end.astimezone(start.tzinfo)


def _joined(first: SlotFlow, a: float, second: SlotFlow, b: float) -> SlotFlow:
    """Two parts of one slot, ``a`` and ``b`` of it, as the slot's mean flows. The
    crossing is the mean of the two parts' too: a crossing neither part ran at, which
    the storage's margins read as the slot's."""
    dispatch = replace(
        first.dispatch, held=second.dispatch.held, topup_gap=second.dispatch.topup_gap
    )
    joined = replace(first, dispatch=dispatch)
    for part, one, other in ((joined, first, second), (dispatch, first.dispatch, second.dispatch)):
        # Every float field is a mean kW over its part: the slot's is the weighted sum.
        for item in fields(part):
            value = getattr(one, item.name)
            if isinstance(value, float) and item.name not in ("soc_start", "soc_end"):
                object.__setattr__(part, item.name, value * a + getattr(other, item.name) * b)
    return joined


def _cover_end(paths: Paths, window: GridCharge, window_end: datetime) -> datetime | None:
    """When ``window``'s cover period ends (``cover_until``): the look-ahead also stops
    at the next grid-charge window, which tops up for what follows it."""
    if window.cover_until is None:
        return None
    local = window_end.astimezone(paths.timezone)
    day = local.date() if window.cover_until > window.end else local.date() + timedelta(days=1)
    return datetime.combine(day, window.cover_until, tzinfo=paths.timezone).astimezone(
        window_end.tzinfo
    )


def split_pv(paths: Paths, pv_kw: float | Mapping[str, float]) -> tuple[float, float, float]:
    """One slot's PV, a site total or per array, as (AC PV, DC PV, clipped above the
    arrays' own caps)."""
    ac = dc = clipped = 0.0
    for name, coupling, share, cap in paths.arrays:
        kw = pv_kw[name] if isinstance(pv_kw, Mapping) else pv_kw * share
        used = min(kw, cap)
        clipped += kw - used
        if coupling == "ac":
            ac += used
        else:
            dc += used
    return ac, dc, clipped


def _at(pv_kw: PvKw, index: int) -> float | Mapping[str, float]:
    """Slot ``index``'s PV: the site's, or each array's."""
    if isinstance(pv_kw, Mapping):
        return {name: series[index] for name, series in pv_kw.items()}
    return pv_kw[index]


@dataclass
class _Slot:
    """One slot's dispatch as it is decided (:func:`project_paths`)."""

    x: float = 0.0
    pv_to_load: float = 0.0
    pv_to_battery: float = 0.0
    dc_pv_to_battery: float = 0.0
    dc_pv_to_ac: float = 0.0
    port_in: float = 0.0
    dc_pv_to_dc_load: float = 0.0
    #: DC kW the AC side sends the DC-bus loads through the hybrid.
    ac_to_dc_load: float = 0.0


def _serve_and_charge(
    paths: Paths, a: float, d: float, demand: float, room: float, dc_demand: float = 0.0
) -> tuple[_Slot, float, float, float, float]:
    """Steps 1 to 3 (module docstring): PV serves the AC side and the DC-bus loads,
    then charges the battery up to ``room`` port kW. Returns the slot, the AC PV and DC
    PV left, and the AC side and the DC-bus loads still short."""
    slot = _Slot()
    curve, rating = paths.curve, paths.rating
    slot.pv_to_load = min(a, demand)
    a_left, d_left, short = a - slot.pv_to_load, d, demand - slot.pv_to_load
    # DC PV serves the DC-bus loads as it is, on their bus, before anything crosses.
    slot.dc_pv_to_dc_load = min(d_left, dc_demand)
    d_left -= slot.dc_pv_to_dc_load
    dc_short = dc_demand - slot.dc_pv_to_dc_load
    if curve is not None and d_left > 0 and short > 0:
        give = max(0.0, min(short, rating, curve.ac(min(d_left, curve.peak))))
        used = min(curve.dc(give), d_left)
        slot.x += used
        slot.dc_pv_to_ac += used
        slot.pv_to_load += give
        d_left -= used
        short -= give
    if curve is not None and a_left > 0 and dc_short > 0:
        # AC PV serves the DC-bus loads through the hybrid, rectifying, within its
        # rating (DC PV is all used by now: nothing crosses the other way).
        take = _rectify(curve, rating, slot.x, dc_short, a_left)
        ac_used = curve.ac(slot.x) - curve.ac(slot.x - take)
        slot.x -= take
        slot.ac_to_dc_load += take
        slot.pv_to_load += ac_used
        a_left = max(0.0, a_left - ac_used)
        dc_short -= take
    port = paths.port
    if port is None or room <= 0:
        return slot, a_left, d_left, short, dc_short
    if paths.on_bus and curve is not None:
        take = min(d_left, room)
        slot.dc_pv_to_battery = take
        slot.port_in = take
        d_left -= take
        if a_left > 0 and slot.port_in < room:
            # AC PV through the hybrid, rectifying, within what is left and its rating.
            floor = max(curve.dc(curve.ac(slot.x) - a_left), _rectifying_floor(curve, rating))
            more = max(0.0, min(room - slot.port_in, slot.x - floor))
            ac_used = curve.ac(slot.x) - curve.ac(slot.x - more)
            slot.x -= more
            slot.port_in += more
            slot.pv_to_battery += ac_used
            a_left = max(0.0, a_left - ac_used)
    else:
        take = min(a_left, room)
        slot.pv_to_battery = take
        slot.port_in = take
        a_left -= take
        if curve is not None and d_left > 0 and slot.port_in < room:
            # DC PV inverts to the battery on the AC bus, within the hybrid's rating.
            now = curve.ac(slot.x)
            give = min(room - slot.port_in, rating - now)
            give = max(0.0, min(give, curve.ac(min(slot.x + d_left, curve.peak)) - now))
            used = min(curve.dc(now + give) - slot.x, d_left)
            slot.x += used
            slot.dc_pv_to_ac += used
            slot.pv_to_battery += give
            slot.port_in += give
            d_left -= used
    return slot, a_left, d_left, short, dc_short


def _rectifying_floor(curve: Curve, rating: float) -> float:
    """The lowest crossing the hybrid's rating allows (rectifying at the rating):
    -inf without one (``Curve.dc`` of -inf is not a number)."""
    return -math.inf if math.isinf(rating) else curve.dc(-rating)


def _rectify(curve: Curve, rating: float, x: float, dc_kw: float, ac_kw: float = math.inf) -> float:
    """DC kW the hybrid can send the DC bus from crossing ``x``, toward ``dc_kw``: within
    its rating rectifying, and taking no more than ``ac_kw`` AC (netted against what
    crosses the other way)."""
    floor = _rectifying_floor(curve, rating)
    if not math.isinf(ac_kw):
        floor = max(floor, curve.dc(curve.ac(x) - ac_kw))
    return max(0.0, min(dc_kw, x - floor))


def _ac_for(curve: Curve | None, rating: float, x: float, dc_kw: float) -> float:
    """AC kW the hybrid would take from crossing ``x`` to send the DC-bus loads ``dc_kw``
    (within its rating): their share of an import limit."""
    if curve is None or dc_kw <= 0:
        return 0.0
    take = _rectify(curve, rating, x, dc_kw)
    return curve.ac(x) - curve.ac(x - take)


def _drawn_for(paths: Paths, port: Port, x: float, short: float, dc_short: float = 0.0) -> float:
    """Stored kW the battery would draw to serve ``short`` AC kW and ``dc_short`` kW of
    DC-bus loads from crossing ``x``."""
    curve = paths.curve
    if paths.on_bus and curve is not None:
        to_dc = min(dc_short, port.discharge_max)  # on its bus, as it is
        now = curve.ac(x)
        give = max(0.0, min(short, paths.rating - now))
        p = min(max(0.0, curve.dc(now + give) - x), port.discharge_max - to_dc) + to_dc
    else:
        p = min(short, port.discharge_max)
        if curve is not None and dc_short > 0 and p < port.discharge_max:
            # Its AC through the hybrid to the DC-bus loads.
            take = _rectify(curve, paths.rating, x, dc_short, port.discharge_max - p)
            p += curve.ac(x) - curve.ac(x - take)
    return port.drawn(p)


def _grid_room(
    paths: Paths, port: Port, slot: _Slot, grid_kw: float = math.inf, dc_kw: float = 0.0
) -> float:
    """The most the battery could charge at its port in ``slot`` once the grid adds what
    it can (``grid_kw`` AC, under an import limit): its limit, and on the DC bus the
    hybrid's rating rectifying, netted against what PV already sends across. The grid
    serves ``dc_kw`` of DC-bus loads first, a battery held for its top-up giving them
    nothing."""
    return min(port.charge_max, slot.port_in + _grid_charge(paths, slot.x, grid_kw, dc_kw))


def _grid_charge(paths: Paths, x: float, grid_kw: float, dc_kw: float) -> float:
    """Port kW the grid can add to the battery from crossing ``x``, within ``grid_kw`` AC
    and the hybrid's rating, once it has served ``dc_kw`` of DC-bus loads through the
    hybrid: on the DC bus, the drop in the crossing that leaves (the loads' share and
    the charge rectify together, so their AC is the net's); on the AC bus, the AC the
    loads leave of ``grid_kw``."""
    curve, rating = paths.curve, paths.rating
    if paths.on_bus and curve is not None:
        reserved = _rectify(curve, rating, x, dc_kw, grid_kw)
        return max(0.0, x - reserved - _grid_floor(curve, rating, x, grid_kw))
    return max(0.0, grid_kw - _ac_for(curve, rating, x, dc_kw))


def _grid_floor(curve: Curve, rating: float, x: float, grid_kw: float) -> float:
    """The lowest crossing the grid can take the hybrid to from ``x``: the rating
    rectifying, and no more AC from the grid than ``grid_kw`` (an import limit)."""
    floor = _rectifying_floor(curve, rating)
    if not math.isinf(grid_kw):
        floor = max(floor, curve.dc(curve.ac(x) - grid_kw))
    return floor


@dataclass(frozen=True, slots=True)
class _Projection:
    """What every slot of one projection shares (:func:`project_paths`)."""

    paths: Paths
    port: Port | None
    starts: Sequence[datetime]
    #: Each slot's length, hours.
    spans: Sequence[float]
    pv_kw: PvKw
    load_kw: Sequence[float]
    dc_loads: Sequence[float]
    #: The battery's capacity, and its full and floor stored energy, kWh (0 without).
    capacity: float
    full: float
    floor: float
    #: The inverters' draws while the battery works, kW.
    overhead: float
    #: The grid's export and import limits, kW (inf: none).
    limit: float
    cap: float


@dataclass
class _Part:
    """One slot, or part of it, as its dispatch is decided (:func:`_settle_part`): its
    PV and what is left of it, what is still short, and each flow as it is set."""

    slot: _Slot
    #: Stored energy, kWh.
    level: float
    a_left: float
    d_left: float
    #: The AC side, and the DC-bus loads, still short.
    short: float
    dc_short: float
    clipped: float
    #: Stored kW the slot's PV charge adds.
    by_pv: float
    grid_to_battery: float = 0.0
    #: The crossing a top-up on the bus began from.
    topped_from: float | None = None
    holding: bool = False
    gap: float | None = None
    pv_export: float = 0.0
    pv_curtail: float = 0.0
    #: The conversion curtailed DC PV did not take.
    spared: float = 0.0
    bus_clipped: float = 0.0
    battery_to_load: float = 0.0
    battery_to_dc_load: float = 0.0
    battery_export: float = 0.0
    #: Of ``battery_to_load``, what serves the AC side's own short.
    to_house: float = 0.0
    port_out: float = 0.0
    grid_to_load: float = 0.0
    unserved: float = 0.0
    unreachable: float = 0.0


def project_paths(
    paths: Paths,
    starts: Sequence[datetime],
    durations: Sequence[timedelta],
    pv_kw: PvKw,
    load_kw: Sequence[float],
    soc: float | None,
    *,
    dc_load_kw: Sequence[float] | None = None,
    held_at_start: bool | None = None,
    stop: Callable[[int, SlotFlow], bool] | None = None,
) -> tuple[SlotFlow, ...]:
    """The general dispatch's flows for each slot from ``soc`` (module docstring), the
    DC-bus loads drawing ``dc_load_kw`` (None: nothing). A hold rule's session under way
    at the first slot holds the battery as ``held_at_start`` says (None: as the SOC
    there decides, the projection not knowing when the session began)."""
    if not len(starts) == pv_slots(pv_kw) == len(load_kw):
        raise ValueError("starts, pv_kw and load_kw must have the same length")
    if dc_load_kw is not None and len(dc_load_kw) != len(starts):
        raise ValueError("dc_load_kw must have one value per slot")
    dc_loads = [0.0] * len(starts) if dc_load_kw is None else dc_load_kw
    port = paths.port
    if (soc is None) != (port is None):
        raise ValueError("the SOC is None exactly when the plant has no battery")
    if soc is not None and not 0 <= soc <= 1:
        raise ValueError("soc must be in [0, 1]")
    spans = _spans(len(starts), durations)
    battery = port.battery if port is not None else None
    capacity = battery.capacity_kwh if battery is not None else 0.0
    stored = (soc or 0.0) * capacity
    p = _Projection(
        paths=paths,
        port=port,
        starts=starts,
        spans=spans,
        pv_kw=pv_kw,
        load_kw=load_kw,
        dc_loads=dc_loads,
        capacity=capacity,
        full=battery.saturation_soc * capacity if battery is not None else 0.0,
        floor=battery.reserve_soc * capacity if battery is not None else 0.0,
        overhead=paths.standby_kw + paths.working_kw,
        limit=math.inf if paths.export_limit_kw is None else paths.export_limit_kw,
        cap=math.inf if paths.import_limit_kw is None else paths.import_limit_kw,
    )
    flows: list[SlotFlow] = []
    # Each hold rule's session: None outside one, else whether it holds the battery,
    # decided by the SOC where it starts.
    sessions: list[bool | None] = [None] * len(paths.hold_rules)
    stopped_until: datetime | None = None  # an export window that reached its floor
    for index, start in enumerate(starts):
        require_utc(start)
        load = load_kw[index]
        stopped = stopped_until is not None and start < stopped_until
        for k, rule in enumerate(paths.hold_rules):
            if dc_loads[index] <= rule.dc_load_above_kw:
                sessions[k] = None
            elif sessions[k] is None:
                below = stored < rule.start_below_soc * capacity
                sessions[k] = held_at_start if index == 0 and held_at_start is not None else below
        kept = any(sessions)
        flow, level, stops = _settle(
            p, index, load + paths.standby_kw, paths.standby_kw, stored, stopped, kept, sessions
        )
        if flow.moving > paths.working_threshold_kw and paths.working_kw > 0:
            working, working_level, working_stops = _settle(
                p, index, load + p.overhead, p.overhead, stored, stopped, kept, sessions
            )
            if working.moving > paths.working_threshold_kw:
                flow, level, stops = working, working_level, working_stops
        if stops:
            stopped_until = _export_end(paths, start)
        if any(session is not None for session in sessions):
            flow = replace(flow, dispatch=replace(flow.dispatch, hold_session=kept))
        stored = level
        flows.append(flow)
        if stop is not None and stop(index, flow):
            break
    return tuple(flows)


def _settle(
    p: _Projection,
    index: int,
    demand: float,
    draw: float,
    stored: float,
    stopped: bool,
    kept: bool,
    sessions: Sequence[bool | None],
) -> tuple[SlotFlow, float, bool]:
    """Slot ``index`` from ``stored``, and whether its export window stops here: the run
    ended in the slot, or the window found the battery at its floor. A window that stops
    stays stopped until it ends: the battery does not run up and export again."""
    port = p.port
    flow, level, run_ended = _run(p, index, demand, draw, stored, stopped, kept, sessions)
    export = _in_export(p.paths, p.starts[index]) if port is not None else None
    if export is None or stopped or kept:
        return flow, level, False
    if not run_ended:
        return flow, level, stored <= export.floor_soc * p.capacity + _EPS
    # The run ends here: what a stored kWh less does, from the dispatch itself (the
    # derivative of the projection).
    floor_kwh = export.floor_soc * p.capacity
    less = min(1e-3, (stored - floor_kwh) / 2)
    if less <= 0:
        return flow, level, True
    shorter, shorter_level, _ = _run(p, index, demand, draw, stored - less, False, False, sessions)
    per = p.spans[index] / less
    ended = RunEnd(
        export_lost=(
            flow.pv_export + flow.battery_export - shorter.pv_export - shorter.battery_export
        )
        * per,
        not_delivered=(flow.battery_out - shorter.battery_out) * per,
        imported=(shorter.grid_to_load - flow.grid_to_load) * per,
        refilled=(shorter.pv_to_battery - flow.pv_to_battery) * per,
        refilled_dc=(shorter.dc_pv_to_battery - flow.dc_pv_to_battery) * per,
        carried=(level - shorter_level) / less,
    )
    return replace(flow, dispatch=replace(flow.dispatch, run_end=ended)), level, True


def _run(
    p: _Projection,
    index: int,
    demand: float,
    draw: float,
    level: float,
    stopped: bool,
    kept: bool,
    sessions: Sequence[bool | None],
) -> tuple[SlotFlow, float, bool]:
    """Slot ``index`` from ``level``: in an export window not yet ``stopped`` and above
    its floor, the battery exports until the floor, and where it reaches it in the slot,
    the rest of the slot runs as self-consumption (the run's end). Held by a hold rule
    (``kept``), it does not export. Also whether the run ended in the slot."""
    port = p.port
    hours = p.spans[index]
    export = _in_export(p.paths, p.starts[index]) if port is not None else None
    if export is None or port is None or stopped or kept:
        return (*_settle_part(p, index, demand, draw, level, hours, None, sessions, kept), False)
    floor_kwh = export.floor_soc * p.capacity
    if level <= floor_kwh + _EPS:
        return (*_settle_part(p, index, demand, draw, level, hours, None, sessions), False)
    whole, end = _settle_part(p, index, demand, draw, level, hours, export, sessions)
    # What the whole slot's export would draw, unclamped (exporting, nothing charges, so
    # it is the battery's port out).
    drawn = port.drawn(max(0.0, -whole.dispatch.battery_port)) * hours
    if drawn <= level - floor_kwh + _EPS:
        return whole, end, False  # the run lasts the slot
    share = (level - floor_kwh) / drawn  # of the slot, exporting
    first, at_floor = _settle_part(p, index, demand, draw, level, share * hours, export, sessions)
    rest, after = _settle_part(
        p, index, demand, draw, at_floor, (1 - share) * hours, None, sessions
    )
    joined = _joined(first, share, rest, 1 - share)
    flow = replace(joined, soc_start=level / p.capacity, soc_end=after / p.capacity)
    return flow, after, True


def _settle_part(
    p: _Projection,
    index: int,
    demand: float,
    draw: float,
    level: float,
    hours: float,
    export: ExportWindow | None,
    sessions: Sequence[bool | None],
    kept: bool = False,
) -> tuple[SlotFlow, float]:
    """Slot ``index``, or part of it ``hours`` long, from ``level`` stored; with
    ``export``, the battery exporting at its window's power; ``kept``, a hold rule
    holding it. The steps in the module docstring's order."""
    paths, port = p.paths, p.port
    start = p.starts[index]
    level0 = level
    a, d, clipped = split_pv(paths, _at(p.pv_kw, index))
    room = 0.0
    if port is not None and export is None:  # exporting, the battery does not charge
        room = min(port.charge_max, port.for_stored(max(0.0, p.full - level) / hours))
    slot, a_left, d_left, short, dc_short = _serve_and_charge(
        paths, a, d, demand, room, p.dc_loads[index]
    )
    by_pv = port.stored(slot.port_in) if port is not None else 0.0
    level += by_pv * hours
    part = _Part(slot, level, a_left, d_left, short, dc_short, clipped, by_pv)

    in_window = _in_window(paths, start) if port is not None else None
    if port is not None and in_window is not None and export is None:
        window, window_end = in_window
        coming, later_room, target = _look_ahead(
            p, port, index, window, window_end, part.level, sessions
        )
        _top_up(p, part, port, window, coming, later_room, target, hours)
    _spill_pv(p, part, export)
    if port is not None and export is not None:
        _export_battery(p, part, port, export, hours)
    elif (
        port is not None and not part.holding and not kept and (part.short > 0 or part.dc_short > 0)
    ):
        _discharge(p, part, port, hours)
    part.grid_to_load = part.short - part.to_house
    _grid_serves_dc(p, part)
    _net_export(part)
    if port is not None and paths.on_bus and part.dc_short > 0:
        _bus_draws_beyond_policy(part, port, p.floor, hours)
    # Beyond the rating, a battery off the bus crosses it too: no stored kWh reaches it.
    part.unreachable = 0.0 if port is not None and paths.on_bus else part.dc_short
    if part.grid_to_load > p.cap:
        _at_import_limit(p, part, hours)
    return _slot_flow(p, part, start, level0, draw, kept)


def _look_ahead(
    p: _Projection,
    port: Port,
    index: int,
    window: GridCharge,
    window_end: datetime,
    level: float,
    sessions: Sequence[bool | None],
) -> tuple[float, float, float]:
    """In a grid-charge window, from slot ``index`` at ``level`` stored: what PV alone
    will still add before the window ends, what the grid could still add then beside it,
    and the window's target, kWh (lowered to what the house needs from the battery until
    the cover period ends)."""
    paths, starts, capacity = p.paths, p.starts, p.capacity
    target = window.target_soc * capacity
    coming, later_room, cover = 0.0, 0.0, 0.0
    cover_end = _cover_end(paths, window, window_end)
    # The sessions ahead, a later one decided at the SOC now: a held one leaves its slots
    # to the grid, so the battery need not cover them.
    ahead_sessions = list(sessions)
    for later in range(index + 1, len(starts)):
        if cover_end is not None and starts[later] >= cover_end:
            break
        if starts[later] >= window_end and _in_window(paths, starts[later]):
            break  # the next window tops up for what follows: it covers no more
        if cover_end is None and starts[later] >= window_end:
            break
        for k, rule in enumerate(paths.hold_rules):
            if p.dc_loads[later] <= rule.dc_load_above_kw:
                ahead_sessions[k] = None
            elif ahead_sessions[k] is None:
                ahead_sessions[k] = level < rule.start_below_soc * capacity
        la, ld, _ = split_pv(paths, _at(p.pv_kw, later))
        need = p.load_kw[later] + p.overhead
        ahead, _, _, later_short, later_dc = _serve_and_charge(
            paths, la, ld, need, port.charge_max, p.dc_loads[later]
        )
        if starts[later] < window_end:
            alone = port.stored(ahead.port_in)
            grid_kw = max(0.0, p.cap - later_short)  # what the import limit leaves
            most = port.stored(_grid_room(paths, port, ahead, grid_kw, later_dc))
            coming += alone * p.spans[later]
            later_room += max(0.0, most - alone) * p.spans[later]
        elif not any(ahead_sessions):
            drawn = _drawn_for(paths, port, ahead.x, later_short, later_dc)
            cover += drawn * p.spans[later]
    if cover_end is not None:
        target = min(target, p.floor + cover)
    return coming, later_room, target


def _top_up(
    p: _Projection,
    part: _Part,
    port: Port,
    window: GridCharge,
    coming: float,
    later_room: float,
    target: float,
    hours: float,
) -> None:
    """Step 4: the grid tops the battery up toward ``target``, and the plant holds it."""
    paths, slot, curve = p.paths, part.slot, p.paths.curve
    # PV first: what PV will still add is the grid's to leave; target first, the grid
    # tops up as the target needs.
    needed = min(target, p.full) - part.level - (0.0 if paths.target_first else coming)
    part.holding = (
        needed + _drawn_for(paths, port, slot.x, part.short, part.dc_short) * hours > _EPS
    )
    if window.timing == "late":
        needed -= later_room
    if needed > _EPS:
        wanted = part.by_pv + min(needed, max(0.0, p.full - part.level)) / hours
        extra = max(0.0, min(port.charge_max, port.for_stored(wanted)) - slot.port_in)
        # The import limit caps the grid's charge first: the loads, held on the grid,
        # take their share of the limit.
        grid_kw = max(0.0, p.cap - part.short)
        extra = min(extra, _grid_charge(paths, slot.x, grid_kw, part.dc_short))
        if paths.on_bus and curve is not None:
            # Through the hybrid, netted against what crosses the other way.
            part.grid_to_battery = curve.ac(slot.x) - curve.ac(slot.x - extra)
            part.topped_from = slot.x
            slot.x -= extra
        else:
            part.grid_to_battery = extra
        slot.port_in += extra
        part.level += (port.stored(slot.port_in) - part.by_pv) * hours
    # On course to end the window at the target, with room to charge more: a kWh less
    # now would be bought back here (after an early top-up, in every slot to the
    # window's end).
    part.gap = needed


def _spill_pv(p: _Projection, part: _Part, export: ExportWindow | None) -> None:
    """Step 5: what PV is left. AC PV exports first, then DC PV through the hybrid;
    above the export limit curtailed; DC PV that neither crosses nor charges, clipped.
    In an export window where PV yields at the limit, the battery's export is held
    first (``ExportWindow.yields``)."""
    slot, curve, rating, limit = part.slot, p.paths.curve, p.paths.rating, p.limit
    pv_room = limit
    if export is not None and export.yields == "pv":
        pv_room = max(0.0, limit - min(export.power_kw, limit))
    a_export = min(part.a_left, pv_room)
    part.pv_curtail = part.a_left - a_export
    room_export = pv_room - a_export
    part.pv_export = a_export
    if curve is not None and part.d_left > 0:
        now = curve.ac(slot.x)
        can = max(0.0, min(rating - now, curve.ac(min(slot.x + part.d_left, curve.peak)) - now))
        give = min(can, room_export)
        used = min(max(0.0, curve.dc(now + give) - slot.x), part.d_left)
        slot.x += used
        slot.dc_pv_to_ac += used
        part.d_left -= used
        part.pv_export += give
        held = can - give
        if held > _EPS:
            held_dc = min(max(0.0, curve.dc(curve.ac(slot.x) + held) - slot.x), part.d_left)
            part.d_left -= held_dc
            part.pv_curtail += held
            part.spared = held_dc - held
    # What the hybrid could not pass: a battery on its DC bus with room takes it.
    part.bus_clipped = max(0.0, part.d_left)
    part.clipped += part.bus_clipped


def _export_battery(
    p: _Projection, part: _Part, port: Port, export: ExportWindow, hours: float
) -> None:
    """Step 6 in a battery-export window: the window's export, held at its power within
    the battery's limits, the rating (PV crosses first) and what the export limit leaves;
    the house beside it from what is left, then the grid."""
    paths, slot, curve, rating = p.paths, part.slot, p.paths.curve, p.paths.rating
    target = max(0.0, min(export.power_kw, p.limit - part.pv_export))
    to_dc = 0.0
    if paths.on_bus and curve is not None:
        # On its bus the battery serves the DC-bus loads first, as they are, as it does
        # outside a window: converting for the export first would leave them to the
        # hybrid, rectifying AC the battery had just sent out (and the meters' split
        # books them on the bus).
        to_dc = min(part.dc_short, port.discharge_max)
        part.battery_to_dc_load = to_dc
        part.dc_short -= to_dc
        now = curve.ac(slot.x)
        left = port.discharge_max - to_dc
        top = curve.ac(min(slot.x + left, curve.peak)) - now
        can = max(0.0, min(rating - now, top))
    else:
        now, can = 0.0, port.discharge_max
    part.battery_export = min(target, can)
    part.battery_to_load = min(part.short, can - part.battery_export)
    part.to_house = part.battery_to_load
    out = part.battery_export + part.battery_to_load
    if paths.on_bus and curve is not None:
        port_out = min(max(0.0, curve.dc(now + out) - slot.x), port.discharge_max - to_dc)
        slot.x += port_out
        part.port_out = port_out + to_dc
    else:
        # The DC-bus loads beside it, through the hybrid, from what is left of the
        # battery's limit.
        _, ac, through, part.port_out = _serve_dc(p, slot, part.dc_short, port.discharge_max, out)
        part.battery_to_load += ac
        part.dc_short -= through
    part.level -= port.drawn(part.port_out) * hours


def _discharge(p: _Projection, part: _Part, port: Port, hours: float) -> None:
    """Step 6: the battery serves what PV did not, within its limits and the hybrid's
    rating, down to its floor."""
    paths, slot, curve, rating = p.paths, part.slot, p.paths.curve, p.paths.rating
    available = port.delivered(max(0.0, part.level - p.floor) / hours)
    most_out = min(port.discharge_max, available)
    to_dc = 0.0
    if paths.on_bus and curve is not None:
        # On its bus the battery serves the DC-bus loads first, as they are.
        to_dc = min(part.dc_short, most_out)
        part.battery_to_dc_load = to_dc
        part.dc_short -= to_dc
        now = curve.ac(slot.x)
        give = min(
            part.short, rating - now, curve.ac(min(slot.x + most_out - to_dc, curve.peak)) - now
        )
        give = max(0.0, give)
        port_out = min(max(0.0, curve.dc(now + give) - slot.x), most_out - to_dc)
        slot.x += port_out
        part.port_out = port_out + to_dc
        part.battery_to_load = give
        part.to_house = give
    else:
        port_out = min(part.short, most_out)
        part.battery_to_load = port_out
        part.to_house = port_out
        # Then, through the hybrid, the DC-bus loads.
        _, ac, through, part.port_out = _serve_dc(p, slot, part.dc_short, most_out, port_out)
        part.battery_to_load += ac
        part.dc_short -= through
    part.level -= port.drawn(part.port_out) * hours


def _serve_dc(
    p: _Projection, slot: _Slot, dc_kw: float, most: float, out: float
) -> tuple[float, float, float, float]:
    """The battery's port, ``out`` of ``most`` kW already given, serving ``dc_kw`` of
    DC-bus loads: on its bus as it is, on its own inverter through the hybrid
    (rectifying, within its rating). Returns the DC kW it gives them directly, the AC kW
    it sends the hybrid, the DC kW that delivers, and its port out after."""
    curve = p.paths.curve
    if dc_kw <= 0 or out >= most:
        return 0.0, 0.0, 0.0, out
    if p.paths.on_bus or curve is None:
        direct = min(dc_kw, most - out)
        return direct, 0.0, 0.0, out + direct
    take = _rectify(curve, p.paths.rating, slot.x, dc_kw, most - out)
    ac = curve.ac(slot.x) - curve.ac(slot.x - take)
    slot.x -= take
    slot.ac_to_dc_load += take
    return 0.0, ac, take, out + ac


def _grid_serves_dc(p: _Projection, part: _Part) -> None:
    """The grid serves the DC-bus loads through the hybrid, within its rating; what that
    cannot reach is unserved."""
    slot, curve = part.slot, p.paths.curve
    if curve is None or part.dc_short <= 0:
        return
    take = _rectify(curve, p.paths.rating, slot.x, part.dc_short)
    if part.topped_from is not None and take > 0:
        # Netted with the top-up, the loads' AC is booked first, at the margin before
        # the charge, as the plant serves loads before it charges (and as the meters'
        # split books it: ``_meters.split_paths``).
        total = part.grid_to_battery + curve.ac(slot.x) - curve.ac(slot.x - take)
        to_loads = curve.ac(part.topped_from) - curve.ac(part.topped_from - take)
        part.grid_to_load += to_loads
        part.grid_to_battery = total - to_loads
    else:
        part.grid_to_load += curve.ac(slot.x) - curve.ac(slot.x - take)
    slot.x -= take
    slot.ac_to_dc_load += take
    part.dc_short -= take


def _net_export(part: _Part) -> None:
    """The meter nets the battery's export against the loads' import: what the loads
    would import, the export serves (booked apart, the export would count kWh the meter
    never sees)."""
    if part.battery_export > 0 and part.grid_to_load > 0:
        moved = min(part.grid_to_load, part.battery_export)
        part.battery_export -= moved
        part.battery_to_load += moved
        part.grid_to_load -= moved


def _bus_draws_beyond_policy(part: _Part, port: Port, floor: float, hours: float) -> None:
    """What the rating keeps from the grid, the bus draws from the battery beyond its
    policy (held, or past its order), down to its reserve, as at an import limit."""
    stored_left = max(0.0, part.level - floor) / hours
    more = min(part.dc_short, port.discharge_max - part.port_out, port.delivered(stored_left))
    more = max(0.0, more)
    part.level -= (port.drawn(part.port_out + more) - port.drawn(part.port_out)) * hours
    part.port_out += more
    part.battery_to_dc_load += more
    part.dc_short -= more


def _at_import_limit(p: _Projection, part: _Part, hours: float) -> None:
    """Above the import limit the battery serves beyond its policy (held, or past its
    discharge order), down to its reserve; what it cannot is unserved."""
    paths, port, slot, curve, cap = p.paths, p.port, part.slot, p.paths.curve, p.cap
    if port is not None:
        beyond = part.grid_to_load - cap
        room = port.discharge_max - part.port_out
        stored_left = max(0.0, part.level - p.floor) / hours
        to_ac = 0.0  # of ``give``, what reaches the AC side's own loads
        if paths.on_bus and curve is not None:
            now = curve.ac(slot.x)
            reach = min(room, port.delivered(stored_left))
            top = curve.ac(min(slot.x + max(reach, 0.0), curve.peak)) - now
            give = max(0.0, min(beyond, paths.rating - now, top))
            more = min(max(0.0, curve.dc(now + give) - slot.x), max(reach, 0.0))
            # On the bus it first serves the DC-bus loads the hybrid was rectifying for,
            # as they are: the AC that saves is the grid's.
            direct = min(more, slot.ac_to_dc_load)
            saved = curve.ac(slot.x + direct) - now
            slot.ac_to_dc_load -= direct
            part.battery_to_dc_load += direct
            to_ac = give - saved
            slot.x += more
        else:
            give = max(0.0, min(beyond, room, port.delivered(stored_left)))
            more = give
            to_ac = give
        part.level -= (port.drawn(part.port_out + more) - port.drawn(part.port_out)) * hours
        part.port_out += more
        part.battery_to_load += to_ac
        part.grid_to_load -= give
    unserved = max(0.0, part.grid_to_load - cap)
    part.grid_to_load -= unserved
    if unserved > 0 and curve is not None and slot.ac_to_dc_load > 0:
        # What the limit leaves unserved comes first off what the hybrid was sending the
        # DC-bus loads: it rectifies less, and they receive less (in DC at the loads; the
        # rest unserved stays AC at the meter).
        most = curve.ac(slot.x + slot.ac_to_dc_load) - curve.ac(slot.x)
        cut_ac = min(unserved, most)
        cut_dc = curve.dc(curve.ac(slot.x) + cut_ac) - slot.x
        cut_dc = min(max(0.0, cut_dc), slot.ac_to_dc_load)
        slot.x += cut_dc
        slot.ac_to_dc_load -= cut_dc
        unserved += cut_dc - cut_ac
    part.unserved = unserved


def _slot_flow(
    p: _Projection, part: _Part, start: datetime, level0: float, draw: float, kept: bool
) -> tuple[SlotFlow, float]:
    """The part's flows, its losses, and its stored energy after (clamped to the
    battery)."""
    port, slot, curve, capacity = p.port, part.slot, p.paths.curve, p.capacity
    hybrid_loss = slot.x - curve.ac(slot.x) if curve is not None else 0.0
    battery_loss = 0.0
    if port is not None:
        battery_loss = (slot.port_in - port.stored(slot.port_in)) + (
            port.drawn(part.port_out) - part.port_out
        )
    level = min(max(part.level, 0.0), capacity) if port is not None else 0.0
    flow = SlotFlow(
        start=start,
        soc_start=level0 / capacity if port is not None else None,
        soc_end=level / capacity if port is not None else None,
        pv_to_load=slot.pv_to_load,
        pv_to_battery=slot.pv_to_battery,
        pv_export=part.pv_export,
        pv_curtail=part.pv_curtail,
        battery_to_load=part.battery_to_load,
        battery_export=part.battery_export,
        grid_to_load=part.grid_to_load,
        grid_to_battery=part.grid_to_battery,
        pv_clipped=part.clipped,
        unserved=part.unserved + part.dc_short,
        dc_pv_to_battery=slot.dc_pv_to_battery,
        dc_pv_to_ac=slot.dc_pv_to_ac,
        dc_pv_to_dc_load=slot.dc_pv_to_dc_load,
        battery_to_dc_load=part.battery_to_dc_load,
        ac_to_dc_load=slot.ac_to_dc_load,
        conversion_loss=hybrid_loss + battery_loss + part.spared,
        inverter_draw=draw,
        dispatch=SlotDispatch(
            held=part.holding or kept,
            topup_gap=part.gap,
            bus_clipped=part.bus_clipped,
            unserved_unreachable=part.unreachable,
            crossing=slot.x,
            battery_port=slot.port_in - part.port_out,
            battery_port_in=slot.port_in,
            battery_port_out=part.port_out,
        ),
    )
    return flow, level


@dataclass(frozen=True, slots=True)
class PathStorage:
    """The battery as the price rule asks about it (``port.Storage``) for a plant with
    a DC bus: each answer at the margin of the slot's net crossing
    (``SlotDispatch.crossing``) and the battery's port power (``battery_port``), by the
    energy's path. Each side of the dead band is read without the battery's own flow the
    other way: a discharge kWh more in a charging slot is a charge kWh less."""

    paths: Paths
    port: Port

    @property
    def battery(self) -> Battery:
        return self.port.battery

    @property
    def capacity_kwh(self) -> float:
        return self.port.battery.capacity_kwh

    @property
    def reserve_soc(self) -> float:
        return self.port.battery.reserve_soc

    @property
    def saturation_soc(self) -> float:
        return self.port.battery.saturation_soc

    @property
    def wear_discharge_per_kwh(self) -> float:
        return self.port.battery.wear_discharge_per_kwh

    # The battery's own flows at its port: the slot's gross flows where it has them (a
    # metered slot can both charge and discharge), else the net ``battery_port``'s.
    @staticmethod
    def _port_in(flow: SlotFlow) -> float:
        d = flow.dispatch
        if d.battery_port_in or d.battery_port_out:
            return d.battery_port_in
        return max(d.battery_port, 0.0)

    @staticmethod
    def _port_out(flow: SlotFlow) -> float:
        d = flow.dispatch
        if d.battery_port_in or d.battery_port_out:
            return d.battery_port_out
        return max(-d.battery_port, 0.0)

    def _charge_side(self, flow: SlotFlow | None) -> float:
        """Stored per AC kWh at the margin of a charge (the AC to the battery, or the AC
        a load would receive from PV charging it)."""
        p_in = 0.0 if flow is None else self._port_in(flow)
        per_port = self.port.stored_per_port(p_in)
        curve = self.paths.curve
        if not self.paths.on_bus or curve is None:
            return per_port
        x = 0.0 if flow is None else flow.dispatch.crossing - self._port_out(flow)
        return per_port / curve.slope_down(x)

    def _discharge_side(self, flow: SlotFlow | None) -> float:
        """AC per stored kWh at the margin of a discharge."""
        p_out = 0.0 if flow is None else self._port_out(flow)
        per_stored = self.port.port_per_stored(p_out)
        curve = self.paths.curve
        if not self.paths.on_bus or curve is None:
            return per_stored
        x = 0.0 if flow is None else flow.dispatch.crossing + self._port_in(flow)
        return per_stored * curve.slope_up(x)

    def _dc_wear(self) -> float:
        """Wear per DC kWh into a battery on the DC bus: the AC wear, scaled to the
        stored energy it adds (a stated choice: it takes cell wear to follow stored
        throughput)."""
        battery = self.port.battery
        if self.port.kind == "split":
            return battery.wear_charge_per_kwh  # its port is the DC bus already
        return battery.wear_charge_per_kwh * battery.meter_charge / battery.eta_charge

    def _on_the_bus(self, source: ChargeSource) -> None:
        if source == ChargeSource.DC_PV and not self.paths.on_bus:
            raise ValueError("this battery is not on the DC bus: DC PV reaches it as AC")

    def charge_wear_rate(self, source: ChargeSource) -> float:
        self._on_the_bus(source)
        if source == ChargeSource.DC_PV:
            return self._dc_wear()
        return self.port.battery.wear_charge_per_kwh

    def divertible_kw(self, flow: SlotFlow) -> float:
        # AC PV charging is AC a load takes as it is; DC PV charging reaches the AC
        # side only through the hybrid, within its rating.
        curve = self.paths.curve
        if flow.dc_pv_to_battery <= 0 or curve is None:
            return flow.pv_to_battery
        now = curve.ac(flow.dispatch.crossing)
        more = curve.ac(flow.dispatch.crossing + flow.dc_pv_to_battery) - now
        return flow.pv_to_battery + max(0.0, min(more, self.paths.rating - now))

    def _charge_room(self, flow: SlotFlow, source: ChargeSource) -> float:
        """Port kW more the battery could take from ``source`` in this slot."""
        room = self.port.charge_max - self._port_in(flow)
        curve = self.paths.curve
        # The grid adds no more than the import limit leaves.
        grid_kw = math.inf
        limit = self.paths.import_limit_kw
        if source == ChargeSource.GRID and limit is not None:
            grid_kw = max(0.0, limit - flow.grid_to_load - flow.grid_to_battery)
        if source != ChargeSource.DC_PV and self.paths.on_bus and curve is not None:
            floor = _grid_floor(curve, self.paths.rating, flow.dispatch.crossing, grid_kw)
            room = min(room, flow.dispatch.crossing - floor)
        elif source == ChargeSource.GRID:
            room = min(room, grid_kw)
        return room

    def can_charge_more(
        self, flow: SlotFlow, source: ChargeSource, *, margin_kw: float = 0.0
    ) -> bool:
        self._on_the_bus(source)
        if self._charge_room(flow, source) <= margin_kw:
            return False
        if source != ChargeSource.GRID:
            return True
        # PV charges first within a slot, whichever the order (the target first only
        # stops the window's top-up waiting for PV still to come): the grid charges
        # more only where no PV it could take is spare (clipped PV only where the
        # hybrid clipped it on the battery's bus).
        reachable = flow.dispatch.bus_clipped if self.paths.on_bus else 0.0
        return flow.pv_export + flow.pv_curtail + reachable <= margin_kw

    def _discharge_room(self, flow: SlotFlow) -> float:
        """AC kW more the battery could deliver: its port limit, and the rating, from
        the crossing without its own charge (undoing a charge is not the battery
        giving)."""
        more = self.port.discharge_max - self._port_out(flow)
        curve = self.paths.curve
        if not self.paths.on_bus or curve is None:
            return max(0.0, more)
        x = flow.dispatch.crossing + self._port_in(flow)
        now = curve.ac(x)
        top = curve.ac(min(x + max(more, 0.0), curve.peak)) - now
        return max(0.0, min(top, self.paths.rating - now))

    def at_discharge_limit(self, flow: SlotFlow, *, margin_kw: float = 0.0) -> bool:
        return self._discharge_room(flow) <= margin_kw

    def can_serve(self, flow: SlotFlow, more_kw: float) -> bool:
        return more_kw <= self._discharge_room(flow)

    def grid_refills_diverted_pv(self, flow: SlotFlow) -> bool:
        return True  # PV charges before the plant decides its top-up

    def takes_clipped_pv(self, flow: SlotFlow) -> bool:
        # What the hybrid clipped is on the battery's bus; an array's own cap clips
        # before the bus, and a battery on its own inverter cannot reach either.
        return self.paths.on_bus

    def headroom(self, flow: SlotFlow, hours: float) -> float:
        soc = flow.soc_end
        if soc is None:
            raise ValueError("a plant with storage projects its SOC")
        port = self.port
        battery = port.battery
        above = (soc - battery.reserve_soc) * battery.capacity_kwh
        above -= port.stored(self._port_in(flow)) * hours
        if above <= 0:
            return 0.0
        out = self._port_out(flow)
        extra = port.delivered(port.drawn(out) + above / hours) - out
        extra = max(0.0, min(extra, port.discharge_max - out))
        curve = self.paths.curve
        if not self.paths.on_bus or curve is None:
            return extra
        x = flow.dispatch.crossing + self._port_in(flow)  # X+, as in ``_discharge_room``
        now = curve.ac(x)
        top = curve.ac(min(x + extra, curve.peak)) - now
        return max(0.0, min(top, self.paths.rating - now))

    def refill_value(self, price: float, source: ChargeSource, flow: SlotFlow) -> float:
        self._on_the_bus(source)
        if source == ChargeSource.DC_PV:
            # The DC PV it takes: clipped at a price of 0, or what crossing it would have
            # earned, AC per DC at the margin.
            curve = self.paths.curve
            x = flow.dispatch.crossing - self._port_out(flow)
            per_ac = curve.slope_down(x) if curve is not None and x > 0 else 1.0
            per_port = self.port.stored_per_port(self._port_in(flow))
            return (price * per_ac + self._dc_wear()) / per_port
        return (price + self.port.battery.wear_charge_per_kwh) / self._charge_side(flow)

    def displaced_value(self, price: float, flow: SlotFlow | None = None) -> float:
        return self._discharge_side(flow) * (price - self.port.battery.wear_discharge_per_kwh)

    def dead_band(self, stored_value: float, flow: SlotFlow) -> tuple[float, float]:
        battery = self.port.battery
        return (
            self._charge_side(flow) * stored_value - battery.wear_charge_per_kwh,
            stored_value / self._discharge_side(flow) + battery.wear_discharge_per_kwh,
        )

    def stored_per_kwh(self, source: ChargeSource, flow: SlotFlow | None = None) -> float:
        # By path: DC PV reaches the battery at its port; AC (PV on the AC bus, the grid)
        # reaches the port through the hybrid's crossing for a battery on the DC bus,
        # directly for one on its own inverter. Each averaged over
        # the slot's charge: the port's whole charge, and the AC's share of the crossing.
        self._on_the_bus(source)
        port = self.port
        curve = self.paths.curve
        through_hybrid = self.paths.on_bus and curve is not None
        if flow is None:
            # A charge at the battery's own limit: at the port, or, folded, its AC-side
            # limit through its curve, where that binds first.
            p_in = port.charge_max
            ac = p_in
            if through_hybrid and curve is not None and source != ChargeSource.DC_PV:
                if port.kind == "folded":
                    from_ac = -curve.dc(-port.battery.charge_kw)
                    if from_ac <= p_in:
                        return port.stored(from_ac) / port.battery.charge_kw
                ac = -curve.ac(-p_in)
            return port.stored(p_in) / ac
        ac_in = flow.pv_to_battery + flow.grid_to_battery
        if not self.paths.on_bus:
            p_in = ac_in  # its port is on the AC bus: the AC charge is its port's
        else:
            p_in = self._port_in(flow)
            if p_in <= _EPS and curve is not None:
                # A slot without its port's flows (built by hand, not projected): the AC
                # through the hybrid from a crossing of 0, as a metered slot works it out.
                rectified = -curve.dc(curve.ac(0.0) - ac_in) if ac_in > _EPS else 0.0
                p_in = rectified + flow.dc_pv_to_battery
        if p_in <= _EPS:
            return self.stored_per_kwh(source)  # nothing charging: at the limit, as bought
        per_port = port.stored(p_in) / p_in
        if source == ChargeSource.DC_PV or not through_hybrid:
            return per_port  # at the port as it is (its port on the AC bus: AC is port kW)
        reached = max(p_in - flow.dc_pv_to_battery, 0.0)  # what the AC gave the port, DC
        if ac_in <= _EPS or reached <= _EPS:
            return self.stored_per_kwh(source)
        return per_port * reached / ac_in
