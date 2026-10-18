"""The surplus forecast: what PV the plan leaves, by case, through the plant model.

PV the site cannot use (:mod:`microgrid.plant.flows`'s surplus), and the
regime of each slot, from the p50 and the pessimistic (p10) PV cases, each
projected by the plant model behind the port: the price model reaches the plant only
through the port.

Pure: no I/O. Times are UTC.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from microgrid._checks import require_utc
from microgrid.engine._timeline import SLOT
from microgrid.plant.flows import PvKw, Regime, SlotFlow, regimes, surplus
from microgrid.plant.port import PlantModel

#: PV that may be unknown from a slot on: the site's, or each array's (``PvKw``).
type PvMaybe = Sequence[float | None] | Mapping[str, Sequence[float | None]]


@dataclass(frozen=True, slots=True)
class SurplusForecast:
    """Surplus and regime per slot, from the p50 and pessimistic (p10) PV cases.

    Every per-slot tuple is None from the first slot whose inputs were unknown.
    """

    starts: tuple[datetime, ...]
    #: PV the plan exports or curtails: additive, the energy there is to share.
    surplus_kw: tuple[float | None, ...]
    surplus_kw_p10: tuple[float | None, ...]
    regime: tuple[Regime | None, ...]
    regime_p10: tuple[Regime | None, ...]
    #: Per kWh of the surplus (``Surplus.value``).
    surplus_value: tuple[float | None, ...]
    surplus_value_p10: tuple[float | None, ...]
    #: The p50 case's run, and the p10 case's: the valuation prices the p10 case on the
    #: same run rather than projecting it again.
    flows: tuple[SlotFlow, ...]
    flows_p10: tuple[SlotFlow, ...] = ()


def forecast_surplus(
    plant: PlantModel,
    starts: Sequence[datetime],
    pv_p50: PvMaybe,
    pv_p10: PvMaybe,
    load_kw: Sequence[float | None],
    soc: float | None,
    *,
    step: timedelta = SLOT,
    first_remaining: timedelta | None = None,
    p_export: Sequence[float | None] | None = None,
    dc_load_kw: Sequence[float | None] | None = None,
    held_at_start: bool | None = None,
) -> SurplusForecast:
    """Project both PV cases through ``plant`` and derive surplus, regime and surplus
    value. A projection ends at the first slot whose PV or load is unknown; every
    later slot is None (the ``plant_model`` cause, R-7). ``soc`` is None
    for a plant without a battery. ``dc_load_kw`` is the DC-bus loads' (None: none
    draws), unknown where a slot's is None."""
    durations = [step] * len(starts)
    if first_remaining is not None and durations:
        durations[0] = first_remaining
    cases = []
    for pv in (pv_p50, pv_p10):
        # The checks, case by case and in this order: the lengths, the SOC, the slot
        # under way, then every start up to and including the first unknown slot.
        if not len(starts) == _slots(pv) == len(load_kw) or (
            dc_load_kw is not None and len(dc_load_kw) != len(starts)
        ):
            raise ValueError("starts, pv_kw and load_kw must have the same length")
        if soc is not None and not 0 <= soc <= 1:
            raise ValueError("soc must be in [0, 1]")
        if first_remaining is not None and not timedelta(0) < first_remaining <= step:
            raise ValueError("first_remaining must be within one slot")
        known = _known(pv, load_kw)
        if dc_load_kw is not None:
            known = min(known, next((i for i, kw in enumerate(dc_load_kw) if kw is None), known))
        require_utc(*starts[: known + 1])
        flows = plant.project(
            starts[:known],
            durations[:known],
            _known_pv(pv, known),
            [kw for kw in load_kw[:known] if kw is not None],
            soc,
            dc_load_kw=(
                None if dc_load_kw is None else [kw for kw in dc_load_kw[:known] if kw is not None]
            ),
            held_at_start=held_at_start,
        )
        cases.append((flows, surplus(flows, p_export[: len(flows)] if p_export else None)))
    (flows, expected), (flows_p10, pessimistic) = cases
    count = len(starts)

    storage = plant.storage
    divertible = storage.divertible_kw if storage is not None else None

    def padded[T](values: Sequence[T]) -> tuple[T | None, ...]:
        return (*values, *([None] * (count - len(values))))

    return SurplusForecast(
        starts=tuple(starts),
        surplus_kw=padded(expected.kw),
        # The pessimistic case never exceeds the expected one (R-6).
        surplus_kw_p10=padded(
            [min(low, high) for low, high in zip(pessimistic.kw, expected.kw, strict=False)]
        ),
        regime=padded(regimes(flows, expected, plant.import_limit_kw, divertible)),
        regime_p10=padded(regimes(flows_p10, pessimistic, plant.import_limit_kw, divertible)),
        surplus_value=padded(expected.value),
        surplus_value_p10=padded(pessimistic.value),
        flows=flows,
        flows_p10=flows_p10,
    )


def _known(pv_kw: PvMaybe, load_kw: Sequence[float | None]) -> int:
    """How many slots from the first have PV (every array's) and load known."""
    series = list(pv_kw.values()) if isinstance(pv_kw, Mapping) else [pv_kw]
    for index, load in enumerate(load_kw):
        if load is None or any(s[index] is None for s in series):
            return index
    return len(load_kw)


def _slots(pv_kw: PvMaybe) -> int:
    """How many slots ``pv_kw`` covers (every array the same)."""
    if isinstance(pv_kw, Mapping):
        lengths = {len(s) for s in pv_kw.values()}
        if len(lengths) > 1:
            raise ValueError("every array's PV must cover the same slots")
        return lengths.pop() if lengths else 0
    return len(pv_kw)


def _known_pv(pv_kw: PvMaybe, known: int) -> PvKw:
    """The first ``known`` slots of ``pv_kw``, every value known there."""
    if isinstance(pv_kw, Mapping):
        return {name: [kw for kw in s[:known] if kw is not None] for name, s in pv_kw.items()}
    return [kw for kw in pv_kw[:known] if kw is not None]
