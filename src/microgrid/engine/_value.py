"""What a kWh of flexible load costs the site, and how sure that is.

The rule prices energy with TariffKit's import and export prices and the likelihood that
the sun charges the battery enough. This service is not a charge controller: it decides
nothing about the battery. It follows the plant model behind its port
(:class:`~microgrid.plant.port.PlantModel`), the same one ``surplus_kw`` comes from, and
asks what one more kWh used in a slot does to the site's bill.

**The rule.** A kWh used now is either taken from PV that is exported now, or
it leaves the battery a kWh lower (charged a kWh less, or discharged a kWh
more). Follow the plant forward to whichever comes first:

- **the battery fills and exports**: the kWh only meant a kWh less exported
  then, so it costs that slot's export price (0 if the PV is curtailed);
- **the plant tops the battery up from the grid** (the site's grid-charge window, when
  it has one): the plant buys the kWh back there, so it costs that slot's import price.
  In the plant model that is wherever its top-up binds: after an early top-up the
  battery is on course to end the window at its target, so a kWh taken later in the
  window is bought back in the next slot (``SlotFlow.topup_binding``). A top-up at the
  charge limit cannot buy more, and the kWh goes to the next event;
- **the battery runs empty and the house imports**: the battery would have to buy the
  kWh back, and it does so from the grid in the cheapest hour before it is needed (every
  kWh the battery does not get from the sun because something else used it is a kWh it
  must buy from the grid), so it costs the cheapest import between now and then. Before
  the expensive hours, that is the day's import price; inside them, with no cheaper hour
  left, the hour's own price.

Neither within the horizon: the day's import price, the cheapest in the
horizon's last day or ahead (prices beyond it are not known; a day's shape is
taken to repeat). A slot that imports now costs its own import price.

**What serves the kWh** (:class:`Served`), through the battery's losses and wear
(docs/methodology/stored-value.md). The make-up sets what a stored kWh is worth, ``v``
(:attr:`FlowPrices.stored_value`):

- a fill or a top-up puts the kWh back: ``v`` is what refilling a stored kWh
  costs, ``(p + c_ch) / eta_c`` (``Battery.refill_value``);
- an empty battery, or the horizon, does not: the stored kWh would have served
  the house in place of an import, ``v = eta_d (p - c_dis)``
  (``Battery.displaced_value``). A kWh drawn now only moves the discharge
  earlier: no more goes through the battery.

Each efficiency is the inverter's at the margin of the power it already converts in that
slot (``Battery.eta_charge_at``, ``eta_discharge_at``; at low power for the horizon,
whose use is unknown): its loss grows with power. Its fixed draws, idle and while the
battery works, are the plant model's: a marginal kWh does not pay them.

Spilling PV serves it at the export it displaces. PV charging the battery is charged a
kWh less: ``eta_c v - c_ch``, the make-up itself after a fill, less than it after an
empty battery (the losses and the charge's wear are saved). The battery, discharging or
able to, gives a kWh: ``v / eta_d + c_dis``, the make-up itself after an empty battery,
more than it after a fill or a top-up (the kWh goes round the battery again), so it can
cost more than importing now. Those two are the battery's dead band
(``Battery.dead_band``). The grid serves it at the import where the house imports now,
and, with no PV charging to divert, where the plant holds the battery (topping it up,
keeping it at its target, or through a DC-bus load's session: ``SlotDispatch.held``) or
it has nothing left to give (at its reserve, or at its discharge limit), even with the
house not importing. The values by source (:class:`PvValue`, :class:`BatteryValue`) are
the same figures for a PV kWh diverted and a battery kWh drawn, with what set them. The
top-up event is the plant closing the gap in an inexpensive hour, made explicit: without
it a purchase inside the projection goes unpriced, and a kWh taken in the morning looks
like it only delayed a spill. Without a window the rule reduces to the sun as the
battery's only source, and the empty event prices the purchase.

**Scenarios.** A PV forecast gives its quantiles per interval (Solcast: p10, p50 and
p90; any levels that include the median, ``series.Quantiles``). They are read as a
distribution per slot (linear between the levels, extended linearly beyond on a side
that has one) and cut into equal-probability traces, ten by default, the 5th to the
95th percentile, each taken at the same percentile in every slot (a day is bright or
dull as a whole). The price is the expectation over them, so the likelihood
that the battery fills is in the price: a low battery on an uncertain morning
costs about the import price, a battery that surely fills about the export.

Two diagnostics say how sure it is:

- **certainty_spill**: the scenario weight in which the kWh is paid for with
  exported (or curtailed) PV only, now or when the battery fills (through the
  battery's losses and wear if it served the kWh);
- **certainty_covered**: the scenario weight in which the battery (charged by
  the sun, or topped up in the plant's window) covers the expensive hours
  ahead without an import: the next hours after PV stops covering the house,
  priced above the cheapest import of the day ahead (part-peak and peak on
  a time-of-use plan), read from TariffKit's curve with no tiers assumed.

Future slots are priced as an unannounced load: the plant runs as forecast up
to the slot (docs/methodology/marginal-price.md).

The rule itself is :func:`price_flows`: it prices a sequence of slot flows,
whether the plant model's or the metered ones (``analysis.scoring`` prices what
happened with it, above the meters' noise: :data:`OBSERVED`).

Pure: no I/O.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Final, Literal

from microgrid.engine._stored import Lots, project_average
from microgrid.engine.series import Quantiles, _interpolate
from microgrid.plant.flows import (
    PvKw,
    SlotFlow,
    pv_frozen,
    pv_slice,
    pv_slots,
)
from microgrid.plant.port import ChargeSource, PlantModel, Storage

#: Ten equal-probability PV traces: the 5th, 15th, ..., 95th percentile.
SCENARIO_QUANTILES = tuple((k + 0.5) / 10 for k in range(10))  # Pricing().quantiles
_TOLERANCE = 1e-9


@dataclass(frozen=True, slots=True)
class Noise:
    """Below these, a flow or an SOC difference is not an event for the rule.

    The kW floors are for 10-minute slots (:data:`OBSERVED` was measured on them).
    """

    #: PV exported or curtailed, net of what the battery and grid supplied in the
    #: same slot, kW.
    spill_kw: float
    #: The house importing, kW.
    short_kw: float
    #: PV charging the battery, kW.
    charge_kw: float
    #: The grid charging the battery, kW.
    topup_kw: float
    #: The battery and the grid serving the house (PV no longer covers it), kW.
    need_kw: float
    #: Full is within this much SOC of saturation, empty of the reserve (0-1).
    soc: float
    #: An import up to this, kW, while the battery was above its reserve and had
    #: the headroom to serve it, is the plant's grid regulation, not the house
    #: importing (metered flows only; 0: none).
    regulation_kw: float = 0.0


#: The plant model's flows: float residue only (plant.surplus's 1 W floor).
#: The house importing too: a battery that runs empty at dawn with 50 W short is
#: empty (20 Wh a slot missed it and priced the night at the next fill).
MODEL: Final = Noise(
    spill_kw=0.001,
    short_kw=0.001,
    charge_kw=0.001,
    topup_kw=_TOLERANCE,
    need_kw=_TOLERANCE,
    soc=1e-6,
)
#: Metered flows: the default floors (``EngineConfig.observed``), measured on the plant
#: the engine was first validated on (the 30 days to 2026-09-24); a plant whose
#: meters report differently sets its own. The measurements:
#:
#: - spill, import, need: above the model's own import floor, 0.12 kW (20 Wh in
#:   a ten-minute slot). The grid and battery sensors report at
#:   different instants: net PV export far from full (SOC under 95 %) was up to
#:   0.066 kW in 90 % of exporting slots, 0.13 kW in 95 %; the 5 % above the
#:   floor are real exports on PV ramps, while the battery charged below its limit.
#: - PV charging: 0.12 kW, the same floor. Where PV fell 0.5 kW or more short of
#:   the site's consumption, so it could not charge the battery, the meters booked
#:   PV charging of at most 0.051 kW in 99.9 % of 2455 slots, and over 0.12 kW once
#:   (sensor timing).
#: - top-up: 0.5 kW. Every grid-charging run the plant made peaked at 2.9 kW or
#:   more; the sensors' timing made single slots of up to 0.42 kW (0.07 kWh).
#: - full and empty: half a point of SOC. Saturation read exactly in 269 of 280
#:   exporting slots at full; the reserve exactly in 64 of 65 importing slots near it.
#: - regulation: 0.3 kW. While the battery served the house with headroom, the
#:   grid supplied up to 0.14 kW in 99.5 % of slots. With the battery idle above
#:   its reserve, imports over 0.12 kW were either the dusk hand-off (up to
#:   0.2 kW, a slot as PV fades) or the plant holding the battery (0.4 kW and up,
#:   the house load): the first is the battery's, the second the grid's.
OBSERVED: Final = Noise(
    spill_kw=0.12,
    short_kw=0.12,
    charge_kw=0.12,
    topup_kw=0.5,
    need_kw=0.12,
    soc=0.005,
    regulation_kw=0.3,
)


#: The rules that value a kWh the battery holds (:attr:`Pricing.battery_value`).
type BatteryValueRule = Literal["stored_cost", "replacement"]
BATTERY_VALUE_RULES: Final = ("stored_cost", "replacement")


@dataclass(frozen=True, slots=True)
class Pricing:
    """How the price is computed."""

    #: Equal-probability PV scenarios cut from Solcast's p10/p50/p90.
    scenarios: int = 10
    #: What a kWh the battery holds is worth, which prices a kWh it delivers:
    #: ``stored_cost``, what the energy it holds cost: its running
    #: average, solar at the export it gave up and grid energy at what it was bought for,
    #: at most the solar that refills the room where a fill comes first; or
    #: ``replacement``, what makes the kWh up later (the make-up rule).
    battery_value: BatteryValueRule = "stored_cost"

    def __post_init__(self) -> None:
        if not 1 <= self.scenarios <= 50:
            raise ValueError("scenarios must be in [1, 50]")
        if self.battery_value not in BATTERY_VALUE_RULES:
            raise ValueError(f"battery_value must be one of {', '.join(BATTERY_VALUE_RULES)}")

    @property
    def quantiles(self) -> tuple[float, ...]:
        """The scenarios' PV percentiles: the midpoints of equal-probability bins."""
        return tuple((k + 0.5) / self.scenarios for k in range(self.scenarios))


