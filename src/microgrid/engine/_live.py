"""The energy context now: live surplus, and smoothing.

The live value runs the latest issue's valuation again from now
(:func:`revalue`): **one slot from now** at measured PV and house load from the
measured SOC, then the issue's forecast. Surplus is what that slot exports or
curtails, as in an issue (:func:`~microgrid.plant.flows.surplus`): PV
beyond what the house takes and the battery can store. While the battery is
charging with room to spare, there is none. The window is a full slot wherever
``now`` falls, so the value does not change with the time left in the slot.

Measured power is noisy (a passing cloud), so PV and load are smoothed: an exponential
average of the measured series over time (docs/methodology/live-value.md, *Smoothing*),
each value weighted by how long it held (:func:`smoothed`), so the value at a moment is
a function of the series up to it, however often it is asked for. The average restarts
at a gap and is not trusted until it has run for one time constant
(:attr:`Smoothed.warm`), so a spike right after a start or a gap is not published at
full weight. SOC is not smoothed: the plant reports it in 0.1 % steps that reverse
direction rarely (on the reference plant, 5,197 readings over 3 days to 2026-09-23: 99 %
of changes ±0.1, 9 reversals within 2 minutes), and a moving average would lag it by up
to ~2.5 pp at full charge rate. No minimum surplus is imposed for any particular load:
consumers compare ``surplus_kw`` with their own draw. The measured load holds every load
as metered, so a consumer already running is in it: it adds its own draw back to the
surplus before comparing.

Pure: no I/O. Times are UTC.
"""

from __future__ import annotations

import math
from bisect import bisect_right
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from itertools import accumulate
from typing import Final

from microgrid._checks import require_utc
from microgrid.engine._stored import Lots
from microgrid.engine._timeline import SLOT
from microgrid.engine._value import OBSERVED, EnergyValue, Model, Noise, importing, value_energy
from microgrid.engine.series import Series
from microgrid.plant.flows import (
    MeterReading,
    Regime,
    SlotFlow,
    pv_joined,
    regimes,
    surplus,
)
from microgrid.plant.port import PlantModel

#: How far back the average reaches, in time constants: what came before weighs
#: e^-10 (4.5e-5) of it, under half a watt on a 10 kW swing. A client keeps its series
#: back this far (:func:`window`).
WINDOW_TAUS: Final = 10

#: A value the load-jump rule held back counts once it has held this long, if no later
#: reading confirmed it sooner: the live period (30 s, a live loop's) over which
#: the rule saw it on two readings in a row while the live value read one per period. A
#: source that reports changes only sends no second reading of a steady value.
JUMP_CONFIRM: Final = timedelta(seconds=30)


def window(tau: timedelta) -> timedelta:
    """How far back from a moment :func:`smoothed` reads a series: a client that keeps
    its readings need keep them back this far, and the one in force then."""
    return max(WINDOW_TAUS * tau, JUMP_CONFIRM)


@dataclass(frozen=True, slots=True)
class Smoothed:
    """A measured series averaged at a moment (:func:`smoothed`)."""

    #: The average; None where the series has no value at the moment (a gap, a value
    #: not vouched for, nothing measured).
    value: float | None = None
    #: The run of values has lasted one time constant: the average has settled.
    warm: bool = False
    #: The run began at the series' first sample: no gap since the client began
    #: reading it (a start, not a return). Judged within :func:`window`: a run that
    #: reaches back past it is warm, and not the first.
    first: bool = False
    #: When the run began; None for one that reaches back past the window.
    since: datetime | None = None


