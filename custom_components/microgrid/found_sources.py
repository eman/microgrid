"""The sources the installed integrations publish, found for each input: each as its
``sources/1`` section, ready to read, and what it still needs (an export price where
the integration publishes none).

Each integration is found as it identifies its own entities: its config entries, or
its entities' ``unique_id`` in the entity registry (its platform's). EMHASS and Predbat
write their entities through Home Assistant's API, with no registry entry, so they are
found by the shape of their states.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from .const import CONF_LOAD_FORECAST, CONF_PRICES, CONF_PV_FORECAST

#: What a source found may still need from the user: a fixed export price.
EXPORT = "export"


@dataclass(frozen=True, slots=True)
class Found:
    """A source an installed integration publishes."""

    label: str
    section: dict[str, Any]
    #: The section's settings the integration cannot give.
    missing: tuple[str, ...] = field(default=())


def _entries(hass: HomeAssistant, domain: str) -> list[ConfigEntry]:
    return [e for e in hass.config_entries.async_entries(domain) if e.disabled_by is None]


def _registry(hass: HomeAssistant, platform: str) -> list[er.RegistryEntry]:
    registry = er.async_get(hass)
    return [
        entry
        for entry in registry.entities.values()
        if entry.platform == platform and entry.disabled_by is None
    ]


# -- prices ----------------------------------------------------------------------------------


def _tariffkit(hass: HomeAssistant) -> Iterator[Found]:
    for entry in _entries(hass, "tariffkit"):
        yield Found(
            f"TariffKit: {entry.title}", {"from": "tariffkit", "config_entry": entry.entry_id}
        )


def _nordpool(hass: HomeAssistant) -> Iterator[Found]:
    for entry in _entries(hass, "nordpool"):
        for area in entry.data.get("areas", []):
            yield Found(
                f"Nord Pool: {area}",
                {"from": "nordpool", "area": str(area), "config_entry": entry.entry_id},
                (EXPORT,),
            )


def _tibber(hass: HomeAssistant) -> Iterator[Found]:
    for entry in _entries(hass, "tibber")[:1]:  # its action reads the first account alone
        yield Found(f"Tibber: {entry.title}", {"from": "tibber"}, (EXPORT,))


def _amber(hass: HomeAssistant) -> Iterator[Found]:
    """Each site's general channel (``<site>-current-general``,
    ``<site>-forecasts-general``) and its feed-in channel where it has one."""
    by_id = {entry.unique_id: entry.entity_id for entry in _registry(hass, "amberelectric")}
    sites = sorted(
        uid.removesuffix("-current-general") for uid in by_id if uid.endswith("-current-general")
    )
    for site in sites:
        forecast = by_id.get(f"{site}-forecasts-general")
        if forecast is None:
            continue
        section: dict[str, Any] = {
            "from": "amber",
            "price": by_id[f"{site}-current-general"],
            "forecast": forecast,
        }
        feed_price = by_id.get(f"{site}-current-feed_in")
        feed_forecast = by_id.get(f"{site}-forecasts-feed_in")
        if feed_price and feed_forecast:
            section |= {"feed_in_price": feed_price, "feed_in_forecast": feed_forecast}
        yield Found(
            f"Amber Electric: {section['price']}",
            section,
            () if "feed_in_price" in section else (EXPORT,),
        )


def _octopus(hass: HomeAssistant) -> Iterator[Found]:
    """Each import meter's rates (``..._current_day_rates``, ``..._next_day_rates``),
    with the export meter's where the account has exactly one."""
    meters: dict[str, dict[str, str]] = {}
    for entry in _registry(hass, "octopus_energy"):
        for day in ("current", "next"):
            suffix = f"_{day}_day_rates"
            if entry.domain == "event" and entry.unique_id.endswith(suffix):
                meters.setdefault(entry.unique_id.removesuffix(suffix), {})[day] = entry.entity_id
    complete = {meter: days for meter, days in meters.items() if "current" in days}

    def entities(days: dict[str, str]) -> list[str]:
        return [days["current"], *([days["next"]] if "next" in days else [])]

    exports = [days for meter, days in complete.items() if meter.endswith("_export")]
    for meter, days in sorted(complete.items()):
        if meter.endswith("_export"):
            continue
        section: dict[str, Any] = {"from": "octopus", "entities": entities(days)}
        if len(exports) == 1:
            section["export_entities"] = entities(exports[0])
        yield Found(
            f"Octopus Energy: {meter.removeprefix('octopus_energy_electricity_')}",
            section,
            () if "export_entities" in section else (EXPORT,),
        )


