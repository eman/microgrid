"""The engine's contract: typed data in, the price curve and surplus out.

A client builds an :class:`EngineConfig` once (the plant and how the price is
computed) and hands :meth:`Engine.value` an :class:`Inputs` and a moment per
valuation: the prices, the PV and load forecasts, each a series of its issues, and the
SOC and whether the plant runs the dispatch its model describes, each a series of
values. The engine reads them as they stood at the moment: what was
received by then, nothing after. It fetches nothing and sends nothing; it returns a
:class:`Valuation`, which the client publishes as it likes. :class:`LiveSession` runs
a valuation again from measured readings, between valuations.

**Fallback is the engine's** (tariffkit#67 R-14). What the
client cannot vouch for (a silent telemetry path, an ``unavailable`` state) is a
sample without a value, or a series vouched for (``known_until``) only up to when the
path went quiet. An SOC outside 0-1 by more than ``EngineConfig.soc_tolerance`` is a
mistake and cannot be used either. The engine judges each input's age and the load
forecast's coverage of the horizon, and whether the plant model covered every slot.
Any input that cannot be used makes the valuation ``fallback_import``: no surplus,
every slot at the import price, the causes (:class:`Cause`) named in ``degraded``. A
stale discount is never reused. Without a price for the slot under way the valuation
is ``unpriced``, with no slots. A gap in the load forecast that cuts
the horizon short is ``horizon_cut``, not a cause. A client's mistake raises.

**The inputs follow the plant.** A plant without PV takes no PV forecast and no PV
reading (no PV meter): ``pv_forecast`` and ``pv_power`` are not inputs of it, and its PV
is 0. A plant with AC and DC arrays takes its forecast per array; a valuation per array
takes no measured PV total until PV is read per meter, so its live value is the
``pv_power`` fallback. A plant without a battery takes no SOC, work mode or battery
power: ``battery_soc`` and ``work_mode`` are not inputs of it, and the live import test
reads the grid meter alone. Passing one is a mistake (``ValueError``). An input the
plant does not have is never a cause.

Units: kW, kWh, money in the prices' currency, UTC-aware datetimes, SOC 0-1; slots of
``EngineConfig.slot`` (10 minutes by default).

Pure: no I/O.
"""

from __future__ import annotations

from bisect import bisect_right
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, fields
from datetime import datetime, timedelta
from types import MappingProxyType
from typing import Final

from microgrid._checks import require_utc
from microgrid.engine import _engine_config
from microgrid.engine._assess import Assessment, Cause, InputState, ValuationKind, assess
from microgrid.engine._inputs import Inputs, issued, reading_at
from microgrid.engine._live import importing_now, revalue, smoothed, window
from microgrid.engine._stored import Lots, project_average
from microgrid.engine._supply import Step, supply_stack
from microgrid.engine._surplus import PvMaybe, forecast_surplus
from microgrid.engine._timeline import SLOT, horizon_slots, slot_floor
from microgrid.engine._value import (
    OBSERVED,
    BatteryValue,
    Model,
    Noise,
    Pricing,
    PvValue,
    Served,
    price_flows,
    quantile_traces,
    value_energy,
)
from microgrid.engine.series import (
    Composed,
    Forecast,
    Mean,
    MisalignedError,
    Quantiles,
    Sample,
    Series,
    _align,
    _compose,
    price_mean,
    quantiles_mean,
)
from microgrid.plant.flows import PvKw, Regime
from microgrid.plant.port import PlantModel

#: The API contract's version: the public names of every tier and what they mean. Its
#: own name, apart from the configuration's schema (``engine/2``, :data:`ENGINE_SCHEMA`):
#: its number goes up with every change a client must follow (a name removed or renamed,
#: a signature or a meaning changed), and an addition leaves it. ``engine-api/2``: names
#: removed, and the live value, the lots and the port changed; the API and the
#: configuration had shared one name.
#: ``engine-api/3``: the plant model left the engine for its own package,
#: ``microgrid.plant`` (``microgrid.engine.plant`` and the port's and flows' modules moved).
#: ``engine-api/4``: no name says a currency (``per_kwh`` for ``usd_per_kwh``, the wear's
#: and the ledger's floor's names), as money is in the prices' own.
ENGINE_CONTRACT: Final = "engine-api/4"

#: The outputs' schema: what :meth:`Valuation.to_mapping` and
#: :meth:`LiveValue.to_mapping` give, shipped as ``schemas/valuation-2.schema.json``.
#: ``valuation/2``: ``per_kwh`` for ``valuation/1``'s ``usd_per_kwh``.
VALUATION_SCHEMA: Final = "valuation/2"

#: The engine's model: the price rule and the plant model it runs, as
#: every :class:`Valuation` names them. A client that publishes its own version keeps
#: it beside this one.
MODEL_VERSION: Final = "pricing/1.3"

#: The default age past which a price forecast cannot be used (``EngineConfig.
#: price_max_age``: without a limit, a price issue however old would be reused, against
#: tariffkit#67 R-14). A day: the longest a price source the library reads goes
#: between issues (a day-ahead market publishes once a day), and two hours' grace for a
#: publication or a fetch running late. A client that fetches more often sets less (an
#: hourly fetch, 60 minutes).
PRICE_MAX_AGE: Final = timedelta(hours=26)

#: An SOC this far outside 0-1 is clamped to it (rounding: 100.4 %); beyond it the
#: reading is a mistake (a percent passed as a fraction, a sensor fault) and cannot be
#: used. The reference site's recorded SOC never left 0-100 % (416,034 readings,
#: 2026-01-09 to 2026-10-06), so the limit changes no value it has published.
SOC_TOLERANCE: Final = 0.01

#: A change in house load larger than this must be seen on two readings in a row
#: before it counts: one stray reading must not move the value.
#: Below this, one 30 s reading moves a 300 s average by at most
#: 0.5 kW * (1 - exp(-30/300)) ~ 48 W.
LOAD_JUMP_KW: Final = 0.5


