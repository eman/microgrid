"""The checks that run the plant and price models against what the meters did.

**What a kWh realised.** The price, regime and certainties are claims about what
a kWh of flexible load would cost. Over metered flows the rule says what one did
(:func:`realise`, :func:`observed_flows`, :func:`observed_regime`), and an hour of them
can be routed through the plant model and priced (:func:`route_hour`,
:func:`price_hour`). The scores over those (the forecast's errors by hours ahead,
:data:`BUCKETS`) are a client's to keep.

Pure: no I/O.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final

from microgrid.engine import Regime
from microgrid.engine.extend import (
    OBSERVED,
    Event,
    Flows,
    Noise,
    PlantModel,
    SlotFlow,
    Storage,
    importing,
    price_flows,
    spilling,
)
from microgrid.engine.series import SLOT
from microgrid.plant import Plant

#: Horizon buckets, ``[from, to)`` ahead of the issue.
BUCKETS: Final[tuple[tuple[timedelta, timedelta], ...]] = (
    (timedelta(0), timedelta(hours=1)),
    (timedelta(hours=1), timedelta(hours=3)),
    (timedelta(hours=3), timedelta(hours=12)),
    (timedelta(hours=12), timedelta(hours=48)),
)


def storage_of(plant: PlantModel) -> Storage:
    """``plant``'s battery, as the rule asks about it. The checks here score a battery's
    regimes, so they take a plant with one; the engine also values plants without."""
    storage = plant.storage
    if storage is None:
        raise ValueError("the checks take a plant with a battery")
    return storage


def soc_end(flow: SlotFlow) -> float:
    """``flow``'s SOC at its end: a plant with a battery projects one, and metered flows
    carry the meters' (``observed_flows``)."""
    soc = flow.soc_end
    if soc is None:
        raise ValueError("a flow of a plant with a battery has its SOC")
    return soc


def bucket_label(low: timedelta, high: timedelta) -> str:
    """A :data:`BUCKETS` entry's name, e.g. ``"3-12h"``."""
    return f"{int(low.total_seconds() // 3600)}-{int(high.total_seconds() // 3600)}h"


def hours_ahead_bucket(ahead: timedelta) -> str | None:
    """The :data:`BUCKETS` name ``ahead`` falls in; None beyond the last."""
    for low, high in BUCKETS:
        if low <= ahead < high:
            return bucket_label(low, high)
    return None


def observed_flows(
    starts: Sequence[datetime],
    flows: Sequence[Flows | None],
    stored_after: Sequence[float | None],
    stored_before: float | None,
    *,
    plant: Plant,
) -> list[SlotFlow | None]:
    """The metered flows (``microgrid.engine.ledger.integrate``) as ``plant``'s slot
    flows (``Plant.metered_slot``), which its battery's answers read.

    SOC at a slot's start is the metered stored energy at the previous slot's
    end; None where the flows or either SOC are unknown. A slot flow has no
    battery export: where the battery exported, its SOC still falls by it, and
    :func:`battery_exports` names those slots for :func:`realise` to leave out.

    Every load the plant metered stays in, the DC-bus load's included: it is a load
    like any other.
    """
    if not len(starts) == len(flows) == len(stored_after):
        raise ValueError("one flow and one stored energy per slot")
    capacity_kwh = plant.battery.capacity_kwh
    result: list[SlotFlow | None] = []
    before = stored_before
    for start, flow, after in zip(starts, flows, stored_after, strict=True):
        if flow is None or before is None or after is None:
            result.append(None)
        else:
            result.append(
                plant.metered_slot(start, before / capacity_kwh, after / capacity_kwh, flow)
            )
        before = after
    return result


def battery_exports(
    starts: Sequence[datetime], flows: Sequence[Flows | None], noise: Noise = OBSERVED
) -> frozenset[datetime]:
    """The slots in which the battery exported: the rule cannot say what a kWh
    then displaced, since the model has no battery export.

    Not where the PV left over after the site's consumption could have been the
    whole export: the split (``split_paths``) gives export to a discharging battery
    first, and at full, sensor timing can show a discharge while PV exports (1 of the
    16 battery exports at full in one site's 30 days to 2026-09-25).
    """

    def exported(flow: Flows) -> bool:
        pv = flow.pv_to_load + flow.pv_to_battery + flow.pv_export
        pv_spare = max(pv - flow.consumed, 0.0)
        return flow.battery_export > noise.spill_kw and pv_spare < (
            flow.pv_export + flow.battery_export
        )

    return frozenset(
        start
        for start, flow in zip(starts, flows, strict=True)
        if flow is not None and exported(flow)
    )