@dataclass(frozen=True, slots=True)
class EnergyValue:
    """Per slot: the price of one more kWh of flexible load, and how sure it is."""

    #: Per kWh, expected over the PV scenarios.
    price: tuple[float, ...]
    #: Per kWh in the pessimistic PV case (R-6): priced on Solcast's p10.
    price_p10: tuple[float, ...]
    #: Scenario weight in which the kWh costs only exported or curtailed PV (0-1).
    certainty_spill: tuple[float, ...]
    #: Scenario weight in which the sun charges the battery enough for the
    #: expensive hours ahead (0-1).
    certainty_covered: tuple[float, ...]
    #: Values by source (R-2, R-3), expected over the scenarios in
    #: which the source has energy to give (None: in none). A PV kWh diverted to a
    #: flexible load, and how that PV was routed; a battery kWh drawn for one, and
    #: what the battery buys it back with.
    pv_value: tuple[PvValue | None, ...] = ()
    battery_value: tuple[BatteryValue | None, ...] = ()
    #: Per stored kWh (:attr:`FlowPrices.stored_value`), expected over the scenarios.
    stored_value: tuple[float, ...] = ()
    #: Scenario weight in which the slot has unserved load (0-1): its price there is
    #: the import as a floor, not a reachable source.
    unserved: tuple[float, ...] = ()
    #: The scenarios as run, for running them again (``_live.revalue``).
    model: Model | None = None