@dataclass(frozen=True, slots=True)
class EngineConfig:
    """What the plant is and how the price is computed, not where data comes from."""

    #: How the plant behaves: a ``Plant`` built from its configuration.
    plant: PlantModel
    pricing: Pricing = field(default_factory=Pricing)
    #: The slot a valuation prices, counted from the epoch in UTC: it divides an hour.
    #: The noise floors (``observed``) are for 10-minute slots.
    slot: timedelta = SLOT
    #: How far a valuation reaches from the slot under way.
    horizon: timedelta = timedelta(hours=48)
    #: A PV forecast older than this cannot be used.
    pv_max_age: timedelta = timedelta(hours=20)
    #: A load forecast issued, or starting, longer ago than this cannot be used
    #: (``series.is_stale``).
    load_max_age: timedelta = timedelta(minutes=90)
    #: A price forecast older than this cannot be used; None, no limit
    #: (:data:`PRICE_MAX_AGE` by default).
    price_max_age: timedelta | None = PRICE_MAX_AGE
    #: The live value's moving-average time constant for measured PV and load (0: off).
    smoothing: timedelta = timedelta(seconds=300)
    #: The meters' noise floors, for 10-minute slots: below them a metered flow or SOC
    #: difference is no event. ``OBSERVED`` by default, measured on one plant's
    #: meters; a plant whose meters report differently sets its own.
    observed: Noise = OBSERVED
    #: An SOC this far outside 0-1 is clamped to it; beyond, it cannot be used
    #: (:data:`SOC_TOLERANCE`).
    soc_tolerance: float = SOC_TOLERANCE
    #: A house-load change larger than this, kW, must be seen on two readings in a row
    #: before the live value takes it (:data:`LOAD_JUMP_KW`).
    load_jump_kw: float = LOAD_JUMP_KW

    def __post_init__(self) -> None:
        if self.slot <= timedelta(0) or timedelta(hours=1) % self.slot:
            raise ValueError(f"the slot must divide an hour: {self.slot}")

    @property
    def smoothing_window(self) -> timedelta:
        """How far back from its moment :meth:`Engine.live` reads the measured series: a
        client keeps its readings back this far, and the one in force then
        (``WINDOW_TAUS`` time constants)."""
        return window(self.smoothing)

    @classmethod
    def from_mapping(cls, data: object, *, plant: PlantModel) -> EngineConfig:
        """The configuration an ``engine/2`` mapping describes, for ``plant``: every
        field optional, today's value its default. Raises
        :class:`EngineConfigError` naming every problem."""
        return cls(plant, **_engine_config.read(data))

    def to_mapping(self) -> dict[str, object]:
        """This configuration as ``engine/2`` (the plant is its own, ``plant/2``)."""
        return _engine_config.write(
            {f.name: getattr(self, f.name) for f in fields(self) if f.name != "plant"}
        )


@dataclass(frozen=True, slots=True)
class SlotValue:
    """One slot of a valuation, before rounding."""

    start: datetime
    p_import: float
    p_export: float
    surplus_kw: float
    surplus_kw_p10: float
    regime: Regime
    marginal_cost: float
    marginal_cost_p10: float
    certainty_spill: float
    certainty_covered: float
    #: Values by source (tariffkit#67 R-2, R-3); None where the source has none.
    pv_value: PvValue | None
    battery_value: BatteryValue | None
    #: The supply stack in the median PV case.
    supply: tuple[Step, ...]
    #: Scenario weight in which the slot has unserved load a stored kWh would serve
    #: (0-1): at an import limit, or a DC-bus load beyond the hybrid's rating with the
    #: battery on its bus at its reserve or limit. Its price is then the import as a
    #: floor. Load beyond any stored kWh's
    #: reach (``SlotDispatch.unserved_unreachable``) is not counted.
    unserved: float = 0.0

    def to_mapping(self) -> dict[str, object]:
        """Plain data (``valuation/2``'s ``SlotValue``): times in ISO 8601 with their
        offset, enums as their values."""
        return {
            "start": self.start.isoformat(),
            "p_import": self.p_import,
            "p_export": self.p_export,
            "surplus_kw": self.surplus_kw,
            "surplus_kw_p10": self.surplus_kw_p10,
            "regime": self.regime.value,
            "marginal_cost": self.marginal_cost,
            "marginal_cost_p10": self.marginal_cost_p10,
            "certainty_spill": self.certainty_spill,
            "certainty_covered": self.certainty_covered,
            "pv_value": None if self.pv_value is None else self.pv_value.to_mapping(),
            "battery_value": (
                None if self.battery_value is None else self.battery_value.to_mapping()
            ),
            "supply": [step.to_mapping() for step in self.supply],
            "unserved": self.unserved,
        }


@dataclass(frozen=True, slots=True)
class Valuation:
    """A horizon valued from ``now``: the price curve and surplus by slot.

    ``unpriced`` (no price for the slot under way) has no slots, and names
    ``prices`` in ``degraded``."""

    #: The price rule and plant model that made it (:data:`MODEL_VERSION`).
    model_version: str
    now: datetime
    kind: ValuationKind
    #: The inputs that could not be used: then ``kind`` is the fallback (or
    #: ``unpriced``, for the prices).
    degraded: tuple[Cause, ...]
    #: Per input: when it was known, and whether it could be used.
    inputs: Mapping[Cause, InputState]
    #: The load forecast's source label, None without one.
    load_forecast: str | None
    slots: tuple[SlotValue, ...]
    #: A gap in the load forecast (a slot unknown with known ones after it) cut the
    #: horizon short of its end: the slots before the gap are valued. Not a cause: the
    #: load forecast was used.
    horizon_cut: bool = False
    #: The scenarios valued: what :class:`LiveSession` runs again. Opaque, and not
    #: part of the contract.
    model: Model | None = field(default=None, repr=False, compare=False)

    @property
    def starts(self) -> tuple[datetime, ...]:
        """Each slot's start."""
        return tuple(slot.start for slot in self.slots)

    @property
    def assessment(self) -> Assessment:
        """The kind and its causes."""
        return Assessment(valuation=self.kind, degraded=self.degraded)

    def to_mapping(self) -> dict[str, object]:
        """Plain data (``valuation/2``, :data:`VALUATION_SCHEMA`): times in ISO 8601
        with their offset, enums as their values; ``model``, opaque, left out."""
        return {
            "model_version": self.model_version,
            "now": self.now.isoformat(),
            "kind": self.kind.value,
            "degraded": [str(cause) for cause in self.degraded],
            "inputs": {
                str(cause): {
                    "as_of": None if state.as_of is None else state.as_of.isoformat(),
                    "usable": state.usable,
                }
                for cause, state in self.inputs.items()
            },
            "load_forecast": self.load_forecast,
            "slots": [slot.to_mapping() for slot in self.slots],
            "horizon_cut": self.horizon_cut,
        }


