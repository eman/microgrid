"""``sources/1``: the file that says where each input comes from.

.. code-block:: toml

    schema = "sources/1"
    plant = "plant.toml"            # plant/2, beside this file
    engine = "engine.toml"          # engine/2; optional (the library's defaults)

    [home_assistant]                # a connection, for the sources that read it
    url = "http://homeassistant.local:8123"
    token_env = "MICROGRID_HA_TOKEN"  # the variable holding the token (this default)

    [prices]
    from = "tariffkit"              # or "entity"

    [pv_forecast]
    from = "solcast"                # or "entity"
    entities = ["sensor.solcast_pv_forecast_forecast_today", "..."]
    last_polled = "sensor.solcast_pv_forecast_api_last_polled"

    [load_forecast]
    from = "emhass"                 # or "predbat", "entity"

    [measured]
    from = "home_assistant"
    soc = "sensor.battery_soc"
    house = "sensor.house_load"
    [measured.meters]               # the plant's meters (plant/2), by name
    pv = "sensor.pv_power"
    grid = "sensor.grid_power"
    battery = "sensor.battery_power"

Every section but ``plant`` is optional: an input with no source is not read, and a
valuation that needs it names it as a cause. No entity has a default (they are each
site's own), except where a tool publishes under a fixed name (EMHASS's and Predbat's
entities, which a section may rename). Paths are relative to this file. The token is
never read from the file.
"""

from __future__ import annotations

import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import timedelta
from ipaddress import ip_address, ip_network
from pathlib import Path
from typing import Final
from urllib.parse import urlsplit

__all__ = [
    "HA_TOKEN_ENV",
    "SCHEMA",
    "Emhass",
    "EntityForecast",
    "HomeAssistant",
    "LoadSource",
    "Measured",
    "Predbat",
    "PriceSource",
    "PvSource",
    "Solcast",
    "Sources",
    "SourcesConfigError",
    "TariffKit",
    "read_sources",
]

SCHEMA: Final = "sources/1"
#: The environment variable a Home Assistant token is read from, unless ``token_env``
#: names another.
HA_TOKEN_ENV: Final = "MICROGRID_HA_TOKEN"


class SourcesConfigError(ValueError):
    """A ``sources/1`` file that cannot be used; ``errors`` names each problem."""

    def __init__(self, errors: list[str]) -> None:
        self.errors = tuple(errors)
        super().__init__(
            "sources configuration is invalid:\n" + "\n".join(f"  {e}" for e in errors)
        )


@dataclass(frozen=True, slots=True)
class HomeAssistant:
    """A Home Assistant instance, read over its REST API."""

    url: str
    token_env: str = HA_TOKEN_ENV


@dataclass(frozen=True, slots=True)
class TariffKit:
    """Prices from TariffKit's Home Assistant integration (its ``get_rates`` service)."""

    #: The integration's config entry; None: the one loaded.
    config_entry: str | None = None


@dataclass(frozen=True, slots=True)
class Solcast:
    """A PV forecast from the Solcast integration's daily forecast entities."""

    entities: tuple[str, ...]
    #: The entity holding the last successful poll: the forecast's age.
    last_polled: str


@dataclass(frozen=True, slots=True)
class Emhass:
    """A load forecast EMHASS publishes: an entity whose attribute lists ``{date, value}``
    rows, the mean power over ``period`` from each ``date``."""

    entity: str = "sensor.p_load_forecast"
    attribute: str = "forecasts"
    value_key: str = "p_load_forecast"
    period: timedelta = timedelta(minutes=30)


@dataclass(frozen=True, slots=True)
class Predbat:
    """A load forecast Predbat publishes: today's predicted energy, cumulative from local
    midnight, by time (``results``), in kWh. It reaches midnight: a valuation's horizon
    ends there (not ``horizon_cut``, which is a gap with forecast after it)."""

    entity: str = "predbat.load_energy_predicted"
    attribute: str = "results"
    #: Each cumulative value includes the step starting at its time.
    step: timedelta = timedelta(minutes=5)


@dataclass(frozen=True, slots=True)
class EntityForecast:
    """Any entity whose attribute holds a forecast: a list of rows (objects), each with a
    start time, an end time or a fixed ``period``, and its values under ``keys``."""

    entity: str
    attribute: str
    #: The row's start time key, and its end (None: ``period`` from the start).
    start_key: str = "start"
    end_key: str | None = "end"
    period: timedelta | None = None
    #: The values' keys: ``import``/``export`` for prices; ``value``, or ``p10``/``p50``/
    #: ``p90``, for PV; ``value`` for a load.
    keys: Mapping[str, str] = field(default_factory=dict)
    #: The values' unit (power: W, kW; prices: per kWh or per MWh, any currency); None:
    #: the entity's ``unit_of_measurement``.
    unit: str | None = None