class PvRouting(StrEnum):
    """Where a PV kWh would have gone had no flexible load taken it (R-3).

    In declaration order: a tie in scenario weight goes to the first.
    """

    EXPORT = "export"
    CURTAIL = "curtail"
    BATTERY = "battery"


class Displaces(StrEnum):
    """What a battery kWh drawn now is bought back with (R-3).

    In declaration order: a tie in scenario weight goes to the first, the import.
    """

    #: A top-up, an import once it runs empty, or the horizon's import: a kWh more bought.
    LATER_IMPORT = "later_import"
    #: The battery fills and spills later: a kWh less exported then.
    EXPORT = "export"


@dataclass(frozen=True, slots=True)
class PvValue:
    """A PV kWh diverted to a flexible load: per kWh, expected over the scenarios
    in which PV had spare (``weight``, 0-1), and where it went in most of them."""

    per_kwh: float
    routing: PvRouting
    weight: float

    def to_mapping(self) -> dict[str, object]:
        """Plain data (``valuation/2``'s ``PvValue``)."""
        return {
            "per_kwh": self.per_kwh,
            "routing": self.routing.value,
            "weight": self.weight,
        }


@dataclass(frozen=True, slots=True)
class BatteryValue:
    """A battery kWh drawn for a flexible load: per kWh, expected over the
    scenarios in which the battery had it to give (``weight``, 0-1), and what buys
    it back in most of them."""

    per_kwh: float
    displaces: Displaces
    weight: float

    def to_mapping(self) -> dict[str, object]:
        """Plain data (``valuation/2``'s ``BatteryValue``)."""
        return {
            "per_kwh": self.per_kwh,
            "displaces": self.displaces.value,
            "weight": self.weight,
        }


@dataclass(frozen=True, slots=True)
class Model:
    """The scenarios a valuation ran (:func:`value_energy`), for running it again.

    Opaque to a client: pass it back (a ``LiveSession``) and leave its fields
    alone. They are not part of the contract,
    and may change between engine/0 releases.
    """

    plant: PlantModel
    starts: tuple[datetime, ...]
    #: Each slot's length.
    durations: tuple[timedelta, ...]
    #: PV per scenario (the site's, or each array's: ``PvKw``), and its weight; the house
    #: load (kW per slot).
    scenarios: tuple[PvKw, ...]
    weights: tuple[float, ...]
    load_kw: tuple[float, ...]
    p_import: tuple[float, ...]
    p_export: tuple[float, ...]
    #: Each scenario's flows and prices.
    flows: tuple[tuple[SlotFlow, ...], ...]
    priced: tuple[FlowPrices, ...]
    #: The DC-bus loads (DC kW per slot); None: none draws.
    dc_load_kw: tuple[float, ...] | None = None
    #: The battery's lots at the valuation's moment (None: all of unknown origin), and
    #: the rule that values them (:attr:`Pricing.battery_value`).
    lots: Lots | None = None
    battery_value: BatteryValueRule = "stored_cost"


def quantile_traces(
    slots: Sequence[Quantiles | None],
    quantiles: Sequence[float] = SCENARIO_QUANTILES,
) -> list[list[float]]:
    """PV traces (kW per slot) at ``quantiles``, each slot read from its forecast's own
    levels (``Quantiles``: linear between them in their values sorted, a forecaster's
    quantiles can cross; extended beyond the outermost on a side that has one, else the
    median), never below 0; a slot without a forecast at 0. Solcast's p10/p50/p90 give
    their three traces; any other set of levels its own."""
    traces = []
    for q in quantiles:
        trace = []
        for slot in slots:
            if slot is None:
                trace.append(0.0)
                continue
            value = _interpolate(tuple(slot.kw), tuple(sorted(slot.kw.values())), q)
            trace.append(max(value, 0.0))
        traces.append(trace)
    return traces


def _cover_runs(
    p_import: Sequence[float], needs: Sequence[bool], day: int
) -> list[tuple[int, int]]:
    """For each slot, the next run ``[start, end)`` of expensive hours (empty if none).

    Expensive hours: PV no longer covers the house (``needs``) and the import
    price is above the cheapest of the ``day`` slots ahead. On a time-of-use plan
    that is part-peak and peak after the sun; a sunny morning never counts. No
    tiers are assumed: it is read from TariffKit's curve.
    """
    count = len(p_import)
    runs = []
    for t in range(count):
        cheapest = min(p_import[t : t + day])

        def expensive(slot: int, cheapest: float = cheapest) -> bool:
            return needs[slot] and p_import[slot] > cheapest + _TOLERANCE

        start = t
        while start < count and not expensive(start):
            start += 1
        end = start
        while end < count and expensive(end):
            end += 1
        runs.append((start, end))
    return runs


class Event(StrEnum):
    """What priced a slot's kWh (module docstring, the rule)."""

    #: PV spilling in the slot itself.
    SPILL = "spill"
    #: The house importing in the slot itself.
    IMPORT = "import"
    #: Later: the battery fills and spills.
    FILL = "fill"
    #: Later: the plant tops the battery up from the grid.
    TOPUP = "topup"
    #: Later: the battery runs empty and the house imports.
    EMPTY = "empty"
    #: Later: a battery-export run ends at its floor sooner, a kWh less exported.
    EXPORT = "export"
    #: Load no source can serve, now or where the battery empties at the import limit,
    #: priced at the slot's import as a floor.
    UNSERVED = "unserved"
    #: None of them within the flows: the day's import price.
    HORIZON = "horizon"


class Served(StrEnum):
    """What a kWh used in a slot takes first."""

    #: PV spilling now: exported or curtailed.
    SPILL = "spill"
    #: PV charging the battery now: a kWh diverted leaves it charged a kWh less.
    PV = "pv"
    #: The battery, discharging or able to.
    BATTERY = "battery"
    #: The grid: the house importing now, the plant topping the battery up, or the
    #: battery with nothing left to give and no PV charging it.
    GRID = "grid"