@dataclass(frozen=True, slots=True)
class LiveReading:
    """Measured now. The engine smooths the meters and the load (:class:`LiveSession`)."""

    at: datetime
    #: House load, as the plant meters it.
    load_kw: float | None
    #: 0-1; None where unknown, and always for a plant without a battery, as is
    #: ``dispatch_as_modelled``.
    soc: float | None
    #: The plant runs the dispatch its model describes (:attr:`Inputs.dispatch_as_modelled`
    #: now). None or False: the ``work_mode`` cause.
    dispatch_as_modelled: bool | None
    #: The client's own causes (a silent telemetry path, a meter it cannot read): any
    #: makes the value the fallback. Kept beside the engine's, in
    #: :attr:`LiveValue.external`, never in place of them.
    external: tuple[str, ...] = ()
    #: The plant's meters, kW keyed by name (``PlantModel.meters``), each in
    #: its configured sign: in place of engine/0's ``pv_kw``, ``grid_kw`` and
    #: ``battery_kw``. A meter missing or None is unknown. Its PV meters give the
    #: measured PV (missing: ``pv_power``); without the grid (or consumption) and battery meters the
    #: house is not known to be importing (not causes). A plant with DC-bus loads reads
    #: their draw from its ``dc_load`` meter (missing: ``load_power``), the one input
    #: for it. A name the plant does not meter raises.
    meters: Mapping[str, float | None] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class LiveValue:
    """The energy context now, before rounding.

    ``kind`` is ``fallback_import`` where a cause, the engine's or the client's, made
    it the import price with no surplus; ``unpriced`` without a price for the slot
    under way (the prices None, no surplus); ``stored_value_v1`` otherwise, the
    warm-up after a start among them (``degraded`` is ``smoothing``)."""

    at: datetime
    kind: ValuationKind
    surplus_kw: float
    regime: Regime
    #: None: ``unpriced``.
    p_import: float | None
    p_export: float | None
    marginal_cost: float | None
    certainty_spill: float
    certainty_covered: float
    #: The engine's causes.
    degraded: tuple[Cause, ...]
    #: The client's own (:attr:`LiveReading.external`), beside the engine's.
    external: tuple[str, ...] = ()
    pv_value: PvValue | None = None
    battery_value: BatteryValue | None = None

    def to_mapping(self) -> dict[str, object]:
        """Plain data (``valuation/2``'s ``LiveValue``): the time in ISO 8601 with its
        offset, enums as their values."""
        return {
            "at": self.at.isoformat(),
            "kind": self.kind.value,
            "surplus_kw": self.surplus_kw,
            "regime": self.regime.value,
            "p_import": self.p_import,
            "p_export": self.p_export,
            "marginal_cost": self.marginal_cost,
            "certainty_spill": self.certainty_spill,
            "certainty_covered": self.certainty_covered,
            "degraded": [str(cause) for cause in self.degraded],
            "external": list(self.external),
            "pv_value": None if self.pv_value is None else self.pv_value.to_mapping(),
            "battery_value": (
                None if self.battery_value is None else self.battery_value.to_mapping()
            ),
        }


def _soc(value: float | None, tolerance: float) -> float | None:
    """``value`` (0-1) clamped within ``tolerance``; None if missing or beyond."""
    if value is None or not -tolerance <= value <= 1 + tolerance:
        return None  # NaN fails the comparison too
    return min(max(value, 0.0), 1.0)


#: PV per slot at its quantiles, None where a slot has none.
type _PvSlots = Sequence[Quantiles | None]


def _p10_known(stepped: _PvSlots | None, by_array: Mapping[str, _PvSlots] | None) -> PvMaybe:
    """The 0.1 quantile per slot, None where a slot has none: the site's, or each
    array's."""
    if by_array is not None:
        return {name: [q.at(0.1) if q else None for q in slots] for name, slots in by_array.items()}
    return [q.at(0.1) if q else None for q in stepped or ()]


def _p10(stepped: _PvSlots | None, by_array: Mapping[str, _PvSlots] | None) -> PvKw:
    """The 0.1 quantile per slot, 0 where a slot has none: the pessimistic case's PV."""
    if by_array is not None:
        return {name: [q.at(0.1) if q else 0.0 for q in slots] for name, slots in by_array.items()}
    return [q.at(0.1) if q else 0.0 for q in stepped or ()]


def _scenarios(
    stepped: _PvSlots | None,
    by_array: Mapping[str, _PvSlots] | None,
    quantiles: Sequence[float],
) -> list[PvKw]:
    """The PV scenarios (``_value.quantile_traces``): each takes every array at the same
    percentile, one sky over the site. Where arrays diverge
    this overstates the spread, in neither direction reliably."""

    def traces(slots: _PvSlots) -> list[list[float]]:
        return quantile_traces(slots, quantiles)

    if by_array is None:
        return list(traces(stepped or ()))
    per_array = {name: traces(slots) for name, slots in by_array.items()}
    return [{name: cut[k] for name, cut in per_array.items()} for k in range(len(quantiles))]


def _per_array(valuation: Valuation) -> bool:
    """Whether ``valuation`` ran PV per array."""
    model = valuation.model
    return model is not None and any(isinstance(pv, Mapping) for pv in model.scenarios)


def _covered_prefix(starts: list[datetime], covered: tuple[object | None, ...]) -> list[datetime]:
    end = next((i for i, value in enumerate(covered) if value is None), len(starts))
    return starts[:end]


#: No age limit (``EngineConfig.price_max_age`` None).
_NO_LIMIT = timedelta.max

#: A plant without PV: one case, at 0.
_NO_PV: Final = Quantiles.point(0.0)


@dataclass(frozen=True, slots=True)
class _Resolved:
    """The inputs as the price model takes them, resolved at the moment and aligned onto
    the slots: the horizon, cut where an input ends; each input on its
    slots; and each input's state."""

    now: datetime
    starts: list[datetime]
    p_import: tuple[float, ...]
    p_export: tuple[float, ...]
    #: Every input's state but ``plant_model``'s, which the model run gives.
    states: dict[Cause, InputState]
    soc: float | None
    #: PV per slot for the site (a plant without PV: 0), or None: unknown or per array.
    stepped: tuple[Quantiles | None, ...] | None
    by_array: dict[str, tuple[Quantiles | None, ...]] | None
    load_kw: tuple[float | None, ...]
    dc_load_kw: tuple[float | None, ...] | None
    #: A gap in the load forecasts cut the horizon short of their end.
    load_gap: bool
    load_source: str | None
    #: The battery's lots in force at the moment (None: none given).
    lots: Lots | None = None


@dataclass(frozen=True, slots=True)
class _Unpriced:
    """No price for the slot under way: the moment, and when the prices read were
    issued (None: none read)."""

    now: datetime
    prices_as_of: datetime | None


def _unpriced(u: _Unpriced) -> Valuation:
    """The ``unpriced`` valuation: no slots, ``prices`` its cause."""
    return Valuation(
        model_version=MODEL_VERSION,
        now=u.now,
        kind=ValuationKind.UNPRICED,
        degraded=(Cause.PRICES,),
        inputs=MappingProxyType({Cause.PRICES: InputState(as_of=u.prices_as_of, usable=False)}),
        load_forecast=None,
        slots=(),
    )