type PriceSource = TariffKit | EntityForecast
type PvSource = Solcast | EntityForecast
type LoadSource = Emhass | Predbat | EntityForecast


@dataclass(frozen=True, slots=True)
class Measured:
    """What is measured, read from Home Assistant: the SOC, the plant's meters by the
    plant's names, the house meter, and whether the plant runs the dispatch its model
    describes."""

    soc: str | None = None
    #: The SOC's unit where the entity states none (``%``; empty: a fraction).
    soc_unit: str = "%"
    meters: Mapping[str, str] = field(default_factory=dict)
    house: str | None = None
    #: The entity whose state is the plant's mode, and the modes that run the modelled
    #: dispatch; or ``dispatch_as_modelled`` true or false, without an entity.
    dispatch: str | None = None
    modelled_modes: tuple[str, ...] = ()
    dispatch_as_modelled: bool | None = None
    #: A series whose samples prove the readings were flowing (for ``fetch``): any meter
    #: that reports every few seconds. None: the first meter's own samples.
    heartbeat: str | None = None
    #: The import and export prices in force, as recorded (for ``fetch``: the ledger).
    import_price: str | None = None
    export_price: str | None = None
    #: The battery inverter's AC power, > 0 out (for ``fetch``: fit-battery).
    inverter: str | None = None


@dataclass(frozen=True, slots=True)
class Sources:
    """A ``sources/1`` file, read."""

    path: Path
    plant: Path
    engine: Path | None = None
    home_assistant: HomeAssistant | None = None
    prices: PriceSource | None = None
    pv_forecast: PvSource | None = None
    load_forecast: LoadSource | None = None
    measured: Measured | None = None

    def uses_home_assistant(self) -> bool:
        """Whether any section reads Home Assistant."""
        named = (TariffKit, Solcast, Emhass, Predbat, EntityForecast)
        return (
            isinstance(self.prices, named)
            or isinstance(self.pv_forecast, named)
            or (isinstance(self.load_forecast, named) or self.measured is not None)
        )


def read_sources(path: Path) -> Sources:
    """The ``sources/1`` file at ``path``. Raises :class:`SourcesConfigError` naming every
    problem."""
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except OSError as err:
        raise SourcesConfigError([f"cannot read {path}: {err.strerror or err}"]) from err
    except tomllib.TOMLDecodeError as err:
        raise SourcesConfigError([f"{path} is not TOML: {err}"]) from err
    return _Reader(path).sources(data)


_FORECASTS: Final = {
    "prices": ("tariffkit", "entity"),
    "pv_forecast": ("solcast", "entity"),
    "load_forecast": ("emhass", "predbat", "entity"),
}
_KEYS: Final = {
    "prices": ({"import", "export"},),
    "pv_forecast": ({"value"}, {"p10", "p50", "p90"}),
    "load_forecast": ({"value"},),
}


#: The environment variables a sources file may send to Home Assistant as its token.
_TOKEN_ENV = re.compile(r"(?:[A-Z][A-Z0-9_]*_)?HA_TOKEN")
#: Host suffixes that name the local network, where HA is commonly served over http.
_LOCAL_SUFFIXES = (".local", ".lan", ".home.arpa", ".internal", ".localdomain")
#: RFC 6598 shared address space (carrier-grade NAT; Tailscale's addresses).
_SHARED = ip_network("100.64.0.0/10")


def _safe_url(url: str) -> bool:
    """https anywhere; http only to a local host: a loopback, private, link-local or
    shared (carrier-grade NAT, as Tailscale uses) address, or a name without a dot or in
    a local-network domain. An address is judged as an address first: an IPv6 literal
    has no dot, and a public one is not local."""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower().rstrip(".")
    if parts.scheme == "https":
        return bool(host)
    if parts.scheme != "http" or not host:
        return False
    try:
        address = ip_address(host)
    except ValueError:
        return host == "localhost" or "." not in host or host.endswith(_LOCAL_SUFFIXES)
    shared = address.version == 4 and address in _SHARED
    return address.is_private or address.is_loopback or address.is_link_local or shared