@dataclass(frozen=True, slots=True)
class FlowPrices:
    """The rule over one sequence of slot flows, per slot."""

    #: Per kWh used in the slot: what serves it (``served``), at its cost.
    prices: tuple[float, ...]
    served: tuple[Served, ...]
    #: Per kWh the battery gives up in the slot: what it buys it back at (the
    #: next fill's export, top-up's or empty's import, or the day's import), before
    #: losses and wear; with what (``make_up_event``).
    make_up: tuple[float, ...]
    make_up_event: tuple[Event, ...]
    #: Per stored kWh the battery gives up in the slot (module docstring):
    #: ``Battery.refill_value`` of the make-up after a fill or a top-up,
    #: ``Battery.displaced_value`` after an empty battery or the horizon.
    stored_value: tuple[float, ...]
    #: The kWh costs only spilled PV, now or when the battery fills.
    spilled: tuple[bool, ...]
    #: The expensive hours ahead are served without an import.
    covered: tuple[bool, ...]
    #: What priced it, and in which slot (the flows' length for the horizon).
    events: tuple[Event, ...]
    event_at: tuple[int, ...]
    #: The first slot of the run of spilling, importing or topping up that the
    #: event belongs to: slots that share it rest on one outcome (a fill and the
    #: afternoon's spill are one).
    outcome_at: tuple[int, ...]
    #: The first slot of the expensive run ``covered`` is about (the flows' length: none).
    cover_at: tuple[int, ...]
    #: The price was settled by an event within the flows, not by the horizon's end.
    resolved: tuple[bool, ...]
    #: ``covered`` was settled within the flows: the run ahead ended in them, or
    #: an import already broke it.
    covered_resolved: tuple[bool, ...]
    #: Per kWh of PV diverted from charging the battery, and of the grid's charge
    #: diverted at the import limit (the supply stack's ``pv`` and ``grid`` steps):
    #: the dead band's charge side on the make-up rule; on the stored cost, PV at the
    #: export, but both at the make-up where the grid buys the kWh back in the slot.
    #: Empty without a battery.
    pv_prices: tuple[float, ...] = ()
    grid_diverted: tuple[float, ...] = ()


def _next(flags: Sequence[bool]) -> list[int]:
    """For each slot, the first slot at or after it where ``flags`` holds (or len)."""
    found = [len(flags)] * (len(flags) + 1)
    for index in reversed(range(len(flags))):
        found[index] = index if flags[index] else found[index + 1]
    return found


def _run_starts(flags: Sequence[bool]) -> list[int]:
    """For each slot, the first slot of the run of ``flags`` it is in (itself if none)."""
    starts: list[int] = []
    for index, flag in enumerate(flags):
        starts.append(starts[-1] if flag and index and flags[index - 1] else index)
    return starts


def _spill_value(flow: SlotFlow, p_export: float) -> float:
    """Per kWh of the PV a slot spills: its export price, 0 where curtailed. Where an
    export limit curtails PV, a kWh more takes the curtailed PV first, worth nothing."""
    if flow.pv_curtail > MODEL.spill_kw:
        return 0.0
    spilled = flow.pv_export + flow.pv_curtail
    return flow.pv_export * p_export / spilled


def importing(storage: Storage | None, flow: SlotFlow, noise: Noise = MODEL) -> bool:
    """The house imports in the slot: the battery could not, or would not, serve it.

    Whether the battery could carry the import is the plant model's answer
    (``storage.can_serve``). The test is on power, so a slot part way through is
    judged as a whole one would be. Without a battery (``storage``
    None), any import above the floor is the house's: there is no regulation.
    """
    if flow.grid_to_load <= noise.short_kw:
        return False
    if storage is None:
        return True
    regulating = (
        flow.grid_to_load <= noise.regulation_kw
        and _soc_end(flow) > storage.reserve_soc + noise.soc
        and storage.can_serve(flow, flow.grid_to_load)
    )
    return not regulating


def _at_limit(flow: SlotFlow, import_limit_kw: float | None, noise: Noise) -> bool:
    """The slot imports at the grid's limit: a kWh more cannot be imported."""
    if import_limit_kw is None:
        return False
    return flow.grid_to_load + flow.grid_to_battery >= import_limit_kw - noise.short_kw


def _soc_end(flow: SlotFlow) -> float:
    """``flow``'s SOC at its end: a plant with storage projects one."""
    soc = flow.soc_end
    if soc is None:
        raise ValueError("a plant with storage projects its SOC")
    return soc


def spilling(flow: SlotFlow, noise: Noise = MODEL) -> bool:
    """PV spills in the slot, net of what the battery and the grid supplied in it.

    The model never exports while it discharges or imports, so for its flows the
    net is the gross; on the meters a slot can hold both (sensor timing).
    """
    spare = flow.pv_export + flow.pv_curtail - flow.battery_to_load - flow.grid_to_load
    return spare > noise.spill_kw


