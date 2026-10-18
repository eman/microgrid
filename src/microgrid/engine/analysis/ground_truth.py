"""The plant model against the plant's own meters.

A plant model is only worth pricing with if what it projects is what the plant does.
:func:`project_against_meters` feeds a plant model what actually happened (the metered
PV, the load the plant served, the metered SOC at the start), so no forecast error is
in the way, and compares what it projects with the meters:

- **from each local midnight, for the day:** the daily balances, and when the battery
  tops up, fills and runs empty (:class:`DayResult`);
- **from each hour, over the price model's 48 h horizon:** the SOC error by hours
  ahead, and whether the model's first event (fill, top-up, empty) is the meters'
  (:class:`HourlyRun`).

**Primary measures are raw meters** (SOC, grid import and export, the inverter's AC
meter), so a fitted conversion is never scored against itself. Two are not: no meter
separates the grid's share that charges the battery, so the metered top-up (when it
starts, how much) comes from the meters' split (``split_paths``), as each slot's routing does. Both
use the battery's fitted conversion, so both are computed with a baseline's conversion
**pinned** (``pinned``), and so are the inputs and the meters' full and empty
thresholds. The served load is consumption less the inverter's draw, both derived
through the pinned plant, so every candidate model gets the same inputs and a change
in conversion shows only in its prediction.

**Every projection goes through** ``project`` (:class:`Projector`), so pointing it at
another plant model moves every measure.

**What is a site's comes in from its caller,** as opaque strings: each day's labels
(why the model does not describe it), and the DC-bus load's draw outside the house
meter. The function adds one label of its own, :data:`GAP`, where the telemetry was
silent. It names no load, and groups nothing: the caller groups the days and runs by
their labels and sets the bars.

**A plant model with a DC-bus load** is fed the session as the plant met it: the house
load (the load served less the DC-bus load's draw) on the AC side, the DC-bus load's
meter on the DC bus, and, for a run that starts inside a session, whether its hold
rules held the battery from the session's metered start. The meters are read through
the pinned plant without its DC-bus loads (:func:`meters_plant`), so a candidate's
change shows only in its prediction. Pure: no I/O.
"""

from __future__ import annotations

import statistics
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from typing import Final, Protocol
from zoneinfo import ZoneInfo

from microgrid.engine.analysis.scoring import (
    battery_exports,
    hours_ahead_bucket,
    observed_flows,
    price_hour,
    realise,
    served_load,
    storage_of,
)
from microgrid.engine.analysis.scoring import soc_end as soc_of
from microgrid.engine.extend import (
    OBSERVED,
    Event,
    Flows,
    Noise,
    SlotFlow,
    importing,
    spilling,
)
from microgrid.engine.series import SLOT
from microgrid.plant import Plant

__all__ = [
    "GAP",
    "HORIZON",
    "REACH",
    "DayResult",
    "HourlyRun",
    "Projection",
    "Projector",
    "Recording",
    "clock",
    "dc_bus_inputs",
    "meters_plant",
    "net_inverter_kw",
    "project_against_meters",
    "routing_class",
    "session_holds",
]

#: How far an hourly run reaches: the price model's default horizon.
REACH: Final = timedelta(hours=48)
#: :data:`REACH` in slots of the default length (``series.SLOT``); a recording on another
#: slot (``Recording.step``) reaches as far in its own.
HORIZON: Final = int(REACH / SLOT)


GAP: Final = "telemetry gap"


class Projector(Protocol):
    """Projects the plant from ``soc`` (0-1) over ``starts`` with the given PV and load,
    and for a plant with a DC-bus load its DC kW and whether a session under way at the
    first slot holds the battery: the ground truth's one call into a plant model (a
    ``PlantModel``'s ``project``).
    """

    def __call__(
        self,
        starts: Sequence[datetime],
        pv: Sequence[float],
        load: Sequence[float],
        soc: float,
        /,
        *,
        dc_load: Sequence[float] | None = None,
        held_at_start: bool | None = None,
    ) -> tuple[SlotFlow, ...]: ...


