"""The plant model behind a port: what the price model may ask of a plant.

The price model needs to know how the plant will behave (its flows over the
horizon, and how they change with more load) and a few things about its battery. It
asks them here; topology, the inverter's conversion, the dispatch and the meters are a
plant model's. The library's
``microgrid.plant.Plant`` is one, for any topology ``plant/2`` describes; a
client's own model is another (``microgrid.engine.extend``, checked by
``microgrid.engine.testing``).

**The predicates take a margin** and do the comparison themselves, so every plant
model compares in the same floating-point form and the outputs agree to the last bit.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Protocol

from microgrid.plant.flows import Flows, MeterReading, PvKw, SlotFlow

__all__ = ["ChargeSource", "PlantModel", "Storage"]


class ChargeSource(StrEnum):
    """Where a kWh that charges the battery comes from, and by which path. Not
    ``Source``: the ledger names its lot sources that (``ledger.Source``)."""

    #: PV on the AC bus (``SlotFlow.pv_to_battery``).
    PV = "pv"
    #: The grid (``SlotFlow.grid_to_battery``).
    GRID = "grid"
    #: PV on the hybrid's DC bus (``SlotFlow.dc_pv_to_battery``). A plant without that
    #: path raises ``ValueError`` when asked about it.
    DC_PV = "dc_pv"


class Storage(Protocol):
    """The battery as the price rule asks about it. Every question names the slot's
    flows and the energy's path, so a topology answers for its own conversion.

    Every answer that reads the battery's discharge counts all of its output
    (``SlotFlow.battery_out``: to the AC side, to DC-bus loads and exported)."""

    @property
    def capacity_kwh(self) -> float:
        """What it stores, kWh."""
        ...

    @property
    def reserve_soc(self) -> float:
        """The SOC (0-1) the plant keeps in reserve: empty, to the price rule."""
        ...

    @property
    def saturation_soc(self) -> float:
        """The SOC (0-1) at which it counts as full."""
        ...

    @property
    def wear_discharge_per_kwh(self) -> float:
        """Wear per stored kWh delivered, in the prices' currency."""
        ...

    def charge_wear_rate(self, source: ChargeSource) -> float:
        """Wear per kWh charged from ``source``, in that source's terms
        (``pv_to_battery``, ``grid_to_battery`` or ``dc_pv_to_battery``): the paths
        convert differently, so a source kWh can wear the battery differently."""
        ...

    def divertible_kw(self, flow: SlotFlow) -> float:
        """AC kW a load could take from the PV now charging the battery, in what the
        load would receive."""
        ...

    def can_charge_more(
        self, flow: SlotFlow, source: ChargeSource, *, margin_kw: float = 0.0
    ) -> bool:
        """Whether this slot could store more from ``source``, short of its limit by
        more than ``margin_kw`` (the top-up test). For the grid it includes the plant's
        own order of charging (``Plant``'s, PV first: the grid charges more only
        where no more than ``margin_kw`` of PV is spare, since spare PV would refill
        the battery first)."""
        ...

    def at_discharge_limit(self, flow: SlotFlow, *, margin_kw: float = 0.0) -> bool:
        """Whether the battery delivers all it can, within ``margin_kw``."""
        ...

    def can_serve(self, flow: SlotFlow, more_kw: float) -> bool:
        """Whether the battery could also deliver ``more_kw`` in this slot."""
        ...

    def grid_refills_diverted_pv(self, flow: SlotFlow) -> bool:
        """Whether a top-up makes up PV a load diverted from charging (where PV charges
        before the grid, as ``Plant``'s dispatch has it, yes)."""
        ...

    def takes_clipped_pv(self, flow: SlotFlow) -> bool:
        """Whether the battery, with room, would take PV this slot clips: true on the
        clipping hybrid's DC bus, false on its own inverter beside it, which the
        clipped PV cannot reach."""
        ...

    def headroom(self, flow: SlotFlow, hours: float) -> float:
        """kW more the battery could deliver in this slot, ``hours`` long."""
        ...

    def refill_value(self, price: float, source: ChargeSource, flow: SlotFlow) -> float:
        """The cost per stored kWh, refilled from ``source`` at ``price`` in this slot
        (that path's conversion at that power, and wear)."""
        ...

    def displaced_value(self, price: float, flow: SlotFlow | None = None) -> float:
        """The worth per stored kWh, at ``price`` once delivered (conversion, wear); at
        ``flow``'s power, or at low power where no one knows it."""
        ...

    def dead_band(self, stored_value: float, flow: SlotFlow) -> tuple[float, float]:
        """The prices a stored kWh worth ``stored_value`` is not worth moving between,
        at this slot's powers: charging pays below the first, discharging above the
        second."""
        ...

    def stored_per_kwh(self, source: ChargeSource, flow: SlotFlow | None = None) -> float:
        """Stored kWh per kWh charged from ``source`` (in that source's terms, as
        :meth:`charge_wear_rate`'s), by that source's own path: averaged over the slot's
        charge where ``flow`` is given (the loss grows with power), else for a charge at
        the battery's own limit (a kWh bought to fill it). What the stored cost books a
        charge at (``project_average``): a topology answers for its paths, as a
        split battery on a hybrid's DC bus converts the grid's AC through the hybrid and
        then its own port, and DC PV through its port alone. A plant without the DC PV
        path raises ``ValueError`` when asked about it."""
        ...