def _load_states(
    starts: list[datetime], given: list[dict[datetime, float | None] | None]
) -> tuple[list[datetime], bool, bool]:
    """Cut ``starts`` at the load forecasts' first unknown slot (``given``: each
    connection point's kW by slot start, None where its forecast is missing): the
    horizon, whether the load is known at the slot under way, and whether a gap (a slot
    unknown with known ones after it) cut it. A published forecast reaches a fixed span
    from when it was made, so an issue made later reaches past it, and a slot without a
    value is never guessed. Unknown at the slot under way, the load is
    the ``load_forecast`` cause (tariffkit#67 R-14)."""
    if any(kw is None for kw in given):
        return starts, False, False
    known = tuple(
        None if any(kw.get(start) is None for kw in given if kw is not None) else start
        for start in starts
    )
    prefix = _covered_prefix(starts, known)
    load_known = bool(prefix)
    # Cut by a gap (a slot unknown with known ones after it: a row missing or
    # unreadable), not by the forecast's end: the shortened issue is valued, and the
    # valuation says so (``horizon_cut``: a client that publishes it names it, in its
    # ``degraded`` for one).
    load_gap = load_known and any(start is not None for start in known[len(prefix) :])
    return prefix or starts, load_known, load_gap


def _last_received[V](issues: list[Forecast[V]]) -> list[Forecast[V]]:
    """The issue received last (``issues`` are in the order received) alone: what a
    client that fetches a source holds, each fetch replacing the one before."""
    return issues[-1:]


def _read_forecast[V](
    issues: list[Forecast[V]],
    starts: list[datetime],
    now: datetime,
    *,
    slot: timedelta,
    max_age: timedelta,
    by_start: bool,
    mean: Mean[V] | None = None,
) -> Composed[V]:
    """``issues`` (received by now) on ``starts``: each slot from the newest fresh issue
    covering it (:func:`series.compose`). A newest issue whose intervals cross the slots
    leaves every slot unknown, its issue time and source kept, so the input's cause is
    named with when it was made; an older one that crosses them is left out, as a slot
    it alone would have covered is then unknown, and named, too."""
    if not issues:
        return Composed((None,) * len(starts), None, None)
    # The newest as ``series.compose`` takes it: by issue time, of two at one time the
    # first received (``max`` keeps the first of equals).
    newest = max(issues, key=lambda forecast: forecast.issued_at)
    aligned: list[Forecast[V]] = []
    for forecast in issues:
        try:
            _align(forecast, starts, slot, mean)
        except MisalignedError:
            if forecast is newest:
                return Composed((None,) * len(starts), newest.issued_at, newest.source)
            continue
        aligned.append(forecast)
    # Received by now is what counts, not the source's stamp (``issued_by_at`` False).
    return _compose(aligned, starts, slot, now, max_age, by_start, False, mean)


