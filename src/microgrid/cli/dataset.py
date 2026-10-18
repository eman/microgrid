"""A dataset: a directory of the library's series (``dataset/1``), so every command
runs without the plant it came from.

**The format, ``dataset/1``,** is the library's own terms, not any integration's:

- ``manifest.json``: ``{"format": "dataset/1", "entries": {name: {"file": ...,
  "kind": ..., "forecasts": bool}}}``. An entry's ``kind`` is how its values read:
  ``number``, ``bool``, ``price`` (``{"import": ..., "export": ...}``, per kWh) or
  ``quantiles`` (``{"0.1": ..., "0.5": ..., "0.9": ...}``, or ``"0.5"`` alone).
- A series file (``forecasts`` false): ``{"unit": ..., "known_until": ...,
  "samples": [[at, value], ...]}``, each value held until the next, ``null`` for none.
- A forecasts file (``forecasts`` true): ``{"unit": ..., "known_until": ...,
  "issues": [{"received_at": ..., "issued_at": ..., "source": ..., "intervals":
  [[start, end, value], ...]}, ...]}``, each issue as the client received it;
  ``{"received_at": ..., "issue": null}`` where the source answered nothing.

Times are ISO 8601 with an offset (read as UTC). Units are the source's, stated in
each file: :func:`microgrid.cli.inputs.inputs` converts them to the engine's
(:mod:`microgrid.engine.units`) and refuses one it cannot, so a value in another unit
is never used as the engine's.

**The names** a dataset gives its entries are conventional.
:func:`microgrid.cli.inputs.inputs` reads the engine's inputs: :data:`PRICES`,
:data:`PV_FORECAST` (or ``pv_forecast.<array>``, one per array, for a plant that takes
PV per array), :data:`LOAD_FORECAST`, :data:`DC_LOAD_FORECAST`, :data:`SOC` and
:data:`BATTERY_FIRST`. The rest are what was metered, for the commands that score
against it: the plant's meters by the plant's names (``pv``, ``grid``, ``battery``,
``dc_load``), :data:`HOUSE`, the house meter, and :data:`HEARTBEAT`, a series whose
samples prove the telemetry path alive. A name is letters, digits, ``_``, ``-`` and
``.``: each entry's file stays in the directory.
"""

from __future__ import annotations

import json
import math
import re
from bisect import bisect_left
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final, Literal, cast

from microgrid.cli.history import Limits
from microgrid.engine.series import (
    Forecast,
    Interval,
    Price,
    Quantiles,
    Sample,
    Series,
    require_utc,
)
from microgrid.engine.units import UnitError, normalise

__all__ = [
    "BATTERY_FIRST",
    "DC_LOAD_FORECAST",
    "DOMAIN",
    "EXPORT_PRICE",
    "FORMAT",
    "HEARTBEAT",
    "HOUSE",
    "IMPORT_PRICE",
    "INVERTER",
    "LOAD_FORECAST",
    "MANIFEST",
    "PRICES",
    "PV_FORECAST",
    "SOC",
    "Dataset",
    "DatasetError",
    "DatasetHistory",
    "Entry",
    "Kind",
    "Value",
    "read_dataset",
    "write_dataset",
]

FORMAT: Final = "dataset/1"
MANIFEST: Final = "manifest.json"

PRICES: Final = "prices"
PV_FORECAST: Final = "pv_forecast"
LOAD_FORECAST: Final = "load_forecast"
DC_LOAD_FORECAST: Final = "dc_load_forecast"
SOC: Final = "soc"
BATTERY_FIRST: Final = "battery_first"
HOUSE: Final = "house"
HEARTBEAT: Final = "heartbeat"
#: The import and export prices in force, as recorded (TariffKit's price sensors' history):
#: what the battery's lots were charged and valued at (``history.read_recorded``).
IMPORT_PRICE: Final = "import_price"
EXPORT_PRICE: Final = "export_price"
#: The battery inverter's AC power, > 0 out: for fitting the battery's conversion.
INVERTER: Final = "inverter"
#: A dataset's entries as entity IDs (``dataset.<name>``), so bindings name them as they
#: name any recorder's (:class:`DatasetHistory`, ``history.Bindings``).
DOMAIN: Final = "dataset"

type Kind = Literal["number", "bool", "price", "quantiles"]
type Value = float | bool | Price | Quantiles
_KINDS: Final = ("number", "bool", "price", "quantiles")
_NAME: Final = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]*")


class DatasetError(ValueError):
    """A dataset that cannot be read: the message names the file and what is wrong."""