class _Reader:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.errors: list[str] = []

    def fail(self, where: str, why: str) -> None:
        self.errors.append(f"{where}: {why}")

    def table(self, data: Mapping[str, object], key: str) -> Mapping[str, object] | None:
        value = data.get(key)
        if value is None:
            return None
        if not isinstance(value, dict):
            self.fail(key, "must be a table")
            return None
        return value

    def text(
        self, data: Mapping[str, object], key: str, where: str, *, required: bool = False
    ) -> str | None:
        value = data.get(key)
        if value is None:
            if required:
                self.fail(f"{where}.{key}", "is required")
            return None
        if not isinstance(value, str) or not value:
            self.fail(f"{where}.{key}", "must be a non-empty string")
            return None
        return value

    def minutes(self, data: Mapping[str, object], key: str, where: str) -> timedelta | None:
        value = data.get(key)
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int | float) or value <= 0:
            self.fail(f"{where}.{key}", "must be a number of minutes above 0")
            return None
        return timedelta(minutes=value)

    def unknown(self, data: Mapping[str, object], known: set[str], where: str) -> None:
        for key in sorted(set(data) - known):
            self.fail(f"{where}.{key}" if where else key, "is not a setting")

    def file(self, value: str) -> Path:
        path = Path(value)
        return path if path.is_absolute() else self.path.parent / path

    def sources(self, data: Mapping[str, object]) -> Sources:
        if data.get("schema") != SCHEMA:
            self.fail("schema", f'must be "{SCHEMA}"')
        self.unknown(
            data,
            {"schema", "plant", "engine", "home_assistant", "measured", *_FORECASTS},
            "",
        )
        plant = self.text(data, "plant", "sources", required=True)
        engine = self.text(data, "engine", "sources")
        ha = self.home_assistant(self.table(data, "home_assistant"))
        forecasts = {name: self.forecast(name, self.table(data, name)) for name in _FORECASTS}
        measured = self.measured(self.table(data, "measured"))
        found = Sources(
            path=self.path,
            plant=self.file(plant or "plant.toml"),
            engine=None if engine is None else self.file(engine),
            home_assistant=ha,
            prices=forecasts["prices"],  # type: ignore[arg-type]
            pv_forecast=forecasts["pv_forecast"],  # type: ignore[arg-type]
            load_forecast=forecasts["load_forecast"],  # type: ignore[arg-type]
            measured=measured,
        )
        if found.uses_home_assistant() and ha is None:
            self.fail("home_assistant", "is required: a section reads Home Assistant")
        if self.errors:
            raise SourcesConfigError(self.errors)
        return found

    def home_assistant(self, data: Mapping[str, object] | None) -> HomeAssistant | None:
        if data is None:
            return None
        self.unknown(data, {"url", "token_env"}, "home_assistant")
        url = self.text(data, "url", "home_assistant", required=True)
        token_env = self.text(data, "token_env", "home_assistant") or HA_TOKEN_ENV
        # The token goes to ``url`` as a Bearer header: only an HA token's variable, and
        # in cleartext only on the local network (a shared sources file could otherwise
        # send any secret anywhere).
        if not _TOKEN_ENV.fullmatch(token_env):
            self.fail(
                "home_assistant.token_env",
                f"must name an HA token (HA_TOKEN, *_HA_TOKEN): {token_env!r}",
            )
        if url is not None and not _safe_url(url):
            self.fail(
                "home_assistant.url",
                f"must be https, or http to a local host (.local, .lan, a private address): {url}",
            )
        return None if url is None else HomeAssistant(url, token_env)

    def forecast(
        self, name: str, data: Mapping[str, object] | None
    ) -> TariffKit | Solcast | Emhass | Predbat | EntityForecast | None:
        if data is None:
            return None
        kind = data.get("from")
        if kind not in _FORECASTS[name]:
            self.fail(f"{name}.from", f"must be one of {', '.join(_FORECASTS[name])}")
            return None
        if kind == "tariffkit":
            self.unknown(data, {"from", "config_entry"}, name)
            return TariffKit(self.text(data, "config_entry", name))
        if kind == "solcast":
            self.unknown(data, {"from", "entities", "last_polled"}, name)
            entities = data.get("entities")
            if (
                not isinstance(entities, list)
                or not entities
                or not all(isinstance(e, str) and e for e in entities)
            ):
                self.fail(f"{name}.entities", "must list the forecast entities, today's first")
                entities = []
            polled = self.text(data, "last_polled", name, required=True)
            return Solcast(tuple(entities), polled or "")
        if kind == "emhass":
            self.unknown(data, {"from", "entity", "attribute", "value_key", "period_minutes"}, name)
            base = Emhass()
            return Emhass(
                self.text(data, "entity", name) or base.entity,
                self.text(data, "attribute", name) or base.attribute,
                self.text(data, "value_key", name) or base.value_key,
                self.minutes(data, "period_minutes", name) or base.period,
            )
        if kind == "predbat":
            self.unknown(data, {"from", "entity", "attribute", "step_minutes"}, name)
            base_p = Predbat()
            return Predbat(
                self.text(data, "entity", name) or base_p.entity,
                self.text(data, "attribute", name) or base_p.attribute,
                self.minutes(data, "step_minutes", name) or base_p.step,
            )
        return self.entity(name, data)

    def entity(self, name: str, data: Mapping[str, object]) -> EntityForecast | None:
        self.unknown(
            data,
            {
                "from",
                "entity",
                "attribute",
                "start_key",
                "end_key",
                "period_minutes",
                "keys",
                "unit",
            },
            name,
        )
        entity = self.text(data, "entity", name, required=True)
        attribute = self.text(data, "attribute", name, required=True)
        end_key = self.text(data, "end_key", name)
        period = self.minutes(data, "period_minutes", name)
        if end_key is None and period is None:
            end_key = "end"
        keys = data.get("keys")
        if not isinstance(keys, dict) or not all(
            isinstance(k, str) and isinstance(v, str) and v for k, v in keys.items()
        ):
            self.fail(f"{name}.keys", "must map each value to its key in a row")
            keys = {}
        if set(keys) not in _KEYS[name]:
            shapes = " or ".join("/".join(sorted(s)) for s in _KEYS[name])
            self.fail(f"{name}.keys", f"must name {shapes}")
        if entity is None or attribute is None:
            return None
        return EntityForecast(
            entity,
            attribute,
            self.text(data, "start_key", name) or "start",
            None if period is not None and end_key is None else end_key,
            period,
            dict(keys),
            self.text(data, "unit", name),
        )

    def measured(self, data: Mapping[str, object] | None) -> Measured | None:
        if data is None:
            return None
        where = "measured"
        self.unknown(
            data,
            {
                "from",
                "soc",
                "soc_unit",
                "meters",
                "house",
                "dispatch",
                "modelled_modes",
                "dispatch_as_modelled",
                "heartbeat",
                "import_price",
                "export_price",
                "inverter",
            },
            where,
        )
        if data.get("from") != "home_assistant":
            self.fail(f"{where}.from", 'must be "home_assistant" (or use a dataset)')
        meters = data.get("meters", {})
        if not isinstance(meters, dict) or not all(
            isinstance(k, str) and isinstance(v, str) and v for k, v in meters.items()
        ):
            self.fail(f"{where}.meters", "must map each of the plant's meters to an entity")
            meters = {}
        modes = data.get("modelled_modes", [])
        if not isinstance(modes, list) or not all(isinstance(m, str) for m in modes):
            self.fail(f"{where}.modelled_modes", "must list the modes, as text")
            modes = []
        constant = data.get("dispatch_as_modelled")
        if constant is not None and not isinstance(constant, bool):
            self.fail(f"{where}.dispatch_as_modelled", "must be true or false")
            constant = None
        dispatch = self.text(data, "dispatch", where)
        if dispatch is not None and constant is not None:
            self.fail(where, "name a dispatch entity or set dispatch_as_modelled, not both")
        if dispatch is not None and not modes:
            self.fail(f"{where}.modelled_modes", "is required with a dispatch entity")
        soc_unit = data.get("soc_unit", "%")
        if not isinstance(soc_unit, str):
            self.fail(f"{where}.soc_unit", "must be text (% or empty)")
            soc_unit = "%"
        return Measured(
            soc=self.text(data, "soc", where),
            soc_unit=soc_unit,
            meters=dict(meters),
            house=self.text(data, "house", where),
            dispatch=dispatch,
            modelled_modes=tuple(modes),
            dispatch_as_modelled=constant,
            heartbeat=self.text(data, "heartbeat", where),
            import_price=self.text(data, "import_price", where),
            export_price=self.text(data, "export_price", where),
            inverter=self.text(data, "inverter", where),
        )