def inverter_kw(plant: Plant, flow: Flows) -> float:
    """The inverter's own draw in a metered slot, kW (``Plant.own_draw_kw``).
    Metered consumption holds it; the house meter and the plant model's load do not."""
    return plant.own_draw_kw(flow)


def served_load(flows: Sequence[Flows | None], *, plant: Plant) -> list[float | None]:
    """Per slot, the load the plant served as its model takes it, kW; None unmetered.

    The site's consumption less the inverter's own draw (:func:`inverter_kw`), which
    the plant model adds itself. The consumption is derived from the plant's PV, grid
    and battery meters, so it includes the DC-bus load: a load like any other. The
    routing check and the plant's ground truth both feed the model this.
    """
    return [
        None if flow is None else max(flow.consumed - inverter_kw(plant, flow), 0.0)
        for flow in flows
    ]


@dataclass(frozen=True, slots=True)
class RoutedHour:
    """The plant model run from one metered slot over what happened, read by the rule."""

    #: The model's first slot, read as the meters are read (:func:`observed_regime`).
    regime: Regime
    #: Whether the rule found what prices the first slot within the window.
    resolved: bool
    price: float
    event: Event
    #: The start of the slot the event happened in; None when not resolved.
    event_at: datetime | None
    spilled: bool


def route_hour(
    plant: PlantModel,
    window: Sequence[SlotFlow],
    load_kw: Sequence[float],
    p_import: Sequence[float],
    p_export: Sequence[float],
    *,
    day: int,
    dc_load_kw: Sequence[float] | None = None,
    held_at_start: bool | None = None,
    noise: Noise = OBSERVED,
    step: timedelta = SLOT,
) -> RoutedHour:
    """The plant model over ``window``'s metered PV and ``load_kw``, from its first
    slot's metered SOC, priced by the rule with the meters' noise floors (``noise``: the
    engine's ``EngineConfig.observed``, ``OBSERVED`` by default): what the model says
    would have priced a kWh in the first slot. A plant model with a DC-bus load takes
    its DC kW and whether a session under way holds the battery. ``step``: the slots'
    length (``EngineConfig.slot``)."""
    pv = [f.pv_to_load + f.pv_to_battery + f.pv_export for f in window]
    starts = [f.start for f in window]
    if dc_load_kw is None:
        modelled = plant.project(starts, [step] * len(window), pv, load_kw, window[0].soc_start)
    else:
        modelled = plant.project(
            starts,
            [step] * len(window),
            pv,
            load_kw,
            window[0].soc_start,
            dc_load_kw=dc_load_kw,
            held_at_start=held_at_start,
        )
    return price_hour(storage_of(plant), modelled, p_import, p_export, day=day, noise=noise)


def price_hour(
    storage: Storage,
    modelled: Sequence[SlotFlow],
    p_import: Sequence[float],
    p_export: Sequence[float],
    *,
    day: int,
    noise: Noise = OBSERVED,
) -> RoutedHour:
    """A projection from a metered slot, priced by the rule with the meters' noise
    floors (``noise``): what the model says would have priced a kWh in its first slot.
    The caller projects, so any plant model can be priced."""
    priced = price_flows(storage, modelled, p_import, p_export, day, noise=noise)
    resolved = priced.resolved[0]
    return RoutedHour(
        regime=observed_regime(storage, modelled[0], noise),
        resolved=resolved,
        price=priced.prices[0],
        event=priced.events[0],
        event_at=modelled[priced.event_at[0]].start if resolved else None,
        spilled=priced.spilled[0],
    )