def _resolve_inputs(config: EngineConfig, inputs: Inputs, at: datetime) -> _Resolved | _Unpriced:
    """:class:`Inputs` read at ``at``, unless no price covers the slot under way.

    Only what the client had received by ``at`` is read: a forecast stamped a moment
    later by its source's clock still counts.

    - **Prices and PV:** the issue received last, alone: each
      fetch replaces the one before, and filling its gaps from an older one would reuse
      an input it superseded. Prices are judged by their issue's age against
      ``EngineConfig.price_max_age``, if any; PV by its issue's age alone (a PV forecast
      reaches back to the start of its day).
    - **Loads:** each slot from the newest fresh issue that covers it: a source that
      publishes from the next half hour leaves the slot under way to the issue before
      (:func:`series.compose`). Judged by issue time and first interval, against
      ``EngineConfig.load_max_age``, at ``at`` itself; prices and PV are judged at its
      whole second.
    - **Misaligned sources:** prices whose intervals cross the slots price none of
      them, so the valuation is ``unpriced`` (``prices``); PV or load that does is that
      input's cause.
    """
    require_utc(at)
    now = at.replace(microsecond=0)
    plant = config.plant
    has_pv, has_battery = plant.has_pv, plant.storage is not None
    if not has_pv and inputs.pv is not None:
        raise ValueError("the plant has no PV: pass no PV forecast")
    measured = (inputs.soc, inputs.dispatch_as_modelled)
    if not has_battery and any(
        series is not None and any(s.value is not None for s in series.samples)
        for series in measured
    ):
        raise ValueError("the plant has no battery: pass no SOC or dispatch")
    if inputs.prices is None:
        return _Unpriced(now, None)
    slot = config.slot
    horizon = list(horizon_slots(now, config.horizon, slot))
    prices = _read_forecast(
        _last_received(issued(inputs.prices, at)),
        horizon,
        now,
        slot=slot,
        max_age=_NO_LIMIT if config.price_max_age is None else config.price_max_age,
        by_start=False,
        mean=price_mean,
    )
    # An issue whose intervals cross the slots (15-minute prices on 10-minute slots) gives
    # no slot a price, as one too old does: ``unpriced``, ``prices`` its cause.
    starts = _covered_prefix(horizon, prices.values)
    if not starts:
        return _Unpriced(now, prices.issued_at)
    price_at = dict(zip(horizon, prices.values, strict=True))

    def pv_of(series: Series[Forecast[Quantiles]]) -> Composed[Quantiles]:
        received = _last_received(issued(series, at))
        for forecast in received:
            for interval in forecast.intervals:
                if min(interval.value.kw.values()) < 0:
                    raise ValueError(f"negative PV forecast at {interval.start}")
        return _read_forecast(
            received,
            starts,
            now,
            slot=slot,
            max_age=config.pv_max_age,
            by_start=False,
            mean=quantiles_mean,
        )

    pv = inputs.pv
    pv_at: dict[str, dict[datetime, Quantiles | None]] = {}
    if isinstance(pv, Mapping):
        if unknown_arrays := set(pv) - set(plant.pv_arrays):
            raise ValueError(f"the plant has no PV array {sorted(unknown_arrays)}")
        composed = {name: pv_of(series) for name, series in pv.items()}
        pv_usable = set(pv) == set(plant.pv_arrays) and all(
            c.issued_at is not None and now - c.issued_at <= config.pv_max_age
            for c in composed.values()
        )
        if pv_usable:
            covered = [_covered_prefix(starts, c.values) for c in composed.values()]
            pv_usable = all(covered)
            if pv_usable:
                pv_at = {
                    name: dict(zip(starts, c.values, strict=True)) for name, c in composed.items()
                }
                starts = min(covered, key=len)
        pv_as_of = min(
            (c.issued_at for c in composed.values() if c.issued_at is not None), default=None
        )
    else:
        if pv is not None and plant.pv_per_array:
            raise ValueError("a plant with AC and DC arrays takes a PV forecast per array")
        site = pv_of(pv) if pv is not None else None
        pv_as_of = None if site is None else site.issued_at
        pv_usable = pv_as_of is not None and now - pv_as_of <= config.pv_max_age
        if site is not None and pv_usable:
            clipped = _covered_prefix(starts, site.values)
            pv_usable = bool(clipped)
            pv_at = {"": dict(zip(starts, site.values, strict=True))}
            starts = clipped or starts

    if not plant.has_dc_loads and inputs.dc_load is not None:
        raise ValueError("the plant has no DC-bus load: give no DC-bus load forecast")
    soc_read = reading_at(inputs.soc, at)
    soc = _soc(soc_read.value, config.soc_tolerance)
    dispatch = reading_at(inputs.dispatch_as_modelled, at)

    def load_of(series: Series[Forecast[float]] | None) -> Composed[float] | None:
        if series is None:
            return None
        # Judged at the moment itself, not its whole second.
        return _read_forecast(
            issued(series, at), starts, at, slot=slot, max_age=config.load_max_age, by_start=True
        )

    load = load_of(inputs.load)
    dc_load = load_of(inputs.dc_load) if plant.has_dc_loads else None
    load_at = None if load is None else dict(zip(starts, load.values, strict=True))
    dc_at = None if dc_load is None else dict(zip(starts, dc_load.values, strict=True))
    starts, load_known, load_gap = _load_states(
        starts, [load_at, dc_at] if plant.has_dc_loads else [load_at]
    )
    load_as_of = load.issued_at if load else None
    if plant.has_dc_loads:
        dc_as_of = dc_load.issued_at if dc_load else None
        load_as_of = None if load_as_of is None or dc_as_of is None else min(load_as_of, dc_as_of)
    states = {Cause.PRICES: InputState(as_of=prices.issued_at, usable=True)}
    if has_pv:
        states[Cause.PV_FORECAST] = InputState(as_of=pv_as_of, usable=pv_usable)
    if has_battery:
        states[Cause.BATTERY_SOC] = InputState(as_of=soc_read.as_of, usable=soc is not None)
        states[Cause.WORK_MODE] = InputState(as_of=dispatch.as_of, usable=dispatch.value is True)
    states[Cause.LOAD_FORECAST] = InputState(as_of=load_as_of, usable=load_known)

    unknown: tuple[float | None, ...] = (None,) * len(starts)
    by_array: dict[str, tuple[Quantiles | None, ...]] | None = None
    stepped: tuple[Quantiles | None, ...] | None = None
    if isinstance(pv, Mapping):
        if pv_usable:
            by_array = {
                name: tuple(values.get(start) for start in starts) for name, values in pv_at.items()
            }
    elif has_pv:
        if pv is not None and pv_usable:
            stepped = tuple(pv_at[""].get(start) for start in starts)
    else:  # no PV: one case, at 0
        stepped = (_NO_PV,) * len(starts)
    priced = [price_at[start] for start in starts]
    return _Resolved(
        now=now,
        starts=starts,
        p_import=tuple(price.p_import for price in priced if price is not None),
        p_export=tuple(price.p_export for price in priced if price is not None),
        states=states,
        soc=soc,
        stepped=stepped,
        by_array=by_array,
        load_kw=unknown if load_at is None else tuple(load_at.get(s) for s in starts),
        dc_load_kw=(
            None if not plant.has_dc_loads else tuple((dc_at or {}).get(start) for start in starts)
        ),
        load_gap=load_gap,
        load_source=load.source if load is not None else None,
        lots=_lots_at(inputs, at) if has_battery else None,
    )


def _no_spread(config: EngineConfig, r: _Resolved) -> bool:
    """A PV forecast with no quantile below its median in a slot valued: the pessimistic
    case is the median there, and the valuation names ``pv_spread`` (tariffkit#67 R-13)."""
    if not config.plant.has_pv:
        return False
    slots = list(r.stepped or ())
    for by_array in (r.by_array or {}).values():
        slots += by_array
    return any(q is not None and not q.has_lower for q in slots)


def _lots_missing(config: EngineConfig, lots: Lots | None) -> bool:
    """A battery priced at its stored cost without its lots: all it holds is of unknown
    origin, at the import (tariffkit#67 R-14), and the output names ``battery_lots``."""
    return (
        lots is None
        and config.plant.storage is not None
        and config.pricing.battery_value == "stored_cost"
    )


def _lots_at(inputs: Inputs, at: datetime) -> Lots | None:
    """The lots in force at ``at``, if the client gives them and vouches for them then."""
    return reading_at(inputs.lots, at).value


@dataclass(frozen=True, slots=True)
class _Run:
    """What the model run gives, slot by slot."""

    surplus: tuple[float, ...]
    surplus_p10: tuple[float, ...]
    regime: tuple[Regime, ...]
    price: tuple[float, ...]
    price_p10: tuple[float, ...]
    certainty_spill: tuple[float, ...]
    certainty_covered: tuple[float, ...]
    unserved: tuple[float, ...]
    pv_value: tuple[PvValue | None, ...]
    battery_value: tuple[BatteryValue | None, ...]
    supply: tuple[tuple[Step, ...], ...]
    model: Model | None


def _fallback(r: _Resolved) -> _Run:
    """Without the model: no surplus, no source values, every kWh imported
    (tariffkit#67 R-14)."""
    count = len(r.starts)
    zeros = (0.0,) * count
    return _Run(
        surplus=zeros,
        surplus_p10=zeros,
        regime=(Regime.IMPORT,) * count,
        price=r.p_import,
        price_p10=r.p_import,
        certainty_spill=zeros,
        certainty_covered=zeros,
        unserved=zeros,
        pv_value=(None,) * count,
        battery_value=(None,) * count,
        supply=tuple((Step(Served.GRID, None, p),) for p in r.p_import),
        model=None,
    )