def price_flows(
    storage: Storage | None,
    flows: Sequence[SlotFlow],
    p_import: Sequence[float],
    p_export: Sequence[float],
    day: int,
    *,
    noise: Noise = MODEL,
    import_limit_kw: float | None = None,
    stored_cost: Sequence[float | None] | None = None,
) -> FlowPrices:
    """The rule (module docstring) over ``flows``: a scenario's, or what was metered.

    ``stored_cost``: what a stored kWh is worth in each slot, the running average cost of
    what the battery holds then (``ledger.project_average``), per stored kWh; where given
    (and not None) it is the value the battery's dead band is drawn from, in place of
    the make-up's. The events ahead still say what displaces the kWh and how sure.

    ``day`` is the slots in a day. Without a battery (``storage`` None), see
    :func:`_price_without_storage`. At the grid's ``import_limit_kw`` a kWh more cannot
    be imported: diverted PV serves it, else the grid's charge diverted, else the battery
    beyond its policy, else it is unserved (docs/methodology/make-up-rule.md, *Limits and
    unusual plants*).
    """
    count = len(flows)
    if not len(p_import) == len(p_export) == count:
        raise ValueError("one price pair per flow")
    if not flows:
        return FlowPrices((), (), (), (), (), (), (), (), (), (), (), (), ())
    at_limit = [_at_limit(flow, import_limit_kw, noise) for flow in flows]
    if storage is None:
        return _price_without_storage(flows, p_import, p_export, day, noise, at_limit)
    full = storage.saturation_soc - noise.soc
    floor = storage.reserve_soc + noise.soc
    last_day = min(p_import[max(0, count - day) :])
    spills = [spilling(flow, noise) for flow in flows]
    imports = [not at_limit[i] and importing(storage, flow, noise) for i, flow in enumerate(flows)]
    unserved = [flow.unserved_reachable > noise.short_kw for flow in flows]
    topping = [flow.grid_to_battery > noise.topup_kw for flow in flows]

    # Where the plant would buy a kWh back from the grid. The model says so: in its
    # window, short of its course to the target or on it (``topup_gap``), a kWh taken
    # earlier is bought back where the grid can still charge more and no PV is spare
    # (PV charges first: spare PV refills it, a fill), and PV diverted in the slot
    # itself frees the charge limit for the grid. On the meters: where the plant
    # charged from the grid.
    def buys_later(index: int, flow: SlotFlow) -> bool:
        if flow.dispatch.topup_gap is None:
            return topping[index]
        return flow.dispatch.topup_gap > -_TOLERANCE and storage.can_charge_more(
            flow, ChargeSource.GRID, margin_kw=_TOLERANCE
        )

    def buys_now(index: int, flow: SlotFlow) -> bool:
        if flow.dispatch.topup_gap is None:
            return topping[index]
        return flow.dispatch.topup_gap > -_TOLERANCE

    buying = [buys_later(i, flow) for i, flow in enumerate(flows)]
    diverting = [buys_now(i, flow) for i, flow in enumerate(flows)]
    # A kWh taken earlier shows up where the battery, full, spills (a kWh less
    # exported), where the plant tops it up from the grid (a kWh more bought),
    # or where it runs empty (a kWh more imported).
    # PV clipped where a battery on the clipping hybrid's DC bus would take it fills it
    # too, first and at no cost: nothing else could use it.
    clipping = [
        flow.dispatch.bus_clipped > noise.spill_kw and storage.takes_clipped_pv(flow)
        for flow in flows
    ]
    refilling = [spills[i] or clipping[i] for i in range(count)]
    fills = _next([refilling[i] and _soc_end(flow) >= full for i, flow in enumerate(flows)])
    topups = _next(buying)
    empties = _next(
        [(imports[i] or unserved[i]) and _soc_end(flow) <= floor for i, flow in enumerate(flows)]
    )
    # ``covered`` asks whether the grid imports at all: at the limit it does.
    imported = _next([imports[i] or at_limit[i] for i in range(count)])
    runs = _cover_runs(
        p_import,
        [
            flow.battery_to_load + flow.battery_to_dc_load + flow.grid_to_load > noise.need_kw
            for flow in flows
        ],
        day,
    )

    # Where a battery-export run reaches its floor: a kWh less ends it sooner.
    ends = _next([flow.dispatch.run_end is not None for flow in flows])

    def make_up_at(t: int, origin: int | None = None) -> tuple[float, Event, int]:
        """What the battery buys a kWh given up in slot ``t`` back at, and how. An
        empty's cheapest import runs from ``origin`` (``t`` unless the kWh was given up
        earlier and carried past an export run's end)."""
        start = t if origin is None else origin
        # PV charges before the plant decides its top-up, so PV diverted in a slot
        # whose top-up binds is bought back in that slot itself.
        refilled = (
            diverting[t]
            and storage.divertible_kw(flows[t]) > noise.charge_kw
            and storage.grid_refills_diverted_pv(flows[t])
        )
        topup = t if refilled else topups[t + 1]
        if ends[t + 1] < count and ends[t + 1] <= min(fills[t + 1], topup, empties[t + 1]):
            # The export run ends first; at its floor at the reserve it is also an
            # empty, which its value already holds (the house's import).
            at = ends[t + 1]
            return p_export[at], Event.EXPORT, at
        if fills[t + 1] < min(topup, empties[t + 1]):
            at = fills[t + 1]
            price = 0.0 if clipping[at] else _spill_value(flows[at], p_export[at])
            return price, Event.FILL, at
        if topup < empties[t + 1]:  # the plant buys it back in its window
            return p_import[topup], Event.TOPUP, topup
        if empties[t + 1] < count:  # bought back in the cheapest hour before it is needed
            at = empties[t + 1]
            if unserved[at]:  # at the import limit: more load unserved, the floor
                return p_import[at], Event.UNSERVED, at
            return min(p_import[start : at + 1]), Event.EMPTY, at
        # Past the horizon: at the day's import price.
        return min(min(p_import[start:]), last_day), Event.HORIZON, count

    def stored_at(later: float, event: Event, at: int, origin: int) -> float:
        """Per stored kWh given up in slot ``origin`` and bought back by ``event``
        in slot ``at`` at ``later`` (module docstring)."""
        # Each at the margin of the power the inverter already converts there.
        there = flows[min(at, count - 1)]
        if event is Event.FILL:  # spare PV refills it
            return storage.refill_value(later, _fill_source(storage, there, noise), there)
        if event is Event.TOPUP:  # the grid refills it
            return storage.refill_value(later, ChargeSource.GRID, there)
        if event in (Event.EMPTY, Event.UNSERVED):  # it would have served the house instead
            return storage.displaced_value(later, there)
        if event is Event.EXPORT and there.dispatch.run_end is not None:
            # The run ends sooner: export lost, the battery's wear saved, the house's
            # import, freed PV's charge wear, and what is still short priced by the
            # next event.
            end = there.dispatch.run_end
            after, after_event, after_at = make_up_at(at, origin)
            carried = stored_at(after, after_event, after_at, origin) if end.carried else 0.0
            # Freed DC PV wears at its path's rate; asked only where some is charged.
            dc_wear = storage.charge_wear_rate(ChargeSource.DC_PV) if end.refilled_dc else 0.0
            return (
                end.export_lost * p_export[at]
                + end.imported * min(p_import[origin : at + 1])
                - end.not_delivered * storage.wear_discharge_per_kwh
                + end.refilled * storage.charge_wear_rate(ChargeSource.PV)
                + end.refilled_dc * dc_wear
                + end.carried * carried
            )
        # Past the horizon, at a power no one knows: the margin at low power.
        return storage.displaced_value(later)

    prices: list[float] = []
    served: list[Served] = []
    make_up: list[float] = []
    make_up_event: list[Event] = []
    stored_value: list[float] = []
    pv_prices: list[float] = []
    grid_diverted: list[float] = []
    events: list[Event] = []
    event_at: list[int] = []
    covered = []
    for t, flow in enumerate(flows):
        start, end = runs[t]
        covered.append(imported[max(t, start)] >= end)
        later, later_event, later_at = make_up_at(t)
        make_up.append(later)
        make_up_event.append(later_event)
        value = stored_at(later, later_event, later_at, t)
        held_cost = None if stored_cost is None else stored_cost[t]
        if held_cost is not None:
            # The stored energy's own cost; but where solar refills the room a draw frees
            # before the battery would need the grid, the draw costs no more than that
            # solar: kept, the kWh would cost the room for it.
            value = min(held_cost, value) if later_event is Event.FILL else held_cost
        stored_value.append(value)
        pv_price, battery_price = storage.dead_band(value, flow)
        # A kWh the grid buys back in this very slot (a binding top-up): certain, not
        # forecast, so it costs that purchase, through the charge's band, on either rule.
        grid_now = pv_price
        if held_cost is not None:
            grid_now = storage.dead_band(stored_at(later, later_event, later_at, t), flow)[0]
            if not (later_event is Event.TOPUP and later_at == t):
                # Solar a load takes before it is stored costs what solar costs: the
                # export it gives up now.
                pv_price = p_export[t]
            else:
                pv_price = grid_now
        pv_prices.append(pv_price)
        grid_diverted.append(grid_now)
        # PV charging the battery that a load could divert: on the AC bus as it is, on a
        # DC bus through the hybrid (``divertible_kw``; on this site ``pv_to_battery``).
        charging = storage.divertible_kw(flow) > noise.charge_kw
        if spills[t]:  # PV spills now: the kWh takes it
            prices.append(_spill_value(flow, p_export[t]))
            served.append(Served.SPILL)
            events.append(Event.SPILL)
            event_at.append(t)
        elif at_limit[t]:
            # A kWh more cannot be imported: diverted PV, the grid's charge diverted (at
            # the band's charge side), the battery beyond its policy, else unserved at
            # the import price as a floor.
            battery_can = (
                not storage.at_discharge_limit(flow, margin_kw=noise.need_kw)
                and _soc_end(flow) > floor
            )
            if charging or topping[t] or battery_can:
                prices.append(pv_price if charging else grid_now if topping[t] else battery_price)
                served.append(
                    Served.PV if charging else Served.GRID if topping[t] else Served.BATTERY
                )
                events.append(later_event)
                event_at.append(later_at)
            else:
                prices.append(p_import[t])
                served.append(Served.GRID)
                events.append(Event.UNSERVED)
                event_at.append(t)
        elif imports[t] or (
            not charging
            and (
                topping[t]
                or flow.dispatch.held
                or _soc_end(flow) <= floor
                or storage.at_discharge_limit(flow, margin_kw=noise.need_kw)
            )
        ):
            # The house imports now; or, with no PV charging to divert, the plant holds
            # the battery (topping it up, or at its target) or it has nothing left to
            # give (empty, or at its discharge limit): the grid. Where PV charges, a kWh
            # diverts it, and whatever buys that back prices it (the top-up in the
            # same slot where it binds).
            prices.append(p_import[t])
            served.append(Served.GRID)
            events.append(Event.IMPORT)
            event_at.append(t)
        else:
            # The battery gives the kWh: charged a kWh less from PV, or discharged a
            # kWh more, at its dead band for what a stored kWh is worth
            # (docs/methodology/stored-value.md, *The dead band*).
            if charging:
                prices.append(pv_price)
                served.append(Served.PV)
            else:
                prices.append(battery_price)
                served.append(Served.BATTERY)
            events.append(later_event)
            event_at.append(later_at)
    run_of = {
        Event.SPILL: _run_starts(spills),
        Event.FILL: _run_starts(refilling),
        Event.IMPORT: _run_starts(imports),
        Event.EMPTY: _run_starts(imports),
        Event.TOPUP: _run_starts(topping),
        Event.EXPORT: _run_starts([flow.dispatch.run_end is not None for flow in flows]),
        Event.UNSERVED: _run_starts([a or u for a, u in zip(at_limit, unserved, strict=True)]),
    }
    return FlowPrices(
        prices=tuple(prices),
        served=tuple(served),
        make_up=tuple(make_up),
        make_up_event=tuple(make_up_event),
        stored_value=tuple(stored_value),
        spilled=tuple(event in (Event.SPILL, Event.FILL) for event in events),
        covered=tuple(covered),
        events=tuple(events),
        event_at=tuple(event_at),
        outcome_at=tuple(
            count if event is Event.HORIZON else run_of[event][at]
            for event, at in zip(events, event_at, strict=True)
        ),
        cover_at=tuple(start for start, _ in runs),
        resolved=tuple(event is not Event.HORIZON for event in events),
        # The expensive run is read against the cheapest import of the day ahead,
        # so it is settled only with that day in the flows, and once the run ended
        # in them or an import inside it broke it.
        covered_resolved=tuple(
            t + day <= count and (end < count or imported[max(t, start)] < end)
            for t, (start, end) in enumerate(runs)
        ),
        pv_prices=tuple(pv_prices),
        grid_diverted=tuple(grid_diverted),
    )