@dataclass(frozen=True)
class Recording:
    """What the meters recorded over the window, per slot of ``step`` (UTC)."""

    starts: tuple[datetime, ...]
    #: Split between sources and sinks at sample resolution, through the pinned
    #: conversion; None where the telemetry path was silent (the caller's reader).
    flows: tuple[Flows | None, ...]
    stored_after: tuple[float | None, ...]
    stored_before: float | None
    p_import: tuple[float, ...]
    p_export: tuple[float, ...]
    #: Raw meters, slot means, kW. The library does not read ``house_kw``; a caller
    #: derives its DC-bus draws (``dc_draws``) from it.
    pv_kw: tuple[float | None, ...]
    house_kw: tuple[float | None, ...]
    grid_import_kw: tuple[float | None, ...]
    grid_export_kw: tuple[float | None, ...]
    #: The inverter's AC meter, out to the house and in from it; None unconfigured.
    inverter_out_kw: tuple[float | None, ...] | None = None
    inverter_in_kw: tuple[float | None, ...] | None = None
    #: The DC-bus load's meter, DC kW; None where the site has none.
    dc_load_kw: tuple[float | None, ...] | None = None
    #: The slot the recording is read on (``EngineConfig.slot``).
    step: timedelta = SLOT


@dataclass(frozen=True)
class DayResult:
    """One local day: why it is not clean, and (when it ran) its measures."""

    day: date
    reasons: tuple[str, ...]
    #: Measure name -> (model, meters); None where the day did not run.
    pairs: dict[str, tuple[float, float]] | None = None
    #: Event -> (model, meters) minutes elapsed since local midnight (so a difference
    #: is a true one across a DST change; ``clock`` gives the wall clock); None: not
    #: that day.
    times: dict[str, tuple[float | None, float | None]] | None = None
    soc_mae_pp: float | None = None
    soc_max_pp: float | None = None


def _minutes(at: datetime, midnight: datetime) -> float:
    return (at - midnight) / timedelta(minutes=1)


def clock(day: date, minutes: float, tz: ZoneInfo) -> str:
    """A ``DayResult.times`` value as the local wall clock, ``HH:MM``."""
    midnight = datetime.combine(day, datetime.min.time(), tz).astimezone(UTC)
    return f"{(midnight + timedelta(minutes=minutes)).astimezone(tz):%H:%M}"


def _first(flags: Sequence[bool], ends: Sequence[datetime], midnight: datetime) -> float | None:
    for flag, end in zip(flags, ends, strict=True):
        if flag:
            return _minutes(end, midnight)
    return None


def routing_class(plant: Plant, flow: SlotFlow, noise: Noise = OBSERVED) -> str:
    """What a slot was doing, by the meters' noise floors (``noise``)."""
    if spilling(flow, noise):
        return "spilling"
    if flow.grid_to_battery > noise.topup_kw:
        return "topping up"
    if flow.pv_to_battery > noise.charge_kw:
        return "PV charging"
    if importing(storage_of(plant), flow, noise):
        return "importing"
    # The battery serving a DC-bus load serves load: the meters, read without the
    # DC-bus load (``meters_plant``), book it as serving the house.
    if flow.battery_to_load + flow.battery_to_dc_load > noise.need_kw:
        return "battery serving"
    return "idle"


def net_inverter_kw(plant: Plant, flow: SlotFlow, house_kw: float) -> float:
    """The model's inverter AC power, > 0 out to the house: what the battery delivers
    less what it takes and the inverter's own draw, and for a plant with a DC-bus load
    less the AC the hybrid takes for it: what serves the AC side beyond the house load
    ``house_kw`` and the draw (``SlotFlow``), with the battery's export."""
    if plant.has_dc_loads:
        draw = flow.inverter_draw
        ac_to_dc = flow.pv_to_load + flow.grid_to_load + flow.battery_to_load - house_kw - draw
        out = flow.battery_to_load + flow.battery_export
        return out - flow.pv_to_battery - flow.grid_to_battery - ac_to_dc - draw
    moving = flow.pv_to_battery + flow.grid_to_battery + flow.battery_to_load
    inverter = plant.inverter
    working = inverter.working_kw if moving > inverter.working_threshold_kw else 0.0
    draw = inverter.standby_kw + working
    return flow.battery_to_load - flow.pv_to_battery - flow.grid_to_battery - draw