def _run_model(config: EngineConfig, r: _Resolved) -> _Run | None:
    """The scenarios projected and priced; None where the plant model did not cover
    every slot (the ``plant_model`` cause)."""
    plant = config.plant
    has_pv = plant.has_pv
    starts, now, slot = r.starts, r.now, config.slot
    stepped, by_array = r.stepped, r.by_array
    unknown: tuple[float | None, ...] = (None,) * len(starts)
    pv_kw: PvMaybe = (
        tuple(q.median if q else None for q in stepped) if stepped is not None else unknown
    )
    if by_array is not None:
        pv_kw = {n: tuple(q.median if q else None for q in st) for n, st in by_array.items()}
    result = forecast_surplus(
        plant,
        starts,
        pv_kw,
        _p10_known(stepped, by_array),
        r.load_kw,
        r.soc,
        step=slot,
        first_remaining=starts[0] + slot - now,
        p_export=r.p_export,
        dc_load_kw=r.dc_load_kw,
    )
    if not all(value is not None for value in result.surplus_kw):
        return None
    # The price: what a kWh costs the site, expected over PV scenarios cut from
    # Solcast's p10/p50/p90.
    quantiles = config.pricing.quantiles if has_pv else (0.5,)
    dc_load_kw = r.dc_load_kw
    energy = value_energy(
        plant,
        starts,
        _scenarios(stepped, by_array, quantiles),
        [kw or 0.0 for kw in r.load_kw],
        r.soc,
        r.p_import,
        r.p_export,
        step=slot,
        first_remaining=starts[0] + slot - now,
        pessimistic=_p10(stepped, by_array),
        # The surplus's p10 run is this one's pessimistic run: the same PV, load, SOC and
        # slots, every one known here.
        pessimistic_flows=result.flows_p10 if has_pv else None,
        dc_load_kw=None if dc_load_kw is None else [kw or 0.0 for kw in dc_load_kw],
        lots=r.lots,
        battery_value=config.pricing.battery_value,
    )
    # The stack: the median case's run (the surplus's), priced by the rule on that run
    # itself, so its quantities and prices agree.
    median = result.flows
    day = round(timedelta(days=1) / slot)
    storage = plant.storage
    limit = plant.import_limit_kw
    hours = [slot / timedelta(hours=1)] * len(starts)
    hours[0] = (starts[0] + slot - now) / timedelta(hours=1)
    stored_cost = (
        project_average(storage, median, hours, r.p_import, r.p_export, lots=r.lots)
        if storage is not None and config.pricing.battery_value == "stored_cost"
        else None
    )
    priced_median = price_flows(
        storage,
        median,
        r.p_import,
        r.p_export,
        day,
        import_limit_kw=limit,
        stored_cost=stored_cost,
    )
    return _Run(
        surplus=tuple(v or 0.0 for v in result.surplus_kw),
        surplus_p10=tuple(v or 0.0 for v in result.surplus_kw_p10),
        regime=tuple(reg or Regime.IMPORT for reg in result.regime),
        price=energy.price,
        price_p10=energy.price_p10,
        certainty_spill=energy.certainty_spill,
        certainty_covered=energy.certainty_covered,
        unserved=energy.unserved,
        pv_value=energy.pv_value,
        battery_value=energy.battery_value,
        supply=tuple(
            supply_stack(
                storage,
                flow,
                hours[i],
                served=priced_median.served[i],
                price=priced_median.prices[i],
                p_import=r.p_import[i],
                stored_value=priced_median.stored_value[i],
                import_limit_kw=limit,
                pv_price=priced_median.pv_prices[i] if priced_median.pv_prices else None,
                grid_diverted=(
                    priced_median.grid_diverted[i] if priced_median.grid_diverted else None
                ),
            )
            for i, flow in enumerate(median)
        ),
        model=energy.model,
    )


def _value_resolved(config: EngineConfig, r: _Resolved) -> Valuation:
    """Assess the inputs, run the model where they allow it, and assemble the valuation."""
    plant = config.plant
    states = dict(r.states)
    run: _Run | None = None
    if (
        assess(states).valuation is ValuationKind.STORED_VALUE_V1
        and (r.stepped or r.by_array)
        and (r.soc is not None or plant.storage is None)
    ):
        run = _run_model(config, r)
        model_ran = run is not None
    else:
        model_ran = True
    states[Cause.PLANT_MODEL] = InputState(as_of=r.now, usable=model_ran)
    assessment = assess(states)
    out = run if run is not None else _fallback(r)
    degraded = tuple(Cause(name) for name in assessment.degraded)
    if run is not None and _lots_missing(config, r.lots):
        degraded = (*degraded, Cause.BATTERY_LOTS)
    if run is not None and _no_spread(config, r):
        degraded = (*degraded, Cause.PV_SPREAD)
    return Valuation(
        model_version=MODEL_VERSION,
        now=r.now,
        kind=assessment.valuation,
        degraded=degraded,
        inputs=MappingProxyType(states),
        load_forecast=r.load_source,
        horizon_cut=r.load_gap,
        slots=tuple(
            SlotValue(
                start=start,
                p_import=r.p_import[i],
                p_export=r.p_export[i],
                surplus_kw=out.surplus[i],
                surplus_kw_p10=out.surplus_p10[i],
                regime=out.regime[i],
                marginal_cost=out.price[i],
                marginal_cost_p10=out.price_p10[i],
                certainty_spill=out.certainty_spill[i],
                certainty_covered=out.certainty_covered[i],
                pv_value=out.pv_value[i],
                battery_value=out.battery_value[i],
                supply=out.supply[i],
                unserved=out.unserved[i],
            )
            for i, start in enumerate(r.starts)
        ),
        model=out.model,
    )


class Engine:
    """The model behind its contract."""

    def __init__(self, config: EngineConfig) -> None:
        self.config = config

    def value(self, inputs: Inputs, /, *, at: datetime) -> Valuation:
        """The horizon from ``at``; ``unpriced``, with no slots, without a price for the
        slot under way.

        Each series is read as it stood at ``at``: what was received by ``at`` itself,
        the load forecasts' ages judged at ``at`` itself. The valuation's moment is
        ``at`` to whole seconds: a client may tag what it publishes with it, and it is
        the ``plant_model`` input's ``as_of``.
        """
        if not isinstance(inputs, Inputs):
            raise TypeError(f"value takes Inputs, not {type(inputs).__name__}")
        resolved = _resolve_inputs(self.config, inputs, at)
        if isinstance(resolved, _Unpriced):
            return _unpriced(resolved)
        return _value_resolved(self.config, resolved)

    def live(
        self,
        inputs: Inputs,
        /,
        *,
        at: datetime,
        valuation: Valuation | None,
        valid_until: datetime | None,
        external: Sequence[str] = (),
    ) -> LiveValue:
        """The energy context at ``at``: ``valuation`` (the latest the client published,
        valid until ``valid_until``: past it, ``issue`` is a cause and its prices still
        serve) run again from the meters, the house load and the SOC as measured
        (``inputs.meters``, ``inputs.measured_load``, ``inputs.soc``,
        ``inputs.dispatch_as_modelled``); ``unpriced`` without a price for the slot under
        way (no valuation, or none that prices it).

        A pure function of the series up to ``at``: one slot from ``at`` at measured PV
        and house load, each averaged over the series (``_live.smoothed``:
        ``EngineConfig.smoothing``, its ``load_jump_kw``), from the measured SOC, then
        the valuation's forecast. Surplus is what that slot would export or curtail.
        When the grid is serving the house now, beyond the plant's regulation, a kWh
        used now is imported too. The averages restart at a gap (tariffkit#67 R-14); the
        warm-up after the client began reading publishes no surplus, one after a gap is
        the fallback. A series need reach back only ``EngineConfig.smoothing_window``
        from ``at``, and the sample in force then.

        ``external`` are the client's own causes (a silent telemetry path, a meter it
        cannot read): any makes the value the fallback. The engine names its own whatever
        the client names, and keeps the client's beside them (``external``).
        ``valid_until`` is required with a valuation: without it a valuation of any age
        would be run again as current (tariffkit#67 R-14).
        """
        if not isinstance(inputs, Inputs):
            raise TypeError(f"live takes Inputs, not {type(inputs).__name__}")
        return _live_value(self.config, inputs, at, valuation, valid_until, tuple(external))

    def live_session(self) -> LiveSession:
        """A live session, for a client that has readings one at a time rather than
        series: it keeps them as series for :meth:`live`."""
        return LiveSession(self.config)