def _fill_source(storage: Storage, flow: SlotFlow, noise: Noise) -> ChargeSource:
    """Which PV refills the battery in a fill slot: PV on its DC bus where it is there
    (clipped, or crossing to the AC side), else PV on the AC bus."""
    on_bus = storage.takes_clipped_pv(flow)
    if on_bus and (flow.dispatch.bus_clipped > noise.spill_kw or flow.dc_pv_to_ac > noise.spill_kw):
        return ChargeSource.DC_PV
    return ChargeSource.PV


def _price_without_storage(
    flows: Sequence[SlotFlow],
    p_import: Sequence[float],
    p_export: Sequence[float],
    day: int,
    noise: Noise,
    at_limit: Sequence[bool],
) -> FlowPrices:
    """The rule for a plant without a battery: nothing stores a kWh, so there is no
    make-up, no fill, top-up or empty. A kWh is PV spilling now, at the export it
    displaces (0 where curtailed), or else the grid's, at the import price, whether the
    house imports now or the kWh tips it into importing. Without PV either, every kWh is
    the import price: the floor case.

    ``make_up`` and ``make_up_event`` are then the slot's own price and event, and a
    stored kWh is worth nothing (``stored_value`` 0). ``covered`` keeps its meaning:
    the expensive hours ahead are served without an import, which without a battery
    holds only where there are none.
    """
    count = len(flows)
    spills = [spilling(flow, noise) for flow in flows]
    imports = [importing(None, flow, noise) for flow in flows]
    imported = _next(imports)
    # At the import limit a kWh more is unserved: the import price, as a floor.
    runs = _cover_runs(
        p_import,
        [
            flow.battery_to_load + flow.battery_to_dc_load + flow.grid_to_load > noise.need_kw
            for flow in flows
        ],
        day,
    )
    prices: list[float] = []
    served: list[Served] = []
    events: list[Event] = []
    covered: list[bool] = []
    for t, flow in enumerate(flows):
        start, end = runs[t]
        covered.append(imported[max(t, start)] >= end)
        if spills[t]:
            prices.append(_spill_value(flow, p_export[t]))
            served.append(Served.SPILL)
            events.append(Event.SPILL)
        else:
            prices.append(p_import[t])
            served.append(Served.GRID)
            events.append(Event.UNSERVED if at_limit[t] else Event.IMPORT)
    run_of = {
        Event.SPILL: _run_starts(spills),
        Event.IMPORT: _run_starts(imports),
        Event.UNSERVED: _run_starts(at_limit),
    }
    return FlowPrices(
        prices=tuple(prices),
        served=tuple(served),
        make_up=tuple(prices),
        make_up_event=tuple(events),
        stored_value=(0.0,) * count,
        spilled=tuple(event is Event.SPILL for event in events),
        covered=tuple(covered),
        events=tuple(events),
        event_at=tuple(range(count)),
        outcome_at=tuple(run_of[event][t] for t, event in enumerate(events)),
        cover_at=tuple(start for start, _ in runs),
        resolved=(True,) * count,
        covered_resolved=tuple(
            t + day <= count and (end < count or imported[max(t, start)] < end)
            for t, (start, end) in enumerate(runs)
        ),
    )


