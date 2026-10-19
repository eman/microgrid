"""A config entry's configuration, read with the library's own readers: the plant
(``plant/2``) in the entry's data; the engine (``engine/2``), each input's source and
what is measured (each as its ``sources/1`` section) in its options."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from microgrid.cli.sources.config import (
    LoadSource,
    Measured,
    PriceSource,
    PvSource,
    SourcesConfigError,
    forecast_source,
    measured_source,
)
from microgrid.engine import EngineConfig, EngineConfigError
from microgrid.plant import Plant, PlantConfig, PlantConfigError

from .const import CONF_ENGINE, CONF_MEASURED, CONF_PLANT, SOURCE_SECTIONS


class SetupError(ValueError):
    """The configuration cannot be read: each problem named."""

    def __init__(self, problems: list[str]) -> None:
        super().__init__("\n".join(problems))
        self.problems = problems


@dataclass(frozen=True, slots=True)
class Setup:
    """What a config entry configures."""

    plant: Plant
    engine: EngineConfig
    #: Each forecast input's source, by its ``sources/1`` section's name.
    sources: Mapping[str, PriceSource | PvSource | LoadSource] = field(default_factory=dict)
    measured: Measured | None = None


def plant_mapping(data: Mapping[str, object], time_zone: str) -> dict[str, object]:
    """The plant's mapping, in Home Assistant's time zone where it names none."""
    found = dict(data)
    found.setdefault("timezone", time_zone)
    return found


def read_plant(data: Mapping[str, object], time_zone: str) -> Plant:
    """The plant (``plant/2``), in its time zone. Raises :class:`SetupError`."""
    mapping = plant_mapping(data, time_zone)
    try:
        tz = ZoneInfo(str(mapping["timezone"]))
    except (ZoneInfoNotFoundError, ValueError) as err:
        raise SetupError([f"timezone: not a time zone: {mapping['timezone']!r}"]) from err
    try:
        return Plant(PlantConfig.from_mapping(mapping, tz=tz))
    except PlantConfigError as err:
        raise SetupError(list(err.errors)) from err


def read_engine(data: Mapping[str, object] | None, plant: Plant) -> EngineConfig:
    """The engine (``engine/2``) for ``plant``. Raises :class:`SetupError`."""
    mapping: dict[str, object] = {"schema": "engine/2", **(data or {})}
    try:
        return EngineConfig.from_mapping(mapping, plant=plant)
    except EngineConfigError as err:
        raise SetupError(list(err.errors)) from err


def read_section(name: str, data: Mapping[str, object]) -> PriceSource | PvSource | LoadSource:
    """One input's source. Raises :class:`SetupError`."""
    try:
        return forecast_source(name, data)  # type: ignore[arg-type]
    except SourcesConfigError as err:
        raise SetupError(list(err.errors)) from err


def read_measured(data: Mapping[str, object]) -> Measured:
    """What is measured. Raises :class:`SetupError`."""
    try:
        return measured_source(data)
    except SourcesConfigError as err:
        raise SetupError(list(err.errors)) from err


def read_setup(data: Mapping[str, object], options: Mapping[str, object], time_zone: str) -> Setup:
    """The whole configuration. Raises :class:`SetupError` naming every problem."""
    plant_data = data.get(CONF_PLANT)
    if not isinstance(plant_data, Mapping):
        raise SetupError(["plant: missing"])
    plant = read_plant(plant_data, time_zone)
    engine_data = options.get(CONF_ENGINE)
    engine = read_engine(engine_data if isinstance(engine_data, Mapping) else None, plant)
    problems: list[str] = []
    sources: dict[str, PriceSource | PvSource | LoadSource] = {}
    for name in SOURCE_SECTIONS:
        section = options.get(name)
        if isinstance(section, Mapping):
            try:
                sources[name] = read_section(name, section)
            except SetupError as err:
                problems.extend(err.problems)
    measured: Measured | None = None
    section = options.get(CONF_MEASURED)
    if isinstance(section, Mapping):
        try:
            measured = read_measured(section)
        except SetupError as err:
            problems.extend(err.problems)
    if problems:
        raise SetupError(problems)
    return Setup(plant, engine, sources, measured)