def observed_regime(storage: Storage, flow: SlotFlow, noise: Noise = OBSERVED) -> Regime:
    """What +1 kWh displaced in a metered slot, as ``regimes`` says of the model's.

    PV spilling is ``export``: curtailment is not metered. A battery
    above its reserve, not topping up and with the house not importing, would
    have served the kWh (``battery``), idle or not: it sits idle where PV just
    covers the house. At the reserve it is ``import``.
    """
    if spilling(flow, noise):
        return Regime.EXPORT
    if importing(storage, flow, noise) or flow.grid_to_battery > noise.topup_kw:
        return Regime.IMPORT
    if soc_end(flow) > storage.reserve_soc + noise.soc:
        return Regime.BATTERY
    return Regime.IMPORT


@dataclass(frozen=True, slots=True)
class Realised:
    """What a kWh used in each slot turned out to cost, by slot start (None: unknown)."""

    price: Mapping[datetime, float]
    #: What priced it (``microgrid.engine.extend.Event``), and the start of the slot it happened in.
    event: Mapping[datetime, tuple[Event, datetime]]
    #: The outcome it rests on: the event's kind and the first slot of its run
    #: (``FlowPrices.outcome_at``). Slots that share one are nearly one trial.
    outcome: Mapping[datetime, tuple[Event, datetime]]
    regime: Mapping[datetime, Regime]
    spilled: Mapping[datetime, bool]
    covered: Mapping[datetime, bool]
    #: The expensive run each ``covered`` is about, by its first slot.
    cover_outcome: Mapping[datetime, datetime]


def realise(
    storage: Storage,
    flows: Sequence[SlotFlow | None],
    p_import: Sequence[float],
    p_export: Sequence[float],
    *,
    step: timedelta,
    unscored: Collection[datetime] = frozenset(),
    noise: Noise = OBSERVED,
    stored_cost: Sequence[float | None] | None = None,
) -> Realised:
    """The rule over what was metered: each unbroken run of known slots on its own.
    ``stored_cost``: per slot, the blended cost of what the battery held at its start
    (the ledger over the meters; ``Pricing.battery_value = "stored_cost"``), which a
    battery kWh is priced at; without it, the make-up's.

    A slot is priced only when its event lies inside its run (``resolved``): a
    run ends at the data's end or at a slot the meters did not see, and what
    came after that is not known. Slots in ``unscored`` (:func:`battery_exports`) are
    followed through but get no values of their own.

    So the realised set leans away from slots whose event lay across a gap in
    the meters, and from the hours the battery exported in.
    """
    if not len(flows) == len(p_import) == len(p_export):
        raise ValueError("one price pair per slot")
    day = max(1, round(timedelta(days=1) / step))
    price: dict[datetime, float] = {}
    event: dict[datetime, tuple[Event, datetime]] = {}
    outcome: dict[datetime, tuple[Event, datetime]] = {}
    regime: dict[datetime, Regime] = {}
    spilled: dict[datetime, bool] = {}
    covered: dict[datetime, bool] = {}
    cover_outcome: dict[datetime, datetime] = {}
    index = 0
    while index < len(flows):
        if flows[index] is None:
            index += 1
            continue
        end = index
        while end < len(flows) and flows[end] is not None:
            end += 1
        run = [flow for flow in flows[index:end] if flow is not None]
        priced = price_flows(
            storage,
            run,
            p_import[index:end],
            p_export[index:end],
            day,
            noise=noise,
            stored_cost=None if stored_cost is None else stored_cost[index:end],
        )
        for offset, flow in enumerate(run):
            if flow.start in unscored:
                continue
            regime[flow.start] = observed_regime(storage, flow, noise)
            if priced.resolved[offset]:
                price[flow.start] = priced.prices[offset]
                kind = priced.events[offset]
                event[flow.start] = (kind, run[priced.event_at[offset]].start)
                family = Event.FILL if kind is Event.SPILL else kind
                family = Event.EMPTY if family is Event.IMPORT else family
                outcome[flow.start] = (family, run[priced.outcome_at[offset]].start)
                spilled[flow.start] = priced.spilled[offset]
            if priced.covered_resolved[offset]:
                covered[flow.start] = priced.covered[offset]
                cover_outcome[flow.start] = run[priced.cover_at[offset]].start
        index = end
    return Realised(price, event, outcome, regime, spilled, covered, cover_outcome)