def value_energy(
    plant: PlantModel,
    starts: Sequence[datetime],
    scenarios: Sequence[PvKw],
    load_kw: Sequence[float],
    soc: float | None,
    p_import: Sequence[float],
    p_export: Sequence[float],
    *,
    step: timedelta,
    first_remaining: timedelta | None = None,
    durations: Sequence[timedelta] | None = None,
    weights: Sequence[float] | None = None,
    pessimistic: PvKw | None = None,
    pessimistic_flows: Sequence[SlotFlow] | None = None,
    dc_load_kw: Sequence[float] | None = None,
    held_at_start: bool | None = None,
    lots: Lots | None = None,
    battery_value: BatteryValueRule = "stored_cost",
) -> EnergyValue:
    """The price of one more kWh of flexible load in each slot (module docstring).

    ``scenarios`` are PV forecasts (kW per slot), equally likely unless ``weights`` says
    otherwise; ``pessimistic`` is the PV trace ``price_p10`` is priced on (else it is
    the expectation), and ``pessimistic_flows`` its run where the caller has projected
    it already, on these same inputs (the surplus forecast's p10 case). Every per-slot
    sequence covers ``starts``. ``soc`` (0-1) is the SOC now, part way through the first
    slot when ``first_remaining`` is given; ``durations`` gives every slot its own
    length instead (the live value's), with ``step`` still the slot a day is counted in.
    ``soc`` is None for a plant without a battery, which then has no values by battery.
    ``load_kw`` is the AC side's, where a flexible load's kWh lands; ``dc_load_kw`` the
    DC-bus loads', which it passes to the plant as they are (None: none draws), and
    ``held_at_start`` whether a hold rule's session under way now holds the battery
    (``PlantModel.project``).
    """
    count = len(starts)
    weights = weights if weights is not None else [1 / len(scenarios)] * len(scenarios)
    if len(scenarios) != len(weights) or not math.isclose(sum(weights), 1.0):
        raise ValueError("one weight per scenario, summing to 1")
    traces = [*scenarios, *([pessimistic] if pessimistic is not None else [])]
    if (
        any(pv_slots(pv) != count for pv in traces)
        or not (len(load_kw) == len(p_import) == len(p_export) == count)
        or (dc_load_kw is not None and len(dc_load_kw) != count)
    ):
        raise ValueError("per-slot inputs must have the same length")
    if count == 0:
        return EnergyValue(price=(), price_p10=(), certainty_spill=(), certainty_covered=())
    storage = plant.storage
    day = max(1, round(timedelta(days=1) / step))
    if durations is not None:
        spans = list(durations)
    else:
        spans = [step] * count
        if first_remaining is not None:
            spans[0] = first_remaining
    if len(spans) != count:
        raise ValueError("durations must have one length per slot")
    load = list(load_kw)
    # The plant refuses a SOC outside 0-1; a reading a little out is clamped here.
    start_soc = None if soc is None else min(max(soc, 0.0), 1.0)
    dc_load = None if dc_load_kw is None else tuple(dc_load_kw)
    if pessimistic_flows is not None and (pessimistic is None or len(pessimistic_flows) != count):
        raise ValueError("pessimistic_flows is the pessimistic trace's run, one flow per slot")
    runs = [
        plant.project(
            starts,
            spans,
            pv_slice(pv, 0),
            load,
            start_soc,
            dc_load_kw=dc_load,
            held_at_start=held_at_start,
        )
        for pv in (scenarios if pessimistic_flows is not None else traces)
    ]
    if pessimistic_flows is not None:
        runs.append(tuple(pessimistic_flows))
    limit = plant.import_limit_kw
    hours_each = [span / timedelta(hours=1) for span in spans]

    def stored_cost_of(flows: Sequence[SlotFlow]) -> list[float | None] | None:
        # The running average cost of what the battery holds, run forward along this
        # scenario by the plant model; none under ``replacement``.
        if battery_value != "stored_cost" or storage is None:
            return None
        return project_average(storage, flows, hours_each, p_import, p_export, lots=lots)

    results = [
        price_flows(
            storage,
            flows,
            p_import,
            p_export,
            day,
            import_limit_kw=limit,
            stored_cost=stored_cost_of(flows),
        )
        for flows in runs
    ]
    count_scenarios = len(scenarios)
    hours = [span / timedelta(hours=1) for span in spans]
    pv_parts: list[list[tuple[float, float, PvRouting]]] = [[] for _ in range(count)]
    battery_parts: list[list[tuple[float, float, Displaces]]] = [[] for _ in range(count)]
    expected = [0.0] * count
    stored = [0.0] * count
    spill: list[list[float]] = [[] for _ in range(count)]
    cover: list[list[float]] = [[] for _ in range(count)]
    unserved: list[list[float]] = [[] for _ in range(count)]
    for result, flows, weight in zip(
        results[:count_scenarios], runs[:count_scenarios], weights, strict=True
    ):
        for index, flow in enumerate(flows):
            expected[index] += weight * result.prices[index]
            stored[index] += weight * result.stored_value[index]
            if result.spilled[index]:
                spill[index].append(weight)
            if result.covered[index]:
                cover[index].append(weight)
            if flow.unserved_reachable > MODEL.short_kw:
                unserved[index].append(weight)
            served = result.served[index]
            if served is Served.SPILL:
                routing = (
                    PvRouting.CURTAIL if flow.pv_curtail > flow.pv_export else PvRouting.EXPORT
                )
                pv_parts[index].append((weight, result.prices[index], routing))
            elif served is Served.PV:
                pv_parts[index].append((weight, result.prices[index], PvRouting.BATTERY))
            if (
                storage is not None
                and (served is not Served.GRID or _at_limit(flow, limit, MODEL))
                and storage.headroom(flow, hours[index]) > MODEL.spill_kw
            ):
                # The battery has a kWh to give: at the top of its dead band.
                displaces = (
                    Displaces.EXPORT
                    if result.make_up_event[index] in (Event.FILL, Event.EXPORT)
                    else Displaces.LATER_IMPORT
                )
                value = storage.dead_band(result.stored_value[index], flow)[1]
                battery_parts[index].append((weight, value, displaces))

    def probability(parts: list[float]) -> float:
        # Summed exactly: ten weights of 0.1 must make 1.0.
        return min(max(round(math.fsum(parts), 9), 0.0), 1.0)

    pv_values: list[PvValue | None] = []
    for pv_part in pv_parts:
        found = _by_weight(pv_part, list(PvRouting))
        pv_values.append(None if found is None else PvValue(*found))
    battery_values: list[BatteryValue | None] = []
    for battery_part in battery_parts:
        held = _by_weight(battery_part, list(Displaces))
        battery_values.append(None if held is None else BatteryValue(*held))
    return EnergyValue(
        price=tuple(expected),
        price_p10=tuple(results[-1].prices) if pessimistic is not None else tuple(expected),
        certainty_spill=tuple(probability(parts) for parts in spill),
        certainty_covered=tuple(probability(parts) for parts in cover),
        unserved=tuple(probability(parts) for parts in unserved),
        pv_value=tuple(pv_values),
        battery_value=tuple(battery_values),
        stored_value=tuple(stored),
        model=Model(
            plant=plant,
            starts=tuple(starts),
            durations=tuple(spans),
            scenarios=tuple(pv_frozen(pv) for pv in scenarios),
            weights=tuple(weights),
            load_kw=tuple(load),
            p_import=tuple(p_import),
            p_export=tuple(p_export),
            flows=tuple(runs[:count_scenarios]),
            priced=tuple(results[:count_scenarios]),
            dc_load_kw=dc_load,
            lots=lots,
            battery_value=battery_value,
        ),
    )


def _by_weight[R](
    parts: Sequence[tuple[float, float, R]], order: Sequence[R]
) -> tuple[float, R, float] | None:
    """The weighted mean of ``parts`` (weight, per kWh, reason), the reason with the
    most weight (a tie: the first in ``order``), and the weight; None without any."""
    total = math.fsum(weight for weight, _, _ in parts)
    if total <= 0:
        return None
    reasons = dict.fromkeys(order, 0.0)
    for weight, _, reason in parts:
        reasons[reason] += weight
    return (
        math.fsum(weight * value for weight, value, _ in parts) / total,
        max(order, key=lambda reason: reasons[reason]),
        min(max(round(total, 9), 0.0), 1.0),
    )
