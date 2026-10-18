"""``microgrid check-plant``: the plant model against the plant's own meters (the
library's method, ``microgrid.engine.analysis.ground_truth``).

    uv run microgrid check-plant --data DIR --from 2026-10-21 --to 2026-10-27

Feeds the plant model what the meters recorded (metered PV, the load the plant served,
the metered SOC at the start), from each local midnight for the day and from each hour
over 48 h, and prints the model against the meters: each day, then over the days the
telemetry left no gap, each measure's bias and error, the SOC's error by hours ahead,
and how often the first event agreed. The days are local, ``--to`` included.

What a site decides about the result, which days its model does not describe and the
bars a plant must meet, is the site's.
"""

from __future__ import annotations

import argparse
import statistics
from collections.abc import Sequence
from datetime import date, datetime, timedelta

from microgrid.cli.history import Bindings, History, read_recording
from microgrid.engine.analysis import ground_truth as lgt
from microgrid.engine.analysis.scoring import BUCKETS, bucket_label
from microgrid.engine.extend import PlantModel, SlotFlow
from microgrid.engine.series import SLOT
from microgrid.plant import Plant

__all__ = [
    "add_arguments",
    "plant_model",
    "run",
]


def plant_model(plant: Plant, step: timedelta = SLOT) -> lgt.Projector:
    """The ground truth's one call into a plant model: the port's ``project``, on the
    plant behind it, over whole slots of ``step``."""
    model: PlantModel = plant

    def project(
        starts: Sequence[datetime],
        pv: Sequence[float],
        load: Sequence[float],
        soc: float,
        /,
        *,
        dc_load: Sequence[float] | None = None,
        held_at_start: bool | None = None,
    ) -> tuple[SlotFlow, ...]:
        if dc_load is None:
            return model.project(starts, [step] * len(starts), pv, load, soc)
        return model.project(
            starts,
            [step] * len(starts),
            pv,
            load,
            soc,
            dc_load_kw=dc_load,
            held_at_start=held_at_start,
        )

    return project


def add_arguments(parser: argparse.ArgumentParser) -> None:
    """The command's flags (beside the dataset's)."""
    parser.add_argument(
        "--from", dest="first", type=date.fromisoformat, required=True, help="first local day"
    )
    parser.add_argument(
        "--to", dest="last", type=date.fromisoformat, required=True, help="last local day"
    )


def _clock(day: date, minutes: float | None, bindings: Bindings) -> str:
    return "  -  " if minutes is None else lgt.clock(day, minutes, bindings.timezone)


def _report(found: lgt.Projection, bindings: Bindings, first: date, last: date) -> None:
    print(f"plant model against the meters, {first} .. {last} ({bindings.timezone.key})")
    print(
        "day         import kWh m/r   export kWh m/r   SOC MAE/max pp  "
        "top-up m|r     full m|r       reasons"
    )
    for d in found.days:
        if d.pairs is None or d.times is None:
            print(f"{d.day}  (not run)  {', '.join(d.reasons)}")
            continue
        imp, exp = d.pairs["import kWh/day"], d.pairs["export kWh/day"]
        top, full = d.times["top-up start"], d.times["first full"]
        print(
            f"{d.day}  {imp[0]:5.1f} / {imp[1]:5.1f}   {exp[0]:5.1f} / {exp[1]:5.1f}   "
            f"{d.soc_mae_pp or 0.0:5.1f} / {d.soc_max_pp or 0.0:5.1f}   "
            f"{_clock(d.day, top[0], bindings)}|{_clock(d.day, top[1], bindings)}  "
            f"{_clock(d.day, full[0], bindings)}|{_clock(d.day, full[1], bindings)}  "
            f"{', '.join(d.reasons) or '-'}"
        )
    ran = [d for d in found.days if d.pairs is not None and lgt.GAP not in d.reasons]
    print(f"\nover {len(ran)} days without a telemetry gap (model - meters):")
    if ran:
        for name in ran[0].pairs or {}:
            errors = [m - r for d in ran if d.pairs is not None for m, r in [d.pairs[name]]]
            print(
                f"  {name:22s} bias {statistics.fmean(errors):+7.3f}"
                f"  MAE {statistics.fmean(abs(e) for e in errors):6.3f}"
            )
        for name in ran[0].times or {}:
            gaps = [
                abs(m - r)
                for d in ran
                if d.times is not None
                for m, r in [d.times[name]]
                if m is not None and r is not None
            ]
            median = f"{statistics.median(gaps):5.0f}" if gaps else "  -  "
            print(f"  {name:22s} median |Δ| {median} min (n {len(gaps)})")
        soc = [d.soc_mae_pp for d in ran if d.soc_mae_pp is not None]
        if soc:
            print(f"  {'SOC MAE, pp':22s} {statistics.fmean(soc):6.2f}")
    runs = [r for r in found.runs if lgt.GAP not in r.reasons]
    by_bucket: dict[str, list[float]] = {}
    for run in runs:
        for label, error in run.soc_errors:
            by_bucket.setdefault(label, []).append(error)
    order = [bucket_label(low, high) for low, high in BUCKETS]
    shown = [
        f"{label} {statistics.fmean(errs):.1f}/{max(errs):.1f} pp (n {len(errs)})"
        for label in order
        if (errs := by_bucket.get(label))
    ]
    if shown:
        print("  SOC by hours ahead, MAE/max: " + ", ".join(shown))
    events = [r.event for r in runs if r.event is not None]
    if events:
        agreed = sum(model == metered for model, metered in events)
        print(f"  first event agreed in {agreed} of {len(events)} hourly runs")
    print("\nrouting, model -> meters (slots):")
    for pair, count in sorted(found.routing.items(), key=lambda kv: -kv[1])[:12]:
        print(f"  {pair:36s} {count}")


async def run(
    args: argparse.Namespace, bindings: Bindings, history: History, *, step: timedelta = SLOT
) -> int:
    """The plant model against the meters over ``--from`` to ``--to``, on slots of
    ``step`` (``EngineConfig.slot``), printed."""
    first, last = args.first, args.last
    if first > last:
        raise SystemExit(f"--from ({first}) is after --to ({last})")
    plant = bindings.plant
    if plant.battery is None:
        raise SystemExit("the plant has no battery: nothing for the model to project")
    recording = await read_recording(history, bindings, plant, first, last, step=step)
    draws = (
        [kw or 0.0 for kw in recording.dc_load_kw]
        if plant.has_dc_loads and recording.dc_load_kw is not None
        else None
    )
    found = lgt.project_against_meters(
        recording,
        pinned=plant,
        plant=plant,
        project=plant_model(plant, step),
        tz=bindings.timezone,
        labels={},
        dc_draws=draws,
    )
    _report(found, bindings, first, last)
    return 0