@dataclass(frozen=True, slots=True)
class Entry:
    """One series or series of forecasts of a dataset, as written or read."""

    kind: Kind
    #: A series of values, or of forecasts as received (``forecasts``), the values of
    #: the entry's kind (``Series`` is invariant, so any series of them).
    series: Series[Any]
    forecasts: bool = False


@dataclass(frozen=True, slots=True)
class Dataset:
    """A dataset's entries by name, and how its record's silences are judged where the
    manifest says (``limits``; None: the command's flags, else the defaults)."""

    entries: Mapping[str, Entry]
    limits: Limits | None = None

    def series(self, name: str) -> Series[Value] | None:
        """A series of values by name; None where the dataset has none."""
        entry = self.entries.get(name)
        if entry is None:
            return None
        if entry.forecasts:
            raise DatasetError(f"{name}: is forecasts, not a series of values")
        return cast("Series[Value]", entry.series)

    def forecasts(self, name: str) -> Series[Forecast[Value]] | None:
        """A series of forecasts by name; None where the dataset has none."""
        entry = self.entries.get(name)
        if entry is None:
            return None
        if not entry.forecasts:
            raise DatasetError(f"{name}: is a series of values, not forecasts")
        return cast("Series[Forecast[Value]]", entry.series)


# -- reading -------------------------------------------------------------------------


def _time(value: object, where: str) -> datetime:
    if not isinstance(value, str):
        raise DatasetError(f"{where}: a time must be an ISO 8601 string, not {value!r}")
    try:
        moment = datetime.fromisoformat(value)
    except ValueError as err:
        raise DatasetError(f"{where}: not an ISO 8601 time: {value!r}") from err
    if moment.tzinfo is None:
        raise DatasetError(f"{where}: {value!r} has no offset")
    return moment.astimezone(UTC)


