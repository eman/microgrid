"""``microgrid fit-battery``: fit the battery's conversion and the inverter's draw from
the meters (read only).

    uv run microgrid fit-battery --data DIR --end 2026-09-25 [--days 30]

The meters are a dataset's (its bindings: :func:`~microgrid.cli.offline.dataset_bindings`).
A plant without a DC-bus load meters none: its draw is 0.

The fit takes the PV as AC-coupled (outside the hybrid inverter), so everything the
battery stores or gives passes one converter, the hybrid inverter, which meters both of
its sides:
its AC power (the ``inverter`` entry, > 0 out to the house) and, on its DC
bus, the battery (> 0 charging) and the DC-bus load. Two fits over 10-minute slot
means, bootstrapped together by day (a resampled day takes all its slots into
both):

1. **The inverter's loss.** What goes in less what comes out,
   ``-AC - battery - DC load``, in every slot, by least squares:

       loss = standby + working x (battery working) + l_c x P_c + q_c x P_c^2
              + l_d x P_d + q_d x P_d^2

   with ``P_c`` and ``P_d`` the battery's DC charge and discharge, and the
   battery working above ``microgrid.plant.WORKING_KW`` either way. Slots where the
   DC-bus load drew are left out (its own loss is its own), and a slot whose
   loss is over ``GLITCH_KW`` (a sensor out of step: one in 30 days). A term in
   PV is fitted too, as a check that the PV sensor reads AC: it should be 0.
2. **The battery meter against its SOC, DC side.** Each hour's change in stored
   energy (SOC x capacity, at the hour's boundaries: ``microgrid.engine.ledger.soc_boundary``)
   against its metered charge and discharge, by least squares without an
   intercept: ``stored = a x charged - b x discharged``. By hour, not by day: a
   day charges about what it discharges, so a daily fit cannot tell ``a`` from
   ``b``; most hours only charge or only discharge. Every hour-long window is
   used, whatever minute it starts at (the phase moved ``b`` by 1 %); idle hours are
   left out, where SOC's 0.1 % steps would dominate.

The site's parameters (``microgrid.plant.Battery``, ``microgrid.plant.Inverter``):
``eta_charge = a / (1 + l_c)`` and ``eta_discharge = (1 - l_d) / b`` (AC to
stored and stored to AC at low power), ``loss_charge_per_kw = q_c``,
``loss_discharge_per_kw = q_d``, ``meter_charge = a``, ``meter_discharge = b``,
``standby_kw`` and ``working_kw = working``. The script prints them
with their intervals (sampling error only) and the fit's residual by power
band, which a right form leaves flat, and the figures under simpler forms: the
spread between forms is the larger uncertainty.
"""

from __future__ import annotations

import argparse
import math
import random
import statistics
from collections.abc import Sequence
from datetime import UTC, date, datetime, time, timedelta
from typing import Final

from microgrid.cli.history import DAY, Bindings, History
from microgrid.engine.ledger import soc_boundary
from microgrid.engine.series import SLOT, means, slot_floor, slot_starts
from microgrid.engine.units import convert
from microgrid.plant import WORKING_KW

__all__ = [
    "add_arguments",
    "run",
]

_HOUR: Final = timedelta(hours=1)
#: The DC-bus load drew in a slot above this, kW DC.
DC_LOAD_KW = 0.05
#: A loss over this, kW, is a sensor out of step, not the inverter: 2.5x the
#: largest the fit predicts, about 0.39 kW at the 8.4 kW charge limit.
GLITCH_KW = 1.0
#: Grid import above this, with PV under half the charge, marks a grid-charged slot.
GRID_CHARGE_KW = 0.3
#: An hour moving less than this, kWh, is idle: SOC's 0.1 % steps would dominate.
IDLE_KWH = 0.5
RESAMPLES, SEED = 300, 23
TERMS = ("standby", "working", "l_c", "q_c", "l_d", "q_d")
UNITS = {
    "standby": "kW, always",
    "working": "kW more while the battery works",
    "l_c": "of the DC charge",
    "q_c": "kW per (DC kW)^2 charging",
    "l_d": "of the DC discharge",
    "q_d": "kW per (DC kW)^2 discharging",
}
FORMS = {
    "full": (0, 1, 2, 3, 4, 5),
    "linear (no q)": (0, 1, 2, 4),
    "no working term": (0, 2, 3, 4, 5),
    "with PV (a check)": (0, 1, 2, 3, 4, 5, 6),
}
BANDS = ((0.05, 0.3), (0.3, 1.0), (1.0, 2.0), (2.0, 3.0), (3.0, 4.0), (4.0, 6.0), (6.0, 9.0))

