"""The plant model behind the port: one plant, built from its configuration.

:class:`Plant` takes a :class:`PlantConfig` and answers the port's questions
(``port.PlantModel``) for the plant it describes. It replaced ``AcCoupledPlant``,
which wrapped this site's plant by hand.

**One dispatch:** every plant with a battery or a DC bus runs the general dispatch
(``_paths.project_paths``), its battery answers by path (``_paths.PathStorage``) and its
meters split by ``_meters.split_paths``. The simpler configurations (AC-coupled PV, or
none, and a battery on its own inverter or folded on a hybrid's DC bus with nothing else
there) once ran their own arithmetic, storage and meter split; the tests keep them as
their oracles (``tests/legacy_dispatch.py``).

**A plant without a battery** has a dispatch of its own, :func:`_without_battery`: PV
serves the house, what is left is exported (curtailed above an export limit), and
the grid serves the rest. It has no storage and its SOCs are None: no stand-in
battery of zero capacity.

**A plant with a DC bus** runs the general dispatch (``_paths.project_paths``):
DC-coupled PV, a battery folded or split on a hybrid's DC bus with anything else there, a
hybrid's AC rating on the net crossing it, a hybrid beside a battery on its own inverter,
and clipping. Its battery answers by path (``_paths.PathStorage``).

**DC-bus loads** run there too: DC PV serves them on their bus, then the battery there,
and the AC side through the hybrid within its rating.

Every configuration ``plant/2`` accepts runs as the plant it describes.

Pure: no I/O.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from types import MappingProxyType
from typing import Literal

from microgrid._checks import require_utc
from microgrid.plant._battery import Battery
from microgrid.plant._config import Inverter, PlantConfig, PlantConfigError
from microgrid.plant._meters import Metered, flows_of, split_paths
from microgrid.plant._paths import Paths, PathStorage, _spans, paths_of, project_paths
from microgrid.plant.flows import Flows, MeterReading, PvKw, SlotDispatch, SlotFlow


def _without_battery(
    config: PlantConfig,
    starts: Sequence[datetime],
    durations: Sequence[timedelta],
    pv_kw: Sequence[float],
    load_kw: Sequence[float],
    stop: Callable[[int, SlotFlow], bool] | None,
) -> tuple[SlotFlow, ...]:
    """A plant without a battery, slot by slot: PV serves the house, what is left is
    exported, curtailed above the export limit, and the grid serves the rest. Nothing is
    stored, so no slot depends on another and every SOC is None. It runs a plant with no
    hybrid and no array's own rating (a rated array caps in the general dispatch), so no
    inverter of the plant's draws: an AC array's own inverters are in its forecast."""
    if not len(starts) == len(pv_kw) == len(load_kw):
        raise ValueError("starts, pv_kw and load_kw must have the same length")
    _spans(len(starts), durations)  # the lengths, as ``project_paths`` checks them
    limit = config.grid.export_limit_kw
    flows: list[SlotFlow] = []
    for index, start in enumerate(starts):
        require_utc(start)
        pv, load = pv_kw[index], load_kw[index]
        pv_to_load = min(pv, load)
        spare = pv - pv_to_load
        pv_export = spare if limit is None else min(spare, limit)
        flow = SlotFlow(
            start=start,
            soc_start=None,
            soc_end=None,
            pv_to_load=pv_to_load,
            pv_export=pv_export,
            pv_curtail=spare - pv_export,
            grid_to_load=load - pv_to_load,
        )
        flows.append(flow)
        if stop is not None and stop(index, flow):
            break
    return tuple(flows)


@dataclass(frozen=True, slots=True)
class Plant:
    """The plant ``config`` describes, behind the port (``port.PlantModel``).

    It runs PV of either coupling or both, per array or, where every array has one
    coupling, as one series shared by their DC ratings; or none; a battery on its own
    inverter or on a hybrid's DC bus, folded or split, or none; a hybrid with DC PV,
    with or without a battery of its own or beside one on its own inverter; loads on the
    AC bus and the hybrid's DC bus; any grid-charge and battery-export windows, PV
    charging first or the target first; and the grid's limits. Today's configurations
    the general dispatch (``_paths``), and a plant without a battery or hybrid
    :func:`_without_battery`. Two plants are equal when their
    configurations are (the time zone compared as :class:`~zoneinfo.ZoneInfo`
    compares: by identity, so the client passes one zone object, as
    ``PlantConfig`` asks).
    """

    config: PlantConfig
    #: The general dispatch, where it runs the plant (``_paths``).
    _paths: Paths | None = field(init=False, repr=False, compare=False)
    _storage: PathStorage | None = field(init=False, repr=False, compare=False)
    # What PV it takes, worked out once: ``project`` checks it on every call.
    _arrays: tuple[str, ...] = field(init=False, repr=False, compare=False)
    _per_array: bool = field(init=False, repr=False, compare=False)
    _meters: Mapping[str, str] = field(init=False, repr=False, compare=False)
    _shareable: bool = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        given: object = self.config  # typed, but a client may pass something else
        if not isinstance(given, PlantConfig):
            raise TypeError(
                f"Plant takes a PlantConfig, not {type(given).__name__} "
                "(AcCoupledPlant(NativePlant) became Plant(PlantConfig))"
            )
        config = self.config
        rated = any(a.dc_rating_kw is not None or a.ac_rating_kw is not None for a in config.pv)
        general = (
            config.battery is not None
            or config.hybrid is not None
            or rated
            or config.grid.import_limit_kw is not None
        )
        paths = paths_of(config) if general else None
        storage = None if paths is None or paths.port is None else PathStorage(paths, paths.port)
        object.__setattr__(self, "_paths", paths)
        object.__setattr__(self, "_storage", storage)
        arrays = config.pv
        capped = any(a.dc_rating_kw is not None or a.ac_rating_kw is not None for a in arrays)
        object.__setattr__(self, "_arrays", tuple(array.name for array in arrays))
        object.__setattr__(self, "_per_array", len({a.coupling for a in arrays}) > 1)
        object.__setattr__(
            self,
            "_meters",
            MappingProxyType({meter.name: meter.measures for meter in config.meter_set}),
        )
        object.__setattr__(
            self,
            "_shareable",
            len(arrays) <= 1 or not capped or all(a.dc_rating_kw is not None for a in arrays),
        )

    @property
    def storage(self) -> PathStorage | None:
        return self._storage

    @property
    def has_pv(self) -> bool:
        return bool(self.config.pv)

    @property
    def pv_arrays(self) -> tuple[str, ...]:
        return self._arrays

    @property
    def pv_per_array(self) -> bool:
        return self._per_array

    @property
    def pv_one_series(self) -> bool:
        """Whether the plant takes one PV series for all its arrays: every array has one
        coupling and, where any is capped, each has its DC rating (``_pv``'s rule)."""
        return not self._per_array and self._shareable

    @property
    def import_limit_kw(self) -> float | None:
        return self.config.grid.import_limit_kw

    @property
    def has_dc_loads(self) -> bool:
        return any(load.connection == "dc_bus" for load in self.config.loads)

    def _pv(self, pv_kw: PvKw) -> PvKw:
        """``pv_kw`` checked against the arrays:
        per array, named as they are; or one series, which several arrays share by
        their DC ratings, so only where every array has one coupling and, where any is
        capped, each has its DC rating."""
        if isinstance(pv_kw, Mapping):
            if set(pv_kw) != set(self._arrays):
                raise ValueError(f"PV per array must name exactly {list(self._arrays)}")
            return pv_kw
        if self._per_array:
            raise ValueError("a plant with AC and DC arrays takes PV per array")
        if not self._shareable:
            raise ValueError(
                "several arrays share one PV series by their DC ratings: give each one, "
                "or PV per array"
            )
        return pv_kw

    @staticmethod
    def _total(pv_kw: PvKw) -> Sequence[float]:
        """One series: the site's, or the arrays' summed (where nothing caps them)."""
        if isinstance(pv_kw, Mapping):
            return [math.fsum(kws) for kws in zip(*pv_kw.values(), strict=True)]
        return pv_kw

    @property
    def battery(self) -> Battery:
        """The battery's conversion and limits: what the service's battery ledger books
        lots in, and the issue's battery block publishes. A topology of its own would
        answer these its own way. Raises :class:`PlantConfigError` for a plant without a battery."""
        part = self.config.battery
        if part is None:
            raise PlantConfigError(["battery: the plant has none"])
        return part.battery

    @property
    def inverter(self) -> Inverter:
        """The inverter the battery converts through, whose draws its work adds
        (``PlantConfig.battery_inverter``). Raises :class:`PlantConfigError` for a
        plant without a battery."""
        inverter = self.config.battery_inverter
        if inverter is None:
            raise PlantConfigError(["battery: the plant has none"])
        return inverter

    def own_draw_kw(self, flow: Flows) -> float:
        """The inverter's own draw in a metered slot, kW: its standby, and its overhead
        while the battery charges or discharges over its working threshold
        (``Inverter.working_kw``, ``working_threshold_kw``). Metered consumption
        holds it; the house meter and the plant model's load do not. With a hybrid
        beside a battery's own inverter, both standbys. 0 without an inverter
        (:func:`_without_battery`)."""
        moving = (
            flow.pv_to_battery
            + flow.grid_to_battery
            + flow.battery_to_load
            + flow.battery_export
            + flow.dc_pv_to_battery
            + flow.battery_to_dc_load
        )
        paths = self._paths
        if paths is not None:
            working = paths.working_kw if moving > paths.working_threshold_kw else 0.0
            return paths.standby_kw + working
        return 0.0

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
        if dc_load_kw is not None and not self.has_dc_loads and any(kw != 0 for kw in dc_load_kw):
            raise ValueError("the plant has no DC-bus load: its DC-bus load is 0")
        if not self.config.pv:
            if isinstance(pv_kw, Mapping) or any(kw != 0 for kw in pv_kw):
                raise ValueError("the plant has no PV: its PV is 0")
        else:
            pv_kw = self._pv(pv_kw)
        if self.config.battery is None and soc is not None:
            raise ValueError("the plant has no battery: its SOC is None")
        if self.config.battery is not None and soc is None:
            raise ValueError("the plant has a battery: it needs the SOC")
        paths = self._paths
        if paths is not None:
            return project_paths(
                paths,
                starts,
                durations,
                pv_kw,
                load_kw,
                soc,
                dc_load_kw=dc_load_kw,
                held_at_start=held_at_start,
                stop=stop,
            )
        return _without_battery(self.config, starts, durations, self._total(pv_kw), load_kw, stop)

    @property
    def meters(self) -> Mapping[str, str]:
        return self._meters

    def _metered(self, reading: MeterReading) -> Metered | None:
        """``reading`` by what each meter measures, in the plant's convention; None
        where a configured meter is unknown. A name the plant does not meter raises."""
        meters = self.config.meter_set
        stray = set(reading) - {meter.name for meter in meters}
        if stray:
            raise ValueError(f"not the plant's meters: {sorted(stray)}")
        couplings = {array.name: array.coupling for array in self.config.pv}
        one = next(iter(couplings.values()), "ac")
        pv_ac = pv_dc = battery = dc_load = 0.0
        grid: float | None = None
        consumption: float | None = None
        at: Literal["dc", "ac"] = "dc"
        pv_ac_read: list[float] = []
        for meter in meters:
            kw = reading.get(meter.name)
            if kw is None:
                return None
            if meter.sign != 1.0:
                kw = -kw
            if meter.measures == "pv":
                coupling = couplings[meter.array] if meter.array is not None else one
                if coupling == "ac":
                    pv_ac_read.append(kw)
                else:
                    pv_dc += kw
            elif meter.measures == "grid":
                grid = kw
            elif meter.measures == "battery":
                battery, at = kw, meter.at or "dc"
            elif meter.measures == "consumption":
                consumption = kw
            else:
                dc_load = kw
        # One AC meter's value as it is (today's), several summed.
        pv_ac = pv_ac_read[0] if len(pv_ac_read) == 1 else math.fsum(pv_ac_read)
        return Metered(pv_ac, pv_dc, grid, battery, at, consumption, dc_load)

    def pv_from_meters(self, reading: MeterReading, *, per_array: bool) -> PvKw | None:
        """The PV ``reading`` meters, for one slot: per array, keyed by name, where
        ``per_array`` and each array has its meter; else the site's (one coupling's)
        total. None where a PV meter is unknown, or the meters cannot say it."""
        pv_meters = [m for m in self.config.meter_set if m.measures == "pv"]
        values: dict[str | None, float] = {}
        for meter in pv_meters:
            kw = reading.get(meter.name)
            if kw is None:
                return None
            values[meter.array] = -kw if meter.sign != 1.0 else kw
        if per_array:
            if None in values or set(values) != set(self._arrays):
                return None
            return {name: [kw] for name, kw in values.items() if name is not None}
        if self._per_array:
            return None  # both couplings: no one total
        kws = list(values.values())
        return [kws[0] if len(kws) == 1 else math.fsum(kws)]

    def read_meters(self, reading: MeterReading) -> Flows | None:
        # ``_live.importing_now``'s split, from the configured meters.
        metered = self._metered(reading)
        if metered is None:
            return None
        if self._paths is not None:
            return flows_of(split_paths(self._paths, metered))
        # No battery and no hybrid: PV serves the house, what is left is exported, and
        # the grid serves the rest. Consumption metered in the grid's place gives the
        # grid by the balance (one of the two is read).
        pv = max(metered.pv_ac, 0.0)
        grid_kw = metered.grid
        if grid_kw is None:
            grid_kw = max(metered.consumption or 0.0, 0.0) - pv
        consumed = max(pv + grid_kw, 0.0)
        pv_export = min(pv, max(-grid_kw, 0.0))
        pv_to_load = min(pv - pv_export, consumed)
        return Flows(
            consumed=consumed,
            pv_to_load=pv_to_load,
            battery_to_load=0.0,
            grid_to_load=max(consumed - pv_to_load, 0.0),
            pv_to_battery=0.0,
            grid_to_battery=0.0,
            pv_export=pv_export,
            battery_export=0.0,
        )

    def metered_slot(
        self, start: datetime, soc_start: float, soc_end: float, flows: Flows
    ) -> SlotFlow:
        """A metered slot's mean flows (``ledger.integrate``) as the plant model's slot,
        between the metered SOCs: its sources and sinks on the AC side, no battery
        export (as :meth:`metered_flow`), the losses and draws at 0. The battery's port
        flows and the hybrid's crossing are worked out from the mean AC charge and
        discharge, as a projection of them would be, each way on its own: a slot of
        means can both charge and discharge. The battery's DC-bus paths (DC PV into it,
        it serving DC-bus loads) add to its port flows as they are, DC, and do not cross
        the hybrid. The DC PV the hybrid converts is not in the metered
        flows, so the crossing is the battery's AC alone."""
        require_utc(start)
        slot = SlotFlow(
            start=start,
            soc_start=soc_start,
            soc_end=soc_end,
            pv_to_load=flows.pv_to_load,
            pv_to_battery=flows.pv_to_battery,
            pv_export=flows.pv_export,
            battery_to_load=flows.battery_to_load,
            grid_to_load=flows.grid_to_load,
            grid_to_battery=flows.grid_to_battery,
            dc_pv_to_battery=flows.dc_pv_to_battery,
            battery_to_dc_load=flows.battery_to_dc_load,
        )
        paths = self._paths
        if paths is None or paths.port is None:
            return slot
        # The AC the battery takes and gives: what crosses to it, on the DC bus.
        charge, out = slot.pv_to_battery + slot.grid_to_battery, slot.battery_to_load
        curve = paths.curve
        if paths.on_bus and curve is not None:
            # Through the hybrid from the AC side, from a crossing of 0.
            base = curve.ac(0.0)
            port_in = -curve.dc(base - charge) if charge > 0 else 0.0
            port_out = curve.dc(base + out) if out > 0 else 0.0
            crossing = port_out - port_in
        else:
            port_in, port_out, crossing = charge, out, 0.0  # its port is on the AC bus
        port_in += slot.dc_pv_to_battery
        port_out += slot.battery_to_dc_load
        return replace(
            slot,
            dispatch=SlotDispatch(
                crossing=crossing,
                battery_port=port_in - port_out,
                battery_port_in=port_in,
                battery_port_out=port_out,
            ),
        )

    def metered_flow(
        self, start: datetime, soc: float | None, reading: MeterReading
    ) -> SlotFlow | None:
        # ``_live.importing_now``'s slot; the core holds UTC only. The
        # battery export, the losses and the draws stay 0 (``SlotFlow``'s fields).
        require_utc(start)
        if (soc is None) != (self.config.battery is None):
            raise ValueError("the SOC is None exactly when the plant has no battery")
        paths = self._paths
        if paths is not None:
            metered = self._metered(reading)
            if metered is None:
                return None
            split = split_paths(paths, metered)
            return replace(split, start=start, soc_start=soc, soc_end=soc, battery_export=0.0)
        flows = self.read_meters(reading)
        if flows is None:
            return None
        return SlotFlow(
            start=start,
            soc_start=soc,
            soc_end=soc,
            pv_to_load=flows.pv_to_load,
            pv_export=flows.pv_export,
            grid_to_load=flows.grid_to_load,
        )