def smoothed(
    series: Series[float] | None,
    at: datetime,
    tau: timedelta,
    *,
    jump: float | None = None,
) -> Smoothed:
    """``series`` exponentially averaged at ``at``, over the run of values since its last
    gap (a sample without a value).

    Each value weighs by how long it held, ``1 - exp(-held / tau)`` of the average moving
    to it: the average of the measured signal itself, whatever the samples' spacing. The
    run's first value starts the average; one before :func:`window` starts it at the
    window's start. ``tau`` of zero is no smoothing: the value in force. Past
    ``known_until`` there is no value (never carried across a gap, tariffkit#67 R-14).

    ``jump`` (kW): a value further than this from the average is held back until a later
    one agrees with it to within ``jump``, or until it has held :data:`JUMP_CONFIRM`, and
    is taken from then on (one stray reading must not move the value).
    """
    require_utc(at)
    if series is None or at > series.known_until:
        return Smoothed()
    samples = series.samples
    end = bisect_right(samples, at, key=lambda s: s.at)
    if not end or samples[end - 1].value is None:
        return Smoothed()
    begin = at - window(tau)
    lowest = max(0, bisect_right(samples, begin, 0, end, key=lambda s: s.at) - 1)
    start = end - 1
    while start > lowest and samples[start - 1].value is not None:
        start -= 1
    if start == lowest and lowest > 0:  # the run reaches back past the window
        warm, first, since_run = True, False, None
    else:
        since_run = samples[start].at
        warm, first = at - since_run >= tau, start == 0
    held = _number(samples[start].value)
    value, since = held, max(samples[start].at, begin)
    pending: float | None = None
    pending_at = since

    def advance(until: datetime) -> None:
        nonlocal value, since, held, pending
        if pending is not None and pending_at + JUMP_CONFIRM < until:
            confirmed = max(pending_at + JUMP_CONFIRM, since)  # held long enough: real
            value = _toward(value, held, confirmed - since, tau)
            since, held, pending = confirmed, pending, None
        value = _toward(value, held, until - since, tau)
        since = until

    for index in range(start + 1, end):
        sample = samples[index]
        advance(sample.at)
        new = _number(sample.value)
        if jump is None or abs(new - value) <= jump:
            held, pending = new, None
        elif pending is not None and abs(new - pending) <= jump:
            held, pending = new, None  # confirmed: a real change
        else:
            pending, pending_at = new, sample.at  # hold until confirmed
    advance(at)
    return Smoothed(value, warm, first, since_run)


def _number(value: float | None) -> float:
    if value is None:  # pragma: no cover - a run holds values only
        raise ValueError("a run holds values")
    return value


def _toward(value: float, held: float, span: timedelta, tau: timedelta) -> float:
    """The average after ``held`` held for ``span``."""
    if tau == timedelta(0):
        return held
    return value + (1 - math.exp(-(span / tau))) * (held - value)


@dataclass(frozen=True, slots=True)
class LiveSurplus:
    """Surplus over one slot from now, what taking it displaces, and its value."""

    kw: float
    regime: Regime
    #: Per kWh of the surplus (``Surplus.value``); None where there is none or no price.
    value: float | None
    #: The simulated slot's flows.
    flow: SlotFlow | None = None


@dataclass(frozen=True, slots=True)
class LiveValuation:
    """The latest issue's valuation run again from now (:func:`revalue`)."""

    #: Per slot from now; slot 0 is the measured window.
    energy: EnergyValue
    #: Surplus over the measured window, and what taking it displaces, in the median
    #: scenario (the one at the median of the weights).
    surplus: LiveSurplus