Row = tuple[list[float], float]


#: Terms whose normal equations' pivot, or eigenvalue, falls under this share of the
#: largest are dependent: an exact dependence leaves rounding (about 1e-16 of it), and
#: the battery's terms (power and its square, to ~9 kW: a condition number near 170)
#: stand far above it. On the normal equations it is a cutoff of about 1e-5 on the terms'
#: singular values, looser than numpy's: terms nearly dependent (a condition number over
#: about 1e5) take the minimum-norm answer where numpy would still separate them.
_DEPENDENT: Final = 1e-10


def _fit(rows: Sequence[Row], columns: Sequence[int]) -> list[float]:
    """Least squares over ``columns`` of the rows' terms, as ``numpy.linalg.lstsq`` gives
    it: the normal equations solved by elimination where the terms are independent, the
    minimum-norm solution where they are not (a term always 0, or two that never differ
    in the rows: a window without a DC-bus load, or with the battery always working)."""
    size = len(columns)
    gram = [[0.0] * size for _ in range(size)]
    moment = [0.0] * size
    for terms, value in rows:
        picked = [terms[c] for c in columns]
        for i, x in enumerate(picked):
            for j, y in enumerate(picked):
                gram[i][j] += x * y
            moment[i] += x * value
    solved = _eliminate(gram, moment)
    return solved if solved is not None else _minimum_norm(gram, moment)


def _eliminate(gram: list[list[float]], moment: list[float]) -> list[float] | None:
    """``gram x = moment`` by elimination with partial pivoting; None where a pivot
    vanishes (the terms are not independent)."""
    size = len(moment)
    work = [[*row, rhs] for row, rhs in zip(gram, moment, strict=True)]
    scale = max((abs(work[i][i]) for i in range(size)), default=0.0) or 1.0
    for col in range(size):
        best = max(range(col, size), key=lambda r: abs(work[r][col]))
        if abs(work[best][col]) <= _DEPENDENT * scale:
            return None
        work[col], work[best] = work[best], work[col]
        for r in range(size):
            if r != col and work[r][col]:
                factor = work[r][col] / work[col][col]
                for c in range(col, size + 1):
                    work[r][c] -= factor * work[col][c]
    return [work[i][size] / work[i][i] for i in range(size)]


def _minimum_norm(gram: list[list[float]], moment: list[float]) -> list[float]:
    """The minimum-norm least-squares solution: ``gram``'s pseudo-inverse (its
    eigenvectors by Jacobi rotations, the eigenvalues under :data:`_DEPENDENT` of the
    largest dropped) times ``moment``."""
    size = len(moment)
    a = [row[:] for row in gram]
    v = [[float(i == j) for j in range(size)] for i in range(size)]
    for _ in range(100):
        off = sum(a[i][j] ** 2 for i in range(size) for j in range(size) if i != j)
        if off <= 1e-30 * (sum(a[i][i] ** 2 for i in range(size)) or 1.0):
            break
        for p in range(size):
            for q in range(p + 1, size):
                if abs(a[p][q]) < 1e-300:
                    continue
                theta = (a[q][q] - a[p][p]) / (2 * a[p][q])
                t = math.copysign(1.0, theta) / (abs(theta) + math.sqrt(theta * theta + 1))
                c = 1 / math.sqrt(t * t + 1)
                s = t * c
                for k in range(size):
                    akp, akq = a[k][p], a[k][q]
                    a[k][p], a[k][q] = c * akp - s * akq, s * akp + c * akq
                for k in range(size):
                    apk, aqk = a[p][k], a[q][k]
                    a[p][k], a[q][k] = c * apk - s * aqk, s * apk + c * aqk
                for k in range(size):
                    vkp, vkq = v[k][p], v[k][q]
                    v[k][p], v[k][q] = c * vkp - s * vkq, s * vkp + c * vkq
    eigen = [max(a[i][i], 0.0) for i in range(size)]
    cutoff = _DEPENDENT * max(eigen, default=0.0)
    solution = [0.0] * size
    for k in range(size):
        if eigen[k] <= cutoff or eigen[k] == 0.0:
            continue
        weight = sum(v[i][k] * moment[i] for i in range(size)) / eigen[k]
        for i in range(size):
            solution[i] += weight * v[i][k]
    return solution


