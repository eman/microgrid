"""``microgrid``: the library run on a dataset (``dataset/1``). Each command reads a
dataset's files, runs the library and prints what it gave.

- ``value``: one valuation at a moment, as a table.
- ``live``: the live value over a span.
- ``replay``: valuations at past times, scored against what the meters realised
  (:mod:`.replay`).
- ``check-plant``: the plant model against the plant's meters (:mod:`.check_plant`).
- ``ledger``: the battery's lots and their cost split over a span.
- ``fit-battery``: the battery's conversion and the inverter's draw fitted from the
  meters (:mod:`.fit_battery`).
- ``config check``: a plant's file (``plant/2``) and an engine's (``engine/2``).
- ``schema``: the outputs' schema (``valuation/2``).

A dataset holds its plant's and engine's files (``plant.toml``, ``engine.toml``);
``--plant`` and ``--engine`` run it with others. Every time is read as ISO 8601: with an
offset as given, without one in the plant's zone (an hour that repeats at a DST change,
its first time; one that does not exist is refused). Printed times are the plant's local
times.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from importlib import metadata
from importlib.resources import files
from importlib.util import find_spec
from pathlib import Path
from typing import Any, Final
from zoneinfo import ZoneInfo

from microgrid.cli import check_plant, fit_battery, replay
from microgrid.cli.dataset import Dataset, DatasetError, DatasetHistory, read_dataset
from microgrid.cli.history import Limits, LotsError, run_ledger
from microgrid.cli.inputs import ASSUMED_DC_LOAD, currency
from microgrid.cli.offline import (
    ENGINE_FILE,
    LOTS_RUN_IN,
    PLANT_FILE,
    Setup,
    client_causes,
    dataset_bindings,
    limits_of,
    live,
    load_setup,
    lots_missing,
    value,
    with_recorded_prices,
)
from microgrid.cli.sources import SourceError
from microgrid.cli.sources.config import Sources, SourcesConfigError, read_sources
from microgrid.engine import (
    ENGINE_CONTRACT,
    MODEL_VERSION,
    EngineConfigError,
    Valuation,
    ValuationKind,
)
from microgrid.engine.ledger import Amount, Source
from microgrid.plant import PlantConfigError

__all__ = [
    "UsageError",
    "duration",
    "local",
    "main",
    "moment",
    "parser",
    "span",
]

_DURATION = re.compile(r"(\d+(?:\.\d+)?)([smhd])")
_UNITS = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days"}


def duration(text: str) -> timedelta:
    """``30s``, ``5m``, ``1h``, ``2d``: a positive duration."""
    match = _DURATION.fullmatch(text)
    if match is None or float(match.group(1)) <= 0:
        raise argparse.ArgumentTypeError(f"not a positive duration (30s, 5m, 1h): {text!r}")
    return timedelta(**{_UNITS[match.group(2)]: float(match.group(1))})


class UsageError(ValueError):
    """An argument or setting the command cannot use."""


def moment(text: str, tz: ZoneInfo) -> datetime:
    try:
        moment = datetime.fromisoformat(text)
    except ValueError as err:
        raise UsageError(f"not an ISO 8601 time: {text!r}") from err
    if moment.tzinfo is not None:
        return moment.astimezone(UTC)
    local = moment.replace(tzinfo=tz)
    utc = local.astimezone(UTC)
    if utc.astimezone(tz).replace(tzinfo=None) != moment:
        raise UsageError(f"{text} does not exist in {tz.key} (the clocks skip it)")
    return utc


def span(args: argparse.Namespace, tz: ZoneInfo) -> tuple[datetime, datetime]:
    start, end = moment(args.start, tz), moment(args.end, tz)
    if start >= end:
        raise UsageError(f"--from ({args.start}) must be before --to ({args.end})")
    return start, end


def local(moment: datetime, tz: ZoneInfo, *, seconds: bool = False) -> str:
    return moment.astimezone(tz).strftime(f"%Y-%m-%d %H:%M{':%S' if seconds else ''} %Z")


def _setup(args: argparse.Namespace) -> Setup:
    return load_setup(args.data, args.plant, args.engine)


#: What each of the command's own causes means, printed beside the engine's.
_ASSUMED: Final = {ASSUMED_DC_LOAD: "the DC-bus loads valued at 0 kW: no forecast of them"}


def _limits(args: argparse.Namespace, dataset: Dataset | None = None) -> Limits:
    """The record's limits: the flags', else the dataset's (its manifest), else the
    defaults (``history.TELEMETRY``, ``history.DC_STALE_AFTER``)."""
    base = limits_of(dataset) if dataset is not None else Limits()
    return Limits(
        args.stale_after or base.telemetry,
        getattr(args, "dc_stale_after", None) or base.dc_stale_after,
    )


def _print_valuation(
    valuation: Valuation,
    at: datetime,
    tz: ZoneInfo,
    slots: int,
    assumed: tuple[str, ...] = (),
) -> int:
    if valuation.kind is ValuationKind.UNPRICED:
        print(
            f"no valuation at {local(at, tz)}: no prices for the slot under way (none read,"
            " too old, or on intervals that cross the slots)"
        )
        return 1
    # The client's own causes beside the engine's, never hiding them.
    degraded = ", ".join((*valuation.degraded, *assumed)) or "none"
    cut = "; horizon cut by a gap in the load forecast" if valuation.horizon_cut else ""
    print(
        f"valued at {local(at, tz)}: {valuation.kind.value}, {len(valuation.slots)} slots,"
        f" degraded: {degraded}{cut}"
    )
    for cause in assumed:
        print(f"  {cause}: {_ASSUMED.get(cause, cause)}")
    print(f"{'slot':<22} {'import':>8} {'export':>8} {'marginal':>9} {'surplus kW':>11}  regime")
    for slot in valuation.slots[:slots]:
        print(
            f"{local(slot.start, tz):<22} {slot.p_import:>8.4f} {slot.p_export:>8.4f}"
            f" {slot.marginal_cost:>9.4f} {slot.surplus_kw:>11.3f}  {slot.regime.value}"
        )
    return 0


def _value(args: argparse.Namespace) -> int:
    setup = _setup(args)
    tz = setup.plant.config.tz
    at = moment(args.at, tz)
    dataset = read_dataset(args.data)
    limits = _limits(args, dataset)
    if setup.plant.storage is not None and setup.engine.config.pricing.battery_value == (
        "stored_cost"
    ):
        why = lots_missing(setup, dataset, limits=limits)
        if why is not None:
            print(f"no battery lots: {why}", file=sys.stderr)
    valuation = value(setup, dataset, at, limits=limits)
    return _print_valuation(valuation, at, tz, args.slots, client_causes(setup, dataset))


def _sources_setup(path: Path) -> tuple[Sources, Setup]:
    """The sources file and the setup it names. The readers need the ``sources`` extra
    (``httpx``, ``pydantic``): without it, say so rather than fail on an import."""
    if find_spec("httpx") is None or find_spec("pydantic") is None:
        raise UsageError("reading sources needs the extra: pip install 'microgrid[sources]'")
    sources = read_sources(path)
    # The engine is the one the file names, else the library's defaults: never an
    # engine.toml that happens to sit beside it (what ``now --save`` copies is what ran).
    return sources, load_setup(sources.path.parent, sources.plant, sources.engine, beside=False)


def _now(args: argparse.Namespace) -> int:
    from microgrid.cli.dataset import write_dataset
    from microgrid.cli.sources.read import read_now

    sources, setup = _sources_setup(args.sources)
    at = datetime.now(UTC).replace(microsecond=0)
    read = asyncio.run(
        read_now(
            sources,
            setup.plant,
            setup.engine.config,
            at,
            lots_run_in=args.lots_run_in,
            limits=_limits(args),
        )
    )
    for name, why in read.problems.items():
        print(f"not read: {name}: {why}", file=sys.stderr)
    if args.save is not None:
        write_dataset(args.save, read.entries)
        for name, path in ((PLANT_FILE, sources.plant), (ENGINE_FILE, sources.engine)):
            if path is not None:
                (args.save / name).write_bytes(path.read_bytes())
    tz = setup.plant.config.tz
    assumed = (ASSUMED_DC_LOAD,) if setup.plant.has_dc_loads else ()
    return _print_valuation(setup.engine.value(read.inputs, at=at), at, tz, args.slots, assumed)


def _sources(args: argparse.Namespace) -> int:
    from microgrid.cli.sources.read import read_now

    sources, setup = _sources_setup(args.sources)
    at = datetime.now(UTC).replace(microsecond=0)
    read = asyncio.run(read_now(sources, setup.plant, setup.engine.config, at))
    print(
        f"{args.sources}: the plant {sources.plant.name}, the engine {sources.engine or 'defaults'}"
    )
    names = sorted({*read.sources, *read.problems})
    for name in names:
        if name in read.sources:
            print(f"  yes  {name:<14} {read.sources[name]}")
        else:
            print(f"  no   {name:<14} {read.problems[name]}")
    return 0 if not read.problems else 1


def _fetch(args: argparse.Namespace) -> int:
    from microgrid.cli.dataset import write_dataset
    from microgrid.cli.sources.fetch import fetch_measured
    from microgrid.cli.sources.home_assistant import HaClient

    sources, setup = _sources_setup(args.sources)
    if sources.measured is None or sources.home_assistant is None:
        raise UsageError(f"{args.sources}: fetch needs a [measured] section and [home_assistant]")
    tz = setup.plant.config.tz
    start, end = span(args, tz)
    measured, connection = sources.measured, sources.home_assistant

    async def run() -> dict[str, Any]:
        async with HaClient.connect(connection) as client:
            return await fetch_measured(client, measured, start, end)

    entries = asyncio.run(run())
    write_dataset(args.out, entries)
    for name, path in ((PLANT_FILE, sources.plant), (ENGINE_FILE, sources.engine)):
        if path is not None:
            (args.out / name).write_bytes(path.read_bytes())
    print(f"wrote {args.out}: {len(entries)} entries, {local(start, tz)} to {local(end, tz)}")
    return 0


def _live(args: argparse.Namespace) -> int:
    setup = _setup(args)
    tz = setup.plant.config.tz
    start, end = span(args, tz)
    dataset = read_dataset(args.data)
    for cause in client_causes(setup, dataset):
        print(f"each valuation: {cause}: {_ASSUMED.get(cause, cause)}", file=sys.stderr)
    print(f"{'at':<25} {'marginal':>9} {'import':>8} {'surplus kW':>11}  {'regime':<8} degraded")
    for at, found in live(
        setup,
        dataset,
        start,
        end,
        every=args.every,
        issue_every=args.issue_every,
        valid_for=args.valid_for,
        limits=_limits(args, dataset),
    ):
        if found.kind is ValuationKind.UNPRICED:
            print(f"{local(at, tz, seconds=True):<25} no value: no price for the slot under way")
            continue
        # The client's own causes (a silent path), then the engine's.
        causes = dict.fromkeys((*found.external, *found.degraded))
        print(
            f"{local(at, tz, seconds=True):<25} {found.marginal_cost:>9.4f} {found.p_import:>8.4f}"
            f" {found.surplus_kw:>11.3f}  {found.regime.value:<8}"
            f" {', '.join(causes) or '-'}"
        )
    return 0


async def _check_plant(args: argparse.Namespace) -> int:
    setup = _setup(args)
    dataset = with_recorded_prices(read_dataset(args.data))
    bindings = dataset_bindings(setup, dataset, limits=_limits(args, dataset))
    return await check_plant.run(
        args, bindings, DatasetHistory(dataset), step=setup.engine.config.slot
    )


async def _fit_battery(args: argparse.Namespace) -> int:
    if args.end is None:
        raise UsageError("--end is needed (a dataset has no now)")
    setup = _setup(args)
    dataset = with_recorded_prices(read_dataset(args.data))
    bindings = dataset_bindings(setup, dataset, limits=_limits(args, dataset))
    return await fit_battery.run(
        args, bindings, DatasetHistory(dataset), step=setup.engine.config.slot
    )


async def _replay(args: argparse.Namespace) -> int:
    setup = _setup(args)
    dataset = with_recorded_prices(read_dataset(args.data))
    limits = _limits(args, dataset)
    bindings = dataset_bindings(setup, dataset, limits=limits)
    start, end = span(args, bindings.timezone)
    return await replay.run(
        args, bindings, DatasetHistory(dataset), setup, dataset, start, end, limits=limits
    )


async def _ledger(args: argparse.Namespace) -> int:
    setup = _setup(args)
    dataset = with_recorded_prices(read_dataset(args.data))
    bindings = dataset_bindings(setup, dataset, limits=_limits(args, dataset))
    tz = bindings.timezone
    start, end = span(args, tz)
    run = await run_ledger(
        DatasetHistory(dataset), bindings, start, end, step=setup.engine.config.slot
    )
    total: dict[str, dict[Source, Amount]] = {"charged": {}, "to_load": {}, "to_export": {}}
    for slot in run.slots:
        for name, flows in (
            ("charged", slot.charged),
            ("to_load", slot.to_load),
            ("to_export", slot.to_export),
        ):
            for source, amount in flows.items():
                total[name][source] = total[name].get(source, Amount()) + amount
    held = run.slots[-1].held if run.slots else {}
    print(f"the battery's lots, {local(start, tz)} to {local(end, tz)} ({len(run.slots)} slots)")
    money = currency(dataset) or "value"  # the prices' own: none is converted
    print(
        f"{'source':<8} {'charged kWh':>12} {'cost ' + money:>9} {'to load kWh':>12}"
        f" {money:>8} {'exported kWh':>13} {'held kWh':>9} {money:>8} {money + '/kWh':>8}"
    )
    for source in Source:
        charged = total["charged"].get(source, Amount())
        to_load = total["to_load"].get(source, Amount())
        exported = total["to_export"].get(source, Amount())
        kept = held.get(source, Amount())
        # Energy of unknown origin has no known cost (``Source.UNKNOWN``).
        known = kept.kwh > 1e-9 and source is not Source.UNKNOWN
        unit = f"{kept.value / kept.kwh:8.4f}" if known else f"{'-':>8}"
        print(
            f"{source.value:<8} {charged.kwh:>12.3f} {charged.value:>9.3f} {to_load.kwh:>12.3f}"
            f" {to_load.value:>8.3f} {exported.kwh:>13.3f} {kept.kwh:>9.3f} {kept.value:>8.3f}"
            f" {unit}"
        )
    unexplained = sum(slot.unexplained.kwh for slot in run.slots)
    print(f"unexplained: {unexplained:.3f} kWh (the meters against the SOC)")
    return 0


def _config_check(args: argparse.Namespace) -> int:
    setup = load_setup(args.data or Path(), args.plant, args.engine)
    battery = setup.plant.config.battery
    capacity = (
        f"{battery.battery.capacity_kwh} kWh battery" if battery is not None else "no battery"
    )
    print(f"ok: the plant ({capacity}) and the engine")
    return 0


def _schema(args: argparse.Namespace) -> int:
    name = "valuation-2.schema.json"
    text = (files("microgrid.engine") / "schemas" / name).read_text(encoding="utf-8")
    schema = json.loads(text)
    out = json.dumps(schema, indent=2, sort_keys=True) + "\n"
    if args.write is None:
        sys.stdout.write(out)
    else:
        (args.write / name).write_text(out, encoding="utf-8")
    return 0


_FROM = "the span's start (ISO 8601; the plant's local time if bare)"
_TO = "the span's end, not included"


def _dataset_arguments(command: argparse.ArgumentParser) -> None:
    command.add_argument("--data", type=Path, required=True, metavar="DIR", help="a dataset")
    command.add_argument(
        "--plant", type=Path, metavar="FILE", help="plant/2 (default: the dataset's plant.toml)"
    )
    command.add_argument(
        "--engine",
        type=Path,
        metavar="FILE",
        help="engine/2 (default: the dataset's engine.toml, else the library's defaults)",
    )


def _limit_arguments(command: argparse.ArgumentParser) -> None:
    """How a record's silences are judged (``history.Limits``): each flag's default is
    the dataset's ``limits``, else the library's (3m, 5m: measured on one plant)."""
    command.add_argument(
        "--stale-after",
        type=duration,
        metavar="D",
        help="the meters unknown after D without a heartbeat (default: the dataset's"
        " limits, else 3m)",
    )
    command.add_argument(
        "--dc-stale-after",
        type=duration,
        metavar="D",
        help="a DC-bus load's reading other than 0 stale after D silent (default: the"
        " dataset's limits, else 5m)",
    )


def _meter_arguments(command: argparse.ArgumentParser) -> None:
    """A dataset whose meters are read, and how its record is judged."""
    _dataset_arguments(command)
    _limit_arguments(command)


def parser() -> argparse.ArgumentParser:
    """The commands and their arguments."""
    top = argparse.ArgumentParser(
        prog="microgrid",
        description="The library run on a dataset (dataset/1). A time without an offset is"
        " the plant's local time.",
    )
    # The distribution's version (pyproject.toml), and the API and model it carries.
    top.add_argument(
        "--version",
        action="version",
        version=f"microgrid {metadata.version('microgrid')} ({ENGINE_CONTRACT}, {MODEL_VERSION})",
    )
    commands = top.add_subparsers(dest="command", required=True)

    one = commands.add_parser("value", help="one valuation of a dataset at a moment")
    _dataset_arguments(one)
    one.add_argument(
        "--at", required=True, metavar="T", help="the moment valued (ISO 8601; local if bare)"
    )
    one.add_argument("--slots", type=int, default=12, metavar="N", help="slots shown (default 12)")
    _limit_arguments(one)
    one.set_defaults(run=_value)

    now = commands.add_parser("now", help="value now, from the sources a sources/1 file names")
    now.add_argument("--sources", type=Path, required=True, metavar="FILE", help="sources/1")
    now.add_argument("--slots", type=int, default=12, metavar="N", help="slots shown (default 12)")
    now.add_argument(
        "--save", type=Path, metavar="DIR", help="also write the inputs read as a dataset"
    )
    now.add_argument(
        "--lots-run-in",
        type=duration,
        default=LOTS_RUN_IN,
        metavar="D",
        help="the battery's lots from the ledger over the D before now, read from [measured]"
        " (default 2d)",
    )
    _limit_arguments(now)
    now.set_defaults(run=_now)

    listed = commands.add_parser("sources", help="each input's source, and whether it answers")
    listed.add_argument("--sources", type=Path, required=True, metavar="FILE", help="sources/1")
    listed.set_defaults(run=_sources)

    fetched = commands.add_parser(
        "fetch", help="what was measured over a span, from Home Assistant, as a dataset"
    )
    fetched.add_argument("--sources", type=Path, required=True, metavar="FILE", help="sources/1")
    fetched.add_argument("--from", dest="start", required=True, metavar="T", help=_FROM)
    fetched.add_argument("--to", dest="end", required=True, metavar="T", help=_TO)
    fetched.add_argument("--out", type=Path, required=True, metavar="DIR")
    fetched.set_defaults(run=_fetch)

    span = commands.add_parser("live", help="the live value over a span of a dataset")
    _dataset_arguments(span)
    span.add_argument("--from", dest="start", required=True, metavar="T", help=_FROM)
    span.add_argument("--to", dest="end", required=True, metavar="T", help=_TO)
    span.add_argument(
        "--every",
        type=duration,
        default=timedelta(seconds=30),
        metavar="D",
        help="a reading every D (default 30s)",
    )
    span.add_argument(
        "--issue-every",
        type=duration,
        default=timedelta(minutes=5),
        metavar="D",
        help="valued again every D (default 5m)",
    )
    span.add_argument(
        "--valid-for",
        type=duration,
        default=timedelta(minutes=30),
        metavar="D",
        help="each valuation valid for D (default 30m)",
    )
    _limit_arguments(span)
    span.set_defaults(run=_live)

    plant = commands.add_parser("check-plant", help="the plant model against its meters")
    _meter_arguments(plant)
    check_plant.add_arguments(plant)
    plant.set_defaults(run=lambda args: asyncio.run(_check_plant(args)))

    lots = commands.add_parser("ledger", help="the battery's lots and their cost split")
    _meter_arguments(lots)
    lots.add_argument("--from", dest="start", required=True, metavar="T", help=_FROM)
    lots.add_argument("--to", dest="end", required=True, metavar="T", help=_TO)
    lots.set_defaults(run=lambda args: asyncio.run(_ledger(args)))

    again = commands.add_parser("replay", help="valuations at past times, scored")
    _meter_arguments(again)
    again.add_argument("--from", dest="start", required=True, metavar="T", help=_FROM)
    again.add_argument("--to", dest="end", required=True, metavar="T", help=_TO)
    replay.add_arguments(again, duration)
    again.set_defaults(run=lambda args: asyncio.run(_replay(args)))

    fit = commands.add_parser("fit-battery", help="the battery's conversion, from its meters")
    _meter_arguments(fit)
    fit_battery.add_arguments(fit)
    fit.set_defaults(run=lambda args: asyncio.run(_fit_battery(args)))

    config = commands.add_parser("config", help="the configuration files")
    config_commands = config.add_subparsers(dest="config_command", required=True)
    check = config_commands.add_parser(
        "check", help="validate a plant's file (plant/2) and an engine's (engine/2)"
    )
    check.add_argument("--data", type=Path, metavar="DIR", help="a dataset's files")
    check.add_argument("--plant", type=Path, metavar="FILE", help="plant/2")
    check.add_argument("--engine", type=Path, metavar="FILE", help="engine/2")
    check.set_defaults(run=_config_check)

    schema = commands.add_parser("schema", help="print or write the outputs' schema")
    schema.add_argument("--write", type=Path, metavar="DIR", help="write the file into DIR")
    schema.set_defaults(run=_schema)
    return top


def main(argv: Sequence[str] | None = None) -> int:
    """Run a command and return its exit status."""
    args = parser().parse_args(argv)
    try:
        status: int = args.run(args)
    except (
        DatasetError,
        SourceError,
        SourcesConfigError,
        PlantConfigError,
        EngineConfigError,
        LotsError,
        UsageError,
    ) as err:
        print(f"error: {err}", file=sys.stderr)
        return 1
    return status