def _number(value: object, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        raise DatasetError(f"{where}: not a finite number: {value!r}")
    return float(value)


def _value(kind: Kind, value: object, where: str) -> Value:
    match kind:
        case "number":
            return _number(value, where)
        case "bool":
            if not isinstance(value, bool):
                raise DatasetError(f"{where}: not true or false: {value!r}")
            return value
        case "price":
            if not isinstance(value, Mapping) or set(value) != {"import", "export"}:
                raise DatasetError(f"{where}: a price is {{import, export}}: {value!r}")
            return Price(_number(value["import"], where), _number(value["export"], where))
        case "quantiles":
            if not isinstance(value, Mapping):
                raise DatasetError(f"{where}: quantiles are {{quantile: value}}: {value!r}")
            try:
                return Quantiles({float(q): _number(v, where) for q, v in value.items()})
            except ValueError as err:
                raise DatasetError(f"{where}: {err}") from err


def _read_entry(path: Path, kind: Kind, forecasts: bool) -> Entry:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as err:
        raise DatasetError(f"{path}: cannot be read as JSON: {err}") from err
    if not isinstance(data, Mapping):
        raise DatasetError(f"{path}: must be an object")
    unit = data.get("unit")
    if unit is not None and not isinstance(unit, str):
        raise DatasetError(f"{path}: unit must be a string or null")
    known_until = _time(data.get("known_until"), f"{path}: known_until")
    key, other = ("issues", "samples") if forecasts else ("samples", "issues")
    rows = data.get(key)
    if not isinstance(rows, list) or other in data:
        raise DatasetError(
            f"{path}: a {'forecasts' if forecasts else 'series'} file has a list {key!r}"
            f" (and no {other!r}), as its manifest entry says"
        )
    try:
        if not forecasts:
            samples: list[Sample[Value]] = []
            for i, row in enumerate(rows):
                where = f"{path}: samples[{i}]"
                if not isinstance(row, list) or len(row) != 2:
                    raise DatasetError(f"{where}: a sample is [at, value]")
                at = _time(row[0], where)
                samples.append(Sample(at, None if row[1] is None else _value(kind, row[1], where)))
            return Entry(kind, Series(tuple(samples), known_until, unit))
        issues: list[Sample[Forecast[Value]]] = []
        for i, issue in enumerate(rows):
            where = f"{path}: issues[{i}]"
            if not isinstance(issue, Mapping):
                raise DatasetError(f"{where}: an issue is an object")
            received = _time(issue.get("received_at"), f"{where}.received_at")
            if "issue" in issue and issue["issue"] is None:
                issues.append(Sample(received, None))
                continue
            rows_of = issue.get("intervals")
            if not isinstance(rows_of, list):
                raise DatasetError(f"{where}.intervals: must be a list")
            intervals = []
            for j, row in enumerate(rows_of):
                spot = f"{where}.intervals[{j}]"
                if not isinstance(row, list) or len(row) != 3:
                    raise DatasetError(f"{spot}: an interval is [start, end, value]")
                try:
                    intervals.append(
                        Interval(
                            _time(row[0], spot), _time(row[1], spot), _value(kind, row[2], spot)
                        )
                    )
                except ValueError as err:  # the library's: one that ends before it starts
                    raise DatasetError(f"{spot}: {err}") from err
            source = issue.get("source", "")
            if not isinstance(source, str):
                raise DatasetError(f"{where}.source: must be a string")
            issued_at = _time(issue.get("issued_at"), f"{where}.issued_at")
            try:
                forecast = Forecast(tuple(intervals), issued_at, source)
            except ValueError as err:  # the library's: intervals out of order
                raise DatasetError(f"{where}: {err}") from err
            issues.append(Sample(received, forecast))
        return Entry(kind, Series(tuple(issues), known_until, unit), forecasts=True)
    except ValueError as err:  # the library's own checks (order, known_until)
        raise DatasetError(f"{path}: {err}") from err


def read_dataset(directory: Path) -> Dataset:
    """The dataset in ``directory``; raises :class:`DatasetError` naming what is wrong."""
    manifest_path = directory / MANIFEST
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as err:
        raise DatasetError(f"{manifest_path}: cannot be read as JSON: {err}") from err
    if not isinstance(manifest, Mapping) or manifest.get("format") != FORMAT:
        raise DatasetError(f"{manifest_path}: format must be {FORMAT!r}")
    entries = manifest.get("entries")
    if not isinstance(entries, Mapping):
        raise DatasetError(f"{manifest_path}: entries must be an object")
    limits = _limits(manifest.get("limits"), f"{manifest_path}: limits")
    read: dict[str, Entry] = {}
    root = directory.resolve()
    for name, spec in entries.items():
        where = f"{manifest_path}: entries.{name}"
        _check_name(str(name), where)
        if not isinstance(spec, Mapping) or not isinstance(spec.get("file"), str):
            raise DatasetError(f"{where}: needs a file")
        if not (directory / spec["file"]).resolve().is_relative_to(root):
            raise DatasetError(f"{where}: its file must be in the dataset's directory")
        kind = spec.get("kind")
        if kind not in _KINDS:
            raise DatasetError(f"{where}: kind must be one of {list(_KINDS)}")
        forecasts = spec.get("forecasts", False)
        if not isinstance(forecasts, bool):
            raise DatasetError(f"{where}: forecasts must be true or false")
        read[str(name)] = _read_entry(directory / spec["file"], cast("Kind", kind), forecasts)
    return Dataset(read, limits)


#: The manifest's ``limits``: each key, and the :class:`~microgrid.cli.history.Limits`
#: field it sets (seconds).
_LIMITS: Final = {"telemetry_seconds": "telemetry", "dc_stale_after_seconds": "dc_stale_after"}


def _limits(value: object, where: str) -> Limits | None:
    """The manifest's optional ``limits`` (``{"telemetry_seconds": 180,
    "dc_stale_after_seconds": 300}``, each optional): how the record's silences are
    judged, as its recorder's cadence asks (``history.TELEMETRY``,
    ``history.DC_STALE_AFTER`` where a key is absent)."""
    if value is None:
        return None
    if not isinstance(value, Mapping) or set(value) - set(_LIMITS):
        raise DatasetError(f"{where}: an object of {sorted(_LIMITS)}")
    given = {}
    for key, field_name in _LIMITS.items():
        if key in value:
            number = _number(value[key], f"{where}.{key}")
            if number <= 0:
                raise DatasetError(f"{where}.{key}: must be more than 0")
            given[field_name] = timedelta(seconds=number)
    return Limits(**given)


def _check_name(name: str, where: str) -> None:
    if not _NAME.fullmatch(name) or ".." in name:
        raise DatasetError(f"{where}: a name is letters, digits, '_', '-' and '.'")


# -- writing -------------------------------------------------------------------------


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat()


def _plain(value: Value) -> object:
    if isinstance(value, Price):
        return {"import": value.p_import, "export": value.p_export}
    if isinstance(value, Quantiles):
        return {str(q): v for q, v in value.kw.items()}
    return value


def write_dataset(
    directory: Path, entries: Mapping[str, Entry], *, limits: Limits | None = None
) -> None:
    """Write ``entries`` as a dataset in ``directory`` (made if it does not exist): one
    file per entry, ``series/<name>.json`` or ``forecasts/<name>.json``, and the record's
    ``limits`` where given."""
    manifest: dict[str, object] = {"format": FORMAT, "entries": {}}
    if limits is not None:
        manifest["limits"] = {
            key: getattr(limits, name).total_seconds() for key, name in _LIMITS.items()
        }
    for name, entry in entries.items():
        _check_name(name, f"{directory}: entry {name!r}")
        folder = "forecasts" if entry.forecasts else "series"
        relative = f"{folder}/{name}.json"
        (directory / folder).mkdir(parents=True, exist_ok=True)
        series = entry.series
        body: dict[str, object] = {"unit": series.unit, "known_until": _iso(series.known_until)}
        if entry.forecasts:
            issues: list[object] = []
            for sample in series.samples:
                forecast = sample.value
                if forecast is None:
                    issues.append({"received_at": _iso(sample.at), "issue": None})
                    continue
                assert isinstance(forecast, Forecast)
                issues.append(
                    {
                        "received_at": _iso(sample.at),
                        "issued_at": _iso(forecast.issued_at),
                        "source": forecast.source,
                        "intervals": [
                            [_iso(i.start), _iso(i.end), _plain(i.value)]
                            for i in forecast.intervals
                        ],
                    }
                )
            body["issues"] = issues
        else:
            body["samples"] = [
                [_iso(s.at), None if s.value is None else _plain(cast("Value", s.value))]
                for s in series.samples
            ]
        (directory / relative).write_text(json.dumps(body, indent=None) + "\n", encoding="utf-8")
        cast("dict[str, object]", manifest["entries"])[name] = {
            "file": relative,
            "kind": entry.kind,
            "forecasts": entry.forecasts,
        }
    directory.mkdir(parents=True, exist_ok=True)
    (directory / MANIFEST).write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")


# -- as recorded history -------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DatasetHistory:
    """A dataset read as recorded history (:class:`~microgrid.cli.history.History`), each
    entity ``dataset.<name>`` its entry ``name``: the ledger, the ground truth and the
    battery's fit run on a dataset as on any recorder."""

    dataset: Dataset

    def _window(
        self, entity_id: str, start: datetime, end: datetime, carry_in: timedelta
    ) -> Series[Value]:
        """The entry's samples over ``[start, end)``, the last before ``start`` (within
        ``carry_in``) first, as a recorder reads them; known until ``end``."""
        require_utc(start, end)
        domain, _, name = entity_id.partition(".")
        series = self.dataset.series(name) if domain == DOMAIN else None
        if series is None:
            raise DatasetError(f"{entity_id}: not an entry of the dataset")
        if end > series.known_until:
            raise DatasetError(f"{entity_id}: known until {series.known_until}, not {end}")
        samples = series.samples
        times = [sample.at for sample in samples]
        first, last = bisect_left(times, start), bisect_left(times, end)
        before = [
            sample
            for sample in samples[max(first - 1, 0) : first]
            if start - carry_in <= sample.at < start and sample.value is not None
        ]
        # A sample with no value is kept: a gap the record vouches for (``Series``).
        kept = tuple(samples[first:last])
        return Series((*before, *kept), end, series.unit)

    async def power_series(
        self,
        entity_id: str,
        start: datetime,
        end: datetime,
        *,
        carry_in: timedelta,
        chunk: timedelta | None = None,
    ) -> Series[float]:
        """Power in kW, converted from the entry's unit."""
        window = cast("Series[float]", self._window(entity_id, start, end, carry_in))
        try:
            return normalise(window, "power")
        except UnitError as err:
            raise DatasetError(f"{entity_id}: {err}") from err

    async def numeric_series(
        self,
        entity_id: str,
        start: datetime,
        end: datetime,
        *,
        carry_in: timedelta,
        chunk: timedelta | None = None,
    ) -> Series[float]:
        """Any numeric entry's values, the unit not carried (``History.numeric_series``)."""
        window = self._window(entity_id, start, end, carry_in)
        return Series(cast("Series[float]", window).samples, end)

    async def sample_times(
        self,
        entity_id: str,
        start: datetime,
        end: datetime,
        *,
        carry_in: timedelta,
        chunk: timedelta | None = None,
    ) -> tuple[datetime, ...]:
        """When the entry's samples arrived (a liveness entry's)."""
        return tuple(s.at for s in self._window(entity_id, start, end, carry_in).samples)