class LiveSession:
    """Readings one at a time, kept as series for :meth:`Engine.live`: each reading is a
    sample held until the next, a missing value a gap. A reading earlier than the last
    (the wall clock stepped back) starts the series again."""

    def __init__(self, config: EngineConfig) -> None:
        self._engine = Engine(config)
        self._window = config.smoothing_window
        self._meters: dict[str, list[Sample[float]]] = {}
        self._load: list[Sample[float]] = []
        self._soc: list[Sample[float]] = []
        self._dispatch: list[Sample[bool]] = []
        self._last: datetime | None = None

    def update(
        self,
        reading: LiveReading,
        valuation: Valuation | None,
        valid_until: datetime | None,
    ) -> LiveValue:
        """The value at ``reading.at`` (:meth:`Engine.live`), with this reading added to
        the series."""
        now = reading.at
        require_utc(now)
        plant = self._engine.config.plant
        stray = set(reading.meters) - set(plant.meters)
        if stray:
            raise ValueError(f"not the plant's meters: {sorted(stray)}")
        if self._last is not None and now < self._last:
            self._meters, self._load, self._soc, self._dispatch = {}, [], [], []
        self._last = now
        begin = now - self._window
        for name in plant.meters:
            _keep(self._meters.setdefault(name, []), Sample(now, reading.meters.get(name)), begin)
        _keep(self._load, Sample(now, reading.load_kw), begin)
        _keep(self._soc, Sample(now, reading.soc), begin)
        _keep(self._dispatch, Sample(now, reading.dispatch_as_modelled), begin)
        has_battery = plant.storage is not None
        inputs = Inputs(
            prices=None,
            soc=Series(tuple(self._soc), now) if has_battery or reading.soc is not None else None,
            dispatch_as_modelled=Series(tuple(self._dispatch), now)
            if has_battery or reading.dispatch_as_modelled is not None
            else None,
            meters={name: Series(tuple(kept), now) for name, kept in self._meters.items()},
            measured_load=Series(tuple(self._load), now),
        )
        return self._engine.live(
            inputs, at=now, valuation=valuation, valid_until=valid_until, external=reading.external
        )


def _keep[T](kept: list[Sample[T]], sample: Sample[T], begin: datetime) -> None:
    """``sample`` added to ``kept``, and those no longer needed from ``begin`` on (all but
    the one in force then) dropped."""
    kept.append(sample)
    while len(kept) > 1 and kept[1].at <= begin:
        del kept[0]


#: The causes a value is named with and still stands on (values toward the import,
#: tariffkit#67 R-14).
_STANDING: Final = frozenset({Cause.BATTERY_LOTS, Cause.PV_SPREAD})