def _percentile(values: Sequence[float], percent: float) -> float:
    """The ``percent``-th percentile, interpolated linearly between the ranked values."""
    ranked = sorted(values)
    at = (len(ranked) - 1) * percent / 100
    low = int(at)
    high = min(low + 1, len(ranked) - 1)
    return ranked[low] + (ranked[high] - ranked[low]) * (at - low)


def _loss_row(series: dict[str, tuple[float | None, ...]], index: int) -> Row | None:
    """One slot: ([1, working, P_c, P_c^2, P_d, P_d^2, PV], loss), kW."""
    inverter, battery = series["inverter"][index], series["battery"][index]
    if inverter is None or battery is None:
        return None
    dc_load = series["dc_load"][index] or 0.0
    if dc_load > DC_LOAD_KW:
        return None
    loss = -inverter - battery - dc_load
    if abs(loss) > GLITCH_KW:
        return None
    charge, discharge = max(battery, 0.0), max(-battery, 0.0)
    working = 1.0 if abs(battery) > WORKING_KW else 0.0
    pv = max(series["pv"][index] or 0.0, 0.0)
    return [1.0, working, charge, charge**2, discharge, discharge**2, pv], loss


def _hour_row(
    series: dict[str, tuple[float | None, ...]],
    first: int,
    capacity: float,
    soc_unit: str,
    step: timedelta = SLOT,
) -> Row | None:
    """The hour from slot ``first`` (slots of ``step``, which divides an hour): ([charged,
    -discharged] kWh through the meter, stored)."""
    soc, battery = series["soc"], series["battery"]
    hours = step / _HOUR
    last = first + round(_HOUR / step) - 1
    if first < 1 or last >= len(soc):
        return None
    before, after = soc_boundary(soc, first - 1), soc_boundary(soc, last)
    powers = battery[first : last + 1]
    if before is None or after is None or any(kw is None for kw in powers):
        return None
    charged = sum(max(kw or 0.0, 0.0) for kw in powers) * hours
    discharged = sum(max(-(kw or 0.0), 0.0) for kw in powers) * hours
    if charged + discharged < IDLE_KWH:
        return None
    return [charged, -discharged], convert(after - before, soc_unit, "fraction") * capacity


