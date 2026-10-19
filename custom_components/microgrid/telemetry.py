"""What the plant measures, as series: each state change of a measured entity a sample,
kept back as far as the engine reads (its smoothing window, and the sample in force
then).

A state that is not a number (``unavailable``, ``unknown``) is a sample with no value: a
gap the engine sees. Powers are converted to kW and the SOC to a fraction from the unit
each state names (the SOC's configured unit where its entity names none); the plant's
mode becomes whether the plant runs the dispatch its model describes.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from microgrid.cli.sources.config import Measured
from microgrid.engine.series import Sample, Series
from microgrid.engine.units import UnitError, convert

#: A reading: the state's text, its unit, and when it changed.
type Reading = tuple[str, str | None, datetime]


def _number(text: str) -> float | None:
    try:
        return float(text)
    except ValueError:
        return None


@dataclass
class Buffer[T]:
    """One series' samples, oldest first."""

    convert: Callable[[str, str | None], T | None]
    samples: list[Sample[T]] = field(default_factory=list)

    def add(self, reading: Reading) -> None:
        text, unit, at = reading
        try:
            value = self.convert(text, unit)
        except UnitError, ValueError:
            value = None
        if self.samples and at < self.samples[-1].at:
            self.samples.clear()  # the clock stepped back: start again
        self.samples.append(Sample(at, value))

    def prune(self, begin: datetime) -> None:
        """Drop the samples no longer needed from ``begin`` on (all but the one in force
        then)."""
        while len(self.samples) > 1 and self.samples[1].at <= begin:
            del self.samples[0]

    def series(self, now: datetime, until: datetime | None = None) -> Series[T] | None:
        """The samples up to ``now``, vouched for until ``until`` (``now``)."""
        known = now if until is None else min(until, now)
        kept = tuple(s for s in self.samples if s.at <= known)
        return Series(kept, known) if kept else None


def _power(text: str, unit: str | None) -> float | None:
    value = _number(text)
    return None if value is None else convert(value, unit, "power")


class Telemetry:
    """The measured entities' series, by what each is: the plant's meters by the
    plant's names, the house load, the SOC and the dispatch."""

    def __init__(self, measured: Measured, meters: Mapping[str, str]) -> None:
        self._measured = measured
        self.meters: dict[str, Buffer[float]] = {
            name: Buffer(_power) for name in meters if name in measured.meters
        }
        self.house: Buffer[float] | None = Buffer(_power) if measured.house else None
        self.soc: Buffer[float] | None = Buffer(self._soc) if measured.soc else None
        self.dispatch: Buffer[bool] | None = (
            Buffer(self._mode)
            if measured.dispatch and measured.dispatch_as_modelled is None
            else None
        )

    def _soc(self, text: str, unit: str | None) -> float | None:
        value = _number(text)
        if value is None:
            return None
        return convert(
            value, unit if unit is not None else self._measured.soc_unit or None, "fraction"
        )

    def _mode(self, text: str, unit: str | None) -> bool | None:
        if text in ("unavailable", "unknown", ""):
            return None
        return text in self._measured.modelled_modes

    def entities(self) -> dict[str, list[Buffer[float] | Buffer[bool]]]:
        """Each measured entity and the buffers its changes feed."""
        found: dict[str, list[Buffer[float] | Buffer[bool]]] = {}
        for name, buffer in self.meters.items():
            found.setdefault(self._measured.meters[name], []).append(buffer)
        named: tuple[tuple[str | None, Buffer[float] | Buffer[bool] | None], ...] = (
            (self._measured.house, self.house),
            (self._measured.soc, self.soc),
            (self._measured.dispatch, self.dispatch),
        )
        for entity, kept in named:
            if entity is not None and kept is not None:
                found.setdefault(entity, []).append(kept)
        return found

    def add(self, entity: str, reading: Reading) -> None:
        for buffer in self.entities().get(entity, []):
            buffer.add(reading)

    def prune(self, now: datetime, window: timedelta) -> None:
        for buffers in self.entities().values():
            for buffer in buffers:
                buffer.prune(now - window)

    def dispatch_series(self, now: datetime, until: datetime | None = None) -> Series[bool] | None:
        """Whether the plant runs as modelled: the mode entity's, or the configured
        constant."""
        if self._measured.dispatch_as_modelled is not None:
            return Series((Sample(now, self._measured.dispatch_as_modelled),), now)
        return None if self.dispatch is None else self.dispatch.series(now, until)