def _live_value(
    config: EngineConfig,
    inputs: Inputs,
    now: datetime,
    valuation: Valuation | None,
    valid_until: datetime | None,
    external: tuple[str, ...],
) -> LiveValue:
    """:meth:`Engine.live`."""
    require_utc(now)
    if valuation is not None and valid_until is None:
        raise ValueError("a valuation needs its valid_until")
    if valid_until is not None:
        require_utc(valid_until)
    plant = config.plant
    has_pv, has_battery = plant.has_pv, plant.storage is not None
    stray = set(inputs.meters) - set(plant.meters)
    if stray:
        raise ValueError(f"not the plant's meters: {sorted(stray)}")
    per_array = plant.pv_per_array or (valuation is not None and _per_array(valuation))
    if not has_battery and (inputs.soc is not None or inputs.dispatch_as_modelled is not None):
        raise ValueError("the plant has no battery: read no SOC or work mode")
    soc = _soc(reading_at(inputs.soc, now).value, config.soc_tolerance)
    dispatch = reading_at(inputs.dispatch_as_modelled, now).value
    tau = config.smoothing
    read = {name: smoothed(inputs.meters.get(name), now, tau) for name in plant.meters}
    # A PV or DC-bus load meter is averaged through the warm-up (``warm`` says whether it
    # has settled), as the house load is; the others are None until a time constant has
    # passed since a gap.
    meters = {
        name: run.value if plant.meters[name] in ("pv", "dc_load") or run.warm else None
        for name, run in read.items()
    }
    pv_names = [name for name, measures in plant.meters.items() if measures == "pv"]
    pv: float | Mapping[str, float] | None = 0.0
    if has_pv:
        pv = _one_slot(plant.pv_from_meters(meters, per_array=per_array))
    # PV None: a meter unknown, or the meters read but they cannot say the PV the
    # valuation takes (one total for arrays valued apart): ``pv_power`` either way.
    load_read = smoothed(inputs.measured_load, now, tau, jump=config.load_jump_kw)
    load = load_read.value
    dc_names = [name for name, measures in plant.meters.items() if measures == "dc_load"]
    dc_load = meters[dc_names[0]] if dc_names else None
    if plant.has_dc_loads and dc_load is None:
        load = None  # the loads are known only together (``load_power``)
    try:
        under_way = slot_floor(now, config.slot)
        index = -1 if valuation is None else valuation.starts.index(under_way)
    except ValueError:
        index = -1  # the valuation does not price the slot under way
    if valuation is None or index < 0:
        return LiveValue(
            at=now,
            kind=ValuationKind.UNPRICED,
            surplus_kw=0.0,
            regime=Regime.IMPORT,
            p_import=None,
            p_export=None,
            marginal_cost=None,
            certainty_spill=0.0,
            certainty_covered=0.0,
            degraded=(Cause.PRICES,),
            external=external,
        )
    slot = valuation.slots[index]
    p_import, p_export = slot.p_import, slot.p_export

    causes: list[Cause] = []
    for missing, cause in (
        (pv is None, Cause.PV_POWER),
        (load is None, Cause.LOAD_POWER),
        (soc is None and has_battery, Cause.BATTERY_SOC),
    ):
        if missing:
            causes.append(cause)
    if has_battery and dispatch is not True:
        causes.append(Cause.WORK_MODE)  # not the modelled dispatch, or unknown
    runs = [read[name] for name in (*pv_names, *dc_names)] + [load_read]
    warm = all(run.warm for run in runs)
    # The warm-up since the client began reading, not interrupted: the one warm-up whose
    # value takes the valuation's price rather than the fallback (after a gap, or with an
    # input that comes and goes, the fallback holds). Interrupted is a gap
    # in a smoothed series, or the SOC or the dispatch unusable at any time since the
    # warm-up began, as the series show it.
    first_warm_up = all(run.first for run in runs)
    if first_warm_up and has_battery:
        began = min(run.since for run in runs if run.since is not None)
        tolerance = config.soc_tolerance
        first_warm_up = _throughout(
            inputs.soc, began, now, lambda soc: _soc(soc, tolerance) is not None
        ) and _throughout(inputs.dispatch_as_modelled, began, now, lambda mode: mode is True)
    if not warm and not causes:
        causes.append(Cause.SMOOTHING)
    if valid_until is not None and now >= valid_until:
        causes.append(Cause.ISSUE)
    if valuation.kind is ValuationKind.FALLBACK_IMPORT:
        causes.extend(valuation.degraded)
    elif valuation.model is None:
        causes.append(Cause.PLANT_MODEL)

    if (
        causes == [Cause.SMOOTHING]
        and not external
        and first_warm_up
        and pv is not None
        and load is not None
        and (soc is not None or not has_battery)
    ):
        # The warm-up after the client began reading, uninterrupted, and nothing else
        # wrong: no surplus is measured yet, so none is offered, but the price
        # never rested on the measurement. It is the valuation's for the slot (on the
        # reference plant, every warm-up of a trial run had published the import price,
        # 0.373-0.430 per kWh, against issues at 0.086-0.145). Nothing measured spills, so
        # a slot the valuation marks exporting or curtailing reads as the battery's, as
        # the warm value does when nothing spills; a plant without one imports then.
        forecast = slot.regime
        unspilt = Regime.BATTERY if has_battery else Regime.IMPORT
        shown = unspilt if forecast in (Regime.EXPORT, Regime.CURTAIL) else forecast
        return LiveValue(
            at=now,
            kind=ValuationKind.STORED_VALUE_V1,
            surplus_kw=0.0,
            regime=shown,
            p_import=p_import,
            p_export=p_export,
            marginal_cost=slot.marginal_cost,
            certainty_spill=slot.certainty_spill,
            certainty_covered=slot.certainty_covered,
            # The valuation's prices, so its missing lots and spread with them.
            degraded=(Cause.SMOOTHING, *(c for c in valuation.degraded if c in _STANDING)),
            pv_value=slot.pv_value,
            battery_value=slot.battery_value,
        )
    model = valuation.model
    if (
        causes
        or external
        or pv is None
        or load is None
        or (soc is None and has_battery)
        or model is None
    ):
        return LiveValue(
            at=now,
            kind=ValuationKind.FALLBACK_IMPORT,
            surplus_kw=0.0,
            regime=Regime.IMPORT,
            p_import=p_import,
            p_export=p_export,
            marginal_cost=p_import,
            certainty_spill=0.0,
            certainty_covered=0.0,
            degraded=tuple(dict.fromkeys(causes)),
            external=external,
        )
    revalued = revalue(
        model,
        index,
        now,
        measured_pv_kw=pv,
        measured_load_kw=load,
        soc=soc,
        step=config.slot,
        measured_dc_load_kw=dc_load,
        lots=_lots_at(inputs, now) if has_battery else None,
    )
    # The lots the revaluation priced from: the client's now, else the valuation's.
    lots_now = (_lots_at(inputs, now) if has_battery else None) or model.lots
    named: tuple[Cause, ...] = (Cause.BATTERY_LOTS,) if _lots_missing(config, lots_now) else ()
    named += tuple(c for c in valuation.degraded if c is Cause.PV_SPREAD)
    energy, found = revalued.energy, revalued.surplus
    if found.kw > 0 and found.value is not None:
        kw, regime = found.kw, found.regime  # PV is spilling now
    else:  # none, or of unknown worth: never offered
        kw, regime = 0.0, (found.regime if found.kw == 0 else slot.regime)
    if kw == 0 and importing_now(config.plant, soc, now=now, meters=meters, noise=config.observed):
        # The grid serves the house now, as the model cannot see (the plant holding
        # its battery): a kWh used now is imported too.
        return LiveValue(
            at=now,
            kind=ValuationKind.STORED_VALUE_V1,
            surplus_kw=0.0,
            regime=Regime.IMPORT,
            p_import=p_import,
            p_export=p_export,
            marginal_cost=p_import,
            certainty_spill=0.0,
            certainty_covered=energy.certainty_covered[0],
            degraded=(),  # the import price rests on no lots
        )
    return LiveValue(
        at=now,
        kind=ValuationKind.STORED_VALUE_V1,
        surplus_kw=kw,
        regime=regime,
        p_import=p_import,
        p_export=p_export,
        marginal_cost=energy.price[0],
        certainty_spill=energy.certainty_spill[0],
        certainty_covered=energy.certainty_covered[0],
        degraded=named,
        pv_value=energy.pv_value[0],
        battery_value=energy.battery_value[0],
    )


def _throughout[T](
    series: Series[T] | None, since: datetime, until: datetime, usable: Callable[[T], bool]
) -> bool:
    """Whether ``series`` had a usable value at every moment from ``since`` to ``until``."""
    if series is None:
        return False
    samples = series.samples
    begin = bisect_right(samples, since, key=lambda s: s.at) - 1
    end = bisect_right(samples, until, key=lambda s: s.at)
    if begin < 0:
        return False  # no value yet at ``since``
    return all(sample.value is not None and usable(sample.value) for sample in samples[begin:end])


def _one_slot(pv: PvKw | None) -> float | Mapping[str, float] | None:
    """One slot's PV, as ``PlantModel.pv_from_meters`` gives it: the total, or each
    array's."""
    if pv is None:
        return None
    if isinstance(pv, Mapping):
        return {name: series[0] for name, series in pv.items()}
    return pv[0]