def _report_data(series: dict[str, tuple[float | None, ...]], starts: Sequence[datetime]) -> None:
    """The data behind the fit's exclusions and thresholds."""
    known = dc_load_slots = glitches = 0
    idle, quiet = 0, 0
    identity = []
    at_limit: dict[str, list[float]] = {"PV": [], "the grid": []}
    for i in range(len(starts)):
        inverter, battery = series["inverter"][i], series["battery"][i]
        if inverter is None or battery is None:
            continue
        known += 1
        dc_load = series["dc_load"][i] or 0.0
        if dc_load > DC_LOAD_KW:
            dc_load_slots += 1
        elif abs(-inverter - battery - dc_load) > GLITCH_KW:
            glitches += 1
        elif 8.0 <= battery < 9.0 and series["grid"][i] is not None:
            # One inverter: charging from the grid at the limit against PV at it.
            importing, sun = series["grid"][i] or 0.0, max(series["pv"][i] or 0.0, 0.0)
            path = "the grid" if importing > GRID_CHARGE_KW and sun < 0.5 * battery else "PV"
            at_limit[path].append(-inverter - battery)
        if abs(battery) < WORKING_KW:
            quiet += 1
            idle += abs(battery) < 0.02
        house, pv, grid = series["house"][i], series["pv"][i], series["grid"][i]
        if house is not None and pv is not None and grid is not None:
            identity.append(house - (max(pv, 0.0) + grid + inverter))
    print(
        f"data: {len(starts)} slots, {known} with the inverter and battery known; left out: "
        f"{dc_load_slots} with the DC-bus load drawing (over {DC_LOAD_KW} kW), {glitches} "
        f"with a loss over "
        f"{GLITCH_KW} kW"
    )
    print(
        f"   the battery under {WORKING_KW} kW either way in {quiet} slots, under 0.02 kW "
        f"in {idle} of them"
    )
    for path, found in at_limit.items():
        if found:
            print(
                f"   charging from {path} at 8-9 kW DC: loss {statistics.fmean(found):.3f} kW "
                f"(n {len(found)})"
            )
    if identity:
        print(
            "   consumption - (PV + grid + inverter AC): "
            f"mean {statistics.fmean(identity):+.4f} kW, SD {statistics.pstdev(identity):.4f},"
            f" n {len(identity)}"
        )


def _parameters(losses: Sequence[Row], hours: Sequence[Row]) -> dict[str, float]:
    """The site's parameters from one sample of slots and hours (module docstring)."""
    fit = dict(zip(TERMS, _fit(losses, FORMS["full"]), strict=True))
    a, b = _fit(hours, (0, 1))
    return fit | {
        "a": a,
        "b": b,
        "eta_charge": a / (1 + fit["l_c"]),
        # No discharge in the sample: b, and so the discharge efficiency, unknown (inf, as
        # numpy's float64 gave it).
        "eta_discharge": (1 - fit["l_d"]) / b if b else math.inf,
    }


def _interval(draws: Sequence[dict[str, float]], name: str) -> str:
    values = [draw[name] for draw in draws]
    return f"{_percentile(values, 5):+.4f} .. {_percentile(values, 95):+.4f}"


def add_arguments(parser: argparse.ArgumentParser) -> None:
    """The command's flags (beside the dataset's)."""
    parser.add_argument("--days", type=int, default=30, help="the window, days (default 30)")
    parser.add_argument(
        "--end", type=date.fromisoformat, help="local day the window ends at (default: now)"
    )