def revalue(
    model: Model,
    index: int,
    now: datetime,
    *,
    measured_pv_kw: float | Mapping[str, float],
    measured_load_kw: float,
    soc: float | None,
    step: timedelta = SLOT,
    measured_dc_load_kw: float | None = None,
    lots: Lots | None = None,
) -> LiveValuation:
    """An issue's valuation (``model``) again from now (tariffkit#67 R-4).

    The window ``[now, now + step)`` runs at measured PV and load from the measured
    ``soc`` (0-1): a full slot wherever ``now`` falls, so a steady input gives a steady
    value. It is priced at slot ``index``'s prices: they are what a kWh used now pays,
    and every later make-up is priced where it falls. Then come the issue's slots from
    the next, the first of them shortened to what the window leaves of it, at their
    forecast PV, load and prices. The plant model looks ahead through them, as in an
    issue (the grid-charge window's top-up: one slot alone saw no PV coming and topped
    up from the grid). So the live surplus, regime, price and values by source all rest
    on one run, on what is measured now, as an issue's slot under way rests on the SOC
    when it was made. ``soc`` is None for a plant without a battery, and
    ``measured_pv_kw`` 0 for one without PV. The DC-bus loads run at
    ``measured_dc_load_kw`` too, where the issue has them.
    """
    require_utc(now)
    window_end = now + step
    starts, durations, chosen = [now], [step], [index]
    for later in range(index + 1, len(model.starts)):
        # Every later slot starts after now, so it ends after the window does.
        begin = max(model.starts[later], window_end)
        starts.append(begin)
        durations.append(model.starts[later] + model.durations[later] - begin)
        chosen.append(later)
    ahead = chosen[1:]
    p_export = [model.p_export[i] for i in chosen]
    energy = value_energy(
        model.plant,
        starts,
        [pv_joined(measured_pv_kw, trace, ahead) for trace in model.scenarios],
        [measured_load_kw, *(model.load_kw[i] for i in ahead)],
        soc,
        [model.p_import[i] for i in chosen],
        p_export,
        step=step,
        durations=durations,
        weights=model.weights,
        dc_load_kw=_dc_joined(model, measured_dc_load_kw, ahead),
        # A session under way now, as the issue's run decided it (its median case).
        held_at_start=model.flows[_median(model)][index].dispatch.hold_session,
        # The lots now where the client gives them, else the valuation's.
        lots=lots if lots is not None else model.lots,
        battery_value=model.battery_value,
    )
    if energy.model is None:  # pragma: no cover - value_energy has a slot to value
        raise ValueError("nothing to value")
    # Surplus and regime from the median scenario, as an issue's come from the median
    # PV case; the window is measured, but inside the grid-charge window the top-up
    # looks ahead at forecast PV, so the scenarios can differ even there.
    median = _median(model)
    window = energy.model.flows[median][:1]
    found = surplus(window, p_export[:1])
    storage = model.plant.storage
    (regime,) = regimes(
        window,
        found,
        model.plant.import_limit_kw,
        storage.divertible_kw if storage is not None else None,
    )
    return LiveValuation(
        energy=energy,
        surplus=LiveSurplus(kw=found.kw[0], regime=regime, value=found.value[0], flow=window[0]),
    )


def importing_now(
    plant: PlantModel,
    soc: float | None,
    *,
    now: datetime,
    meters: MeterReading,
    noise: Noise = OBSERVED,
) -> bool:
    """Whether the grid is serving the house now, beyond the plant's regulation.

    The meters (``meters``, by the plant's configuration) are read by the plant model
    (``PlantModel.metered_flow``: on this site, split as the ledger splits them,
    ``split_paths``), and read by the rule the evaluation reads the meters by
    (``_value.importing`` with the meters' floors, ``noise``: ``EngineConfig.observed``,
    ``OBSERVED`` by default): an import of up to 0.3 kW with the battery above its
    reserve and able to serve it is regulation, anything more is the house importing. So
    the plant holding its battery (through a DC-bus load's session with the SOC below
    its top-up level, or in its grid-charge window), which the plant model cannot see,
    reads as importing, and a kWh used now is imported too. Without a battery (``soc``
    None) the grid meter alone says so: there is no regulation to allow for. A meter
    unknown leaves it unknown: False.
    """
    require_utc(now)
    slot = plant.metered_flow(now, soc, meters)
    return slot is not None and importing(plant.storage, slot, noise)


def _dc_joined(model: Model, measured: float | None, ahead: Sequence[int]) -> list[float] | None:
    """The DC-bus loads for :func:`revalue`: measured now, then the issue's; None where
    the issue has none."""
    if model.dc_load_kw is None:
        return None
    if measured is None:
        raise ValueError("the issue has DC-bus loads: measure them")
    return [measured, *(model.dc_load_kw[i] for i in ahead)]


def _median(model: Model) -> int:
    """The scenario at the median of the weights."""
    return next(i for i, total in enumerate(accumulate(model.weights)) if total >= 0.5 - 1e-9)