def dc_bus_inputs(
    plant: Plant,
    served: Sequence[float | None],
    draws: Sequence[float],
    dc_load_kw: Sequence[float | None],
    soc_start: Sequence[float | None],
) -> tuple[list[float | None], list[float | None], list[bool | None]]:
    """What a plant model with a DC-bus load is fed of a session as the plant met it,
    per slot: the house (the load served less the DC-bus load's draw outside the house
    meter, the caller's ``draws``), the DC-bus load (its meter where it drew, else 0;
    None where it drew and the meter is unknown), and whether its hold rules held the
    battery from the session's metered start (``session_holds``). The ground truth and
    the routing check share it."""
    house = [
        None if kw is None else max(kw - max(draw, 0.0), 0.0)
        for kw, draw in zip(served, draws, strict=True)
    ]
    bus = [kw if draw > 0 else 0.0 for kw, draw in zip(dc_load_kw, draws, strict=True)]
    held = session_holds(plant, [kw or 0.0 for kw in bus], soc_start)
    return house, bus, held


def meters_plant(plant: Plant) -> Plant:
    """``plant`` as the ground truth reads the meters: without its DC-bus loads, their
    hold rules and the limits only they need (the battery's DC limits, the hybrid's AC
    rating), so a candidate with one is scored against the same meters as one without.
    ``plant`` itself where it has none."""
    config = plant.config
    if not plant.has_dc_loads and not config.dispatch.hold_rules:
        return plant
    part = config.battery
    hybrid = config.hybrid
    return Plant(
        replace(
            config,
            loads=tuple(load for load in config.loads if load.connection == "ac"),
            battery=None
            if part is None
            else replace(part, dc_charge_kw=None, dc_discharge_kw=None),
            hybrid=None if hybrid is None else replace(hybrid, ac_rating_kw=None),
            dispatch=replace(config.dispatch, hold_rules=()),
        )
    )


def session_holds(
    plant: Plant, dc_kw: Sequence[float], soc_start: Sequence[float | None]
) -> list[bool | None]:
    """Per slot inside a session of ``plant``'s hold rules, whether it holds the battery:
    any rule whose session (the DC-bus load over its ``dc_load_above_kw``) started below
    its ``start_below_soc``, by the metered SOC at the session's first slot (as the
    general dispatch decides; ``_paths``); None outside every session, and where the
    SOC at a session's start is unknown (the engine then decides from the run's start).

    The engine takes one answer for every rule (``held_at_start``), so overlapping
    rules can disagree with it; one rule is the common case. A session is the DC-bus load
    as fed, which is gated on its draw outside the house meter (``dc_draws``): a
    dip under its limit ends a session, and the next starts at that slot's SOC."""
    held: list[bool | None] = [None] * len(dc_kw)
    for rule in plant.config.dispatch.hold_rules:
        level: bool | None = None
        for i, kw in enumerate(dc_kw):
            on = kw > rule.dc_load_above_kw
            if on and (i == 0 or dc_kw[i - 1] <= rule.dc_load_above_kw):
                start = soc_start[i]
                level = None if start is None else start < rule.start_below_soc
            if on and level is not None:
                held[i] = bool(held[i]) or level
    return held


def _limits(plant: Plant, noise: Noise) -> tuple[float, float]:
    """The SOC at which ``plant``'s battery counts as full, and as empty, within the
    meters' noise (``noise.soc``)."""
    battery = plant.battery
    return battery.saturation_soc - noise.soc, battery.reserve_soc + noise.soc


@dataclass(frozen=True)
class HourlyRun:
    """A run of the plant model from one hour, over the horizon or to the first unknown
    input: what the caller groups by its labels."""

    start: datetime
    #: Every label of every day it touches (:data:`GAP` among them), sorted.
    reasons: tuple[str, ...]
    #: The local days it touches.
    touched: frozenset[date]
    #: It reached the full horizon (:data:`HORIZON`).
    full: bool
    #: Each slot's hours-ahead bucket and |SOC error|, pp, in order.
    soc_errors: tuple[tuple[str, float], ...]
    #: The model's first event and the meters', where both are known; None otherwise.
    event: tuple[Event, Event] | None


@dataclass(frozen=True)
class Projection:
    """What :func:`project_against_meters` found: each day, each hourly run, and the
    routing classes ("model -> meters" counts over every day that ran)."""

    days: tuple[DayResult, ...]
    runs: tuple[HourlyRun, ...]
    routing: dict[str, int]