class PlantModel(Protocol):
    """How the plant will behave: the price model's only view of it."""

    def project(
        self,
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
        """The plant's flows slot by slot from ``soc`` (0-1), as it would dispatch
        them, given PV (its source's terms) and the loads it serves by connection point:
        ``load_kw`` on the AC side, and ``dc_load_kw`` on the hybrid's DC bus, DC kW at
        the loads (None: none draws). A load series that holds a DC-bus load's draw is
        all AC, as before. A hold rule's session under way at the first slot holds the
        battery as ``held_at_start`` says (None: as the SOC there decides;
        ``SlotDispatch.hold_session`` reports it slot by slot). It is one state: with
        several rules, every session open at the first slot takes it, exact where one
        rule holds (plant/2 allows several, and a plant whose rules overlap carries the
        approximation). It validates only what it receives: every value is known,
        because the caller builds ``durations`` and ends its inputs at the first unknown
        slot, and a projection shorter than the horizon is the ``plant_model`` cause
        (R-7). A what-if is the same call with a different load. ``stop``
        ends the projection early (a what-if, once the battery agrees again): the price
        model no longer asks for it, but it is kept for the port's
        stability and for the tests' re-simulation oracle (``engine_fixtures.rerun_cost``).

        A plant without a battery takes ``soc`` None and projects SOCs of None; one
        without PV takes PV of 0. PV is one series for the site, or one per array
        named as :attr:`pv_arrays` (``PvKw``)."""
        ...

    @property
    def storage(self) -> Storage | None:
        """The battery as the price rule sees it; None without one."""
        ...

    @property
    def has_pv(self) -> bool:
        """Whether the plant has PV: without it, no PV forecast is asked for (its
        ``pv_forecast`` and ``pv_power`` are not causes) and its PV is 0."""
        ...

    @property
    def pv_arrays(self) -> tuple[str, ...]:
        """Its PV arrays' names: what PV per array is keyed by."""
        ...

    @property
    def pv_per_array(self) -> bool:
        """Whether it needs PV per array: its arrays have both couplings, so no one
        series can say what each makes."""
        ...

    @property
    def import_limit_kw(self) -> float | None:
        """The grid's import limit, kW AC at the meter; None where none binds. A slot
        at it cannot import a kWh more."""
        ...

    @property
    def has_dc_loads(self) -> bool:
        """Whether a load connects to the hybrid's DC bus: then its forecast and live
        draw are inputs (``dc_load_kw``)."""
        ...

    @property
    def meters(self) -> Mapping[str, str]:
        """Its meters: each name a ``MeterReading`` keys, to what it measures (``pv``,
        ``grid``, ``battery``, ``consumption`` or ``dc_load``)."""
        ...

    def pv_from_meters(self, reading: MeterReading, *, per_array: bool) -> PvKw | None:
        """The PV ``reading`` meters for one slot, as :meth:`project` takes it: per
        array where ``per_array``, else the site's. None where a PV meter is unknown or
        the meters cannot say it (one total for arrays valued apart)."""
        ...

    def read_meters(self, reading: MeterReading) -> Flows | None:
        """The plant's meters split between sources and sinks, all of it, battery
        export included: what the battery ledger books lots from. Each meter in its
        configured sign; a name the plant does not meter raises, and a meter unknown
        (missing, or None) leaves the split unknown: None."""
        ...

    def metered_flow(
        self, start: datetime, soc: float | None, reading: MeterReading
    ) -> SlotFlow | None:
        """The meters now (``start`` UTC), read as a slot's flows for the price rule
        (the live value; built from :meth:`read_meters`); None where a meter is
        unknown. The price model then asks ``importing(storage, slot, noise)`` with the
        configured floors (``EngineConfig.observed``), so the noise thresholds stay on
        its side."""
        ...