async def run(
    args: argparse.Namespace, bindings: Bindings, history: History, *, step: timedelta = SLOT
) -> int:
    """The fit over the window, on slots of ``step`` (``EngineConfig.slot``), printed."""
    if bindings.inverter is None:
        raise SystemExit("the fit needs the inverter's AC meter (an inverter entry)")
    battery = bindings.plant.battery
    if battery is None:
        raise SystemExit("the plant has no battery to fit")
    capacity = battery.capacity_kwh
    now = (
        datetime.combine(args.end, time(), bindings.timezone).astimezone(UTC)
        if args.end is not None
        else slot_floor(datetime.now(UTC), step)
    )
    start = now - args.days * DAY
    starts = list(slot_starts(start, now, step))
    series: dict[str, tuple[float | None, ...]] = {}
    for name, entity in (
        ("pv", bindings.pv),
        ("inverter", bindings.inverter),
        ("battery", bindings.battery),
        ("dc_load", None if bindings.dc_load is None else bindings.dc_load.entity),
        ("soc", bindings.soc),
        ("house", bindings.house),
        ("grid", bindings.grid),
    ):
        if entity is None:  # no DC-bus load: it draws nothing
            series[name] = (0.0,) * len(starts)
            continue
        series[name] = means(
            await history.numeric_series(entity, start, now, carry_in=DAY), starts, step
        )

    tz = bindings.timezone
    losses_by_day: dict[date, list[Row]] = {}
    hours_by_day: dict[date, list[Row]] = {}
    for i, at in enumerate(starts):
        day = at.astimezone(tz).date()
        if (row := _loss_row(series, i)) is not None:
            losses_by_day.setdefault(day, []).append(row)
        if (hour := _hour_row(series, i, capacity, bindings.soc_unit, step)) is not None:
            hours_by_day.setdefault(day, []).append(hour)
    days = sorted(losses_by_day.keys() & hours_by_day.keys())
    losses = [row for day in days for row in losses_by_day[day]]
    hours = [row for day in days for row in hours_by_day[day]]
    rng = random.Random(SEED)
    draws = []
    for _ in range(RESAMPLES):
        chosen = [rng.choice(days) for _ in days]
        draws.append(
            _parameters(
                [row for day in chosen for row in losses_by_day[day]],
                [row for day in chosen for row in hours_by_day[day]],
            )
        )
    point = _parameters(losses, hours)

    full = _fit(losses, FORMS["full"])
    residual = [
        value - sum(row[c] * k for c, k in zip(FORMS["full"], full, strict=True))
        for row, value in losses
    ]
    working = sum(row[1] > 0 for row, _ in losses)
    _report_data(series, starts)
    print(
        f"1. the inverter's loss: {len(losses)} slots over {len(days)} days, the battery "
        f"working in {working}; residual SD {statistics.pstdev(residual):.4f} kW"
    )
    for name in TERMS:
        print(f"   {name:8s} {point[name]:+.5f} {UNITS[name]:32s} (90 % {_interval(draws, name)})")
    for label, columns in FORMS.items():
        if label == "full":
            continue
        names = [(*TERMS, "pv")[c] for c in columns]
        other = dict(zip(names, _fit(losses, columns), strict=True))
        print(f"   {label}: " + ", ".join(f"{n} {v:+.5f}" for n, v in other.items()))
    for column, what in ((2, "charging"), (4, "discharging")):
        bands = []
        for low, high in BANDS:
            band = [k for k, (row, _) in enumerate(losses) if low <= row[column] < high]
            if band:
                mean_loss = statistics.fmean([losses[k][1] for k in band])
                bands.append(
                    f"{low:g}-{high:g} kW: loss {mean_loss:.3f}, residual "
                    f"{statistics.fmean([residual[k] for k in band]):+.4f} (n {len(band)})"
                )
        print(f"   by DC power {what}:\n     " + "\n     ".join(bands))

    print(
        f"2. stored = a x charged - b x discharged (DC meter, {len(hours)} hour-long windows "
        f"moving at least {IDLE_KWH} kWh)"
    )
    for name in ("a", "b"):
        print(f"   {name} {point[name]:.4f} (90 % {_interval(draws, name)})")
    print("=> the plant's parameters (plant/2):")
    for label, name in (
        ("[battery] eta_charge", "eta_charge"),
        ("[battery] eta_discharge", "eta_discharge"),
        ("[battery] loss_charge_per_kw", "q_c"),
        ("[battery] loss_discharge_per_kw", "q_d"),
        ("[battery] meter_charge", "a"),
        ("[battery] meter_discharge", "b"),
        ("[hybrid] standby_kw", "standby"),  # [battery.inverter] for an AC battery
        ("[hybrid] working_kw", "working"),
    ):
        print(f"   {label:34s} {point[name]:.5f} (90 % {_interval(draws, name)})")
    # End to end at the house's night load: what the inverter delivers of a DC discharge.
    for dc in (0.52, 1.0, 3.0):
        delivered = (
            dc * point["eta_discharge"] * point["b"]
            - point["q_d"] * dc**2
            - point["standby"]
            - point["working"]
        )
        print(f"   discharging {dc:g} kW DC delivers {delivered / dc:.1%} of it, draws included")
    return 0