def project_against_meters(
    recording: Recording,
    *,
    pinned: Plant,
    plant: Plant,
    project: Projector,
    tz: ZoneInfo,
    labels: Mapping[date, tuple[str, ...]],
    dc_draws: Sequence[float] | None = None,
    noise: Noise = OBSERVED,
) -> Projection:
    """Run ``plant`` (through ``project``) against ``recording``; ``pinned`` derives the
    inputs and the metered routing (module docstring). ``labels``: each local day's
    labels, the caller's; :data:`GAP` is prepended to a day the telemetry was silent
    in. ``dc_draws``: the DC-bus load's draw outside the house meter, per slot (0 where
    none), for a plant model with a DC-bus load. ``noise``: the meters' floors the
    routing, the events and full and empty are read by (``EngineConfig.observed``)."""
    rec = recording
    n = len(rec.starts)
    step = rec.step
    hours_each = step / timedelta(hours=1)
    horizon = int(REACH / step)
    per_day = int(timedelta(days=1) / step)
    capacity = pinned.battery.capacity_kwh
    metered = meters_plant(pinned)  # the meters, read through the pinned plant
    served = served_load(rec.flows, plant=metered)
    soc_end = [None if kwh is None else kwh / capacity for kwh in rec.stored_after]
    soc_start = [(rec.stored_before if i == 0 else rec.stored_after[i - 1]) for i in range(n)]
    # The starting SOC, clamped to 0-1 as a reading is clamped.
    soc_start_f = [None if kwh is None else min(max(kwh / capacity, 0.0), 1.0) for kwh in soc_start]
    # A plant model with a DC-bus load: its slots' draw leaves the AC load for the DC
    # bus, where the DC-bus load's meter gives it (module docstring).
    on_dc = plant.has_dc_loads
    if on_dc and rec.dc_load_kw is None:
        raise ValueError("a plant model with a DC-bus load needs the DC-bus load's meter")
    if on_dc and dc_draws is None:
        raise ValueError("a plant model with a DC-bus load needs the DC-bus load's draws")
    house: Sequence[float | None] = served
    dc_kw: Sequence[float | None] = [0.0] * n
    held: Sequence[bool | None] = [None] * n
    if on_dc and rec.dc_load_kw is not None and dc_draws is not None:
        house, dc_kw, held = dc_bus_inputs(plant, served, dc_draws, rec.dc_load_kw, soc_start_f)
    known = [
        rec.flows[i] is not None
        and rec.pv_kw[i] is not None
        and house[i] is not None
        and dc_kw[i] is not None
        and soc_end[i] is not None
        and soc_start_f[i] is not None
        and rec.grid_import_kw[i] is not None
        and rec.grid_export_kw[i] is not None
        for i in range(n)
    ]

    def run(indices: Sequence[int]) -> tuple[SlotFlow, ...]:
        """The plant model from the first slot of ``indices`` over them."""
        args = (
            [rec.starts[i] for i in indices],
            [rec.pv_kw[i] or 0.0 for i in indices],
            [house[i] or 0.0 for i in indices],
            soc_start_f[indices[0]] or 0.0,
        )
        if not on_dc:
            return project(*args)
        return project(
            *args,
            dc_load=[dc_kw[i] or 0.0 for i in indices],
            held_at_start=held[indices[0]],
        )

    exports = battery_exports(rec.starts, rec.flows, noise)
    observed = observed_flows(
        rec.starts,
        rec.flows,
        rec.stored_after,
        rec.stored_before,
        plant=metered,
    )
    # The DC-bus load is a load like any other: the meters' side keeps it, as the
    # model's side does (served_load).
    realised = realise(
        storage_of(metered),
        observed,
        rec.p_import,
        rec.p_export,
        step=step,
        unscored=exports,
        noise=noise,
    )

    # Days, and why each is not clean: a silent stretch, then the caller's labels.
    by_day: dict[date, list[int]] = {}
    for i, start in enumerate(rec.starts):
        by_day.setdefault(start.astimezone(tz).date(), []).append(i)
    reasons_of: dict[date, tuple[str, ...]] = {
        day: ((GAP,) if not all(known[i] for i in slots) else ()) + tuple(labels.get(day, ()))
        for day, slots in by_day.items()
    }

    routing = Counter[str]()
    days = []
    for day, slots in sorted(by_day.items()):
        reasons = reasons_of[day]
        if GAP in reasons:
            days.append(DayResult(day, reasons))
            continue
        starts = [rec.starts[i] for i in slots]
        modelled = run(slots)
        midnight = datetime.combine(day, datetime.min.time(), tz).astimezone(UTC)
        ends = [start + step for start in starts]
        # A day runs only when every slot is known (``GAP``), so no SOC here is None.
        metered_soc = [soc for i in slots if (soc := soc_end[i]) is not None]
        errors = [
            (soc_of(flow) - soc) * 100 for flow, soc in zip(modelled, metered_soc, strict=True)
        ]
        metered_flows = [rec.flows[i] for i in slots]
        pairs: dict[str, tuple[float, float]] = {
            "import kWh/day": (
                sum((f.grid_to_load + f.grid_to_battery) * hours_each for f in modelled),
                sum((rec.grid_import_kw[i] or 0.0) * hours_each for i in slots),
            ),
            "export kWh/day": (
                sum(f.pv_export * hours_each for f in modelled),
                sum((rec.grid_export_kw[i] or 0.0) * hours_each for i in slots),
            ),
            "top-up kWh/day": (
                sum(f.grid_to_battery * hours_each for f in modelled),
                sum(m.grid_to_battery * hours_each for m in metered_flows if m is not None),
            ),
        }
        if (
            rec.inverter_out_kw is not None
            and rec.inverter_in_kw is not None
            and all(
                rec.inverter_out_kw[i] is not None and rec.inverter_in_kw[i] is not None
                for i in slots
            )
        ):
            net = [
                net_inverter_kw(plant, f, house[i] or 0.0)
                for f, i in zip(modelled, slots, strict=True)
            ]
            pairs["inverter out kWh/day"] = (
                sum(max(kw, 0.0) * hours_each for kw in net),
                sum((rec.inverter_out_kw[i] or 0.0) * hours_each for i in slots),
            )
            pairs["inverter in kWh/day"] = (
                sum(max(-kw, 0.0) * hours_each for kw in net),
                sum((rec.inverter_in_kw[i] or 0.0) * hours_each for i in slots),
            )
        # Full and empty by each side's own battery: the candidate's for its projection,
        # the pinned one's for the meters, so a candidate cannot move its own target.
        model_full, model_empty = _limits(plant, noise)
        full, empty = _limits(pinned, noise)
        times = {
            "top-up start": (
                _first([f.grid_to_battery > noise.topup_kw for f in modelled], starts, midnight),
                _first(
                    [m is not None and m.grid_to_battery > noise.topup_kw for m in metered_flows],
                    starts,
                    midnight,
                ),
            ),
            "first full": (
                _first([soc_of(f) >= model_full for f in modelled], ends, midnight),
                _first([soc >= full for soc in metered_soc], ends, midnight),
            ),
            "first empty": (
                _first([soc_of(f) <= model_empty for f in modelled], ends, midnight),
                _first([soc <= empty for soc in metered_soc], ends, midnight),
            ),
        }
        for flow, i in zip(modelled, slots, strict=True):
            seen = observed[i]
            if seen is not None:
                routing[
                    f"{routing_class(plant, flow, noise)} -> {routing_class(metered, seen, noise)}"
                ] += 1
        days.append(
            DayResult(
                day,
                reasons,
                pairs,
                times,
                statistics.fmean(abs(e) for e in errors),
                max(abs(e) for e in errors),
            )
        )

    # From each hour, over the horizon: SOC error by hours ahead, and the first event.
    runs: list[HourlyRun] = []
    for i in range(n):
        if not known[i] or rec.starts[i].minute != 0:
            continue
        end = i
        while end < n and end - i < horizon and known[end]:
            end += 1
        touched = frozenset(rec.starts[k].astimezone(tz).date() for k in range(i, end))
        reasons = tuple(sorted({r for d in touched for r in reasons_of[d]}))
        modelled = run(range(i, end))
        soc_errors: list[tuple[str, float]] = []
        for k, flow in enumerate(modelled):
            label = hours_ahead_bucket(rec.starts[i + k] - rec.starts[i])
            if label is None:
                continue
            soc_errors.append((label, abs((soc_of(flow) - (soc_end[i + k] or 0.0)) * 100)))
        # The event check prices this same projection, so the model's side has the
        # inputs every other measure has (metered PV, not the DC-bus-adjusted split).
        event: tuple[Event, Event] | None = None
        metered_event = realised.event.get(rec.starts[i])
        if metered_event is not None:
            hour = price_hour(
                storage_of(plant),
                modelled,
                rec.p_import[i:end],
                rec.p_export[i:end],
                day=per_day,
                noise=noise,
            )
            if hour.resolved:
                event = (hour.event, metered_event[0])
        runs.append(
            HourlyRun(rec.starts[i], reasons, touched, end - i == horizon, tuple(soc_errors), event)
        )
    return Projection(tuple(days), tuple(runs), dict(sorted(routing.items())))