def _entsoe(hass: HomeAssistant) -> Iterator[Found]:
    for entry in _registry(hass, "entsoe"):
        if entry.unique_id.endswith("avg_price"):
            yield Found(
                f"ENTSO-E: {entry.entity_id}",
                {"from": "entsoe", "entity": entry.entity_id},
                (EXPORT,),
            )


def _openei(hass: HomeAssistant) -> Iterator[Found]:
    """Each plan's sensors (``unique_id`` ``<key>_<entry id>``)."""
    for config in _entries(hass, "openei"):
        by_key = {
            entry.unique_id.removesuffix(f"_{config.entry_id}"): entry.entity_id
            for entry in _registry(hass, "openei")
            if entry.config_entry_id == config.entry_id
        }
        rate = by_key.get("current_rate")
        change = by_key.get("next_energy_rate_structure_time")
        if rate is None or change is None:
            continue
        section: dict[str, Any] = {
            "from": "openei",
            "current_rate": rate,
            "next_structure_time": change,
        }
        if adjustment := by_key.get("current_adjustment"):
            section["adjustment"] = adjustment
        sell = by_key.get("current_sell_rate")
        if sell and _number(hass, sell):
            section["sell_rate"] = sell
        yield Found(f"OpenEI: {config.title}", section, () if "sell_rate" in section else (EXPORT,))


def _number(hass: HomeAssistant, entity: str) -> bool:
    """Whether the entity's state is a number (OpenEI's sell rate may have none)."""
    state = hass.states.get(entity)
    if state is None:
        return False
    try:
        float(state.state)
    except ValueError:
        return False
    return True


# -- PV --------------------------------------------------------------------------------------

#: Solcast's daily forecast sensors, by ``unique_id``, today's first.
_SOLCAST_DAYS = (
    "total_kwh_forecast_today",
    "total_kwh_forecast_tomorrow",
    *(f"total_kwh_forecast_d{n}" for n in range(3, 8)),
)


def _solcast(hass: HomeAssistant) -> Iterator[Found]:
    """Its daily forecasts as far as they are enabled (day 3 on are not by default), and
    its last poll (``lastupdated``)."""
    by_id = {entry.unique_id: entry.entity_id for entry in _registry(hass, "solcast_solar")}
    days = []
    for key in _SOLCAST_DAYS:
        if key not in by_id:
            break
        days.append(by_id[key])
    polled = by_id.get("lastupdated")
    if days and polled:
        yield Found("Solcast", {"from": "solcast", "entities": days, "last_polled": polled})


# -- loads -----------------------------------------------------------------------------------


def _emhass(hass: HomeAssistant) -> Iterator[Found]:
    """EMHASS's load forecast: a sensor named for it (``p_load_forecast``, under any
    prefix or suffix) whose ``forecasts`` rows hold ``date`` and a value keyed by its own
    object id. EMHASS publishes its PV, battery and grid forecasts the same way, so the
    name tells the load's apart."""
    for state in hass.states.async_all("sensor"):
        rows = state.attributes.get("forecasts")
        key = state.object_id
        if "load_forecast" not in key:
            continue
        if not isinstance(rows, list) or not rows:
            continue
        if not all(isinstance(row, dict) and "date" in row and key in row for row in rows[:2]):
            continue
        section: dict[str, Any] = {"from": "emhass", "entity": state.entity_id, "value_key": key}
        if (period := _period_minutes(rows)) is not None:
            section["period_minutes"] = period
        yield Found(f"EMHASS: {state.entity_id}", section)


def _period_minutes(rows: list[Any]) -> int | None:
    """The minutes between the first two rows: the optimisation step."""
    try:
        first, second = (datetime.fromisoformat(str(rows[i]["date"])) for i in (0, 1))
    except IndexError, ValueError:
        return None
    minutes = (second - first).total_seconds() / 60
    return int(minutes) if minutes > 0 and minutes.is_integer() else None


def _predbat(hass: HomeAssistant) -> Iterator[Found]:
    """``<prefix>.load_energy_predicted`` with its ``results``."""
    for state in hass.states.async_all():
        if state.object_id == "load_energy_predicted" and isinstance(
            state.attributes.get("results"), dict
        ):
            yield Found(
                f"Predbat: {state.entity_id}", {"from": "predbat", "entity": state.entity_id}
            )


_FINDERS = {
    CONF_PRICES: (_tariffkit, _nordpool, _amber, _tibber, _octopus, _entsoe, _openei),
    CONF_PV_FORECAST: (_solcast,),
    CONF_LOAD_FORECAST: (_emhass, _predbat),
}


def found_sources(hass: HomeAssistant, name: str) -> list[Found]:
    """The sources found for the input ``name`` (a ``sources/1`` section's name)."""
    return [found for finder in _FINDERS[name] for found in finder(hass)]
